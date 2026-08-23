from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from typing import Any

import pytest

from partsouq_crawler.vncs.browser import (
    KIND_OPTION_VALUES,
    VncsBrowserError,
    VncsBrowserHarvester,
)
from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.models import ParsedRecord, VncsRunHandle
from partsouq_crawler.vncs.parser import parse_grid_records
from partsouq_crawler.vncs.service import UPSERT_BATCH_SIZE, VncsSyncService

VIN_A = "KNAPX81BDV7443274"
VIN_B = "JN1DT3MW9VW100002"
ENGINE_CODE = "R1152PH00142"


def _grid_row(code: str, name: str = "TOYOTA COROLLA ALTIS 1800 4D") -> dict[str, str]:
    return {
        "車輛種類": "汽油車",
        "車型名稱": name,
        "車型年份": "2027",
        "受測轉速(rpm)": "3750",
        "使用中原地噪音管制值": "93",
        "車型組代號": "T2-A24",
        "車身碼或引擎碼": code,
        "噪音測值原地dB(A)": "73",
        "噪音測值加速dB(A)": "68",
        "最大馬力轉速(rpm)": "5500",
        "核准日期": "2026/04/14",
        "查核碼": "",
        "期別": "六期",
        "原地檢測模式": "",
    }


class FakePage:
    """以同步狀態機模擬 Playwright Page：evaluate/content 序列驅動 harvest 迴圈。"""

    def __init__(
        self,
        *,
        rows_by_page: list[list[dict[str, str]]],
        total_pages: int,
    ) -> None:
        self.rows_by_page = rows_by_page
        self.total_pages = total_pages
        self.index = 0
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    def set_default_timeout(self, ms: float) -> None:
        self.calls.append(("set_default_timeout", ms))

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self.calls.append(("goto", url, wait_until))

    async def select_option(self, selector: str, value: str) -> None:
        self.calls.append(("select_option", selector, value))

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        if "__vncsFindPaging = findPaging;" in script:
            self.calls.append(("install", None))
            return True
        if "(pageIndexZeroBased)" in script:
            assert isinstance(arg, int)
            self.index = arg
            self.calls.append(("set_page_index", arg))
            return True
        if "state.pageCount" in script:
            self.calls.append(("paging_state", None))
            return {"pageIndex": self.index, "pageCount": self.total_pages}
        if "() => (window.__vncsGridRows" in script:
            self.calls.append(("grid_rows", self.index))
            if self.index < len(self.rows_by_page):
                return self.rows_by_page[self.index]
            return []
        if "() => (window.__vncsGridSignature" in script:
            self.calls.append(("signature", self.index))
            return f"sig-{self.index}"
        if "ubnDoFilter" in script:
            self.calls.append(("click_filter", None))
            return True
        raise AssertionError(f"unexpected script: {script[:120]!r}")

    async def content(self) -> str:
        self.calls.append(("content", self.index))
        return f"<html><div id='wdgMain'></div>fake-page-{self.index}</html>"

    async def wait_for_function(self, script: str, arg: Any = None) -> None:
        target, _previous_signature = arg
        # FakePage 的 evaluate 已同步推進 pageIndex，因此這裡直接斷言一致。
        assert target == self.index
        self.calls.append(("wait_page", target))

    @contextlib.asynccontextmanager
    async def expect_navigation(self, wait_until: str | None = None):  # type: ignore[no-untyped-def]
        self.calls.append(("expect_navigation", wait_until))
        yield

    async def close(self) -> None:
        self.closed = True


class _SinglePageContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def new_page(self) -> FakePage:
        return self._page


class FakeSessionHarvester(VncsBrowserHarvester):
    """共用同一個 FakePage 的 harvester（不啟動 Chromium）。"""

    def __init__(self, config: VncsConfig, page: FakePage) -> None:
        super().__init__(config)
        self._page = page

    async def __aenter__(self) -> FakeSessionHarvester:
        self.inter_page_delay = 0.0
        self.context = _SinglePageContext(self._page)  # type: ignore[assignment]
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.context = None


def _config(**overrides: object) -> VncsConfig:
    return replace(
        VncsConfig.from_env(),
        base_url="https://vncs.moenv.gov.tw/VNCSEXLRPT.aspx",
        **overrides,  # type: ignore[arg-type]
    )


def _harvester(config: VncsConfig, page: FakePage) -> FakeSessionHarvester:
    return FakeSessionHarvester(config, page)


