from __future__ import annotations

import ssl
from time import monotonic

import aiohttp
import certifi

from partsouq_crawler.crawl.rate_limit import HostRateLimiter
from partsouq_crawler.models.crawl import FetchResult


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        delay_seconds: float,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self.rate_limiter = HostRateLimiter(delay_seconds)
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Fetcher:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.session = aiohttp.ClientSession(
            timeout=self.timeout,
            cookie_jar=aiohttp.CookieJar(),
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xml;q=0.9,*/*;q=0.1",
            },
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.session is not None:
            await self.session.close()

    async def fetch_once(self, url: str, *, attempt: int = 1) -> FetchResult:
        if self.session is None:
            raise RuntimeError("fetcher must be used as an async context manager")
        await self.rate_limiter.wait()
        started = monotonic()
        try:
            async with self.session.get(url, allow_redirects=True) as response:
                body = await response.read()
                elapsed_ms = round((monotonic() - started) * 1000)
                history = tuple(str(item.url) for item in response.history)
                return FetchResult(
                    requested_url=url,
                    final_url=str(response.url),
                    status=response.status,
                    headers={key: value for key, value in response.headers.items()},
                    body=body,
                    elapsed_ms=elapsed_ms,
                    attempt=attempt,
                    redirect_chain=history,
                )
        except (aiohttp.ClientError, TimeoutError) as error:
            raise FetchError(f"{type(error).__name__}: {error}") from error
