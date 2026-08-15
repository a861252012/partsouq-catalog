from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from partsouq_crawler.crawl.browser_fetcher import BrowserFetcher


class FakeRequest:
    def __init__(self, url: str, redirected_from: FakeRequest | None = None) -> None:
        self.url = url
        self.redirected_from = redirected_from


class FakeResponse:
    url = "https://example.test/final"
    status = 200
    request = FakeRequest(url, FakeRequest("https://example.test/start"))

    async def body(self) -> bytes:
        return b"<html>catalog</html>"

    async def all_headers(self) -> dict[str, str]:
        return {"content-type": "text/html; charset=utf-8"}


class FakePage:
    async def goto(self, url: str, *, wait_until: str) -> FakeResponse:
        assert url == "https://example.test/start"
        assert wait_until == "domcontentloaded"
        return FakeResponse()


def test_browser_fetcher_returns_raw_document_response() -> None:
    async def scenario() -> None:
        fetcher = BrowserFetcher(
            timeout_seconds=3,
            delay_seconds=0,
            executable_path=Path("/not-used-by-fetch-once"),
            headless=True,
            user_agent=None,
        )
        fetcher.page = cast(Any, FakePage())

        result = await fetcher.fetch_once("https://example.test/start")

        assert result.final_url == "https://example.test/final"
        assert result.status == 200
        assert result.body == b"<html>catalog</html>"
        assert result.content_type == "text/html"
        assert result.redirect_chain == ("https://example.test/start",)

    asyncio.run(scenario())
