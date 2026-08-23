from __future__ import annotations

import asyncio
from typing import Any

from partsouq_crawler.vncs.client import VncsClient
from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.models import ParsedRecord, VncsRunHandle
from partsouq_crawler.vncs.parser import parse_vehicles, record_payload
from partsouq_crawler.vncs.repository import VncsMySQLRepository

# fail-closed 品質關卡：單一車輛種類的 malformed 列超過解析列數的 10% 即失敗，
# 防止網站改版時把壞資料靜默入庫。
MALFORMED_RATIO_LIMIT = 0.1


class VncsSyncService:
    def __init__(self, repository: VncsMySQLRepository, config: VncsConfig) -> None:
        self.repository = repository
        self.config = config

    async def run(
        self,
        run_key: str,
        *,
        scheduled_job_run_id: int | None = None,
    ) -> dict[str, Any]:
        """抓汽油車＋柴油車 → parse → upsert → 完成 run ledger。

        任何 HTTP 失敗或 parser 大量 malformed 都讓 run 走 'failed'，
        回傳 JSON report（不 raise），比照 NHTSA sync 的對外契約。
        """
        handle = self.repository.start_run(run_key, scheduled_job_run_id=scheduled_job_run_id)
        rows_seen = 0
        rows_upserted = 0
        malformed_rows = 0
        gasoline_rows = 0
        diesel_rows = 0
        try:
            async with VncsClient(self.config) as client:
                pages = await client.fetch_reports()
            for kind, html_bytes in pages:
                records, malformed = parse_vehicles(html_bytes)
                rows_seen += len(records)
                malformed_rows += malformed
                _assert_parser_quality(kind, len(records), malformed)
                upserted = self.repository.upsert_vehicles(self._to_parsed_records(records))
                rows_upserted += upserted
                if kind == "汽油車":
                    gasoline_rows += len(records)
                else:
                    diesel_rows += len(records)
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
            }
        except asyncio.CancelledError:
            self._finish_best_effort(
                handle,
                status="interrupted",
                rows_seen=rows_seen,
                rows_upserted=rows_upserted,
                malformed_rows=malformed_rows,
                error_message="sync interrupted",
            )
            raise
        except Exception as error:
            self._finish_best_effort(
                handle,
                status="failed",
                rows_seen=rows_seen,
                rows_upserted=rows_upserted,
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
                "rows_upserted": rows_upserted,
                "malformed_rows": malformed_rows,
                "gasoline_rows": gasoline_rows,
                "diesel_rows": diesel_rows,
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
