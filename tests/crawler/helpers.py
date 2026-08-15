from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiohttp import web

from partsouq_crawler.config import CrawlerConfig
from partsouq_crawler.crawl.engine import CrawlerEngine
from partsouq_crawler.db.repository import Repository

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def fake_site(handler: Handler) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server else []
    port = sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def crawl_fake(
    database: Path,
    seed_url: str,
    *,
    run_key: str = "test-run",
    max_pages: int = 0,
    max_retries: int = 0,
    robots_policy: str = "require",
    concurrency: int = 1,
) -> tuple[int, dict[str, Any]]:
    repository = await Repository.create(database)
    try:
        config = CrawlerConfig(
            database=database,
            concurrency=concurrency,
            delay_seconds=0,
            request_timeout_seconds=3,
            max_retries=max_retries,
            max_pages=max_pages,
            user_agent="partsouq-test/1.0",
            robots_policy=robots_policy,
            lease_seconds=1,
        )
        code = await CrawlerEngine(
            repository=repository,
            config=config,
            run_key=run_key,
            seed_url=seed_url,
        ).run()
        return code, await repository.status_report(run_key)
    finally:
        await repository.close()


def robots_response() -> web.Response:
    return web.Response(text="User-agent: *\nAllow: /\n", content_type="text/plain")
