from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import web

from partsouq_crawler.vncs.client import VncsClient
from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.parser import RESULT_HEADERS
from partsouq_crawler.vncs.repository import VncsMySQLRepository
from partsouq_crawler.vncs.service import VncsSyncService

from ..helpers import fake_site

pytestmark = pytest.mark.skipif(
    os.getenv("NHTSA_TEST_MYSQL") != "1",
    reason="set NHTSA_TEST_MYSQL=1 to run MySQL integration tests",
)

VIN_A = "KNAPX81BDV7443274"
VIN_B = "LZZTEST00X0000999"
ENGINE_CODE = "12345678"

GASOLINE_ROWS: list[list[str]] = [
    [
        "汽油車",
        "TOYOTA COROLLA ALTIS 1800 4D 自排",
        "2024",
        "T2-A24",
        VIN_A,
        "六期",
        "113/05/22",
        "A1",
    ],
    ["汽油車", "HONDA FIT 1500 5D CVT", "2025", "H3-F25", VIN_B, "七期", "114/02/07", "D4"],
]

DIESEL_ROWS: list[list[str]] = [
    [
        "柴油車",
        "CMC VERYCA 1200 2D 手排",
        "2023",
        "C5-D23",
        ENGINE_CODE,
        "六期",
        "112/11/03",
        "B2",
    ],
]


def _initial_page() -> bytes:
    return (
        '<html><body><form method="post">'
        '<input type="hidden" name="__VIEWSTATE" value="/wEPstate==" />'
        '<input type="hidden" name="__VIEWSTATEGENERATOR" value="C2EE9ABB" />'
        '<input type="hidden" name="__EVENTVALIDATION" value="/wEdvalidation==" />'
        '<select name="dlFtrMOBTYPE"><option>汽油車</option></select>'
        '<select name="dlFtrPERIOD"><option>第一期</option></select>'
        '<select name="dlFtrTESTTYPE"><option>新車型</option></select>'
        "</form></body></html>"
    ).encode()


def _result_table(rows: list[list[str]]) -> str:
    head = "</th><th>".join(RESULT_HEADERS)
    body_rows = "".join(f"<tr><td>{'</td><td>'.join(cells)}</td></tr>" for cells in rows)
    return f"<html><body><table><tr><th>{head}</th></tr>{body_rows}</table></body></html>"


def _site_handler(pages: dict[str, str | int]) -> Any:
    async def handler(request: web.Request) -> web.Response:
        if request.method == "GET":
            return web.Response(body=_initial_page(), content_type="text/html")
        form = await request.post()
        payload = pages[str(form.get("dlFtrMOBTYPE"))]
        if isinstance(payload, int):
            return web.Response(status=payload, text="server error")
        return web.Response(body=payload.encode(), content_type="text/html")

    return handler


class _FastVncsClient(VncsClient):
    """關閉節流與 retry backoff，讓整合測試快速且確定。"""

    def __init__(self, config: VncsConfig) -> None:
        super().__init__(config)
        self.rate_limiter.delay_seconds = 0.0
        self.retry_backoff_seconds = 0.0


def _config(base_url: str) -> VncsConfig:
    # replace() 繞過 validate() 的正式站台 allowlist，僅用於 fake_site 測試。
    config = replace(
        VncsConfig.from_env(user_agent="vncs-test/1.0", request_timeout_seconds=10),
        base_url=f"{base_url}/VNCSEXLRPT.aspx",
    )
    if not config.mysql_database.endswith("_test"):
        raise ValueError("NHTSA_TEST_MYSQL requires a database name ending in _test")
    return config


