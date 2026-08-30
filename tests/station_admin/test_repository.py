from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from partsouq_station_admin.db import SqlParams
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import (
    ENTITY_SPECS,
    FIELD_LABELS,
    PAGE_SIZES,
    AdminDataError,
    AdminReadinessError,
    AdminRepository,
    RecordNotFoundError,
    RevisionConflictError,
)

from .fakes import ScriptedDatabase, source_row


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
    vin_spec = ENTITY_SPECS["vin_vehicle_mappings"]
    assert vin_spec.title == "VIN 解碼與車型確認"
    assert "mapping_status" in vin_spec.source_fields
    assert "mapping_status" in vin_spec.search_fields
    assert "mapping_status" in vin_spec.display_fields
    assert vin_spec.editable_fields == ()


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
def test_catalog_source_details_are_limited_to_the_formal_snapshot(
    entity_type: str,
    formal_view: str,
) -> None:
    database = ScriptedDatabase(QueryTrace())

    AdminRepository(database).get_record(entity_type, "source:1")

    detail = next(call for call in database.calls if call.tag == f"detail.base.{entity_type}")
    assert f"FROM {formal_view}" in detail.sql


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
    assert "FROM station_admin_formal_part_numbers" in lock_base.sql


def test_unpublished_raw_part_id_is_rejected_by_detail_and_update() -> None:
    database = ScriptedDatabase(QueryTrace())
    original_fetch_one = database.fetch_one

    def unpublished_source(
        tag: str,
        sql: str,
        params: SqlParams = None,
    ) -> dict[str, Any] | None:
        if tag in {"detail.base.part_numbers", "write.lock-base.part_numbers"}:
            assert "FROM station_admin_formal_part_numbers" in sql
            return None
        return original_fetch_one(tag, sql, params)

    database.fetch_one = unpublished_source  # type: ignore[method-assign]
    repository = AdminRepository(database)

    with pytest.raises(RecordNotFoundError, match="來源資料"):
        repository.get_record("part_numbers", "source:1")
    with pytest.raises(RecordNotFoundError, match="來源資料"):
        repository.update_record(
            "part_numbers",
            "source:1",
            {"name_en_raw": "不得寫入"},
            expected_revision=0,
            expected_base_sha256="0" * 64,
            actor="tester",
            reason="unpublished raw row",
        )

    assert not any(
        call.tag.startswith(("write.insert-head", "write.update-head", "write.append-event"))
        for call in database.calls
    )


def test_part_number_search_normalizes_spaces_and_hyphens() -> None:
    database = ScriptedDatabase(QueryTrace())

    AdminRepository(database).list_records("part_numbers", query="P-1", limit=10)

    count = next(call for call in database.calls if call.tag == "list.count.part_numbers")
    keys = next(call for call in database.calls if call.tag == "list.keys.part_numbers")
    expected_candidates = (42, "P-1%", 42, "P1%", 42, "P-1%")
    assert tuple(count.params[:6]) == expected_candidates
    assert tuple(keys.params[:6]) == expected_candidates
    assert "FROM bounded_parts FORCE INDEX (idx_bounded_part_number_normalized)" in count.sql
    assert "FROM bounded_parts FORCE INDEX (idx_bounded_part_number_normalized)" in keys.sql
    assert "JOIN station_admin_formal_part_numbers AS s" in count.sql
    assert "JOIN station_admin_formal_part_numbers AS s" in keys.sql
    assert [call.tag for call in database.calls].count("list.snapshot.part_numbers") == 1
    assert any(call.tag == "list.source-batch.part_numbers" for call in database.calls)
    assert database.transaction_modes == [True]


def test_part_number_search_does_not_turn_only_separators_into_wildcard() -> None:
    database = ScriptedDatabase(QueryTrace())

    AdminRepository(database).list_records("part_numbers", query="---", limit=10)

    count = next(call for call in database.calls if call.tag == "list.count.part_numbers")
    keys = next(call for call in database.calls if call.tag == "list.keys.part_numbers")
    expected_candidates = (42, "---%", 42, "---%", 42, "---%")
    assert tuple(count.params[:6]) == expected_candidates
    assert tuple(keys.params[:6]) == expected_candidates


