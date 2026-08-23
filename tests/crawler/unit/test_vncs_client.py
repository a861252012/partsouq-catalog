from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from partsouq_crawler.vncs.client import VncsClient, VncsClientError
from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.parser import VncsParserError

from ..helpers import fake_site

INITIAL_PAGE = """<!DOCTYPE html>
<html><body><form method="post">
<input type="hidden" name="__VIEWSTATE" value="/wEPDwULLTEstate==" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="C2EE9ABB" />
<input type="hidden" name="__EVENTVALIDATION" value="/wEdAAEeVvvalidation==" />
<select name="dlFtrMOBTYPE"><option>汽油車</option></select>
<select name="dlFtrPERIOD"><option>第一期</option></select>
<select name="dlFtrTESTTYPE"><option>新車型</option></select>
<input type="submit" name="btnQuery" value="查詢" />
</form></body></html>"""

RESULT_PAGE_TEMPLATE = "<html><body>{marker}</body></html>"


def _config(base_url: str) -> VncsConfig:
    # replace() 繞過 validate() 的正式站台 allowlist，僅用於 fake_site 測試。
    return replace(
        VncsConfig.from_env(user_agent="vncs-test/1.0", request_timeout_seconds=10),
        base_url=f"{base_url}/VNCSEXLRPT.aspx",
    )


def _client(config: VncsConfig) -> VncsClient:
    client = VncsClient(config)
    # 加速測試：關掉節流等待，但保留 retry backoff 的可控性。
    client.rate_limiter.delay_seconds = 0.0
    return client


def _result_page(kind: str) -> bytes:
    return RESULT_PAGE_TEMPLATE.format(marker=f"results-for-{kind}").encode()


def test_fetch_reports_gets_state_then_posts_each_vehicle_kind() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: web.Request) -> web.Response:
        if request.method == "GET":
            requests.append({"method": "GET", "path": request.path})
            return web.Response(body=INITIAL_PAGE.encode(), content_type="text/html")
        form = await request.post()
        kind = str(form.get("dlFtrMOBTYPE"))
        requests.append(
            {
                "method": "POST",
                "path": request.path,
                "kind": kind,
                "viewstate": form.get("__VIEWSTATE"),
                "event_validation": form.get("__EVENTVALIDATION"),
                "viewstate_generator": form.get("__VIEWSTATEGENERATOR"),
                "period": form.get("dlFtrPERIOD"),
                "test_type": form.get("dlFtrTESTTYPE"),
                "content_type": request.headers.get("Content-Type", ""),
                "user_agent": request.headers.get("User-Agent", ""),
            }
        )
        return web.Response(body=_result_page(kind), content_type="text/html")

    async def scenario() -> None:
        async with fake_site(handler) as base_url, _client(_config(base_url)) as client:
            reports = await client.fetch_reports()

        assert [kind for kind, _html in reports] == ["汽油車", "柴油車"]
        assert reports[0][1] == _result_page("汽油車")
        assert reports[1][1] == _result_page("柴油車")
        assert [entry["method"] for entry in requests] == ["GET", "POST", "POST"]
        for entry in requests[1:]:
            assert entry["kind"] in ("汽油車", "柴油車")
            # POST 必須回傳 GET 頁面動態解析到的 ASP.NET state。
            assert entry["viewstate"] == "/wEPDwULLTEstate=="
            assert entry["event_validation"] == "/wEdAAEeVvvalidation=="
            assert entry["viewstate_generator"] == "C2EE9ABB"
            # dlFtr* 實名為 live 驗證過的合約斷言。
            assert entry["period"] == ""
            assert entry["test_type"] == ""
            assert entry["content_type"].startswith("application/x-www-form-urlencoded")
            assert entry["user_agent"] == "vncs-test/1.0"

    asyncio.run(scenario())


def test_fetch_reports_fails_closed_when_initial_page_lacks_viewstate() -> None:
    broken_html = INITIAL_PAGE.replace('name="__VIEWSTATE"', 'name="__OLD_STATE"').encode()

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=broken_html, content_type="text/html")

    async def scenario() -> None:
        async with fake_site(handler) as base_url, _client(_config(base_url)) as client:
            with pytest.raises(VncsParserError):
                await client.fetch_reports()

    asyncio.run(scenario())


def test_fetch_reports_fails_closed_when_form_controls_are_renamed() -> None:
    broken_html = INITIAL_PAGE.replace('name="dlFtrMOBTYPE"', 'name="dlRenamed"').encode()

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=broken_html, content_type="text/html")

    async def scenario() -> None:
        async with fake_site(handler) as base_url, _client(_config(base_url)) as client:
            with pytest.raises(VncsParserError, match="dlFtrMOBTYPE"):
                await client.fetch_reports()

    asyncio.run(scenario())


def test_request_retries_server_errors_with_backoff_and_recovers() -> None:
    attempts: list[int] = []

    async def handler(request: web.Request) -> web.Response:
        if request.method == "POST":
            attempts.append(1)
            if len(attempts) < 3:
                return web.Response(status=503, text="temporarily unavailable")
            return web.Response(body=_result_page("柴油車"), content_type="text/html")
        return web.Response(body=INITIAL_PAGE.encode(), content_type="text/html")

    async def scenario() -> None:
        async with fake_site(handler) as base_url:
            config = _config(base_url)
            async with _client(config) as client:
                client.retry_backoff_seconds = 0.0
                initial = await client._request("GET", config.base_url)
                body = await client._request("POST", config.base_url, data={"x": "1"})

        assert b"__VIEWSTATE" in initial
        assert body == _result_page("柴油車")
        # 兩次 503 後第三次成功：共 3 次 POST。
        assert len(attempts) == 3

    asyncio.run(scenario())


def test_request_gives_up_after_max_attempts() -> None:
    attempts: list[int] = []

    async def handler(request: web.Request) -> web.Response:
        if request.method == "POST":
            attempts.append(1)
            return web.Response(status=500, text="broken")
        return web.Response(body=INITIAL_PAGE.encode(), content_type="text/html")

    async def scenario() -> None:
        async with fake_site(handler) as base_url:
            config = _config(base_url)
            async with _client(config) as client:
                client.retry_backoff_seconds = 0.0
                with pytest.raises(VncsClientError, match="after 3 attempts"):
                    await client._request("POST", config.base_url, data={"x": "1"})

        assert len(attempts) == 3

    asyncio.run(scenario())


def test_config_rejects_offsite_base_url_and_slow_violations() -> None:
    with pytest.raises(ValueError, match="base URL"):
        VncsConfig.from_env(base_url="https://example.com/VNCSEXLRPT.aspx")
    with pytest.raises(ValueError, match="rate limit"):
        VncsConfig.from_env(rate_limit_seconds=0.5)


def test_config_from_env_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VNCS_RATE_LIMIT_SECONDS", "2")
    monkeypatch.setenv("VNCS_REQUEST_TIMEOUT_SECONDS", "30")

    config = VncsConfig.from_env()

    assert config.rate_limit_seconds == 2.0
    assert config.request_timeout_seconds == 30.0
    assert config.public_dict()["rate_limit_seconds"] == 2.0


def test_config_rejects_missing_tls_ca_bundle(tmp_path) -> None:
    with pytest.raises(ValueError, match="TLS CA bundle"):
        VncsConfig.from_env(tls_ca_bundle=str(tmp_path / "missing.pem"))

    # 預設錨定 repo 內 TWCA 中繼憑證，必須存在且可解析。
    config = VncsConfig.from_env()
    assert Path(config.tls_ca_bundle).is_file()
