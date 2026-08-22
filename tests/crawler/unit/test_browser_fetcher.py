from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from playwright._impl import _transport as playwright_transport

from partsouq_crawler.crawl import browser_fetcher as browser_fetcher_module
from partsouq_crawler.crawl.browser_fetcher import (
    BrowserFetcher,
    browser_driver_environment,
    browser_process_environment,
)
from partsouq_crawler.crawl.fetcher import FetchError


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


def test_browser_fetcher_refuses_macos_codex_sandbox_before_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        fetcher = BrowserFetcher(
            timeout_seconds=3,
            delay_seconds=0,
            executable_path=None,
            headless=True,
            user_agent=None,
        )

        def playwright_start() -> None:
            pytest.fail("Playwright must not start")

        monkeypatch.setattr(
            "partsouq_crawler.crawl.browser_fetcher.async_playwright",
            playwright_start,
        )
        with pytest.raises(FetchError, match="macOS Codex sandbox"):
            await fetcher.__aenter__()

    monkeypatch.setattr("partsouq_crawler.crawl.browser_fetcher.sys.platform", "darwin")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")

    asyncio.run(scenario())


def test_browser_process_environment_does_not_inherit_application_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "database-secret")
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("PARTSOUQ_MYSQL_ROOT_PASSWORD", "root-secret")
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_SECRET_KEY", "session-secret")
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_PASSWORD", "login-secret")
    monkeypatch.setenv("PSQ_DB_PASS", "legacy-secret")
    monkeypatch.setenv("PATH", "/test/bin")

    environment = browser_process_environment()

    assert environment["PATH"] == "/test/bin"
    assert "PARTSOUQ_DB_PASSWORD" not in environment
    assert "PARTSOUQ_ADMIN_TOKEN" not in environment
    assert "PARTSOUQ_MYSQL_ROOT_PASSWORD" not in environment
    assert "PARTSOUQ_STATION_ADMIN_SECRET_KEY" not in environment
    assert "PARTSOUQ_STATION_ADMIN_PASSWORD" not in environment
    assert "PSQ_DB_PASS" not in environment


def test_browser_driver_environment_restores_parent_after_sanitized_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_driver_environment = playwright_transport.get_driver_env
    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "database-secret")
    monkeypatch.setenv("PATH", "/test/bin")

    with browser_driver_environment():
        driver_environment = playwright_transport.get_driver_env()
        assert driver_environment["PATH"] == "/test/bin"
        assert "PARTSOUQ_DB_PASSWORD" not in driver_environment
        assert os.environ["PARTSOUQ_DB_PASSWORD"] == "database-secret"

    assert os.environ["PARTSOUQ_DB_PASSWORD"] == "database-secret"
    assert playwright_transport.get_driver_env is original_driver_environment


def test_browser_driver_environment_does_not_hold_lock_during_driver_start() -> None:
    with browser_driver_environment():
        acquired = browser_fetcher_module._BROWSER_DRIVER_ENVIRONMENT_LOCK.acquire(blocking=False)
        assert acquired is True
        browser_fetcher_module._BROWSER_DRIVER_ENVIRONMENT_LOCK.release()


def test_browser_driver_environment_stays_sanitized_until_last_user_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_driver_environment = playwright_transport.get_driver_env
    first_entered = threading.Event()
    second_entered = threading.Event()
    first_exited = threading.Event()
    observed_environment: dict[str, str] = {}
    thread_failures: list[str] = []

    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "database-secret")

    def first_user() -> None:
        try:
            with browser_driver_environment():
                first_entered.set()
                if not second_entered.wait(timeout=2):
                    thread_failures.append("second user did not enter concurrently")
        finally:
            first_exited.set()

    def second_user() -> None:
        if not first_entered.wait(timeout=2):
            thread_failures.append("first user did not enter")
            return
        with browser_driver_environment():
            second_entered.set()
            if not first_exited.wait(timeout=2):
                thread_failures.append("first user did not exit")
            observed_environment.update(playwright_transport.get_driver_env())

    first_thread = threading.Thread(target=first_user)
    second_thread = threading.Thread(target=second_user)
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=3)
    second_thread.join(timeout=3)

    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    assert thread_failures == []
    assert "PARTSOUQ_DB_PASSWORD" not in observed_environment
    assert playwright_transport.get_driver_env is original_driver_environment


