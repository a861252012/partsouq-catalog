from __future__ import annotations

from pathlib import Path

import pytest

from partsouq_admin import app as admin_app


def test_database_summary_fails_closed_for_empty_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(admin_app, "_fetch_one", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setenv("PSQ_LIMIT_PARTS", "1000")

    summary = admin_app.database_summary()

    assert summary["requirements_met"] is False
    assert summary["sample_progress"] == {
        "target_rows": 1000,
        "latest_run_rows": 0,
        "published_rows": 0,
    }
    assert "no_published_parts" in summary["blocking_reasons"]
    assert "sample_rows_below_target" in summary["blocking_reasons"]
    assert "no_nhtsa_vin_decodes" in summary["blocking_reasons"]
    assert "no_confirmed_vin_mapping" in summary["blocking_reasons"]
    assert "partsouq_small_category_source_unavailable" in summary["blocking_reasons"]
    assert summary["data_quality"]["small_category"] == {
        "source_status": "unavailable_in_current_partsouq_hierarchy",
        "crawled_rows": 0,
    }


def test_part_queries_return_internal_and_partsouq_source_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    def capture(sql: str, _params: tuple[object, ...] = ()) -> list[dict]:
        queries.append(sql)
        return []

    monkeypatch.setattr(admin_app, "_fetch_all", capture)

    assert admin_app.list_parts(limit=1) == []
    assert admin_app.part_fitments("123-AB") == {"catalog": [], "manual": []}
    assert admin_app.list_sample_parts(limit=1) == []

    published_sql, fitment_sql, _manual_sql, sample_sql = queries
    for sql in (published_sql, fitment_sql, sample_sql):
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
    assert "status = 'sample'" in sample_sql
    assert "sample_not_published" in sample_sql


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
    admin_app.database_summary()

    published_quality_sql = queries[2]
    sample_quality_sql = queries[3]
    assert "TRIM(pp.code)" in published_quality_sql
    assert "TRIM(p.code)" in sample_quality_sql


def test_admin_page_loads_summary_published_and_sample_parts() -> None:
    html = Path(admin_app.STATIC_DIR / "admin.html").read_text()

    assert "/api/database-summary" in html
    assert "/api/parts?limit=200" in html
    assert "/api/sample-parts?limit=1000" in html
    assert "共用 DB 資料總覽" in html
    assert "站方小分類來源尚未取得" in html
    assert "<code>part_id</code>" in html
    assert "<code>vehicle_vid</code>" in html
    assert "<code>part_code</code> 是零件表 Code，不是型號 ID" in html
