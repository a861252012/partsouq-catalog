from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from partsouq_station_admin.db import ExecutionResult, SqlParams
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import ENTITY_SPECS


@dataclass(frozen=True)
class SqlCall:
    tag: str
    sql: str
    params: SqlParams


class ScriptedDatabase:
    def __init__(
        self,
        trace: QueryTrace,
        *,
        dataset_size: int = 1,
        event_count: int = 0,
        provenance_count: int = 0,
    ) -> None:
        self.trace = trace
        self.dataset_size = dataset_size
        self.event_count = event_count
        self.provenance_count = provenance_count
        self.calls: list[SqlCall] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True

    @contextmanager
    def transaction(self) -> Iterator[ScriptedDatabase]:
        yield self

    def fetch_one(
        self,
        tag: str,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
    ) -> dict[str, Any] | None:
        self._record(tag, sql, params)
        if tag.startswith("detail.base.") or tag.startswith("write.lock-base."):
            entity = tag.rsplit(".", 1)[-1]
            source_id = int(params[0]) if isinstance(params, Sequence) and params else 0
            if source_id < 1:
                return None
            return source_row(entity, source_id)
        if tag.startswith("detail.head.") or tag.startswith("write.lock-head."):
            return None
        if tag == "dashboard.source-counts":
            return {key: self.dataset_size for key in ENTITY_SPECS}
        if tag == "dashboard.quarantine-summary":
            return {"total": 3, "unresolved": 1}
        if tag == "quarantine.count":
            return {"total": self.dataset_size}
        if tag == "quarantine.lock-row":
            return {"id": int(params[0])} if isinstance(params, Sequence) and params else None
        if tag == "dashboard.system-data-summary":
            return {
                "partsouq_normalized_rows": 1000,
                "partsouq_distinct_part_numbers": 923,
                "partsouq_published_rows": 0,
                "partsouq_current_scope": "bounded",
                "partsouq_current_rows": 10000,
                "partsouq_current_distinct_part_numbers": 9234,
                "partsouq_bounded_rows": 10000,
                "partsouq_bounded_distinct_part_numbers": 9234,
                "bounded_crawl_run_id": 42,
                "bounded_target_parts": 10000,
                "bounded_status": "bounded_success",
                "bounded_scheduled_job_run_id": 77,
                "bounded_scheduler_trigger_mode": "daemon",
                "bounded_scheduler_status": "completed",
                "bounded_scheduler_exit_code": 0,
                "bounded_scheduler_linked_crawl_runs": 1,
                "bounded_non_live_data_marker": 0,
                "bounded_active_override_rows": 0,
                "nhtsa_current_records": 137120,
                "nhtsa_vin_decodes": 0,
            }
        if tag.startswith("list.count."):
            return {"total": self.dataset_size}
        return None

    def fetch_all(
        self,
        tag: str,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        self._record(tag, sql, params)
        if tag.startswith("list.keys."):
            assert isinstance(params, Sequence)
            requested = int(params[-2])
            offset = int(params[-1])
            count = min(max(self.dataset_size - offset, 0), requested)
            return [
                {"kind_order": 0, "sort_id": source_id}
                for source_id in range(
                    self.dataset_size - offset,
                    self.dataset_size - offset - count,
                    -1,
                )
            ]
        if tag.startswith("list.source-batch."):
            assert isinstance(params, Sequence)
            entity = tag.rsplit(".", 1)[-1]
            return [source_row(entity, int(value)) for value in params[1:] if int(value) > 0]
        if tag.startswith("list.manual-batch."):
            return []
        if tag.startswith("detail.events."):
            return [
                {
                    "id": index,
                    "action": "update",
                    "revision": index,
                    "base_sha256": "a" * 64,
                    "before_json": "{}",
                    "after_json": "{}",
                    "actor": "tester",
                    "reason": "test",
                    "created_at": "2026-08-10 00:00:00",
                }
                for index in range(self.event_count)
            ]
        if tag.startswith("detail.provenance."):
            return [
                {
                    "id": index,
                    "parser_name": "fixture",
                    "parser_version": "1",
                    "source_url": "https://example.test/catalog?ssd=opaque-secret",
                    "extracted_at": "2026-08-10 00:00:00",
                    "response_id": index,
                    "http_status": 200,
                    "body_sha256": "b" * 64,
                    "fetched_at": "2026-08-10 00:00:00",
                    "archive_source": None,
                    "collection_name": None,
                    "captured_at": None,
                }
                for index in range(self.provenance_count)
            ]
        if tag == "dashboard.override-counts":
            return []
        if tag == "quarantine.list":
            return [
                {
                    "id": row_id,
                    "part_number": f"IMG{row_id:05d}",
                    "range_str": None,
                    "reason": "no name on site",
                    "code": None,
                    "quantity": 1,
                    "note": None,
                    "run_key": "bounded-1",
                    "resolved_at": None if row_id % 2 else "2098-12-31 17:00:00",
                    "resolution": None if row_id % 2 else "verified",
                    "updated_at": "2098-12-31 17:00:00",
                    "group_code": "GC-1",
                    "uid": "U-1",
                }
                for row_id in (1, 2, 3)
            ]
        if tag == "monitor.scheduled-job-runs":
            return [
                {
                    "id": 1,
                    "job_name": "nhtsa-bulk",
                    "trigger_mode": "daemon",
                    "status": "completed",
                    "started_at": "2098-12-31 17:00:00",
                    "finished_at": "2098-12-31 17:01:00",
                    "exit_code": 0,
                    "output_text": "ok",
                }
            ]
        if tag == "monitor.crawl-runs":
            return [
                {
                    "id": 2,
                    "run_key": "monthly-2099-01-partsouq",
                    "status": "running",
                    "dataset_kind": "bounded",
                    "target_parts": 10000,
                    "scheduled_job_run_id": 77,
                    "started_at": "2098-12-31 17:00:00",
                    "finished_at": None,
                    "brands_ok": 1,
                    "models_ok": 1,
                    "vehicles_ok": 1,
                    "groups_ok": 47,
                    "parts_ok": 1000,
                    "parts_new": 1000,
                    "error_msg": None,
                }
            ]
        if tag == "monitor.admin-crawl-requests":
            return [
                {
                    "id": 3,
                    "job_name": "nhtsa-vin",
                    "requested_scope": "TES**********0000",
                    "status": "pending",
                    "requested_by": "tester",
                    "requested_at": "2098-12-31 17:00:00",
                    "started_at": None,
                    "finished_at": None,
                    "error_message": None,
                }
            ]
        return []

    def execute(
        self,
        tag: str,
        sql: str,
        params: Sequence[object] | Mapping[str, object] | None = None,
    ) -> ExecutionResult:
        self._record(tag, sql, params)
        return ExecutionResult(lastrowid=77, rowcount=1)

    def _record(self, tag: str, sql: str, params: SqlParams) -> None:
        self.calls.append(SqlCall(tag, sql, params))
        self.trace.record(tag=tag, sql=sql, elapsed_ms=0.01, row_count=0)


def source_row(entity_type: str, source_id: int) -> dict[str, Any]:
    spec = ENTITY_SPECS[entity_type]
    row: dict[str, Any] = {"id": source_id}
    row.update({field: None for field in spec.source_fields})
    if entity_type == "part_numbers":
        row.update(
            {
                "number_raw": f"P-{source_id}",
                "number_normalized": f"P{source_id}",
                "name_en_raw": "Fixture part",
                "is_assembly_inferred": 0,
                "source_url": "https://example.test/catalog?ssd=opaque-secret",
            }
        )
    return row
