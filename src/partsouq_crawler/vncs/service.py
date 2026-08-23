from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Protocol, Self

from partsouq_crawler.vncs.browser import VncsBrowserHarvester
from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.models import ParsedRecord, VncsRunHandle
from partsouq_crawler.vncs.parser import record_payload
from partsouq_crawler.vncs.repository import VncsMySQLRepository

# fail-closed 品質關卡：單一車輛種類的 malformed 列超過解析列數的 10% 即失敗，
# 防止網站改版時把壞資料靜默入庫。
MALFORMED_RATIO_LIMIT = 0.1

# on_rows 每頁回呼約 10 列；累積到這個量才批次 upsert（commit）。
UPSERT_BATCH_SIZE = 100

# harvest 的 kind 參數是 dlFtrMOBTYPE 選項值（G/D），對應 parser 的中文車輛種類。
KIND_LABELS: tuple[tuple[str, str], ...] = (("G", "汽油車"), ("D", "柴油車"))


class VncsHarvesterSession(Protocol):
    """瀏覽器 harvester 的最小契約；測試以 fake 實作注入。"""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> object: ...

    async def harvest(
        self,
        kind: str,
        *,
        start_page: int,
        max_pages: int | None,
        page_timeout_s: float,
        on_rows: Callable[[list[dict[str, object]]], None],
    ) -> dict[str, Any]: ...


HarvesterFactory = Callable[[VncsConfig], VncsHarvesterSession]


