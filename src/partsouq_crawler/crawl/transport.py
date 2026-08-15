from __future__ import annotations

from typing import Protocol, Self

from partsouq_crawler.config import CrawlerConfig
from partsouq_crawler.crawl.browser_fetcher import BrowserFetcher
from partsouq_crawler.crawl.fetcher import Fetcher
from partsouq_crawler.models.crawl import FetchResult


class FetchTransport(Protocol):
    user_agent: str

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *_: object) -> None: ...

    async def fetch_once(self, url: str, *, attempt: int = 1) -> FetchResult: ...


def create_fetch_transport(
    config: CrawlerConfig, *, delay_seconds: float | None = None
) -> FetchTransport:
    delay = config.delay_seconds if delay_seconds is None else delay_seconds
    if config.transport == "browser":
        return BrowserFetcher(
            timeout_seconds=config.request_timeout_seconds,
            delay_seconds=delay,
            executable_path=config.browser_executable,
            headless=config.browser_headless,
            user_agent=config.user_agent or None,
        )
    return Fetcher(
        user_agent=config.user_agent or "partsouq-crawler/0.1",
        timeout_seconds=config.request_timeout_seconds,
        delay_seconds=delay,
    )
