from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import Any

import pytest

from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.parser import parse_grid_records
from partsouq_crawler.vncs.repository import VncsMySQLRepository
from partsouq_crawler.vncs.service import VncsSyncService

pytestmark = pytest.mark.skipif(
    os.getenv("NHTSA_TEST_MYSQL") != "1",
    reason="set NHTSA_TEST_MYSQL=1 to run MySQL integration tests",
)

VIN_A = "KNAPX81BDV7443274"
VIN_B = "LZZTEST00X0000999"
ENGINE_CODE = "12345678"


def _grid_row(
    kind: str, name: str, year: str, group: str, code: str, period: str
) -> dict[str, str]:
    return {
        "車輛種類": kind,
        "車型名稱": name,
        "車型年份": year,
        "受測轉速(rpm)": "",
        "使用中原地噪音管制值": "",
        "車型組代號": group,
        "車身碼或引擎碼": code,
        "噪音測值原地dB(A)": "",
        "噪音測值加速dB(A)": "",
        "最大馬力轉速(rpm)": "",
        "核准日期": "",
        "查核碼": "",
        "期別": period,
        "原地檢測模式": "",
    }


GASOLINE_ROWS: list[list[dict[str, str]]] = [
    [
        _grid_row("汽油車", "TOYOTA COROLLA ALTIS 1800 4D 自排", "2024", "T2-A24", VIN_A, "六期"),
        _grid_row("汽油車", "HONDA FIT 1500 5D CVT", "2025", "H3-F25", VIN_B, "七期"),
    ],
]

DIESEL_ROWS: list[list[dict[str, str]]] = [
    [
        _grid_row("柴油車", "CMC VERYCA 1200 2D 手排", "2023", "C5-D23", ENGINE_CODE, "六期"),
    ],
]


class ScriptedHarvester:
    """fake harvester：不開瀏覽器，逐頁把預先建好的格線列交給 service。"""

    def __init__(
        self,
        pages_by_kind: dict[str, list[list[dict[str, str]]]],
        *,
        raise_on: str | None = None,
    ) -> None:
        self.pages_by_kind = pages_by_kind
        self.raise_on = raise_on
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> ScriptedHarvester:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def harvest(
        self,
        kind: str,
        *,
        start_page: int,
        max_pages: int | None,
        page_timeout_s: float,
        on_rows: Any,
    ) -> dict[str, Any]:
        self.calls.append({"kind": kind, "start_page": start_page})
        if self.raise_on == kind:
            raise RuntimeError(f"harvester exploded on {kind}")
        pages = self.pages_by_kind.get(kind, [])
        rows_seen = 0
        malformed_total = 0
        for page_rows in pages:
            records, malformed = parse_grid_records(page_rows)
            on_rows(records)
            rows_seen += len(records)
            malformed_total += malformed
        return {
            "kind": kind,
            "pages_done": len(pages),
            "rows_seen": rows_seen,
            "malformed_rows": malformed_total,
            "total_pages": len(pages),
            "last_page": len(pages),
            "start_page": start_page,
        }


def _config() -> VncsConfig:
    config = replace(
        VncsConfig.from_env(request_timeout_seconds=10),
        base_url="https://vncs.moenv.gov.tw/VNCSEXLRPT.aspx",
    )
    if not config.mysql_database.endswith("_test"):
        raise ValueError("NHTSA_TEST_MYSQL requires a database name ending in _test")
    return config


def test_sync_end_to_end_is_idempotent_for_vin_and_appends_engine_codes() -> None:
    harvester = ScriptedHarvester({"G": GASOLINE_ROWS, "D": DIESEL_ROWS})

    async def scenario() -> None:
        config = _config()
        repository = VncsMySQLRepository.create(config)
        try:
            repository.ensure_schema()
            repository.clear_for_tests()
            service = VncsSyncService(repository, config, harvester_factory=lambda cfg: harvester)
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


def test_harvester_failure_fails_closed_and_records_run() -> None:
    harvester = ScriptedHarvester({"G": [], "D": DIESEL_ROWS}, raise_on="G")

    async def scenario() -> None:
        config = _config()
        repository = VncsMySQLRepository.create(config)
        try:
            repository.ensure_schema()
            repository.clear_for_tests()
            report = await VncsSyncService(
                repository, config, harvester_factory=lambda cfg: harvester
            ).run(run_key="vncs-harvest-fail")

            assert report["status"] == "failed"
            assert "exploded" in str(report["error"])
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


def test_mass_malformed_rows_fail_closed() -> None:
    malformed_rows = [
        _grid_row("汽油車", f"BROKEN MODEL {index}", "", f"X{index}", f"B{index}", "")
        for index in range(5)
    ]
    harvester = ScriptedHarvester(
        {
            "G": [malformed_rows],
            "D": [[]],
        }
    )

    async def scenario() -> None:
        config = _config()
        repository = VncsMySQLRepository.create(config)
        try:
            repository.ensure_schema()
            repository.clear_for_tests()
            report = await VncsSyncService(
                repository, config, harvester_factory=lambda cfg: harvester
            ).run(run_key="vncs-malformed")

            # 空名稱列在 parse_grid_records 就被計為 malformed 並略過，
            # service 的 10% 品質關卡必須讓整個 run fail-closed。
            assert report["status"] == "failed"
            assert "rejected" in str(report["error"])
            with repository.connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS row_count FROM tw_vncs_vehicles")
                assert cursor.fetchone()["row_count"] == 0
                cursor.execute("SELECT status, malformed_rows FROM vncs_sync_runs")
                run_row = cursor.fetchone()
            assert run_row is not None and str(run_row["status"]) == "failed"
        finally:
            repository.clear_for_tests()
            repository.close()

    asyncio.run(scenario())