def test_formal_part_search_falls_back_when_current_snapshot_is_not_bounded() -> None:
    database = ScriptedDatabase(QueryTrace())
    original_fetch_all = database.fetch_all

    def full_snapshot(
        tag: str,
        sql: str,
        params: SqlParams = None,
    ) -> list[dict[str, Any]]:
        if tag == "list.snapshot.part_numbers":
            return [{"dataset_scope": "full", "source_crawl_run_id": 99}]
        return original_fetch_all(tag, sql, params)

    database.fetch_all = full_snapshot  # type: ignore[method-assign]

    AdminRepository(database).list_records("part_numbers", query="P-1", limit=10)

    count = next(call for call in database.calls if call.tag == "list.count.part_numbers")
    keys = next(call for call in database.calls if call.tag == "list.keys.part_numbers")
    assert tuple(count.params[:3]) == ("P-1%", "P1%", "P-1%")
    assert tuple(keys.params[:3]) == ("P-1%", "P1%", "P-1%")
    assert "SELECT id FROM station_admin_formal_part_numbers" in count.sql
    assert "SELECT id FROM station_admin_formal_part_numbers" in keys.sql
    assert "FORCE INDEX (idx_bounded_part_number_normalized)" not in count.sql
    assert "FORCE INDEX (idx_bounded_part_number_normalized)" not in keys.sql


def test_dashboard_counts_nhtsa_rows_from_current_artifact_metadata() -> None:
    database = ScriptedDatabase(QueryTrace())

    summary = AdminRepository(database).system_data_summary()

    call = next(call for call in database.calls if call.tag == "dashboard.system-data-summary")
    assert summary["nhtsa_current_records"] == 137120
    assert summary["nhtsa_terminal_undecodable_vins"] == 0
    assert "SUM(a.source_rows)" in call.sql
    assert "terminal_artifact.status = 'undecodable'" in call.sql
    assert "decoded_artifact.source_key = terminal_artifact.source_key" in call.sql
    assert "decoded_vin.vin = terminal_artifact.source_key" not in call.sql
    assert "FROM nhtsa_current_records" not in call.sql
    assert "bounded_part.source_crawl_run_id = bounded_metadata.id" in call.sql
    assert "bounded_part.dataset_scope = 'bounded'" in call.sql
    assert "LEFT JOIN bounded_parts AS bounded_part" not in call.sql
    assert "FROM v_current_catalog_parts" in call.sql
    assert call.sql.count("FROM crawl_runs") == 2
    assert summary["partsouq_current_scope"] == "bounded"
    assert summary["partsouq_current_crawl_run_id"] == 42
    assert summary["partsouq_current_rows"] == 10000
    assert summary["partsouq_bounded_rows"] == 10000
    assert summary["bounded_scheduled_job_run_id"] == 77
    assert summary["bounded_scheduler_trigger_mode"] == "daemon"
    assert "scheduled_job.trigger_mode AS scheduler_trigger_mode" in call.sql
    assert "MAX(bounded_metadata.non_live_data_marker)" in call.sql
    assert "DATABASE() <> 'partsouq_catalog'" in call.sql
    assert summary["bounded_scheduler_linked_crawl_runs"] == 1
    assert summary["bounded_non_live_data_marker"] == 0
    assert summary["bounded_active_override_rows"] == 0
    assert summary["desired_bounded_scope"] == {
        "brand": "toyota",
        "model": "tacoma",
        "vehicle_year_floor": 2006,
        "updated_at": "2026-08-30 00:00:00",
    }
    assert summary["latest_bounded_run_scope"] == {
        "brand": "toyota",
        "model": "tacoma",
        "vehicle_year_floor": 2006,
    }
    assert summary["bounded_scope_matches_desired"] is True
    assert summary["bounded_scope_blocking_reason"] is None


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


