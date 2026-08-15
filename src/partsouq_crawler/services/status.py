from __future__ import annotations

from partsouq_crawler.db.repository import Repository


async def crawl_status(repository: Repository, run_key: str) -> dict[str, object]:
    return await repository.status_report(run_key)


async def database_status(repository: Repository) -> dict[str, object]:
    return await repository.db_status()
