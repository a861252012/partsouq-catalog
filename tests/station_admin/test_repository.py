from __future__ import annotations

from pathlib import Path

import pytest

from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import (
    ENTITY_SPECS,
    FIELD_LABELS,
    PAGE_SIZES,
    AdminDataError,
    AdminRepository,
)

from .fakes import ScriptedDatabase


def test_ten_entities_read_adapter_views_in_the_unified_database() -> None:
    assert tuple(ENTITY_SPECS) == (
        "vehicle_configurations",
        "taxonomy_nodes",
        "diagrams",
        "part_numbers",
        "part_occurrences",
        "fitments",
        "part_term_mappings",
        "vin_vehicle_mappings",
        "vin_part_fitments",
        "reconciliation_cases",
    )
    assert all(spec.table.startswith("station_admin_") for spec in ENTITY_SPECS.values())
    assert PAGE_SIZES == (10, 25, 50, 100, 200)
    assert FIELD_LABELS["source_part_code"] == "零件表 Code／圖號呼叫碼"


@pytest.mark.parametrize("entity_type", ENTITY_SPECS)
def test_each_entity_can_be_browsed(entity_type: str) -> None:
    trace = QueryTrace()

    page = AdminRepository(ScriptedDatabase(trace)).list_records(entity_type, limit=10)

    assert len(page.records) == 1
    assert page.page_size == 10
    assert page.records[0].identity_key == "source:1"


def test_source_update_only_writes_overlay_and_append_only_event() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)

    revision = AdminRepository(database).update_record(
        "part_numbers",
        "source:1",
        {"name_en_raw": "人工校正名稱"},
        expected_revision=0,
        actor="tester",
        reason="比對來源",
    )

    assert revision == 1
    write_sql = "\n".join(
        call.sql.lower() for call in database.calls if call.tag.startswith("write.")
    )
    assert "insert into admin_override_heads" in write_sql
    assert "insert into admin_override_events" in write_sql
    assert "update station_admin_part_numbers" not in write_sql
    assert "delete" not in write_sql
    lock_base = next(call for call in database.calls if call.tag == "write.lock-base.part_numbers")
    assert "for share" not in lock_base.sql.lower()


def test_allowlist_rejects_entity_sql_injection_before_query() -> None:
    trace = QueryTrace()

    with pytest.raises(AdminDataError):
        AdminRepository(ScriptedDatabase(trace)).list_records("part_numbers; DROP TABLE parts")

    assert trace.count == 0


def test_source_urls_are_redacted_in_list_and_detail() -> None:
    repository = AdminRepository(ScriptedDatabase(QueryTrace()))

    page = repository.list_records("part_numbers", limit=10)
    detail = repository.get_record("part_numbers", "source:1")

    assert page.records[0].payload["source_url"].endswith("ssd=%5BREDACTED%5D")
    assert detail.record.source_payload is not None
    assert detail.record.source_payload["source_url"].endswith("ssd=%5BREDACTED%5D")
    assert detail.provenance[0]["source_url"].endswith("ssd=%5BREDACTED%5D")
    assert detail.provenance[0]["source_record_id"] == 1
    assert detail.provenance[0]["response_id"] is None


def test_fitment_adapter_uses_date_intersection_and_marks_unpublished_rows() -> None:
    schema = (Path(__file__).resolve().parents[2] / "db" / "station_admin.sql").read_text(
        encoding="utf-8"
    )

    assert "GREATEST(p.part_from, v.production_from)" in schema
    assert "LEAST(p.part_to, v.production_to)" in schema
    assert "partsouq_normalized_unpublished" in schema
    assert "WHEN published.part_id IS NOT NULL THEN 1" in schema
