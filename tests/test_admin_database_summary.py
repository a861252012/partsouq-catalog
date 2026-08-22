from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from partsouq_admin import app as admin_app
from partsouq_admin.app import VinVehicleMappingUpdateInput


def test_database_reads_redact_sensitive_source_url_query_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [
        {
            "id": 1,
            "source_url": "https://partsouq.com/en/catalog/genuine/unit?ssd=secret&uid=9",
        }
    ]
    monkeypatch.setattr(admin_app, "_connect", lambda: connection)

    rows = admin_app._fetch_all("SELECT source_url FROM parts")

    assert rows[0]["source_url"].endswith("ssd=%5BREDACTED%5D&uid=9")
    connection.close.assert_called_once()


def test_database_summary_fails_closed_for_empty_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one_queries: list[str] = []
    all_queries: list[str] = []
    responses = iter(
        [
            {},
            {},
            {},
            {"row_count": 0},
            {},
            None,
            None,
        ]
    )

    def capture_one(sql: str, *_args: object, **_kwargs: object) -> dict | None:
        one_queries.append(sql)
        return next(responses)

    def capture_all(sql: str, *_args: object, **_kwargs: object) -> list[dict]:
        all_queries.append(sql)
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)
    monkeypatch.setenv("PSQ_LIMIT_PARTS", "1000")

    summary = admin_app.database_summary()

    assert summary["demo_ready"] is False
    assert summary["production_ready"] is False
    assert summary["requirements_met"] is False
    assert summary["sample_progress"] == {
        "target_rows": 1000,
        "latest_run_rows": 0,
        "unique_part_numbers": 0,
        "published_rows": 0,
    }
    assert "sample_rows_below_target" in summary["demo_blocking_reasons"]
    assert "no_nhtsa_reference_data" in summary["demo_blocking_reasons"]
    assert "full_catalog_not_published" in summary["production_pending_reasons"]
    assert "awaiting_authorized_vin" in summary["production_pending_reasons"]
    assert "no_confirmed_vin_mapping" not in summary["production_pending_reasons"]
    assert "partsouq_small_category_source_unavailable" in summary["production_pending_reasons"]
    assert summary["data_quality"]["small_category"] == {
        "source_status": "unavailable_in_current_partsouq_hierarchy",
        "crawled_rows": 0,
    }
    assert "SUM(a.source_rows)" in one_queries[0]
    assert "FROM nhtsa_current_records" not in one_queries[0]
    assert "SUM(a.source_rows)" in all_queries[0]
    assert "FROM nhtsa_current_records" not in all_queries[0]


def test_part_queries_return_internal_and_partsouq_source_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count_queries: list[tuple[str, tuple[object, ...]]] = []
    item_queries: list[tuple[str, tuple[object, ...]]] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        count_queries.append((sql, params))
        return {"total": 1000}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        item_queries.append((sql, params))
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    published = admin_app.list_parts(part_number="09481-15501", page=2, page_size=25)
    assert admin_app.part_fitments("123-AB") == {"catalog": [], "manual": []}
    sample = admin_app.list_sample_parts(page=3, page_size=25)
    bounded = admin_app.list_bounded_parts(part_number="09481-15501", page=4, page_size=25)

    assert published == {
        "items": [],
        "datasetScope": None,
        "crawlRunId": None,
        "page": 2,
        "pageSize": 25,
        "total": 1000,
        "totalPages": 40,
    }
    assert sample == {
        "items": [],
        "page": 3,
        "pageSize": 25,
        "total": 1000,
        "totalPages": 40,
    }
    assert bounded == {
        "items": [],
        "crawlRunId": None,
        "page": 4,
        "pageSize": 25,
        "total": 1000,
        "totalPages": 40,
    }
    published_sql, published_params = item_queries[0]
    fitment_sql, fitment_params = item_queries[1]
    sample_sql, sample_params = item_queries[3]
    bounded_sql, bounded_params = item_queries[4]
    for sql in (published_sql, fitment_sql, sample_sql, bounded_sql):
        assert "station_admin_effective_parts" in sql
        assert "LEFT JOIN station_admin_effective_parts" in sql
        assert "override_status" in sql
        for field in (
            "part_id",
            "model_id",
            "vehicle_id",
            "vehicle_vid",
            "category_id",
            "category_cid",
            "group_id",
            "group_code",
            "group_uid",
            "part_code",
        ):
            assert field in sql
    assert published_params == ("0948115501", "0948115501", 25, 25)
    assert fitment_params == ("123AB", "123AB")
    assert sample_params == (25, 50)
    assert bounded_params == ("0948115501", "0948115501", 25, 75)
    assert "FROM bounded_parts AS bp" in bounded_sql
    assert "candidate.part_number_normalized = %s" in bounded_sql
    assert "UNION ALL SELECT candidate_override.part_id" in bounded_sql
    assert "dataset_kind = 'bounded'" in bounded_sql
    assert "status = 'bounded_success'" in bounded_sql
    assert "scheduled_bounded_not_full_published" in bounded_sql
    assert "FROM v_current_catalog_parts AS current_part" in published_sql
    assert "candidate.part_number_normalized = %s" in published_sql
    assert "UNION ALL SELECT candidate_override.part_id" in published_sql
    assert "REGEXP_REPLACE(UPPER(current_part.part_number)" not in published_sql
    assert "ORDER BY current_part.snapshot_at DESC, current_part.part_id DESC" in published_sql
    assert "candidate.part_number_normalized = %s" in fitment_sql
    assert "REGEXP_REPLACE(UPPER(pp.part_number)" not in fitment_sql
    assert "ORDER BY p.id ASC" in sample_sql
    assert all("COUNT(*) AS total" in sql for sql, _params in count_queries)
    assert "status = 'sample'" in sample_sql
    assert "sample_not_published" in sample_sql