def test_harvest_pages_until_last_page_and_reports_totals() -> None:
    page = FakePage(
        rows_by_page=[
            [_grid_row(VIN_A), _grid_row(VIN_B)],
            [_grid_row(ENGINE_CODE)],
            [],
        ],
        total_pages=3,
    )
    batches: list[list[dict[str, object]]] = []

    async def scenario() -> dict[str, Any]:
        async with _harvester(_config(), page) as harvester:
            return await harvester.harvest(
                "G",
                max_pages=None,
                page_timeout_s=5,
                on_rows=batches.append,
            )

    report = asyncio.run(scenario())

    assert report["kind"] == "G"
    assert report["pages_done"] == 3
    assert report["total_pages"] == 3
    assert report["last_page"] == 3
    assert report["rows_seen"] == 3
    assert report["malformed_rows"] == 0
    assert [len(batch) for batch in batches] == [2, 1, 0]
    kinds = [call[0] for call in page.calls]
    assert kinds.count("set_page_index") == 2
    set_targets = [call[1] for call in page.calls if call[0] == "set_page_index"]
    assert set_targets == [1, 2]
    assert ("select_option", "#dlFtrMOBTYPE", "G") in [
        (call[0], call[1], call[2]) for call in page.calls if call[0] == "select_option"
    ]
    assert page.closed


def test_harvest_respects_max_pages() -> None:
    page = FakePage(rows_by_page=[[_grid_row(VIN_A)] for _ in range(5)], total_pages=5)

    async def scenario() -> dict[str, Any]:
        async with _harvester(_config(), page) as harvester:
            return await harvester.harvest(
                "G", max_pages=2, page_timeout_s=5, on_rows=lambda rows: None
            )

    report = asyncio.run(scenario())

    assert report["pages_done"] == 2
    assert report["last_page"] == 2
    set_targets = [call[1] for call in page.calls if call[0] == "set_page_index"]
    assert set_targets == [1]


def test_harvest_start_page_jumps_then_continues_to_end() -> None:
    page = FakePage(
        rows_by_page=[[_grid_row(f"{i}") for i in range(2)] for _ in range(4)], total_pages=4
    )

    async def scenario() -> dict[str, Any]:
        async with _harvester(_config(), page) as harvester:
            return await harvester.harvest(
                "G", start_page=3, max_pages=None, page_timeout_s=5, on_rows=lambda rows: None
            )

    report = asyncio.run(scenario())

    assert report["start_page"] == 3
    assert report["pages_done"] == 2
    assert report["last_page"] == 4
    set_targets = [call[1] for call in page.calls if call[0] == "set_page_index"]
    assert set_targets == [2, 3]
    assert page.calls[page.calls.index(("set_page_index", 2)) - 1][0] == "signature"


def test_harvest_rejects_start_page_beyond_last_page() -> None:
    page = FakePage(rows_by_page=[[_grid_row(VIN_A)]], total_pages=1)

    async def scenario() -> None:
        async with _harvester(_config(), page) as harvester:
            await harvester.harvest(
                "G", start_page=2, max_pages=None, page_timeout_s=5, on_rows=lambda rows: None
            )

    with pytest.raises(VncsBrowserError, match="beyond the last page"):
        asyncio.run(scenario())


def test_harvest_validates_kind_option_value() -> None:
    page = FakePage(rows_by_page=[], total_pages=1)

    async def scenario() -> None:
        async with _harvester(_config(), page) as harvester:
            await harvester.harvest(
                "M", max_pages=None, page_timeout_s=5, on_rows=lambda rows: None
            )

    with pytest.raises(ValueError, match="unsupported VNCS vehicle kind"):
        asyncio.run(scenario())


def test_kind_option_values_are_gd_only() -> None:
    assert frozenset({"G", "D"}) == KIND_OPTION_VALUES


# ---------------------------------------------------------------------------
# service 層：fake harvester 注入、批次 upsert、fail-closed 契約
# ---------------------------------------------------------------------------


class FakeRepository:
    def __init__(self) -> None:
        self.upsert_batches: list[list[ParsedRecord]] = []
        self.finished: list[dict[str, Any]] = []
        self.started = 0

    def start_run(self, run_key: str, *, scheduled_job_run_id: int | None = None) -> VncsRunHandle:
        self.started += 1
        return VncsRunHandle(id=self.started)

    def upsert_vehicles(self, records: list[ParsedRecord]) -> int:
        self.upsert_batches.append(list(records))
        return len(records)

    def finish_run(
        self,
        handle: VncsRunHandle,
        *,
        status: str,
        rows_seen: int,
        rows_upserted: int,
        malformed_rows: int,
        error_message: str | None = None,
    ) -> None:
        self.finished.append(
            {
                "status": status,
                "rows_seen": rows_seen,
                "rows_upserted": rows_upserted,
                "malformed_rows": malformed_rows,
                "error_message": error_message,
            }
        )


