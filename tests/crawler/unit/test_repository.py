import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.models.crawl import FetchResult


def test_persistent_queue_and_url_dedupe(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "queue.sqlite3"
        repository = await Repository.create(path)
        run_id = await repository.create_or_get_run("run", ["https://x/"], {})
        assert await repository.enqueue(run_id, "https://x/a?b=1", parent_url=None, depth=0)
        assert not await repository.enqueue(
            run_id, "https://x/a?b=1#frag", parent_url=None, depth=0
        )
        await repository.close()

        reopened = await Repository.create(path)
        assert (await reopened.queue_counts(run_id))["pending"] == 1
        await reopened.close()

    asyncio.run(scenario())


def test_query_order_creates_distinct_queue_items(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "query.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        assert await repository.enqueue(run_id, "https://x/a?ssd=x&uid=1", parent_url=None, depth=0)
        assert await repository.enqueue(run_id, "https://x/a?uid=1&ssd=x", parent_url=None, depth=0)
        assert (await repository.queue_counts(run_id))["pending"] == 2
        await repository.close()

    asyncio.run(scenario())


def test_lease_expiry_recovery(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "lease.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        await repository.enqueue(run_id, "https://x/a", parent_url=None, depth=0)
        item = await repository.acquire_next(run_id, worker_id="dead", lease_seconds=1, max_depth=0)
        assert item is not None
        expired = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        await repository.connection.execute(
            "UPDATE crawl_queue SET lease_expires_at = ? WHERE id = ?", (expired, item.id)
        )
        await repository.connection.commit()
        assert await repository.recover_expired_leases(run_id) == 1
        assert (await repository.queue_counts(run_id))["pending"] == 1
        await repository.close()

    asyncio.run(scenario())


def test_response_body_sha_dedupe_and_restore(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "body.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        result = FetchResult(
            requested_url="https://x/a",
            final_url="https://x/a",
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"same body" * 100,
            elapsed_ms=1,
            attempt=1,
        )
        first_id, first_sha = await repository.store_response(
            run_id, None, result, challenged=False, challenge_reason=None
        )
        second_id, second_sha = await repository.store_response(
            run_id, None, result, challenged=False, challenge_reason=None
        )
        assert first_id != second_id and first_sha == second_sha
        status = await repository.db_status()
        assert status["unique_body_count"] == 1
        restored = await repository.body_by_response(first_id)
        assert restored is not None and restored[1] == result.body
        await repository.close()

    asyncio.run(scenario())


def test_status_report_counts_challenge_response_without_queue_item(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "probe.sqlite3")
        run_id = await repository.create_or_get_run("probe", [], {})
        result = FetchResult(
            requested_url="https://x/challenge",
            final_url="https://x/challenge",
            status=403,
            headers={"cf-mitigated": "challenge", "Content-Type": "text/html"},
            body=b"challenge",
            elapsed_ms=1,
            attempt=1,
        )
        await repository.store_response(
            run_id,
            None,
            result,
            challenged=True,
            challenge_reason="cloudflare_challenge",
        )
        await repository.set_run_status(
            run_id,
            "blocked",
            blocked_reason="cloudflare_challenge",
            ended=True,
        )

        status = await repository.status_report("probe")

        assert status["cloudflare_challenge_response_count"] == 1
        assert status["completion_checks"]["no_cloudflare_challenges"] is False
        assert status["strict_complete"] is False
        await repository.close()

    asyncio.run(scenario())


def test_set_cookie_is_redacted(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "cookie.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        result = FetchResult(
            requested_url="https://x/a",
            final_url="https://x/a",
            status=200,
            headers={"Set-Cookie": "secret=1", "Content-Type": "text/plain"},
            body=b"ok",
            elapsed_ms=1,
            attempt=1,
        )
        response_id, _ = await repository.store_response(
            run_id, None, result, challenged=False, challenge_reason=None
        )
        row, _ = (await repository.body_by_response(response_id)) or (None, b"")
        assert row is not None
        assert json.loads(row["response_headers_json"])["Set-Cookie"] == "[redacted]"
        await repository.close()

    asyncio.run(scenario())


def test_sqlite_backup_api(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source.sqlite3"
        destination = tmp_path / "backup.sqlite3"
        repository = await Repository.create(source)
        await repository.create_or_get_run("run", [], {})
        await repository.backup(destination)
        backup = await Repository.create(destination)
        assert await backup.get_run("run") is not None
        await backup.close()
        await repository.close()

    asyncio.run(scenario())


def test_snapshot_publish_writes_valid_database_and_manifest(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source.sqlite3"
        destination = tmp_path / "partsouq-current.sqlite3"
        repository = await Repository.create(source)
        await repository.create_or_get_run("published-run", [], {})

        manifest = await repository.publish_snapshot(destination)

        assert destination.exists()
        assert destination.with_name(f"{destination.name}.manifest.json").exists()
        assert manifest["format"] == "partsouq-snapshot-manifest-v1"
        assert manifest["schema_version"] == 1
        assert manifest["bytes"] == destination.stat().st_size

        snapshot = await aiosqlite.connect(f"file:{destination}?mode=ro", uri=True)
        integrity = await (await snapshot.execute("PRAGMA integrity_check")).fetchone()
        run = await (
            await snapshot.execute("SELECT run_key FROM crawl_runs ORDER BY id DESC LIMIT 1")
        ).fetchone()
        await snapshot.close()

        assert integrity == ("ok",)
        assert run == ("published-run",)
        assert not destination.with_name(f"{destination.name}.publishing").exists()
        await repository.close()

    asyncio.run(scenario())


def test_snapshot_publish_rejects_live_database_as_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "live.sqlite3"
        repository = await Repository.create(path)
        try:
            await repository.publish_snapshot(path)
        except ValueError as error:
            assert "must differ" in str(error)
        else:
            raise AssertionError("live database was accepted as snapshot output")
        await repository.close()

    asyncio.run(scenario())


def test_requeue_challenge_requires_explicit_command(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = await Repository.create(tmp_path / "requeue.sqlite3")
        run_id = await repository.create_or_get_run("run", [], {})
        await repository.enqueue(run_id, "https://x/a", parent_url=None, depth=0)
        item = await repository.acquire_next(run_id, worker_id="w", lease_seconds=1, max_depth=0)
        assert item is not None
        await repository.finish_queue(item.id, "challenged", error="challenge")
        assert (await repository.queue_counts(run_id))["challenged"] == 1
        assert await repository.requeue_problems("run", ["challenged"]) == 1
        assert (await repository.queue_counts(run_id))["pending"] == 1
        await repository.close()

    asyncio.run(scenario())
