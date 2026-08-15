from __future__ import annotations

import asyncio
import hashlib
import json
import zlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from partsouq_crawler.crawl.discovery import normalize_url
from partsouq_crawler.db.backup import backup_database, publish_snapshot
from partsouq_crawler.db.connection import connect
from partsouq_crawler.db.migrations import migrate
from partsouq_crawler.models.crawl import FetchResult, QueueItem

QUEUE_STATUSES = (
    "pending",
    "in_progress",
    "done",
    "gone",
    "failed",
    "challenged",
    "skipped_robots",
    "parse_failed",
)
NORMALIZED_TABLES = (
    ("vehicle_configuration", "vehicle_configurations"),
    ("taxonomy_node", "taxonomy_nodes"),
    ("diagram", "diagrams"),
    ("part_number", "part_numbers"),
    ("part_occurrence", "part_occurrences"),
    ("fitment", "fitments"),
    ("compatibility_hint", "compatibility_hints"),
    ("part_relation", "part_relations"),
)


def _paths_resolve_equal(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, path: Path, connection: aiosqlite.Connection) -> None:
        self.path = path
        self.connection = connection
        self._write_lock = asyncio.Lock()

    @classmethod
    async def create(cls, path: Path) -> Repository:
        connection = await connect(path)
        await migrate(connection)
        return cls(path, connection)

    async def close(self) -> None:
        await self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        await self.connection.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                await self.connection.rollback()
                raise
            else:
                await self.connection.commit()

    async def create_or_get_run(
        self,
        run_key: str,
        seed_urls: Sequence[str],
        config: dict[str, object],
    ) -> int:
        now = utc_now()
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT OR IGNORE INTO crawl_runs(
                    run_key, seed_urls_json, config_json, status, started_at, updated_at
                ) VALUES (?, ?, ?, 'created', ?, ?)
                """,
                (run_key, json.dumps(seed_urls), json.dumps(config, sort_keys=True), now, now),
            )
            cursor = await connection.execute(
                "SELECT id FROM crawl_runs WHERE run_key = ?", (run_key,)
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("failed to create crawl run")
        return int(row["id"])

    async def get_run(self, run_key: str) -> aiosqlite.Row | None:
        cursor = await self.connection.execute(
            "SELECT * FROM crawl_runs WHERE run_key = ?", (run_key,)
        )
        return await cursor.fetchone()

    async def set_run_status(
        self,
        run_id: int,
        status: str,
        *,
        blocked_reason: str | None = None,
        ended: bool = False,
    ) -> None:
        now = utc_now()
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE crawl_runs
                SET status = ?, blocked_reason = ?, updated_at = ?,
                    ended_at = CASE WHEN ? THEN ? ELSE ended_at END
                WHERE id = ?
                """,
                (status, blocked_reason, now, ended, now, run_id),
            )

    async def enqueue(
        self,
        run_id: int,
        requested_url: str,
        *,
        parent_url: str | None,
        depth: int,
        page_type_hint: str | None = None,
        priority: int = 0,
        discovery_method: str = "html",
        source_response_id: int | None = None,
    ) -> bool:
        normalized = normalize_url(requested_url)
        url_hash = hashlib.sha256(normalized.encode()).hexdigest()
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT OR IGNORE INTO crawl_queue(
                    run_id, requested_url, url_hash, parent_url, depth,
                    page_type_hint, priority, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    normalized,
                    url_hash,
                    parent_url,
                    depth,
                    page_type_hint,
                    priority,
                    now,
                ),
            )
            await connection.execute(
                """
                INSERT OR IGNORE INTO discovery_edges(
                    run_id, source_response_id, parent_url, discovered_url,
                    discovery_method, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, source_response_id, parent_url, normalized, discovery_method, now),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                await connection.execute(
                    """
                    UPDATE crawl_runs
                    SET pages_discovered = pages_discovered + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, run_id),
                )
        return inserted

    async def recover_expired_leases(self, run_id: int) -> int:
        now = utc_now()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE crawl_queue
                SET status = 'pending', worker_id = NULL, lease_expires_at = NULL,
                    last_error = 'expired lease recovered'
                WHERE run_id = ? AND status = 'in_progress'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at < ?
                """,
                (run_id, now),
            )
        return cursor.rowcount

    async def acquire_next(
        self,
        run_id: int,
        *,
        worker_id: str,
        lease_seconds: int,
        max_depth: int,
    ) -> QueueItem | None:
        now = utc_now()
        lease = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        async with self.transaction() as connection:
            cursor = await connection.execute(
                """
                SELECT id, run_id, requested_url, depth, attempts, page_type_hint
                FROM crawl_queue
                WHERE run_id = ? AND status = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                  AND (? = 0 OR depth <= ?)
                ORDER BY priority DESC, id ASC
                LIMIT 1
                """,
                (run_id, now, max_depth, max_depth),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            await connection.execute(
                """
                UPDATE crawl_queue
                SET status = 'in_progress', attempts = attempts + 1,
                    worker_id = ?, lease_expires_at = ?, started_at = COALESCE(started_at, ?)
                WHERE id = ?
                """,
                (worker_id, lease, now, row["id"]),
            )
        return QueueItem(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            requested_url=str(row["requested_url"]),
            depth=int(row["depth"]),
            attempts=int(row["attempts"]) + 1,
            page_type_hint=row["page_type_hint"],
        )

    async def release_in_progress(self, run_id: int, worker_id: str) -> int:
        async with self.transaction() as connection:
            cursor = await connection.execute(
                """
                UPDATE crawl_queue
                SET status = 'pending', worker_id = NULL, lease_expires_at = NULL
                WHERE run_id = ? AND worker_id = ? AND status = 'in_progress'
                """,
                (run_id, worker_id),
            )
        return cursor.rowcount

    async def store_response(
        self,
        run_id: int,
        queue_id: int | None,
        result: FetchResult,
        *,
        challenged: bool,
        challenge_reason: str | None,
    ) -> tuple[int, str]:
        sha256 = hashlib.sha256(result.body).hexdigest()
        compressed = zlib.compress(result.body, level=6)
        now = utc_now()
        safe_headers = {
            key: "[redacted]" if key.lower() == "set-cookie" else value
            for key, value in result.headers.items()
        }
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT OR IGNORE INTO response_bodies(
                    sha256, compression, body_blob, original_bytes,
                    stored_bytes, created_at
                ) VALUES (?, 'zlib', ?, ?, ?, ?)
                """,
                (sha256, compressed, len(result.body), len(compressed), now),
            )
            cursor = await connection.execute(
                """
                INSERT INTO http_responses(
                    run_id, queue_id, requested_url, final_url, redirect_chain_json,
                    http_status, response_headers_json, content_type, charset,
                    body_sha256, response_bytes, elapsed_ms, attempt,
                    is_cloudflare_challenge, challenge_reason, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    queue_id,
                    result.requested_url,
                    result.final_url,
                    json.dumps(result.redirect_chain),
                    result.status,
                    json.dumps(safe_headers, sort_keys=True),
                    result.content_type,
                    result.charset,
                    sha256,
                    len(result.body),
                    result.elapsed_ms,
                    result.attempt,
                    challenged,
                    challenge_reason,
                    now,
                ),
            )
            response_id = int(cursor.lastrowid or 0)
            if queue_id is not None:
                await connection.execute(
                    "UPDATE crawl_queue SET response_id = ? WHERE id = ?",
                    (response_id, queue_id),
                )
        return response_id, sha256

    async def finish_queue(
        self,
        queue_id: int,
        status: str,
        *,
        error: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        if status not in QUEUE_STATUSES:
            raise ValueError(f"invalid queue status: {status}")
        terminal = status not in {"pending", "in_progress"}
        now = utc_now()
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE crawl_queue
                SET status = ?, last_error = ?, next_attempt_at = ?,
                    worker_id = NULL, lease_expires_at = NULL,
                    finished_at = CASE WHEN ? THEN ? ELSE finished_at END
                WHERE id = ?
                """,
                (status, error, next_attempt_at, terminal, now, queue_id),
            )

    async def add_archive_capture(
        self,
        *,
        response_id: int,
        archive_source: str,
        captured_at: str,
        collection_name: str | None = None,
        warc_filename: str | None = None,
        warc_offset: int | None = None,
        warc_length: int | None = None,
        archive_digest: str | None = None,
        truncation_reason: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT INTO archive_captures(
                    response_id, archive_source, collection_name, captured_at,
                    warc_filename, warc_offset, warc_length, archive_digest,
                    truncation_reason, metadata_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response_id,
                    archive_source,
                    collection_name,
                    captured_at,
                    warc_filename,
                    warc_offset,
                    warc_length,
                    archive_digest,
                    truncation_reason,
                    json.dumps(metadata or {}, sort_keys=True),
                    utc_now(),
                ),
            )

    async def archive_capture_exists(
        self,
        *,
        archive_source: str,
        collection_name: str,
        warc_filename: str,
        warc_offset: int,
        warc_length: int,
    ) -> bool:
        cursor = await self.connection.execute(
            """
            SELECT 1 FROM archive_captures
            WHERE archive_source = ? AND collection_name = ?
              AND warc_filename = ? AND warc_offset = ? AND warc_length = ?
            LIMIT 1
            """,
            (
                archive_source,
                collection_name,
                warc_filename,
                warc_offset,
                warc_length,
            ),
        )
        return await cursor.fetchone() is not None

    async def add_parse_failure(
        self,
        response_id: int,
        parser_name: str,
        page_type: str,
        error: Exception,
        context: dict[str, object] | None = None,
    ) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT OR IGNORE INTO parse_failures(
                    response_id, parser_name, page_type, error_type,
                    error_message, selector_context_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response_id,
                    parser_name,
                    page_type,
                    type(error).__name__,
                    str(error),
                    json.dumps(context or {}, sort_keys=True),
                    utc_now(),
                ),
            )

    async def body_by_response(self, response_id: int) -> tuple[aiosqlite.Row, bytes] | None:
        cursor = await self.connection.execute(
            """
            SELECT h.*, b.compression, b.body_blob
            FROM http_responses h
            JOIN response_bodies b ON b.sha256 = h.body_sha256
            WHERE h.id = ?
            """,
            (response_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row, self._restore_body(row["compression"], row["body_blob"])

    async def latest_response_for_url(
        self, run_id: int, requested_url: str
    ) -> tuple[aiosqlite.Row, bytes] | None:
        cursor = await self.connection.execute(
            """
            SELECT h.*, b.compression, b.body_blob
            FROM http_responses h
            JOIN response_bodies b ON b.sha256 = h.body_sha256
            WHERE h.run_id = ? AND h.requested_url = ?
            ORDER BY h.id DESC LIMIT 1
            """,
            (run_id, requested_url),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row, self._restore_body(row["compression"], row["body_blob"])

    async def add_robots_snapshot(
        self,
        *,
        run_id: int,
        response_id: int,
        user_agent: str,
        body_sha256: str,
    ) -> None:
        async with self.transaction() as connection:
            await connection.execute(
                """
                INSERT OR IGNORE INTO robots_snapshots(
                    run_id, response_id, user_agent, body_sha256, fetched_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, response_id, user_agent, body_sha256, utc_now()),
            )

    async def find_responses(
        self,
        *,
        response_id: int | None = None,
        url: str | None = None,
        sha256: str | None = None,
        run_id: int | None = None,
    ) -> list[aiosqlite.Row]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("h.id", response_id),
            ("h.requested_url", url),
            ("h.body_sha256", sha256),
            ("h.run_id", run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await self.connection.execute(
            f"""
            SELECT h.*, b.compression, b.body_blob
            FROM http_responses h
            JOIN response_bodies b ON b.sha256 = h.body_sha256
            {where}
            ORDER BY h.id
            """,  # noqa: S608 - clauses are selected from fixed column names.
            parameters,
        )
        return list(await cursor.fetchall())

    @staticmethod
    def restore_body(row: aiosqlite.Row) -> bytes:
        return Repository._restore_body(row["compression"], row["body_blob"])

    @staticmethod
    def _restore_body(compression: str, body: bytes) -> bytes:
        if compression == "zlib":
            return zlib.decompress(body)
        if compression == "none":
            return bytes(body)
        raise ValueError(f"unsupported body compression: {compression}")

    async def queue_counts(self, run_id: int) -> dict[str, int]:
        counts = {status: 0 for status in QUEUE_STATUSES}
        cursor = await self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM crawl_queue WHERE run_id = ? GROUP BY status",
            (run_id,),
        )
        for row in await cursor.fetchall():
            counts[str(row["status"])] = int(row["count"])
        return counts

    async def refresh_run_counters(self, run_id: int) -> None:
        counts = await self.queue_counts(run_id)
        async with self.transaction() as connection:
            await connection.execute(
                """
                UPDATE crawl_runs SET
                    pages_discovered = (SELECT COUNT(*) FROM crawl_queue WHERE run_id = ?),
                    pages_done = ?, pages_failed = ?, pages_challenged = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    run_id,
                    counts["done"],
                    counts["failed"] + counts["parse_failed"],
                    counts["challenged"],
                    utc_now(),
                    run_id,
                ),
            )

    async def status_report(self, run_key: str) -> dict[str, object]:
        run = await self.get_run(run_key)
        if run is None:
            raise KeyError(f"run not found: {run_key}")
        run_id = int(run["id"])
        queue = await self.queue_counts(run_id)
        response_count = await self._scalar(
            "SELECT COUNT(*) FROM http_responses WHERE run_id = ?", (run_id,)
        )
        challenge_response_count = await self._scalar(
            """
            SELECT COUNT(*) FROM http_responses
            WHERE run_id = ? AND is_cloudflare_challenge = 1
            """,
            (run_id,),
        )
        record_count = await self.normalized_count(run_id)
        provenance_missing = await self.missing_provenance_count()
        foreign_keys = await self.foreign_key_violations()
        checks = {
            "queue_exhausted": queue["pending"] == 0 and queue["in_progress"] == 0,
            "no_failed_pages": queue["failed"] == 0,
            "no_cloudflare_challenges": (
                queue["challenged"] == 0 and challenge_response_count == 0
            ),
            "no_robots_skips": queue["skipped_robots"] == 0,
            "no_parse_failures": queue["parse_failed"] == 0,
            "provenance_complete": provenance_missing == 0,
            "foreign_keys_valid": not foreign_keys,
        }
        return {
            "run_id": run_id,
            "run_key": run_key,
            "status": run["status"],
            "blocked_reason": run["blocked_reason"],
            "queue": queue,
            "http_response_count": response_count,
            "cloudflare_challenge_response_count": challenge_response_count,
            "normalized_record_count": record_count,
            "completion_checks": checks,
            "strict_complete": all(checks.values()) and run["status"] == "completed",
        }

    async def normalized_count(self, run_id: int | None = None) -> int:
        if run_id is not None:
            return await self._scalar(
                """
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT source.record_type, source.record_id
                    FROM record_sources source
                    JOIN http_responses response ON response.id = source.response_id
                    WHERE response.run_id = ?
                )
                """,
                (run_id,),
            )
        total = 0
        for _, table in NORMALIZED_TABLES:
            total += await self._scalar(f"SELECT COUNT(*) FROM {table}")
        return total

    async def table_counts(self) -> dict[str, int]:
        tables = [
            "crawl_runs",
            "crawl_queue",
            "discovery_edges",
            "http_responses",
            "response_bodies",
            "archive_captures",
            "record_sources",
            *(table for _, table in NORMALIZED_TABLES),
            "parse_failures",
        ]
        return {table: await self._scalar(f"SELECT COUNT(*) FROM {table}") for table in tables}

    async def missing_provenance_count(self) -> int:
        total = 0
        for record_type, table in NORMALIZED_TABLES:
            total += await self._scalar(
                f"""
                SELECT COUNT(*) FROM {table} record
                WHERE NOT EXISTS (
                    SELECT 1 FROM record_sources source
                    WHERE source.record_type = ? AND source.record_id = record.id
                )
                """,
                (record_type,),
            )
        return total

    async def foreign_key_violations(self) -> list[dict[str, object]]:
        cursor = await self.connection.execute("PRAGMA foreign_key_check")
        return [dict(row) for row in await cursor.fetchall()]

    async def db_status(self) -> dict[str, object]:
        body = await self.connection.execute(
            """
            SELECT COUNT(*) AS count,
                   COALESCE(SUM(original_bytes), 0) AS raw,
                   COALESCE(SUM(stored_bytes), 0) AS stored
            FROM response_bodies
            """
        )
        body_row = await body.fetchone()
        raw = int(body_row["raw"] if body_row else 0)
        stored = int(body_row["stored"] if body_row else 0)
        return {
            "database": str(self.path),
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "tables": await self.table_counts(),
            "unique_body_count": int(body_row["count"] if body_row else 0),
            "raw_bytes": raw,
            "compressed_bytes": stored,
            "compression_ratio": round(stored / raw, 4) if raw else None,
            "records_missing_provenance": await self.missing_provenance_count(),
            "orphan_records": await self.orphan_record_count(),
            "foreign_key_violations": await self.foreign_key_violations(),
        }

    async def orphan_record_count(self) -> int:
        queries = (
            """
            SELECT COUNT(*) FROM part_occurrences occurrence
            LEFT JOIN part_numbers part ON part.id = occurrence.part_number_id
            LEFT JOIN diagrams diagram ON diagram.id = occurrence.diagram_id
            LEFT JOIN vehicle_configurations vehicle
              ON vehicle.id = occurrence.vehicle_configuration_id
            WHERE part.id IS NULL OR diagram.id IS NULL OR vehicle.id IS NULL
            """,
            """
            SELECT COUNT(*) FROM fitments fitment
            LEFT JOIN part_occurrences occurrence ON occurrence.id = fitment.part_occurrence_id
            LEFT JOIN part_numbers part ON part.id = fitment.part_number_id
            LEFT JOIN diagrams diagram ON diagram.id = fitment.diagram_id
            LEFT JOIN vehicle_configurations vehicle
              ON vehicle.id = fitment.vehicle_configuration_id
            WHERE occurrence.id IS NULL OR part.id IS NULL
               OR diagram.id IS NULL OR vehicle.id IS NULL
            """,
        )
        total = 0
        for query in queries:
            total += await self._scalar(query)
        return total

    async def problem_urls(
        self, run_key: str, statuses: Sequence[str] | None = None
    ) -> list[dict[str, object]]:
        statuses = tuple(statuses or ("failed", "challenged", "skipped_robots", "parse_failed"))
        placeholders = ",".join("?" for _ in statuses)
        cursor = await self.connection.execute(
            f"""
            SELECT q.requested_url, q.status, q.attempts, q.last_error, q.response_id
            FROM crawl_queue q JOIN crawl_runs r ON r.id = q.run_id
            WHERE r.run_key = ? AND q.status IN ({placeholders})
            ORDER BY q.id
            """,  # noqa: S608 - placeholders are generated, not user SQL.
            (run_key, *statuses),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def requeue_problems(self, run_key: str, statuses: Sequence[str]) -> int:
        invalid = set(statuses) - {"failed", "parse_failed", "challenged"}
        if invalid:
            raise ValueError(f"statuses cannot be requeued: {sorted(invalid)}")
        placeholders = ",".join("?" for _ in statuses)
        async with self.transaction() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE crawl_queue SET status = 'pending', next_attempt_at = NULL,
                    last_error = NULL, finished_at = NULL
                WHERE run_id = (SELECT id FROM crawl_runs WHERE run_key = ?)
                  AND status IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated, not user SQL.
                (run_key, *statuses),
            )
            if cursor.rowcount:
                await connection.execute(
                    """
                    UPDATE crawl_runs SET status = 'paused', blocked_reason = NULL,
                        ended_at = NULL, updated_at = ? WHERE run_key = ?
                    """,
                    (utc_now(), run_key),
                )
        return cursor.rowcount

    async def backup(self, destination: Path) -> None:
        async with self._write_lock:
            await self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            await backup_database(self.connection, destination)

    async def publish_snapshot(self, destination: Path) -> dict[str, object]:
        if await asyncio.to_thread(_paths_resolve_equal, self.path, destination):
            raise ValueError("snapshot output must differ from the live database")

        async with self._write_lock:
            await self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return await publish_snapshot(self.connection, destination)

    async def checkpoint(self) -> None:
        async with self._write_lock:
            await self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    async def _scalar(self, query: str, parameters: Sequence[object] = ()) -> int:
        cursor = await self.connection.execute(query, parameters)
        row = await cursor.fetchone()
        return int(row[0] if row else 0)
