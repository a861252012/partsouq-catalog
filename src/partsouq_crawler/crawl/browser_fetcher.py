from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Protocol, cast

from playwright._impl import _transport as playwright_transport
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

_BROWSER_DRIVER_ENVIRONMENT_LOCK = threading.Lock()
_BROWSER_DRIVER_ENVIRONMENT_USERS = 0


class _DriverTransportModule(Protocol):
    get_driver_env: Callable[[], dict[str, str]]


_DRIVER_TRANSPORT = cast(_DriverTransportModule, playwright_transport)
# Playwright 沒有公開 node env 參數；此 hook 由 uv.lock 固定版本，回歸測試會在
# transport 契約變動時失敗，升級 Playwright 時必須同步重驗。
_ORIGINAL_DRIVER_ENVIRONMENT = _DRIVER_TRANSPORT.get_driver_env


def browser_process_environment() -> dict[str, str | float | bool]:
    """Return the browser's minimal runtime environment without app secrets."""
    allowed = {
        "ALL_PROXY",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_NODEJS_PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _sanitized_driver_environment() -> dict[str, str]:
    allowed_environment = browser_process_environment()
    return {
        key: str(value)
        for key, value in _ORIGINAL_DRIVER_ENVIRONMENT().items()
        if key in allowed_environment or key.startswith("PW_")
    }


@contextmanager
def browser_driver_environment() -> Iterator[None]:
    """Start Playwright's node driver without exposing application secrets."""
    global _BROWSER_DRIVER_ENVIRONMENT_USERS

    with _BROWSER_DRIVER_ENVIRONMENT_LOCK:
        if _BROWSER_DRIVER_ENVIRONMENT_USERS == 0:
            _DRIVER_TRANSPORT.get_driver_env = _sanitized_driver_environment
        _BROWSER_DRIVER_ENVIRONMENT_USERS += 1
    try:
        yield
    finally:
        with _BROWSER_DRIVER_ENVIRONMENT_LOCK:
            _BROWSER_DRIVER_ENVIRONMENT_USERS -= 1
            if _BROWSER_DRIVER_ENVIRONMENT_USERS == 0:
                _DRIVER_TRANSPORT.get_driver_env = _ORIGINAL_DRIVER_ENVIRONMENT


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
        if sys.platform == "darwin" and os.environ.get("CODEX_SANDBOX"):
            raise FetchError(
                "refusing to launch Chromium inside the macOS Codex sandbox; "
                "run browser transport from an Aqua LaunchAgent"
            )
        if self.executable_path is not None and not self.executable_path.is_file():
            raise FetchError(f"browser executable not found: {self.executable_path}")
        manager = None
        try:
            manager = async_playwright()
            with browser_driver_environment():
                self.playwright = await manager.start()
            self.browser = await self.playwright.chromium.launch(
                executable_path=str(self.executable_path) if self.executable_path else None,
                headless=self.headless,
                env=browser_process_environment(),
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
        except BaseException as error:
            cleanup_error: BaseException | None = None
            if self.playwright is None and manager is not None:
                try:
                    await manager.__aexit__()
                except BaseException as caught_cleanup_error:
                    cleanup_error = caught_cleanup_error
            try:
                await self.__aexit__()
            except BaseException as caught_cleanup_error:
                if cleanup_error is None:
                    cleanup_error = caught_cleanup_error
            if isinstance(error, PlaywrightError):
                detail = f"{type(error).__name__}: {error}"
                if cleanup_error is not None:
                    detail += f"; cleanup failed: {type(cleanup_error).__name__}: {cleanup_error}"
                raise FetchError(detail) from error
            if cleanup_error is not None:
                error.add_note(
                    "browser cleanup failed after startup error: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            raise

    async def __aexit__(self, *_: object) -> None:
        cleanup_error: BaseException | None = None
        if self.page is not None:
            page, self.page = self.page, None
            try:
                await page.close()
            except BaseException as error:
                cleanup_error = error
        if self.context is not None:
            context, self.context = self.context, None
            try:
                await context.close()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if self.browser is not None:
            browser, self.browser = self.browser, None
            try:
                await browser.close()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if self.playwright is not None:
            playwright, self.playwright = self.playwright, None
            try:
                await playwright.stop()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error

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
