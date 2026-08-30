from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from partsouq_admin import app as admin_app
from partsouq_admin.app import QuarantineResolveInput


def test_quarantine_list_filters_unresolved_and_paginates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[tuple[str, tuple[object, ...]]] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        queries.append((sql, params))
        return {"n": 2}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        queries.append((sql, params))
        return [{"id": 1, "part_number": "IMG10001"}, {"id": 2, "part_number": "IMG20002"}]

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    result = admin_app.list_quarantine(
        state="unresolved", run_key="bounded-1", page=1, page_size=25
    )

    assert result == {
        "items": [{"id": 1, "part_number": "IMG10001"}, {"id": 2, "part_number": "IMG20002"}],
        "page": 1,
        "pageSize": 25,
        "total": 2,
        "totalPages": 1,
    }
    count_sql, count_params = queries[0]
    list_sql, list_params = queries[1]
    assert "part_quarantine.resolved_at IS NULL" in count_sql
    assert "part_quarantine.run_key = %s" in count_sql
    assert count_params == ("bounded-1",)
    assert "LIMIT %s OFFSET %s" in list_sql
    assert list_params == ("bounded-1", 25, 0)
    assert "FORCE INDEX (idx_quarantine_run_key_resolved_updated)" in list_sql
    assert "STRAIGHT_JOIN groups_t" in list_sql
    assert "ORDER BY part_quarantine.updated_at DESC, part_quarantine.id DESC" in list_sql
    assert "resolved_at IS NOT NULL" not in list_sql


def test_quarantine_list_all_state_has_no_resolved_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        captured.append(sql)
        return {"n": 0}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        captured.append(sql)
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    admin_app.list_quarantine(state="all", page=1, page_size=10)

    assert "resolved_at IS NULL" not in captured[0]
    assert "ORDER BY (part_quarantine.resolved_at IS NOT NULL), " in captured[1]
    assert "part_quarantine.updated_at DESC, part_quarantine.id DESC" in captured[1]
    assert "FORCE INDEX" not in captured[1]
    assert "STRAIGHT_JOIN" not in captured[1]


def test_quarantine_all_state_unresolved_first_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        captured.append(sql)
        return {"n": 2}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        captured.append(sql)
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    admin_app.list_quarantine(state="all", page=1, page_size=10)
    assert "resolved_at IS NULL" not in captured[0]
    assert "(part_quarantine.resolved_at IS NOT NULL)" in captured[1]
    assert captured[1].index("(part_quarantine.resolved_at IS NOT NULL)") < captured[1].index(
        "part_quarantine.updated_at DESC"
    )


def test_quarantine_unresolved_uses_forced_ordered_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        return {"n": 2}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        captured.append(sql)
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    admin_app.list_quarantine(state="unresolved", page=1, page_size=10)
    admin_app.list_quarantine(state="unresolved", run_key="bounded-1", page=1, page_size=10)

    assert "FORCE INDEX (idx_quarantine_list)" in captured[0]
    assert "STRAIGHT_JOIN groups_t" in captured[0]
    assert "FORCE INDEX (idx_quarantine_run_key_resolved_updated)" in captured[1]
    assert "STRAIGHT_JOIN groups_t" in captured[1]


def test_quarantine_out_of_range_page_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[tuple[str, tuple[object, ...]]] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        return {"n": 50}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        queries.append((sql, params))
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    result = admin_app.list_quarantine(state="unresolved", page=2, page_size=50)

    assert result["page"] == 1
    assert result["totalPages"] == 1
    list_sql, list_params = queries[0]
    assert list_params == (50, 0)


