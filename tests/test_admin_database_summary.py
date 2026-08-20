from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from partsouq_admin import app as admin_app


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

    published = admin_app.list_parts(page=2, page_size=25)
    assert admin_app.part_fitments("123-AB") == {"catalog": [], "manual": []}
    sample = admin_app.list_sample_parts(page=3, page_size=25)

    assert published == {
        "items": [],
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
    published_sql, published_params = item_queries[0]
    fitment_sql, _fitment_params = item_queries[1]
    sample_sql, sample_params = item_queries[3]
    for sql in (published_sql, fitment_sql, sample_sql):
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
    assert published_params == (25, 25)
    assert sample_params == (25, 50)
    assert "ORDER BY pp.snapshot_at DESC, pp.part_id DESC" in published_sql
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
        for page_size, total_pages in ((10, 100), (25, 40), (50, 20), (100, 10), (200, 5)):
            response = client.get(f"/api/sample-parts?page=1&pageSize={page_size}")
            assert response.status_code == 200
            assert response.json()["pageSize"] == page_size
            assert response.json()["totalPages"] == total_pages

        assert client.get("/api/sample-parts").json()["pageSize"] == 10
        assert client.get("/api/sample-parts?page=0&pageSize=25").status_code == 422
        assert client.get("/api/sample-parts?page=1&pageSize=15").status_code == 422


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
    assert "TRIM(pp.code)" in published_quality_sql
    assert "TRIM(p.code)" in sample_quality_sql


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


def test_admin_page_loads_summary_published_and_sample_parts() -> None:
    html = Path(admin_app.STATIC_DIR / "admin.html").read_text()

    assert "/api/database-summary" in html
    assert "'/api/parts'" in html
    assert "'/api/sample-parts'" in html
    assert "pageSize=${partsState.pageSize}" in html
    assert "sample-parts?limit=1000" not in html
    for page_size in (10, 25, 50, 100, 200):
        assert f'<option value="{page_size}">{page_size}</option>' in html
    assert "cell.textContent = value" in html
    assert "樣本（尚未發布）" in html
    for label in ("首頁", "上一頁", "下一頁", "末頁", "重新整理"):
        assert f">{label}</button>" in html
    assert 'id="parts-page-number"' in html
    assert "event.key === 'Enter'" in html
    assert 'id="parts-range-label"' in html
    assert "共用 DB 資料總覽" in html
    assert "NHTSA 基礎資料" in html
    assert "尚未提供合法 VIN" in html
    assert "正式全量 PartSouq snapshot 尚未發布" in html
    assert "沒有成功發布的 NHTSA VIN 解碼" not in html
    assert "站方小分類來源尚未取得" in html
    assert "<code>part_id</code>" in html
    assert "<code>vehicle_vid</code>" in html
    assert "<code>part_code</code> 是零件表 Code，不是型號 ID" in html