def test_dashboard_summary_fails_closed_when_bounded_scope_does_not_match() -> None:
    database = ScriptedDatabase(QueryTrace())
    original_fetch_one = database.fetch_one

    def mismatched_scope(
        tag: str,
        sql: str,
        params: SqlParams = None,
    ) -> dict[str, Any] | None:
        row = original_fetch_one(tag, sql, params)
        if tag != "dashboard.system-data-summary" or row is None:
            return row
        return {
            **row,
            "partsouq_current_scope": None,
            "partsouq_current_crawl_run_id": None,
            "partsouq_current_rows": 0,
            "partsouq_current_distinct_part_numbers": 0,
            "partsouq_bounded_rows": 0,
            "partsouq_bounded_distinct_part_numbers": 0,
            "bounded_scope_model": "1000",
        }

    database.fetch_one = mismatched_scope  # type: ignore[method-assign]

    summary = AdminRepository(database).system_data_summary()

    assert summary["bounded_scope_matches_desired"] is False
    assert summary["bounded_scope_blocking_reason"] == "bounded_scope_mismatch"
    assert summary["partsouq_current_rows"] == 0
    assert summary["partsouq_bounded_rows"] == 0


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
    assert "WHEN current_catalog.part_id IS NOT NULL THEN 1" in schema
    assert "REGEXP_REPLACE(p.part_number, '[[:space:]-]+', '')" in schema
    assert "CREATE OR REPLACE VIEW station_admin_effective_parts" in schema
    assert "JSON_TYPE(JSON_EXTRACT(h.payload_json, '$.number_raw')) = 'NULL'" in schema
    assert "JSON_TYPE(JSON_EXTRACT(h.payload_json, '$.name_en_raw')) = 'NULL'" in schema
    assert "h.status AS override_status" in schema
    assert "FROM admin_override_heads AS h" in schema
    assert "CREATE OR REPLACE VIEW station_admin_formal_part_numbers" in schema
    assert "CREATE OR REPLACE VIEW station_admin_historical_sample_part_numbers" in schema
    assert "FROM v_current_catalog_parts AS current_catalog" in schema
    assert "current_catalog.part_number_normalized AS number_normalized" in schema
    for view_name in (
        "station_admin_formal_vehicle_configurations",
        "station_admin_formal_taxonomy_nodes",
        "station_admin_formal_diagrams",
    ):
        assert f"CREATE OR REPLACE VIEW {view_name}" in schema
    formal_schema = schema.split(
        "CREATE OR REPLACE VIEW station_admin_formal_vehicle_configurations", 1
    )[1].split("CREATE OR REPLACE VIEW station_admin_historical_sample_part_numbers", 1)[0]
    assert formal_schema.count("FROM v_current_catalog_parts AS current_catalog") == 7
    for mutable_table in ("parts", "groups_t", "categories", "vehicles", "models", "brands"):
        assert f"FROM {mutable_table} " not in formal_schema
        assert f"JOIN {mutable_table} " not in formal_schema
    assert "SELECT id FROM crawl_runs WHERE status = 'sample'" in schema


def test_quarantine_list_filters_unresolved_and_run_key() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)
    repository = AdminRepository(database)

    page = repository.list_quarantine(state="unresolved", run_key="bounded-1", limit=25)

    assert page["pageSize"] == 25
    assert page["total"] == 1
    assert page["totalPages"] == 1
    count_call, list_call = [call for call in database.calls if call.tag.startswith("quarantine.")]
    assert "part_quarantine.resolved_at IS NULL" in count_call.sql
    assert "part_quarantine.run_key = %s" in count_call.sql
    assert count_call.params == ("bounded-1",)
    assert "part_quarantine.resolved_at IS NULL" in list_call.sql
    assert "JOIN groups_t" in list_call.sql
    assert "FORCE INDEX (idx_quarantine_run_key_resolved_updated)" in list_call.sql
    assert "STRAIGHT_JOIN groups_t" in list_call.sql
    assert "ORDER BY part_quarantine.updated_at DESC," in list_call.sql
    assert "part_quarantine.id DESC" in list_call.sql
    assert "resolved_at IS NOT NULL" not in list_call.sql
    assert list_call.params == ("bounded-1", 25, 0)


