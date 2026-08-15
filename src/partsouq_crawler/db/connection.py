from __future__ import annotations

from pathlib import Path

import aiosqlite


async def connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute("PRAGMA journal_mode = WAL")
    await connection.execute("PRAGMA busy_timeout = 5000")
    return connection


async def apply_schema(connection: aiosqlite.Connection) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    await connection.executescript(schema_path.read_text(encoding="utf-8"))
    await connection.commit()
