from __future__ import annotations

import aiosqlite

from partsouq_crawler.db.connection import apply_schema


async def migrate(connection: aiosqlite.Connection) -> None:
    """Apply the current idempotent schema."""
    await apply_schema(connection)
