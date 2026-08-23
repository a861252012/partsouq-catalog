from __future__ import annotations

import asyncio
import ssl

import aiohttp
import certifi

from partsouq_crawler.crawl.rate_limit import HostRateLimiter
from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.parser import (
    VEHICLE_KINDS_QUERIED,
    assert_form_contract,
    parse_hidden_fields,
)


class VncsClientError(RuntimeError):
    pass


class VncsClient:
    """ASP.NET WebForms 用戶端：GET 初始頁取 hidden fields → POST 查詢。

    只查汽油車與柴油車各一輪（機車依政策排除）；HTTP 失敗以指數退避重試，
    上限 MAX_ATTEMPTS 次，仍失敗即 fail-closed。
    """

    MAX_ATTEMPTS = 3

    def __init__(self, config: VncsConfig) -> None:
        self.config = config
        self.rate_limiter = HostRateLimiter(config.rate_limit_seconds)
        # 測試可覆寫以加速；正式值跟隨 rate_limit_seconds。
        self.retry_backoff_seconds = config.rate_limit_seconds
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> VncsClient:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        self.session = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers={"User-Agent": self.config.user_agent, "Accept": "text/html"},
        )
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.session is not None:
            await self.session.close()

    async def fetch_reports(self) -> list[tuple[str, bytes]]:
        """回傳 [(車輛種類, 結果頁 HTML), ...]，固定汽油車→柴油車順序。"""
        initial_html = await self._request("GET", self.config.base_url)
        hidden_fields = parse_hidden_fields(initial_html)
        assert_form_contract(initial_html)
        reports: list[tuple[str, bytes]] = []
        for index, kind in enumerate(VEHICLE_KINDS_QUERIED):
            form_data = {
                **hidden_fields,
                "dlFtrMOBTYPE": kind,
                "dlFtrPERIOD": "",
                "dlFtrTESTTYPE": "",
            }
            html_bytes = await self._request("POST", self.config.base_url, data=form_data)
            reports.append((kind, html_bytes))
            if index < len(VEHICLE_KINDS_QUERIED) - 1:
                await self.rate_limiter.wait()
        return reports

    async def _request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
    ) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            await self.rate_limiter.wait()
            try:
                if self.session is None:
                    raise RuntimeError("VNCS client is not open")
                async with self.session.request(method, url, data=data) as response:
                    if response.status != 200:
                        raise VncsClientError(f"VNCS returned HTTP {response.status} from {url}")
                    body = await response.read()
                if not body:
                    raise VncsClientError(f"VNCS returned an empty response from {url}")
                return body
            except (aiohttp.ClientError, TimeoutError, VncsClientError) as error:
                last_error = error
                if attempt < self.MAX_ATTEMPTS:
                    await asyncio.sleep(self.retry_backoff_seconds * attempt)
        raise VncsClientError(
            f"VNCS request failed after {self.MAX_ATTEMPTS} attempts: {last_error}"
        )