def test_readiness_exercises_indexes_and_backoffice_schema() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)

    AdminRepository(database).check_readiness()

    assert [call.tag for call in database.calls] == [
        "health.quarantine-list",
        "health.quarantine-run-key",
        "health.backoffice-schema",
        "health.published-provenance",
    ]
    assert "FORCE INDEX (idx_quarantine_list)" in database.calls[0].sql
    assert "FORCE INDEX (idx_quarantine_run_key_resolved_updated)" in database.calls[1].sql
    assert database.calls[1].params == ("__health__",)
    readiness_sql = database.calls[2].sql
    for table in (
        "station_admin_formal_vehicle_configurations",
        "station_admin_formal_taxonomy_nodes",
        "station_admin_formal_diagrams",
        "station_admin_formal_part_numbers",
        "station_admin_formal_part_occurrences",
        "station_admin_formal_fitments",
        "station_admin_part_term_mappings",
        "station_admin_vin_vehicle_mappings",
        "station_admin_vin_part_fitments",
        "station_admin_reconciliation_cases",
        "station_admin_historical_sample_part_numbers",
        "station_admin_historical_sample_part_occurrences",
        "station_admin_historical_sample_fitments",
        "admin_override_heads",
        "admin_override_events",
        "admin_crawl_requests",
        "admin_crawl_request_audits",
        "scheduled_job_runs",
        "nhtsa_current_artifacts",
        "nhtsa_source_artifacts",
        "brands",
        "models",
        "vehicles",
        "categories",
        "groups_t",
        "parts",
        "published_parts",
        "published_parts_previous",
        "bounded_parts",
        "catalog_desired_bounded_scope",
        "crawl_runs",
        "v_current_catalog_parts",
        "part_quarantine",
        "admin_vehicle_mappings",
        "admin_part_translations",
        "admin_reconciliation_items",
        "nhtsa_vin_decodes",
    ):
        assert table in readiness_sql
    assert "LIMIT 0" in readiness_sql
    contract_sql = database.calls[3].sql
    for marker in (
        "idx_published_crawl_run",
        "fk_published_crawl_run",
        "fk_published_previous_crawl_run",
        "bounded_parts",
        "verified_bounded_evidence",
        "verified_bounded_records",
        "evidence_record_sha256",
        "partsouq_http_artifacts",
        "partsouq_artifact_records",
        "evidence_status",
        "live_http",
        "dataset_scope",
        "source_crawl_run_id",
        "catalog_desired_bounded_scope",
        "desired_scope",
        "scope_brand",
        "scope_model",
        "scope_vehicle_year_floor",
        "prevent_bounded_parts_update",
        "bounded_snapshot_immutable_ready",
    ):
        assert marker in contract_sql
    assert "LOCATE('formal_full_parts', LOWER(VIEW_DEFINITION)) = 0" in contract_sql
    assert "LOCATE('published_parts', LOWER(VIEW_DEFINITION)) = 0" in contract_sql
    assert "qualified_full_runs" not in contract_sql
    assert "full_scheduler_run" not in contract_sql


def test_readiness_rejects_incomplete_published_provenance_contract() -> None:
    database = ScriptedDatabase(QueryTrace(), readiness_contract_ready=False)

    with pytest.raises(AdminReadinessError, match="migration 033"):
        AdminRepository(database).check_readiness()


def test_quarantine_list_all_state_has_no_resolved_filter() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)

    AdminRepository(database).list_quarantine(state="all", limit=10)

    sql = "\n".join(call.sql for call in database.calls)
    assert "resolved_at IS NULL" not in sql
    assert "(part_quarantine.resolved_at IS NOT NULL)" in sql
    assert sql.index("(part_quarantine.resolved_at IS NOT NULL)") < sql.index(
        "part_quarantine.updated_at DESC"
    )
    assert "FORCE INDEX" not in sql
    assert "STRAIGHT_JOIN" not in sql


