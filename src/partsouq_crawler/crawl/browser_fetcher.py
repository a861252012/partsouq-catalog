from __future__ import annotations

from pathlib import Path
from time import monotonic

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from partsouq_crawler.crawl.fetcher import FetchError
from partsouq_crawler.crawl.rate_limit import HostRateLimiter
from partsouq_crawler.models.crawl import FetchResult


class BrowserFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        delay_seconds: float,
        executable_path: Path | None,
        headless: bool,
        user_agent: str | None,
    ) -> None:
        self.timeout_ms = timeout_seconds * 1000
        self.rate_limiter = HostRateLimiter(delay_seconds)
        self.executable_path = executable_path
        self.headless = headless
        self.configured_user_agent = user_agent
        self.user_agent = user_agent or ""
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self) -> BrowserFetcher:
        if self.executable_path is not None and not self.executable_path.is_file():
            raise FetchError(f"browser executable not found: {self.executable_path}")
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                executable_path=str(self.executable_path) if self.executable_path else None,
                headless=self.headless,
            )
            if self.configured_user_agent:
                self.context = await self.browser.new_context(user_agent=self.configured_user_agent)
            else:
                self.context = await self.browser.new_context()
            self.page = await self.context.new_page()
            self.page.set_default_navigation_timeout(self.timeout_ms)
            if not self.user_agent:
                self.user_agent = str(await self.page.evaluate("navigator.userAgent"))
            return self
        except PlaywrightError as error:
            await self.__aexit__()
            raise FetchError(f"{type(error).__name__}: {error}") from error

    async def __aexit__(self, *_: object) -> None:
        if self.page is not None:
            await self.page.close()
        if self.context is not None:
            await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self.playwright is not None:
            await self.playwright.stop()

    async def fetch_once(self, url: str, *, attempt: int = 1) -> FetchResult:
        if self.page is None:
            raise RuntimeError("browser fetcher must be used as an async context manager")
        await self.rate_limiter.wait()
        started = monotonic()
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded")
            if response is None:
                raise FetchError("browser navigation returned no document response")
            body = await response.body()
            headers = await response.all_headers()
            elapsed_ms = round((monotonic() - started) * 1000)
            return FetchResult(
                requested_url=url,
                final_url=response.url,
                status=response.status,
                headers=headers,
                body=body,
                elapsed_ms=elapsed_ms,
                attempt=attempt,
                redirect_chain=self._redirect_chain(response),
            )
        except FetchError:
            raise
        except PlaywrightError as error:
            raise FetchError(f"{type(error).__name__}: {error}") from error

    @staticmethod
    def _redirect_chain(response: Response) -> tuple[str, ...]:
        chain: list[str] = []
        request = response.request.redirected_from
        while request is not None:
            chain.append(request.url)
            request = request.redirected_from
        chain.reverse()
        return tuple(chain)