def test_browser_fetcher_sanitizes_node_and_browser_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_environment: dict[str, str] = {}
    launch_options: dict[str, object] = {}

    class FakePage:
        def set_default_navigation_timeout(self, _timeout: float) -> None:
            return None

        async def evaluate(self, _expression: str) -> str:
            return "test-agent"

        async def close(self) -> None:
            return None

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def close(self) -> None:
            return None

    class FakeBrowser:
        async def new_context(self, **_kwargs: object) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            return None

    class FakeChromium:
        async def launch(self, **kwargs: object) -> FakeBrowser:
            launch_options.update(kwargs)
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self) -> None:
            return None

    class FakeManager:
        async def start(self) -> FakePlaywright:
            node_environment.update(playwright_transport.get_driver_env())
            return FakePlaywright()

    async def scenario() -> None:
        async with BrowserFetcher(
            timeout_seconds=3,
            delay_seconds=0,
            executable_path=None,
            headless=True,
            user_agent=None,
        ):
            pass

    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "database-secret")
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(
        "partsouq_crawler.crawl.browser_fetcher.async_playwright",
        lambda: FakeManager(),
    )

    asyncio.run(scenario())

    assert "PARTSOUQ_DB_PASSWORD" not in node_environment
    assert "PARTSOUQ_ADMIN_TOKEN" not in node_environment
    browser_environment = launch_options["env"]
    assert isinstance(browser_environment, dict)
    assert "PARTSOUQ_DB_PASSWORD" not in browser_environment
    assert "PARTSOUQ_ADMIN_TOKEN" not in browser_environment


def test_browser_fetcher_stops_playwright_when_browser_launch_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = False

    class FakeChromium:
        async def launch(self, **_kwargs: object) -> None:
            raise asyncio.CancelledError

    class FakePlaywright:
        chromium = FakeChromium()

        async def stop(self) -> None:
            nonlocal stopped
            stopped = True

    class FakeManager:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    async def scenario() -> None:
        fetcher = BrowserFetcher(
            timeout_seconds=3,
            delay_seconds=0,
            executable_path=None,
            headless=True,
            user_agent=None,
        )
        with pytest.raises(asyncio.CancelledError):
            await fetcher.__aenter__()
        assert fetcher.playwright is None

    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(
        "partsouq_crawler.crawl.browser_fetcher.async_playwright",
        lambda: FakeManager(),
    )

    asyncio.run(scenario())

    assert stopped is True


def test_browser_fetcher_stops_manager_when_driver_start_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager_stopped = False

    class FakeManager:
        async def start(self) -> None:
            raise asyncio.CancelledError

        async def __aexit__(self, *_args: object) -> None:
            nonlocal manager_stopped
            manager_stopped = True

    async def scenario() -> None:
        fetcher = BrowserFetcher(
            timeout_seconds=3,
            delay_seconds=0,
            executable_path=None,
            headless=True,
            user_agent=None,
        )
        with pytest.raises(asyncio.CancelledError):
            await fetcher.__aenter__()

    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(
        "partsouq_crawler.crawl.browser_fetcher.async_playwright",
        FakeManager,
    )

    asyncio.run(scenario())

    assert manager_stopped is True


def test_browser_fetcher_closes_remaining_resources_after_page_close_error() -> None:
    events: list[str] = []

    class FailingPage:
        async def close(self) -> None:
            events.append("page")
            raise RuntimeError("page close failed")

    class FakeContext:
        async def close(self) -> None:
            events.append("context")

    class FakeBrowser:
        async def close(self) -> None:
            events.append("browser")

    class FakePlaywright:
        async def stop(self) -> None:
            events.append("playwright")

    async def scenario() -> None:
        fetcher = BrowserFetcher(
            timeout_seconds=3,
            delay_seconds=0,
            executable_path=None,
            headless=True,
            user_agent=None,
        )
        fetcher.page = cast(Any, FailingPage())
        fetcher.context = cast(Any, FakeContext())
        fetcher.browser = cast(Any, FakeBrowser())
        fetcher.playwright = cast(Any, FakePlaywright())

        with pytest.raises(RuntimeError, match="page close failed"):
            await fetcher.__aexit__()

        assert fetcher.page is None
        assert fetcher.context is None
        assert fetcher.browser is None
        assert fetcher.playwright is None

    asyncio.run(scenario())

    assert events == ["page", "context", "browser", "playwright"]