def test_quarantine_unresolved_uses_forced_ordered_index() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)

    AdminRepository(database).list_quarantine(state="unresolved", limit=10)
    AdminRepository(database).list_quarantine(state="unresolved", run_key="bounded-1", limit=10)

    calls = [call for call in database.calls if call.tag == "quarantine.list"]
    assert "FORCE INDEX (idx_quarantine_list)" in calls[0].sql
    assert "STRAIGHT_JOIN groups_t" in calls[0].sql
    assert "FORCE INDEX (idx_quarantine_run_key_resolved_updated)" in calls[1].sql
    assert "STRAIGHT_JOIN groups_t" in calls[1].sql


def test_quarantine_resolve_updates_row_in_transaction() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)

    AdminRepository(database).resolve_quarantine(
        7,
        "verified removed from site",
        expected_run_key="bounded-1",
    )

    tags = [call.tag for call in database.calls]
    assert tags == ["quarantine.lock-row", "quarantine.resolve"]
    resolve_call = database.calls[-1]
    assert "SET resolved_at = NOW(), resolution = %s" in resolve_call.sql
    assert "WHERE id = %s AND run_key = %s AND resolved_at IS NULL" in resolve_call.sql
    assert resolve_call.params == ("verified removed from site", 7, "bounded-1")


def test_quarantine_resolve_unknown_row_raises_record_not_found() -> None:
    trace = QueryTrace()
    database = ScriptedDatabase(trace)

    def deny_lock(tag: str, sql: str, params: object = None) -> dict | None:
        return None

    database.fetch_one = deny_lock  # type: ignore[method-assign]
    with pytest.raises(RecordNotFoundError):
        AdminRepository(database).resolve_quarantine(
            999,
            "checked",
            expected_run_key="bounded-1",
        )