class ScriptedHarvester:
    """依腳本逐頁回呼記錄的 fake harvester；可指定失敗點與 malformed 數。"""

    def __init__(
        self,
        pages_by_kind: dict[str, list[list[dict[str, str]]]],
        *,
        malformed_by_kind: dict[str, int] | None = None,
        raise_on: str | None = None,
    ) -> None:
        self.pages_by_kind = pages_by_kind
        self.malformed_by_kind = malformed_by_kind or {}
        self.raise_on = raise_on
        self.seen_kinds: list[str] = []
        self.start_pages: dict[str, int] = {}

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
        if self.raise_on == kind:
            raise RuntimeError("boom")
        self.seen_kinds.append(kind)
        self.start_pages[kind] = start_page
        pages = self.pages_by_kind.get(kind, [])
        rows_seen = 0
        for page_rows in pages:
            records, _malformed = parse_grid_records(page_rows)
            on_rows(records)
            rows_seen += len(records)
        return {
            "kind": kind,
            "pages_done": len(pages),
            "rows_seen": rows_seen,
            "malformed_rows": self.malformed_by_kind.get(kind, 0),
            "total_pages": max(len(pages), start_page),
            "last_page": len(pages),
            "start_page": start_page,
        }


def _service(repository: FakeRepository, harvester: ScriptedHarvester) -> VncsSyncService:
    return VncsSyncService(repository, _config(), harvester_factory=lambda cfg: harvester)


def test_run_aggregates_kinds_and_batches_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("partsouq_crawler.vncs.service.UPSERT_BATCH_SIZE", 3)
    harvester = ScriptedHarvester(
        {
            "G": [[_grid_row(VIN_A), _grid_row(VIN_B)], [_grid_row(ENGINE_CODE)]],
            "D": [[]],
        }
    )
    repository = FakeRepository()

    report = asyncio.run(_service(repository, harvester).run(run_key="unit-run"))

    assert report["status"] == "completed"
    assert report["rows_seen"] == 3
    assert report["gasoline_rows"] == 3
    assert report["diesel_rows"] == 0
    assert report["gasoline_pages"] == 2
    assert report["diesel_pages"] == 1
    assert harvester.seen_kinds == ["G", "D"]
    assert harvester.start_pages == {"G": 1, "D": 1}
    assert [len(batch) for batch in repository.upsert_batches] == [3]
    assert report["rows_upserted"] == 3
    assert repository.finished[0]["status"] == "completed"


def test_run_passes_resume_start_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("partsouq_crawler.vncs.service.UPSERT_BATCH_SIZE", UPSERT_BATCH_SIZE)
    harvester = ScriptedHarvester({"G": [[_grid_row(VIN_A)]], "D": [[_grid_row(VIN_B)]]})
    repository = FakeRepository()

    report = asyncio.run(
        _service(repository, harvester).run(run_key="resume", start_pages={"G": 41})
    )

    assert report["status"] == "completed"
    assert harvester.start_pages == {"G": 41, "D": 1}


def test_run_fails_closed_when_harvester_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("partsouq_crawler.vncs.service.UPSERT_BATCH_SIZE", UPSERT_BATCH_SIZE)
    harvester = ScriptedHarvester({"G": [], "D": []}, raise_on="G")
    repository = FakeRepository()

    report = asyncio.run(_service(repository, harvester).run(run_key="boom"))

    assert report["status"] == "failed"
    assert report["error_type"] == "RuntimeError"
    assert "boom" in str(report["error"])
    assert repository.finished[0]["status"] == "failed"


def test_run_fails_closed_on_malformed_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("partsouq_crawler.vncs.service.UPSERT_BATCH_SIZE", UPSERT_BATCH_SIZE)
    broken = dict(_grid_row(VIN_A))
    broken["車型名稱"] = ""
    harvester = ScriptedHarvester(
        {
            "G": [[broken, broken, broken]],
            "D": [[]],
        },
        malformed_by_kind={"G": 3},
    )
    repository = FakeRepository()

    report = asyncio.run(_service(repository, harvester).run(run_key="malformed"))

    assert report["status"] == "failed"
    assert "rejected" in str(report["error"])