def test_quarantine_resolve_marks_row(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [
        {"id": 7, "part_number": "IMG10001", "run_key": "bounded-1", "resolved_at": None},
        {
            "id": 7,
            "part_number": "IMG10001",
            "run_key": "bounded-1",
            "resolved_at": "2026-08-22 12:00:00",
        },
    ]
    cursor.rowcount = 1
    monkeypatch.setattr(admin_app, "_connect", lambda: connection)

    row = admin_app.resolve_quarantine(
        7,
        QuarantineResolveInput(
            expected_run_key="bounded-1",
            resolution="checked: site removed",
        ),
    )

    assert row["resolved_at"] == "2026-08-22 12:00:00"
    connection.begin.assert_called_once_with()
    connection.commit.assert_called_once_with()
    connection.rollback.assert_not_called()
    connection.close.assert_called_once_with()
    lock_sql, lock_params = cursor.execute.call_args_list[0].args
    assert "FOR UPDATE" in lock_sql
    assert lock_params == (7,)
    update_sql, update_params = cursor.execute.call_args_list[1].args
    assert "SET resolved_at = NOW(), resolution = %s" in update_sql
    assert "run_key = %s" in update_sql
    assert "resolved_at IS NULL" in update_sql
    assert update_params == ("checked: site removed", 7, "bounded-1")


def test_quarantine_resolve_rejects_stale_run_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {
        "id": 7,
        "part_number": "IMG10001",
        "run_key": "bounded-new",
        "resolved_at": None,
    }
    monkeypatch.setattr(admin_app, "_connect", lambda: connection)

    with pytest.raises(HTTPException) as exc_info:
        admin_app.resolve_quarantine(
            7,
            QuarantineResolveInput(
                expected_run_key="bounded-old",
                resolution="stale form",
            ),
        )

    assert exc_info.value.status_code == 409
    assert cursor.execute.call_count == 1
    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()


def test_quarantine_resolve_unknown_row_404s(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = None
    monkeypatch.setattr(admin_app, "_connect", lambda: connection)

    with pytest.raises(HTTPException) as exc_info:
        admin_app.resolve_quarantine(
            999,
            QuarantineResolveInput(expected_run_key="bounded-1", resolution=""),
        )
    assert exc_info.value.status_code == 404
    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()
    connection.close.assert_called_once_with()


def test_health_exercises_indexes_and_backoffice_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[tuple[str, tuple[object, ...]]] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> None:
        queries.append((sql, params))
        if "information_schema.COLUMNS" in sql:
            return {
                "current_column_ready": 1,
                "previous_column_ready": 1,
                "current_index_ready": 1,
                "previous_index_ready": 1,
                "current_foreign_key_ready": 1,
                "previous_foreign_key_ready": 1,
                "formal_view_ready": 1,
                "formal_view_columns_ready": 1,
                "desired_scope_columns_ready": 1,
                "bounded_snapshot_immutable_ready": 1,
            }

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)

    assert admin_app.health() == {"status": "ok"}
    assert "FORCE INDEX (idx_quarantine_list)" in queries[0][0]
    assert "FORCE INDEX (idx_quarantine_run_key_resolved_updated)" in queries[1][0]
    assert queries[1][1] == ("__health__",)
    readiness_sql = queries[2][0]
    for table in (
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
        "nhtsa_sync_runs",
        "nhtsa_source_artifacts",
        "nhtsa_current_artifacts",
        "nhtsa_vin_decodes",
        "admin_vehicle_mappings",
        "admin_part_translations",
        "admin_part_fitments",
        "admin_category_labels",
        "admin_reconciliation_items",
        "admin_crawl_requests",
        "scheduled_job_runs",
        "part_quarantine",
        "v_current_catalog_parts",
        "v_vin_part_fitments",
        "admin_override_heads",
        "station_admin_effective_parts",
    ):
        assert table in readiness_sql
    provenance_sql = queries[3][0]
    assert "idx_published_crawl_run" in provenance_sql
    assert "fk_published_crawl_run" in provenance_sql
    assert "fk_published_previous_crawl_run" in provenance_sql
    assert "formal_current_parts" not in provenance_sql
    assert "formal_previous_parts" not in provenance_sql
    assert "LOCATE('formal_full_parts', LOWER(VIEW_DEFINITION)) = 0" in provenance_sql
    assert "LOCATE('published_parts', LOWER(VIEW_DEFINITION)) = 0" in provenance_sql
    assert "qualified_full_runs" not in provenance_sql
    assert "full_scheduler_run" not in provenance_sql
    assert "bounded_parts" in provenance_sql
    assert "verified_bounded_evidence" in provenance_sql
    assert "verified_bounded_records" in provenance_sql
    assert "evidence_record_sha256" in provenance_sql
    assert "partsouq_http_artifacts" in provenance_sql
    assert "partsouq_artifact_records" in provenance_sql
    assert "evidence_status" in provenance_sql
    assert "live_http" in provenance_sql
    assert "source_crawl_run_id" in provenance_sql
    assert "catalog_desired_bounded_scope" in provenance_sql
    assert "desired_scope" in provenance_sql
    assert "scope_brand" in provenance_sql
    assert "scope_model" in provenance_sql
    assert "scope_vehicle_year_floor" in provenance_sql
    assert "provenance_count" not in provenance_sql
    assert "LIMIT 0" in readiness_sql


def test_health_fails_closed_when_published_provenance_schema_is_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    def fetch_one(sql: str, params: tuple[object, ...] = ()) -> dict[str, int]:
        if "information_schema.COLUMNS" in sql:
            return {
                "current_column_ready": 1,
                "previous_column_ready": 1,
                "current_index_ready": 1,
                "previous_index_ready": 1,
                "current_foreign_key_ready": 1,
                "previous_foreign_key_ready": 1,
                "formal_view_ready": 0,
                "formal_view_columns_ready": 1,
                "desired_scope_columns_ready": 1,
                "bounded_snapshot_immutable_ready": 1,
            }
        return {}

    monkeypatch.setattr(admin_app, "_fetch_one", fetch_one)

    with pytest.raises(HTTPException) as exc_info:
        admin_app.health()

    assert exc_info.value.status_code == 503
    assert "migration 033" in exc_info.value.detail


def test_database_summary_includes_quarantine_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {"quarantine_total": 5, "quarantine_unresolved": 3},
            {},
            {},
            {"row_count": 0},
            {},
            None,
            None,
        ]
    )

    def capture_one(sql: str, *_args: object, **_kwargs: object) -> dict | None:
        return next(responses)

    def capture_all(sql: str, *_args: object, **_kwargs: object) -> list[dict]:
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    summary = admin_app.database_summary()

    assert summary["quarantine"] == {"total": 5, "unresolved": 3}