@pytest.mark.parametrize(
    ("entity_type", "payload"),
    (
        ("vin_vehicle_mappings", {"vin": "INVALID"}),
        ("vin_vehicle_mappings", {"vin": 123}),
        ("vehicle_configurations", {"production_from": "2020-1"}),
        ("vehicle_configurations", {"production_to": "2101-01"}),
        ("vehicle_configurations", {"production_precision": "quarter"}),
        ("part_term_mappings", {"mapping_status": "maybe"}),
        ("vin_vehicle_mappings", {"decode_status": "decoded"}),
        ("reconciliation_cases", {"severity": "critical"}),
        ("reconciliation_cases", {"status": "resolved"}),
        ("vin_vehicle_mappings", {"engine_cylinders": "4"}),
        ("vehicle_configurations", {"catalog_brand": 123}),
    ),
)
def test_clean_payload_rejects_invalid_vin_date_enum_and_types(
    entity_type: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(AdminDataError):
        AdminRepository._clean_payload(ENTITY_SPECS[entity_type], payload)


def test_clean_payload_rejects_every_vin_mapping_field() -> None:
    spec = ENTITY_SPECS["vin_vehicle_mappings"]
    for field in spec.source_fields:
        with pytest.raises(AdminDataError, match="不可編輯"):
            AdminRepository._clean_payload(spec, {field: "decoded"})


def test_vin_mapping_repository_rejects_generic_mutations_before_database_access() -> None:
    database = ScriptedDatabase(QueryTrace())
    repository = AdminRepository(database)

    with pytest.raises(AdminDataError, match="唯讀"):
        repository.create_manual(
            "vin_vehicle_mappings",
            {"vin": "TEST0000000000000"},
            actor="tester",
            reason="must use dedicated workflow",
        )
    with pytest.raises(AdminDataError, match="唯讀"):
        repository.update_record(
            "vin_vehicle_mappings",
            "source:1",
            {"make_name": "Toyota"},
            expected_revision=0,
            expected_base_sha256="0" * 64,
            actor="tester",
            reason="must use dedicated workflow",
        )
    with pytest.raises(AdminDataError, match="唯讀"):
        repository.retire_record(
            "vin_vehicle_mappings",
            "source:1",
            expected_revision=0,
            actor="tester",
            reason="must use dedicated workflow",
        )
    with pytest.raises(AdminDataError, match="唯讀"):
        repository.restore_record(
            "vin_vehicle_mappings",
            "source:1",
            expected_revision=0,
            actor="tester",
            reason="must use dedicated workflow",
        )

    assert database.calls == []


def test_vin_mapping_display_ignores_legacy_overlay_payload() -> None:
    spec = ENTITY_SPECS["vin_vehicle_mappings"]
    row = source_row("vin_vehicle_mappings", 1)
    row.update(
        {
            "make_name": "NHTSA Make",
            "mapping_status": "confirmed",
            "override_payload_json": '{"make_name":"Legacy Make","mapping_status":"unmapped"}',
            "override_status": "retired",
            "override_revision": 7,
            "override_updated_at": "2099-01-01 00:00:00",
            "updated_at": "2026-08-30 00:00:00",
        }
    )

    record = AdminRepository._source_record(spec, row)

    assert record.payload["make_name"] == "NHTSA Make"
    assert record.payload["mapping_status"] == "confirmed"
    assert record.status == "active"
    assert record.revision == 0
    assert record.updated_at == "2026-08-30 00:00:00"


def test_vin_mapping_list_ignores_legacy_source_and_manual_overlays() -> None:
    database = ScriptedDatabase(QueryTrace())

    page = AdminRepository(database).list_records(
        "vin_vehicle_mappings",
        query="NHTSA Make",
        include_retired=True,
        limit=10,
    )

    assert page.total == 1
    for tag in (
        "list.count.vin_vehicle_mappings",
        "list.keys.vin_vehicle_mappings",
        "list.source-batch.vin_vehicle_mappings",
    ):
        call = next(call for call in database.calls if call.tag == tag)
        assert "admin_override_heads" not in call.sql
    assert not any(call.tag.startswith("list.manual-batch.") for call in database.calls)
    assert database.transaction_modes == [True]


def test_vin_mapping_legacy_manual_identity_is_not_a_derived_record() -> None:
    database = ScriptedDatabase(QueryTrace())

    with pytest.raises(RecordNotFoundError, match="來源資料"):
        AdminRepository(database).get_record(
            "vin_vehicle_mappings",
            "manual:00000000-0000-0000-0000-000000000001",
        )

    assert database.calls == []


def test_clean_payload_rejects_reversed_month_range() -> None:
    with pytest.raises(AdminDataError, match="不可晚於"):
        AdminRepository._clean_payload(
            ENTITY_SPECS["vehicle_configurations"],
            {"production_from": "2021-01", "production_to": "2020-12"},
        )


def test_quarantine_resolve_rejects_stale_or_already_resolved_occurrence() -> None:
    stale_database = ScriptedDatabase(QueryTrace())
    with pytest.raises(RevisionConflictError, match="已更新"):
        AdminRepository(stale_database).resolve_quarantine(
            7,
            "checked",
            expected_run_key="bounded-old",
        )
    assert [call.tag for call in stale_database.calls] == ["quarantine.lock-row"]

    resolved_database = ScriptedDatabase(QueryTrace())
    original_fetch_one = resolved_database.fetch_one

    def resolved_lock(tag: str, sql: str, params: object = None) -> dict | None:
        if tag == "quarantine.lock-row":
            return {"id": 7, "run_key": "bounded-1", "resolved_at": "2026-08-22"}
        return original_fetch_one(tag, sql, params)  # type: ignore[arg-type]

    resolved_database.fetch_one = resolved_lock  # type: ignore[method-assign]
    with pytest.raises(RevisionConflictError, match="已更新"):
        AdminRepository(resolved_database).resolve_quarantine(
            7,
            "checked again",
            expected_run_key="bounded-1",
        )
