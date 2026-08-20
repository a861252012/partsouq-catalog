from __future__ import annotations

import json
from pathlib import Path

import pytest

from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import (
    ENTITY_SPECS,
    FIELD_LABELS,
    PAGE_SIZES,
    AdminDataError,
    AdminRepository,
    RevisionConflictError,
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
    assert PAGE_SIZES == (10, 25, 30, 50, 100, 200)
    assert FIELD_LABELS["source_part_code"] == "零件表 Code／圖號呼叫碼"
    assert FIELD_LABELS["part_brand_raw"] == "適用車輛品牌（非零件品牌）"


@pytest.mark.parametrize("entity_type", ENTITY_SPECS)
def test_each_entity_can_be_browsed(entity_type: str) -> None:
    trace = QueryTrace()

    page = AdminRepository(ScriptedDatabase(trace)).list_records(entity_type, limit=10)

    assert len(page.records) == 1
    assert page.page_size == 10
    assert page.records[0].identity_key == "source:1"


def test_part_lists_default_to_formal_and_keep_explicit_sample_history() -> None:
    formal_database = ScriptedDatabase(QueryTrace())
    historical_database = ScriptedDatabase(QueryTrace())

    AdminRepository(formal_database).list_records("part_numbers", limit=10)
    AdminRepository(historical_database).list_records(
        "part_numbers", source_scope="historical_sample", limit=10
    )

    formal_sql = "\n".join(call.sql for call in formal_database.calls)
    historical_sql = "\n".join(call.sql for call in historical_database.calls)
    assert "station_admin_formal_part_numbers" in formal_sql
    assert "station_admin_historical_sample_part_numbers" in historical_sql
    assert "station_admin_historical_sample_part_numbers" not in formal_sql


@pytest.mark.parametrize(
    ("entity_type", "formal_view"),
    (
        ("vehicle_configurations", "station_admin_formal_vehicle_configurations"),
        ("taxonomy_nodes", "station_admin_formal_taxonomy_nodes"),
        ("diagrams", "station_admin_formal_diagrams"),
        ("part_numbers", "station_admin_formal_part_numbers"),
        ("part_occurrences", "station_admin_formal_part_occurrences"),
        ("fitments", "station_admin_formal_fitments"),
    ),
)
def test_catalog_lists_default_to_shared_current_catalog(
    entity_type: str,
    formal_view: str,
) -> None:
    database = ScriptedDatabase(QueryTrace())

    AdminRepository(database).list_records(entity_type, limit=10)

    assert formal_view in "\n".join(call.sql for call in database.calls)


def test_non_part_entity_rejects_historical_sample_scope() -> None:
    with pytest.raises(AdminDataError, match="沒有歷史 sample"):
        AdminRepository(ScriptedDatabase(QueryTrace())).list_records(
            "vehicle_configurations", source_scope="historical_sample"
        )


def test_source_update_only_writes_overlay_and_append_only_event() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)
    repository = AdminRepository(database)
    base_sha256 = repository.get_record("part_numbers", "source:1").record.base_sha256

    revision = repository.update_record(
        "part_numbers",
        "source:1",
        {
            "part_brand_raw": None,
            "number_raw": "P-1",
            "name_en_raw": "人工校正名稱",
            "is_assembly_inferred": False,
            "assembly_inference_reason": None,
        },
        expected_revision=0,
        expected_base_sha256=base_sha256,
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
    insert_head = next(
        call for call in database.calls if call.tag == "write.insert-head.part_numbers"
    )
    assert json.loads(str(insert_head.params[3])) == {"name_en_raw": "人工校正名稱"}
    lock_source = next(
        call for call in database.calls if call.tag == "write.lock-source.part_numbers"
    )
    assert lock_source.sql.rstrip().lower().endswith("for share")
    assert "from parts as p" in lock_source.sql.lower()
    lock_base = next(call for call in database.calls if call.tag == "write.lock-base.part_numbers")
    assert "for share" not in lock_base.sql.lower()


def test_part_number_search_normalizes_spaces_and_hyphens() -> None:
    database = ScriptedDatabase(QueryTrace())

    AdminRepository(database).list_records("part_numbers", query="P-1", limit=10)

    count = next(call for call in database.calls if call.tag == "list.count.part_numbers")
    keys = next(call for call in database.calls if call.tag == "list.keys.part_numbers")
    assert tuple(count.params[:3]) == ("P-1%", "P1%", "P-1%")
    assert tuple(keys.params[:3]) == ("P-1%", "P1%", "P-1%")


def test_part_number_search_does_not_turn_only_separators_into_wildcard() -> None:
    database = ScriptedDatabase(QueryTrace())

    AdminRepository(database).list_records("part_numbers", query="---", limit=10)

    count = next(call for call in database.calls if call.tag == "list.count.part_numbers")
    keys = next(call for call in database.calls if call.tag == "list.keys.part_numbers")
    assert tuple(count.params[:3]) == ("---%", "---%", "---%")
    assert tuple(keys.params[:3]) == ("---%", "---%", "---%")


def test_dashboard_counts_nhtsa_rows_from_current_artifact_metadata() -> None:
    database = ScriptedDatabase(QueryTrace())

    summary = AdminRepository(database).system_data_summary()

    call = next(call for call in database.calls if call.tag == "dashboard.system-data-summary")
    assert summary["nhtsa_current_records"] == 137120
    assert "SUM(a.source_rows)" in call.sql
    assert "FROM nhtsa_current_records" not in call.sql
    assert "LEFT JOIN bounded_parts AS bp ON bp.crawl_run_id = r.id" in call.sql
    assert "FROM v_current_catalog_parts" in call.sql
    assert call.sql.count("FROM crawl_runs") == 2
    assert summary["partsouq_current_scope"] == "bounded"
    assert summary["partsouq_current_rows"] == 10000
    assert summary["partsouq_bounded_rows"] == 10000
    assert summary["bounded_scheduled_job_run_id"] == 77
    assert summary["bounded_scheduler_trigger_mode"] == "daemon"
    assert "MAX(jobs.trigger_mode)" in call.sql
    assert summary["bounded_scheduler_linked_crawl_runs"] == 1
    assert summary["bounded_non_live_data_marker"] == 0
    assert summary["bounded_active_override_rows"] == 0


def test_dashboard_source_counts_use_formal_part_views() -> None:
    database = ScriptedDatabase(QueryTrace())

    AdminRepository(database).dashboard_counts()

    call = next(call for call in database.calls if call.tag == "dashboard.source-counts")
    assert "station_admin_formal_part_numbers" in call.sql
    assert "station_admin_formal_part_occurrences" in call.sql
    assert "station_admin_formal_fitments" in call.sql
    assert "station_admin_formal_vehicle_configurations" in call.sql
    assert "station_admin_formal_taxonomy_nodes" in call.sql
    assert "station_admin_formal_diagrams" in call.sql


@pytest.mark.parametrize(
    "payload",
    (
        {"number_raw": "---"},
        {"name_en_raw": "   "},
        {"number_raw": "A" * 65},
        {"name_en_raw": "A" * 513},
    ),
)
def test_part_number_update_rejects_empty_or_oversized_required_fields(
    payload: dict[str, str],
) -> None:
    database = ScriptedDatabase(QueryTrace())
    repository = AdminRepository(database)
    base_sha256 = repository.get_record("part_numbers", "source:1").record.base_sha256
    with pytest.raises(AdminDataError):
        repository.update_record(
            "part_numbers",
            "source:1",
            payload,
            expected_revision=0,
            expected_base_sha256=base_sha256,
            actor="tester",
            reason="invalid fixture",
        )


def test_source_update_rejects_stale_base_snapshot() -> None:
    database = ScriptedDatabase(QueryTrace())

    with pytest.raises(RevisionConflictError, match="來源資料已更新"):
        AdminRepository(database).update_record(
            "part_numbers",
            "source:1",
            {"name_en_raw": "stale update"},
            expected_revision=0,
            expected_base_sha256="0" * 64,
            actor="tester",
            reason="stale fixture",
        )

    assert not any(call.tag.startswith("write.insert-head") for call in database.calls)


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
    assert "REGEXP_REPLACE(p.part_number, '[[:space:]-]+', '')" in schema
    assert "CREATE OR REPLACE VIEW station_admin_effective_parts" in schema
    assert "JSON_TYPE(JSON_EXTRACT(h.payload_json, '$.number_raw')) = 'NULL'" in schema
    assert "JSON_TYPE(JSON_EXTRACT(h.payload_json, '$.name_en_raw')) = 'NULL'" in schema
    assert "h.status AS override_status" in schema
    assert "FROM admin_override_heads AS h" in schema
    assert "CREATE OR REPLACE VIEW station_admin_formal_part_numbers" in schema
    assert "CREATE OR REPLACE VIEW station_admin_historical_sample_part_numbers" in schema
    assert schema.count("JOIN v_current_catalog_parts AS current_catalog") == 3
    for view_name in (
        "station_admin_formal_vehicle_configurations",
        "station_admin_formal_taxonomy_nodes",
        "station_admin_formal_diagrams",
    ):
        assert f"CREATE OR REPLACE VIEW {view_name}" in schema
    assert schema.count("FROM v_current_catalog_parts") >= 4
    assert "SELECT id FROM crawl_runs WHERE status = 'sample'" in schema