def test_part_pagination_validates_page_size_and_calculates_total_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_app, "_fetch_one", lambda *_args, **_kwargs: {"total": 1000})
    monkeypatch.setattr(admin_app, "_fetch_all", lambda *_args, **_kwargs: [])

    with TestClient(admin_app.app) as client:
        for page_size, total_pages in (
            (10, 100),
            (25, 40),
            (30, 34),
            (50, 20),
            (100, 10),
            (200, 5),
        ):
            response = client.get(f"/api/sample-parts?page=1&pageSize={page_size}")
            assert response.status_code == 200
            assert response.json()["pageSize"] == page_size
            assert response.json()["totalPages"] == total_pages

        assert client.get("/api/sample-parts").json()["pageSize"] == 10
        assert client.get("/api/sample-parts?page=0&pageSize=25").status_code == 422
        assert client.get("/api/sample-parts?page=1&pageSize=15").status_code == 422


@pytest.mark.parametrize(
    ("path", "expected_count_marker", "expected_order_marker"),
    (
        ("/api/parts", "v_current_catalog_parts", "current_part.snapshot_at DESC"),
        ("/api/sample-parts", "status = 'sample'", "p.id ASC"),
        ("/api/bounded-parts", "bounded_parts", "bp.part_id ASC"),
    ),
)
def test_part_page_endpoints_clamp_out_of_range_page(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_count_marker: str,
    expected_order_marker: str,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict[str, object]:
        assert expected_count_marker in sql
        return {"total": 21}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
        calls.append((sql, params))
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    with TestClient(admin_app.app) as client:
        response = client.get(path, params={"page": 99, "pageSize": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["page"] == 3
    assert payload["totalPages"] == 3
    assert expected_order_marker in calls[0][0]
    assert calls[0][1][-2:] == (10, 20)


@pytest.mark.parametrize("path", ("/api/parts", "/api/sample-parts", "/api/bounded-parts"))
def test_empty_part_page_endpoints_use_page_one_and_zero_total_pages(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    monkeypatch.setattr(admin_app, "_fetch_one", lambda *_args, **_kwargs: {"total": 0})
    monkeypatch.setattr(admin_app, "_fetch_all", lambda *_args, **_kwargs: [])

    with TestClient(admin_app.app) as client:
        response = client.get(path, params={"page": 99, "pageSize": 10})

    assert response.status_code == 200
    assert response.json()["page"] == 1
    assert response.json()["totalPages"] == 0


@pytest.mark.parametrize(
    ("path", "payload"),
    (
        (
            "/api/vehicle-mappings",
            {"vin_prefix": 123, "make_name": "A", "model_name": "B"},
        ),
        (
            "/api/vin-vehicle-mappings",
            {"vin": 123, "partsouq_vehicle_id": 1},
        ),
        (
            "/api/part-fitments",
            {
                "part_number": "P1",
                "vin_prefix": 123,
                "make_name": "A",
                "model_name": "B",
            },
        ),
    ),
)
def test_malformed_admin_json_types_return_422_without_database_access(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    payload: dict[str, object],
) -> None:
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "test-token")
    connect = MagicMock(side_effect=AssertionError("validation must happen before DB access"))
    monkeypatch.setattr(admin_app, "_connect", connect)

    with TestClient(admin_app.app) as client:
        response = client.post(path, json=payload, headers={"X-Admin-Token": "test-token"})

    assert response.status_code == 422
    connect.assert_not_called()


def test_invalid_json_syntax_returns_422() -> None:
    with TestClient(admin_app.app) as client:
        response = client.post(
            "/api/vehicle-mappings",
            content="{",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422


def test_data_admin_rejects_untrusted_host_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = MagicMock(side_effect=AssertionError("untrusted host must not reach DB"))
    monkeypatch.setattr(admin_app, "_fetch_one", fetch)

    with TestClient(admin_app.app) as client:
        response = client.get("/api/health", headers={"Host": "evil.example"})

    assert response.status_code == 400
    fetch.assert_not_called()


def test_data_admin_cli_binds_loopback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = MagicMock()
    monkeypatch.delenv("PARTSOUQ_ADMIN_BIND_HOST", raising=False)
    monkeypatch.setattr(admin_app.uvicorn, "run", run)

    admin_app.main()

    run.assert_called_once_with(
        "partsouq_admin.app:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


@pytest.mark.parametrize("configured_host", ("", "   "))
def test_data_admin_cli_empty_bind_host_still_binds_loopback(
    monkeypatch: pytest.MonkeyPatch,
    configured_host: str,
) -> None:
    run = MagicMock()
    monkeypatch.setenv("PARTSOUQ_ADMIN_BIND_HOST", configured_host)
    monkeypatch.setattr(admin_app.uvicorn, "run", run)

    admin_app.main()

    assert run.call_args.kwargs["host"] == "127.0.0.1"


def test_categories_mark_small_category_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    monkeypatch.setattr(
        admin_app,
        "_fetch_all",
        lambda sql, _params: queries.append(sql) or [],
    )

    assert admin_app.list_categories(limit=1) == []
    assert "NULL AS category_small" in queries[0]
    assert "unavailable_in_current_partsouq_hierarchy" in queries[0]
    assert "manual_only" in queries[0]
    assert "sample_not_published" in queries[0]
    assert "status = 'sample'" in queries[0]


def test_data_quality_queries_require_partsouq_part_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    responses = iter([{}, {}, {}, {"row_count": 0}, {}, None, None])

    def capture(sql: str, *_args: object, **_kwargs: object) -> dict | None:
        queries.append(sql)
        return next(responses)

    monkeypatch.setattr(admin_app, "_fetch_one", capture)
    monkeypatch.setattr(admin_app, "_fetch_all", lambda *_args, **_kwargs: [])
    admin_app.database_summary()

    published_quality_sql = queries[2]
    sample_quality_sql = queries[3]
    mapping_status_sql = queries[1]
    assert "TRIM(pp.code)" in published_quality_sql
    assert "TRIM(p.code)" in sample_quality_sql
    assert "TRIM(bp.vehicle_code)" in queries[0]
    assert "DATABASE() <> 'partsouq_catalog'" in queries[0]
    assert "bp.part_number_normalized <>" in queries[0]
    assert "v_current_catalog_parts AS current_year" in mapping_status_sql
    assert "current_year.production_from IS NOT NULL" in mapping_status_sql
    assert "d.model_year >=" in mapping_status_sql


def test_mapping_list_requires_current_catalog_vehicle_year_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    monkeypatch.setattr(
        admin_app,
        "_fetch_all",
        lambda sql, _params: queries.append(sql) or [],
    )

    assert admin_app.list_vin_vehicle_mappings(limit=1) == []

    assert "v_current_catalog_parts AS current_year" in queries[0]
    assert "current_year.production_from IS NOT NULL" in queries[0]
    assert "current_year.production_to IS NOT NULL" in queries[0]
    assert "d.model_year >=" in queries[0]


def test_vin_mapping_update_requires_matching_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.rowcount = 1
    current = {
        "id": 7,
        "vin": "1HGCM82633A004352",
        "updated_at": datetime(2026, 8, 22, 12, 0, 0),
    }
    monkeypatch.setattr(admin_app, "_row_or_404", lambda *_args: current)
    monkeypatch.setattr(admin_app, "_validated_vin_vehicle_mapping", lambda *_args: (1,) * 10)
    monkeypatch.setattr(admin_app, "_connect", lambda: connection)
    expected_updated_at = datetime(2026, 8, 22, 12, 0, 0)

    result = admin_app.update_vin_vehicle_mapping(
        7,
        VinVehicleMappingUpdateInput(
            vin="1HGCM82633A004352",
            partsouq_vehicle_id=1,
            expected_updated_at=expected_updated_at,
        ),
    )

    assert result == current
    sql, params = cursor.execute.call_args.args
    assert "WHERE id=%s AND updated_at=%s" in sql
    assert "updated_at + INTERVAL 1 SECOND" in sql
    assert params[-2:] == (7, expected_updated_at)
    connection.commit.assert_called_once_with()
    connection.rollback.assert_not_called()


def test_get_vin_mapping_reads_exact_mapping_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, tuple[object, ...]]] = []

    def fetch_one(sql: str, params: tuple[object, ...] = ()) -> dict[str, object]:
        queries.append((sql, params))
        return {"id": 101, "vin": "1HGCM82633A004352", "updated_at": "2026-08-22 12:00:00"}

    monkeypatch.setattr(admin_app, "_fetch_one", fetch_one)

    row = admin_app.get_vin_vehicle_mapping(101)

    assert row["id"] == 101
    assert "id = %s AND vin IS NOT NULL" in queries[0][0]
    assert queries[0][1] == (101,)


def test_vin_mapping_update_rejects_stale_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.rowcount = 0
    monkeypatch.setattr(
        admin_app,
        "_row_or_404",
        lambda *_args: {"id": 7, "vin": "1HGCM82633A004352"},
    )
    monkeypatch.setattr(admin_app, "_validated_vin_vehicle_mapping", lambda *_args: (1,) * 10)
    monkeypatch.setattr(admin_app, "_connect", lambda: connection)

    with pytest.raises(HTTPException) as exc_info:
        admin_app.update_vin_vehicle_mapping(
            7,
            VinVehicleMappingUpdateInput(
                vin="1HGCM82633A004352",
                partsouq_vehicle_id=1,
                expected_updated_at=datetime(2026, 8, 22, 11, 0, 0),
            ),
        )

    assert exc_info.value.status_code == 409
    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()


def test_vin_mapping_update_http_requires_expected_updated_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "test-token")
    connect = MagicMock(side_effect=AssertionError("validation must happen before DB access"))
    monkeypatch.setattr(admin_app, "_connect", connect)

    with TestClient(admin_app.app) as client:
        response = client.put(
            "/api/vin-vehicle-mappings/7",
            json={"vin": "1HGCM82633A004352", "partsouq_vehicle_id": 1},
            headers={"X-Admin-Token": "test-token"},
        )

    assert response.status_code == 422
    connect.assert_not_called()


def test_vin_mapping_update_rejects_timezone_version_before_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "test-token")
    connect = MagicMock(side_effect=AssertionError("validation must happen before DB access"))
    monkeypatch.setattr(admin_app, "_connect", connect)

    with TestClient(admin_app.app) as client:
        response = client.put(
            "/api/vin-vehicle-mappings/7",
            json={
                "vin": "1HGCM82633A004352",
                "partsouq_vehicle_id": 1,
                "expected_updated_at": "2026-08-22T04:00:00Z",
            },
            headers={"X-Admin-Token": "test-token"},
        )

    assert response.status_code == 422
    connect.assert_not_called()


def test_sample_part_range_is_informational_when_vehicle_years_are_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {
                "published_fitment_rows": 0,
                "nhtsa_vin_decodes": 0,
            },
            {},
            {},
            {
                "row_count": 1000,
                "required_field_missing_rows": 0,
                "id_missing_rows": 0,
                "source_id_missing_rows": 0,
                "orphan_relation_rows": 0,
                "vehicle_range_missing_rows": 0,
                "part_range_missing_rows": 1000,
                "category_main_missing_rows": 0,
                "category_group_missing_rows": 0,
            },
            {},
            None,
            None,
        ]
    )
    monkeypatch.setattr(admin_app, "_fetch_one", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(admin_app, "_fetch_all", lambda *_args, **_kwargs: [])

    summary = admin_app.database_summary()

    assert "sample_parts_data_quality_failed" not in summary["blocking_reasons"]
    assert summary["data_quality"]["sample"]["part_range_missing_rows"] == 1000


def test_database_summary_separates_nhtsa_reference_sync_from_vin_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    datasets = [
        {"dataset_name": "vpic_makes", "row_count": 12337},
        {"dataset_name": "vpic_models", "row_count": 31897},
        {"dataset_name": "vpic_variable_values", "row_count": 69818},
    ]
    monkeypatch.setattr(admin_app, "_fetch_all", lambda *_args, **_kwargs: datasets)
    monkeypatch.setenv("PSQ_LIMIT_PARTS", "1000")

    def summary_for(vin_decodes: int) -> dict:
        responses = iter(
            [
                {
                    "published_fitment_rows": 0,
                    "nhtsa_current_records": 137120,
                    "nhtsa_current_artifacts": 377,
                    "nhtsa_sync_runs": 1,
                    "nhtsa_completed_runs": 1,
                    "nhtsa_failed_runs": 0,
                    "nhtsa_rejected_rows": 0,
                    "nhtsa_vin_decodes": vin_decodes,
                },
                {"confirmed": 0, "stale": 0},
                {},
                {
                    "row_count": 1000,
                    "unique_part_numbers": 923,
                    "required_field_missing_rows": 0,
                    "id_missing_rows": 0,
                    "source_id_missing_rows": 0,
                    "orphan_relation_rows": 0,
                    "vehicle_range_missing_rows": 0,
                    "part_range_missing_rows": 1000,
                    "category_main_missing_rows": 0,
                    "category_group_missing_rows": 0,
                },
                {},
                {"status": "sample", "parts_ok": 1000},
                {"status": "sample", "parts_ok": 1000},
            ]
        )
        monkeypatch.setattr(
            admin_app,
            "_fetch_one",
            lambda *_args, **_kwargs: next(responses),
        )
        return admin_app.database_summary()

    waiting = summary_for(0)

    assert waiting["demo_ready"] is True
    assert waiting["production_ready"] is False
    assert waiting["demo_blocking_reasons"] == []
    assert "full_catalog_not_published" in waiting["production_pending_reasons"]
    assert "awaiting_authorized_vin" in waiting["production_pending_reasons"]
    assert "no_confirmed_vin_mapping" not in waiting["production_pending_reasons"]
    assert waiting["nhtsa"] == {
        "current_records": 137120,
        "current_artifacts": 377,
        "datasets": datasets,
        "sync_runs": {"total": 1, "completed": 1, "failed": 0},
        "rejected_rows": 0,
        "vin_decodes": 0,
        "vin_decode_status": "awaiting_authorized_vin",
    }

    decoded_without_mapping = summary_for(1)

    assert "awaiting_authorized_vin" not in decoded_without_mapping["production_pending_reasons"]
    assert "no_confirmed_vin_mapping" in decoded_without_mapping["production_pending_reasons"]


def _bounded_summary(
    monkeypatch: pytest.MonkeyPatch,
    **count_overrides: object,
) -> dict:
    counts = {
        "published_fitment_rows": 0,
        "nhtsa_current_records": 137120,
        "nhtsa_vin_decodes": 0,
        "bounded_crawl_run_id": 42,
        "bounded_run_key": "bounded-10000-20260820T120000000000",
        "bounded_dataset_kind": "bounded",
        "bounded_run_status": "bounded_success",
        "bounded_target_parts": 10000,
        "bounded_run_parts_ok": 10000,
        "bounded_scheduled_job_run_id": 77,
        "bounded_scheduler_job_name": "catalog",
        "bounded_scheduler_trigger_mode": "daemon",
        "bounded_scheduler_status": "completed",
        "bounded_scheduler_exit_code": 0,
        "bounded_scheduler_linked_crawl_runs": 1,
        "bounded_fitment_rows": 10000,
        "bounded_snapshot_min_run_id": 42,
        "bounded_snapshot_max_run_id": 42,
        "bounded_unique_part_numbers": 9234,
        "bounded_unique_vehicles": 318,
        "bounded_official_source_url_rows": 10000,
        "bounded_evidence_status": "verified",
        "bounded_evidence_manifest_sha256": "a" * 64,
        "bounded_evidence_dataset_sha256": "b" * 64,
        "bounded_evidence_artifact_count": 250,
        "bounded_active_artifact_count": 250,
        "bounded_live_artifact_count": 250,
        "bounded_evidence_page_type_count": 6,
        "bounded_evidence_record_count": 10000,
        "bounded_accepted_evidence_records": 10000,
        "bounded_evidence_original_bytes": 1000000,
        "bounded_evidence_stored_bytes": 250000,
        "bounded_evidence_verified_at": datetime(2026, 8, 22, 12, 0, 0),
        "bounded_part_range_missing_rows": 10000,
        "current_catalog_scope": "bounded",
        "current_catalog_crawl_run_id": 42,
        "current_catalog_rows": 10000,
        "current_unique_part_numbers": 9234,
        "current_non_ascii_part_name_rows": 237,
    }
    counts.update(count_overrides)
    responses = iter(
        [
            counts,
            {},
            {},
            {"row_count": 0},
            {},
            {"status": counts.get("bounded_run_status"), "parts_ok": 10000},
            None,
        ]
    )
    monkeypatch.setattr(admin_app, "_fetch_one", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(admin_app, "_fetch_all", lambda *_args, **_kwargs: [])
    return admin_app.database_summary()


def test_bounded_summary_requires_exact_scheduled_10000_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _bounded_summary(monkeypatch)

    assert summary["bounded_ready"] is True
    assert summary["bounded"]["ready"] is True
    assert summary["bounded"]["blocking_reasons"] == []
    assert summary["bounded"]["fitment_rows"] == 10000
    assert summary["bounded"]["snapshot_crawl_run_id"] == 42
    assert summary["bounded"]["unique_part_numbers"] == 9234
    assert summary["bounded"]["scheduler"] == {
        "run_id": 77,
        "job_name": "catalog",
        "trigger_mode": "daemon",
        "status": "completed",
        "exit_code": 0,
        "started_at": None,
        "finished_at": None,
    }
    assert summary["bounded"]["source_provenance"] == {
        "official_source_url_rows": 10000,
        "invalid_source_url_rows": 0,
        "evidence_level": "verified_live_http_replay_mapping_chain",
        "raw_http_artifact_status": "raw_hash_and_sanitized_parser_body_persisted",
        "live_http_evidence": True,
        "evidence_status": "verified",
        "manifest_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "artifact_count": 250,
        "record_count": 10000,
        "required_page_types": ["genuine", "locate", "pick", "vehicle", "category", "unit"],
        "verified_page_type_count": 6,
        "non_live_data_marker": False,
    }
    assert summary["bounded"]["part_range_source"] == {
        "populated_rows": 0,
        "missing_rows": 10000,
        "status": "unavailable_vehicle_range_used",
    }
    assert summary["bounded"]["name_language"] == {
        "status": "not_verified",
        "english_name_unverified_rows": 0,
        "screening": "non_ascii_conservative_only",
    }
    assert summary["current_catalog"] == {
        "dataset_scope": "bounded",
        "crawl_run_id": 42,
        "fitment_rows": 10000,
        "unique_part_numbers": 9234,
        "name_language": {
            "status": "not_verified",
            "non_ascii_rows": 237,
            "screening": "non_ascii_conservative_only",
        },
    }
    assert summary["production_ready"] is False
    assert "full_catalog_not_published" in summary["production_pending_reasons"]
    assert "partsouq_english_name_language_not_verified" in summary["production_pending_reasons"]
    assert (
        "partsouq_live_mapping_evidence_not_verified" not in summary["production_pending_reasons"]
    )


def test_bounded_summary_does_not_claim_source_evidence_for_zero_source_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _bounded_summary(
        monkeypatch,
        bounded_fitment_rows=0,
        bounded_official_source_url_rows=0,
    )

    assert summary["bounded"]["source_provenance"]["evidence_level"] == "not_verified"


def test_bounded_summary_requires_source_evidence_for_every_bounded_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary = _bounded_summary(
        monkeypatch,
        bounded_fitment_rows=10_000,
        bounded_official_source_url_rows=9_999,
    )

    assert summary["bounded"]["source_provenance"]["evidence_level"] == "not_verified"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"bounded_fitment_rows": 9999}, "bounded_snapshot_count_mismatch"),
        ({"bounded_fitment_rows": 10001}, "bounded_snapshot_count_mismatch"),
        ({"bounded_run_status": "sample"}, "bounded_run_not_successful"),
        ({"bounded_scheduled_job_run_id": None}, "bounded_scheduler_not_linked"),
        ({"bounded_scheduler_status": "failed"}, "bounded_scheduler_not_completed"),
        (
            {"bounded_scheduler_trigger_mode": "manual"},
            "bounded_scheduler_trigger_not_daemon",
        ),
        ({"bounded_scheduler_linked_crawl_runs": 2}, "bounded_scheduler_link_not_unique"),
        ({"bounded_required_field_missing_rows": 1}, "bounded_required_fields_missing"),
        ({"bounded_id_missing_rows": 1}, "bounded_ids_missing"),
        ({"bounded_source_id_missing_rows": 1}, "bounded_source_ids_missing"),
        ({"bounded_orphan_relation_rows": 1}, "bounded_orphan_relations"),
        ({"bounded_effective_year_missing_rows": 1}, "bounded_vehicle_years_missing"),
        ({"bounded_category_group_missing_rows": 1}, "bounded_categories_missing"),
        ({"bounded_invalid_source_url_rows": 1}, "bounded_source_url_invalid"),
        ({"bounded_active_override_rows": 1}, "bounded_active_overrides_present"),
        ({"bounded_non_live_data_marker": 1}, "bounded_non_live_data_marker"),
        (
            {"bounded_evidence_status": "missing"},
            "bounded_live_mapping_evidence_not_verified",
        ),
        (
            {"bounded_evidence_page_type_count": 5},
            "bounded_live_mapping_evidence_not_verified",
        ),
        (
            {"bounded_live_artifact_count": 249},
            "bounded_live_mapping_evidence_not_verified",
        ),
        (
            {"bounded_evidence_record_count": 9999},
            "bounded_live_mapping_evidence_not_verified",
        ),
        (
            {"bounded_evidence_manifest_sha256": None},
            "bounded_live_mapping_evidence_not_verified",
        ),
    ),
)
def test_bounded_summary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    reason: str,
) -> None:
    summary = _bounded_summary(monkeypatch, **overrides)

    assert summary["bounded_ready"] is False
    assert reason in summary["bounded"]["blocking_reasons"]