def test_quarantine_http_accepts_pageSize_alias_and_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SOL review P1：前端送 pageSize / page 參數，HTTP 層必須真正接通
    （FastAPI Query alias），不能只作用在函式呼叫層。"""
    from fastapi.testclient import TestClient

    queries: list[tuple[str, tuple[object, ...]]] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        queries.append((sql, params))
        return {"n": 260}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        queries.append((sql, params))
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "test-token")

    with TestClient(admin_app.app) as client:
        response = client.get(
            "/api/quarantine?state=all&page=2&pageSize=200",
            headers={"X-Admin-Token": "test-token"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 2
    assert body["pageSize"] == 200
    assert body["total"] == 260
    assert body["totalPages"] == 2
    list_sql, list_params = queries[1]
    assert "LIMIT %s OFFSET %s" in list_sql
    assert list_params == (200, 200)
    assert "resolved_at IS NULL" not in queries[0][0]


def test_quarantine_http_page_size_error_lists_all_allowed_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SOL review round 9 P3：ALLOWED_PAGE_SIZES 含 30，錯誤訊息必須
    完整列出所有允許值（含 30），否則前端照訊息送 30 會被拒絕。"""
    from fastapi.testclient import TestClient

    from partsouq_admin.app import ALLOWED_PAGE_SIZES

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        return {"n": 0}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "test-token")

    with TestClient(admin_app.app) as client:
        response = client.get(
            "/api/quarantine?state=unresolved&pageSize=30",
            headers={"X-Admin-Token": "test-token"},
        )
        invalid = client.get(
            "/api/quarantine?state=unresolved&pageSize=7",
            headers={"X-Admin-Token": "test-token"},
        )

    assert response.status_code == 200
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    for size in sorted(ALLOWED_PAGE_SIZES):
        assert str(size) in detail
    assert "30" in detail


def test_quarantine_admin_html_has_pagination_ui_and_valid_js() -> None:
    """SOL review P1：前端必須有真正的分頁控制（頁碼、上一頁／下一頁、
    每頁筆數），且整份 inline JS 語法正確。"""
    html = Path("src/partsouq_admin/static/admin.html").read_text(encoding="utf-8")
    for element in (
        'id="quarantine-run-key"',
        'id="quarantine-page-size"',
        'id="quarantine-first"',
        'id="quarantine-prev"',
        'id="quarantine-page-number"',
        'id="quarantine-next"',
        'id="quarantine-last"',
        'id="quarantine-total-pages"',
        'id="quarantine-range-label"',
    ):
        assert element in html, f"admin.html 缺少 {element}"
    assert '<option value="30">30</option>' in html
    assert "expected_run_key: row.run_key" in html
    script = html.split("<script>")[1].split("</script>")[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        js_path = fh.name
    try:
        result = subprocess.run(["node", "--check", js_path], capture_output=True, text=True)
    finally:
        Path(js_path).unlink()
    assert result.returncode == 0, result.stderr
