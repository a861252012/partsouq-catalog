from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite


async def backup_database(source: aiosqlite.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = await aiosqlite.connect(destination)
    try:
        await source.backup(target)
        await target.commit()
    finally:
        await target.close()


async def publish_snapshot(
    source: aiosqlite.Connection,
    destination: Path,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = destination.with_name(f"{destination.name}.manifest.json")
    lock_path = destination.with_name(f"{destination.name}.publishing")

    try:
        lock_path.touch(exist_ok=False)
    except FileExistsError as error:
        raise RuntimeError(f"snapshot publish lock already exists: {lock_path}") from error

    database_fd, database_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    manifest_fd, manifest_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(database_fd)
    os.close(manifest_fd)
    temporary_database = Path(database_name)
    temporary_manifest = Path(manifest_name)

    try:
        await backup_database(source, temporary_database)
        snapshot = await aiosqlite.connect(
            f"file:{temporary_database}?mode=ro",
            uri=True,
        )
        try:
            integrity_cursor = await snapshot.execute("PRAGMA integrity_check")
            integrity_rows = [row[0] for row in await integrity_cursor.fetchall()]
            if integrity_rows != ["ok"]:
                raise RuntimeError(f"snapshot integrity check failed: {integrity_rows}")

            version_cursor = await snapshot.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            )
            version_row = await version_cursor.fetchone()
            schema_version = int(version_row[0] if version_row else 0)

            run_cursor = await snapshot.execute(
                """
                SELECT id, run_key, status, blocked_reason, ended_at
                FROM crawl_runs
                ORDER BY id DESC
                LIMIT 1
                """
            )
            run_row = await run_cursor.fetchone()
        finally:
            await snapshot.close()

        sha256, database_bytes = await asyncio.to_thread(
            _digest_and_size,
            temporary_database,
        )

        manifest: dict[str, Any] = {
            "format": "partsouq-snapshot-manifest-v1",
            "database": destination.name,
            "sha256": sha256,
            "bytes": database_bytes,
            "schema_version": schema_version,
            "published_at": datetime.now(UTC).isoformat(),
            "integrity_check": "ok",
            "latest_run": (
                {
                    "id": int(run_row[0]),
                    "run_key": str(run_row[1]),
                    "status": str(run_row[2]),
                    "blocked_reason": run_row[3],
                    "ended_at": run_row[4],
                }
                if run_row
                else None
            ),
        }
        await asyncio.to_thread(
            _replace_published_files,
            temporary_database,
            temporary_manifest,
            destination,
            manifest_path,
            manifest,
        )
        return manifest
    finally:
        await asyncio.to_thread(
            _remove_paths,
            temporary_database,
            temporary_manifest,
            lock_path,
        )


def _digest_and_size(path: Path) -> tuple[str, int]:
    with path.open("rb") as snapshot_file:
        sha256 = hashlib.file_digest(snapshot_file, "sha256").hexdigest()
    return sha256, path.stat().st_size


def _replace_published_files(
    temporary_database: Path,
    temporary_manifest: Path,
    destination: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_database.chmod(0o444)
    temporary_manifest.chmod(0o444)
    os.replace(temporary_database, destination)
    os.replace(temporary_manifest, manifest_path)


def _remove_paths(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