def test_sync_end_to_end_is_idempotent_for_vin_and_appends_engine_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "汽油車": _result_table(GASOLINE_ROWS),
        "柴油車": _result_table(DIESEL_ROWS),
    }

    async def scenario() -> None:
        async with fake_site(_site_handler(pages)) as base_url:
            config = _config(base_url)
            monkeypatch.setattr(
                "partsouq_crawler.vncs.service.VncsClient",
                _FastVncsClient,
            )
            repository = VncsMySQLRepository.create(config)
            try:
                repository.ensure_schema()
                repository.clear_for_tests()
                service = VncsSyncService(repository, config)
                first = await service.run(run_key="vncs-fixture")

                assert first["status"] == "completed"
                assert first["rows_seen"] == 3
                assert first["gasoline_rows"] == 2
                assert first["diesel_rows"] == 1
                assert first["malformed_rows"] == 0
                with repository.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT body_or_engine_code, is_vin, vehicle_kind "
                        "FROM tw_vncs_vehicles ORDER BY id"
                    )
                    rows = cursor.fetchall()
                assert [row["body_or_engine_code"] for row in rows] == [
                    VIN_A,
                    VIN_B,
                    ENGINE_CODE,
                ]
                assert [bool(row["is_vin"]) for row in rows] == [True, True, False]
                assert [row["vehicle_kind"] for row in rows] == [
                    "汽油車",
                    "汽油車",
                    "柴油車",
                ]

                # 同一批資料再跑一輪：VIN 走 uq_vncs_vin 條件唯一鍵 upsert 不重複；
                # 非 VIN 引擎碼不參與唯一、可多筆。
                second = await service.run(run_key="vncs-fixture-second")
                assert second["status"] == "completed"
                with repository.connection.cursor() as cursor:
                    cursor.execute("SELECT body_or_engine_code FROM tw_vncs_vehicles ORDER BY id")
                    codes = [row["body_or_engine_code"] for row in cursor.fetchall()]
                    cursor.execute("SELECT status FROM vncs_sync_runs ORDER BY id")
                    run_statuses = [str(row["status"]) for row in cursor.fetchall()]
                assert codes.count(VIN_A) == 1
                assert codes.count(VIN_B) == 1
                assert codes.count(ENGINE_CODE) == 2
                assert run_statuses == ["completed", "completed"]
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


def test_http_failure_fails_closed_and_records_run(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: dict[str, str | int] = {"汽油車": 500, "柴油車": _result_table(DIESEL_ROWS)}

    async def scenario() -> None:
        async with fake_site(_site_handler(pages)) as base_url:
            config = _config(base_url)
            monkeypatch.setattr("partsouq_crawler.vncs.service.VncsClient", _FastVncsClient)
            repository = VncsMySQLRepository.create(config)
            try:
                repository.ensure_schema()
                repository.clear_for_tests()
                report = await VncsSyncService(repository, config).run(run_key="vncs-http-fail")

                assert report["status"] == "failed"
                assert "500" in str(report["error"])
                with repository.connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) AS row_count FROM tw_vncs_vehicles")
                    assert cursor.fetchone()["row_count"] == 0
                    cursor.execute("SELECT status, error_message FROM vncs_sync_runs")
                    run_row = cursor.fetchone()
                assert run_row is not None and str(run_row["status"]) == "failed"
                assert run_row["error_message"] is not None
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


def test_mass_malformed_rows_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    malformed_rows: list[list[str]] = [
        ["汽油車", f"BROKEN MODEL {index}", "", f"X{index}", "", "", "", ""] for index in range(5)
    ]
    pages: dict[str, str | int] = {
        "汽油車": _result_table(malformed_rows),
        "柴油車": _result_table([]),
    }

    async def scenario() -> None:
        async with fake_site(_site_handler(pages)) as base_url:
            config = _config(base_url)
            monkeypatch.setattr("partsouq_crawler.vncs.service.VncsClient", _FastVncsClient)
            repository = VncsMySQLRepository.create(config)
            try:
                repository.ensure_schema()
                repository.clear_for_tests()
                report = await VncsSyncService(repository, config).run(run_key="vncs-malformed")

                assert report["status"] == "failed"
                assert "rejected" in str(report["error"])
                with repository.connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) AS row_count FROM tw_vncs_vehicles")
                    assert cursor.fetchone()["row_count"] == 0
                    cursor.execute("SELECT status, malformed_rows FROM vncs_sync_runs")
                    run_row = cursor.fetchone()
                assert run_row is not None and str(run_row["status"]) == "failed"
                assert int(str(run_row["malformed_rows"])) >= 4
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())