class VncsSyncService:
    def __init__(
        self,
        repository: VncsMySQLRepository,
        config: VncsConfig,
        *,
        harvester_factory: HarvesterFactory | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.harvester_factory: HarvesterFactory = harvester_factory or (
            lambda cfg: VncsBrowserHarvester(cfg)
        )

    async def run(
        self,
        run_key: str,
        *,
        scheduled_job_run_id: int | None = None,
        start_pages: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """用真實瀏覽器抓汽油車＋柴油車 → parse → 批次 upsert → 完成 run ledger。

        任何 harvester 失敗或 parser 大量 malformed 都讓 run 走 'failed'，
        回傳 JSON report（不 raise），比照 NHTSA sync 的對外契約。
        斷點續傳：start_pages 以選項值（G/D）指定各 kind 起始頁。
        """
        handle = self.repository.start_run(run_key, scheduled_job_run_id=scheduled_job_run_id)
        rows_seen = 0
        rows_upserted = 0
        malformed_rows = 0
        gasoline_rows = 0
        diesel_rows = 0
        gasoline_pages = 0
        diesel_pages = 0
        try:
            pending = _UpsertBuffer(self.repository, self._to_parsed_records)
            for index, (kind_option, label) in enumerate(KIND_LABELS):
                if index > 0:
                    await asyncio.sleep(self.config.rate_limit_seconds)
                start_page = int((start_pages or {}).get(kind_option, 1))
                stats = _KindStats()
                async with self.harvester_factory(self.config) as harvester:
                    report = await harvester.harvest(
                        kind_option,
                        start_page=start_page,
                        max_pages=self.config.max_pages_per_kind,
                        page_timeout_s=self.config.browser_timeout_seconds,
                        on_rows=pending.callback_for(stats),
                    )
                pending.flush()
                stats.malformed_rows = int(report.get("malformed_rows", 0))
                _assert_parser_quality(label, stats.rows_seen, stats.malformed_rows)
                rows_seen += stats.rows_seen
                malformed_rows += stats.malformed_rows
                if kind_option == "G":
                    gasoline_rows += stats.rows_seen
                    gasoline_pages = int(report.get("pages_done", 0))
                else:
                    diesel_rows += stats.rows_seen
                    diesel_pages = int(report.get("pages_done", 0))
            rows_upserted += pending.total_upserted
            self.repository.finish_run(
                handle,
                status="completed",
                rows_seen=rows_seen,
                rows_upserted=rows_upserted,
                malformed_rows=malformed_rows,
            )
            return {
                "run_id": handle.id,
                "run_key": run_key,
                "status": "completed",
                "rows_seen": rows_seen,
                "rows_upserted": rows_upserted,
                "malformed_rows": malformed_rows,
                "gasoline_rows": gasoline_rows,
                "diesel_rows": diesel_rows,
                "gasoline_pages": gasoline_pages,
                "diesel_pages": diesel_pages,
            }
        except asyncio.CancelledError:
            self._finish_best_effort(
                handle,
                status="interrupted",
                rows_seen=rows_seen,
                rows_upserted=rows_upserted + _count_buffered(pending),
                malformed_rows=malformed_rows,
                error_message="sync interrupted",
            )
            raise
        except Exception as error:
            self._finish_best_effort(
                handle,
                status="failed",
                rows_seen=rows_seen,
                rows_upserted=rows_upserted + _count_buffered(pending),
                malformed_rows=malformed_rows,
                error_message=f"{type(error).__name__}: {error}",
            )
            return {
                "run_id": handle.id,
                "run_key": run_key,
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "rows_seen": rows_seen,
                "rows_upserted": rows_upserted + _count_buffered(pending),
                "malformed_rows": malformed_rows,
                "gasoline_rows": gasoline_rows,
                "diesel_rows": diesel_rows,
                "gasoline_pages": gasoline_pages,
                "diesel_pages": diesel_pages,
            }

    def _to_parsed_records(self, records: list[dict[str, object]]) -> list[ParsedRecord]:
        return [
            ParsedRecord(
                vehicle_kind=str(record["vehicle_kind"]),
                make=str(record["make"]),
                model_raw=str(record["model_raw"]),
                displacement_cc=_optional_int(record["displacement_cc"]),
                body_rule=_optional_str(record["body_rule"]),
                transmission=_optional_str(record["transmission"]),
                doors=_optional_int(record["doors"]),
                style=_optional_str(record["style"]),
                model_year=int(str(record["model_year"])),
                model_group_code=str(record["model_group_code"]),
                body_or_engine_code=str(record["body_or_engine_code"]),
                is_vin=bool(record["is_vin"]),
                period=_optional_str(record["period"]),
                approval_date=_optional_str(record["approval_date"]),
                check_code=_optional_str(record["check_code"]),
                source_url=self.config.base_url,
                payload_json=record_payload(record),
            )
            for record in records
        ]

    def _finish_best_effort(
        self,
        handle: VncsRunHandle,
        *,
        status: str,
        rows_seen: int,
        rows_upserted: int,
        malformed_rows: int,
        error_message: str | None,
    ) -> None:
        try:
            self.repository.finish_run(
                handle,
                status=status,
                rows_seen=rows_seen,
                rows_upserted=rows_upserted,
                malformed_rows=malformed_rows,
                error_message=error_message,
            )
        except Exception as finish_error:  # pragma: no cover - cleanup path
            print(f"vncs sync terminal cleanup failed: {finish_error}", flush=True)


class _KindStats:
    __slots__ = ("malformed_rows", "rows_seen")

    def __init__(self) -> None:
        self.rows_seen = 0
        self.malformed_rows = 0


class _UpsertBuffer:
    """on_rows 進來的列先緩衝，累積到 UPSERT_BATCH_SIZE 或收尾時批次 upsert。

    malformed 列數由 harvester 的最終 report 提供（parse 在 harvester 內進行），
    不在 callback 路徑上。
    """

    def __init__(
        self,
        repository: VncsMySQLRepository,
        to_parsed_records: Callable[[list[dict[str, object]]], list[ParsedRecord]],
    ) -> None:
        self.repository = repository
        self._to_parsed_records = to_parsed_records
        self.buffer: list[ParsedRecord] = []
        self.total_upserted = 0

    def callback_for(self, stats: _KindStats) -> Callable[[list[dict[str, object]]], None]:
        def on_rows(records: list[dict[str, object]]) -> None:
            stats.rows_seen += len(records)
            self.buffer.extend(self._to_parsed_records(records))
            while len(self.buffer) >= UPSERT_BATCH_SIZE:
                self.flush_chunk()

        return on_rows

    def flush_chunk(self) -> None:
        if not self.buffer:
            return
        chunk = self.buffer[:UPSERT_BATCH_SIZE]
        del self.buffer[:UPSERT_BATCH_SIZE]
        self.total_upserted += self.repository.upsert_vehicles(chunk)

    def flush(self) -> None:
        while self.buffer:
            self.flush_chunk()


def _count_buffered(pending: _UpsertBuffer) -> int:
    return len(pending.buffer)


def _assert_parser_quality(kind: str, record_count: int, malformed_count: int) -> None:
    allowed_malformed = int(record_count * MALFORMED_RATIO_LIMIT)
    if malformed_count > allowed_malformed:
        raise ValueError(
            f"VNCS {kind} parser rejected {malformed_count} of "
            f"{record_count} source rows (limit {allowed_malformed})"
        )


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) else None


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None
