from __future__ import annotations

import aiosqlite

from partsouq_crawler.db.repository import utc_now


async def add_source(
    connection: aiosqlite.Connection,
    *,
    record_type: str,
    record_id: int,
    response_id: int,
    parser_name: str,
    parser_version: str,
    source_url: str,
) -> None:
    await connection.execute(
        """
        INSERT OR IGNORE INTO record_sources(
            record_type, record_id, response_id, parser_name,
            parser_version, source_url, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_type,
            record_id,
            response_id,
            parser_name,
            parser_version,
            source_url,
            utc_now(),
        ),
    )