def test_admin_page_loads_summary_published_and_sample_parts() -> None:
    html = Path(admin_app.STATIC_DIR / "admin.html").read_text()

    assert "/api/database-summary" in html
    assert "'/api/parts'" in html
    assert "'/api/sample-parts'" in html
    assert "'/api/bounded-parts'" in html
    assert "pageSize=${partsState.pageSize}" in html
    assert "sample-parts?limit=1000" not in html
    for page_size in (10, 25, 30, 50, 100, 200):
        assert f'<option value="{page_size}">{page_size}</option>' in html
    assert "cell.textContent = value" in html
    assert "舊樣本（僅供診斷）" in html
    assert "正式排程限量（未發布全量）" in html
    assert '<option value="current">' in html
    assert html.index('<option value="current">') < html.index('<option value="sample">')
    assert "舊 sample，只供診斷，不列入 10,000 筆驗收" in html
    for label in ("首頁", "上一頁", "下一頁", "末頁", "重新整理"):
        assert f">{label}</button>" in html
    assert 'id="parts-page-number"' in html
    assert "event.key === 'Enter'" in html
    assert "const current = await call(`/api/vin-vehicle-mappings/${mappingId}`)" in html
    assert "if (!body.expected_updated_at)" in html
    assert "vinMappingId.addEventListener('input', () => { vinMappingVersion.value = ''; })" in html
    assert "const current = await call(`${endpoint}/${body.mapping_id}`)" not in html
    assert "method === 'PUT' ? '已更新資料。' : '已建立資料。'" in html
    assert 'id="parts-range-label"' in html
    assert "共用 DB 資料總覽" in html
    assert "NHTSA 基礎資料" in html
    assert "尚未提供合法 VIN" in html
    assert "正式全量 PartSouq snapshot 尚未發布" in html
    assert "沒有成功發布的 NHTSA VIN 解碼" not in html
    assert "站方小分類來源尚未取得" in html
    assert '<option value="catalog">' not in html
    assert "<code>part_id</code>" in html
    assert "<code>vehicle_vid</code>" in html
    assert "<code>part_code</code> 是零件表 Code，不是型號 ID" in html


def test_catalog_crawl_request_is_rejected_from_short_job_queue() -> None:
    with pytest.raises(ValueError):
        admin_app.CrawlRequestInput(job_name="catalog", requested_scope="all")
