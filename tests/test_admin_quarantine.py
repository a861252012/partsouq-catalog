from __future__ import annotations

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
    assert "FORCE INDEX (idx_quarantine_run_key_updated)" in list_sql
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
    assert "FORCE INDEX (idx_quarantine_run_key_updated)" in captured[1]
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
    executed: list[tuple[str, tuple[object, ...]]] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        return {"id": 7, "part_number": "IMG10001", "resolved_at": None}

    def capture_execute(sql: str, params: tuple[object, ...]) -> int:
        executed.append((sql, params))
        return 1

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_execute", capture_execute)

    row = admin_app.resolve_quarantine(
        7, QuarantineResolveInput(resolution="checked: site removed")
    )

    assert row == {"id": 7, "part_number": "IMG10001", "resolved_at": None}
    update_sql, update_params = executed[0]
    assert "SET resolved_at = NOW(), resolution = %s" in update_sql
    assert update_params == ("checked: site removed", 7)


def test_quarantine_resolve_unknown_row_404s(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(admin_app, "_fetch_one", lambda sql, params=(): None)

    with pytest.raises(HTTPException) as exc_info:
        admin_app.resolve_quarantine(999, QuarantineResolveInput(resolution=""))
    assert exc_info.value.status_code == 404


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


def test_quarantine_admin_html_has_pagination_ui_and_valid_js() -> None:
    """SOL review P1：前端必須有真正的分頁控制（頁碼、上一頁／下一頁、
    每頁筆數），且整份 inline JS 語法正確。"""
    import subprocess
    import tempfile

    html_path = "src/partsouq_admin/static/admin.html"
    html = open(html_path, encoding="utf-8").read()
    for element in (
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
    script = html.split("<script>")[1].split("</script>")[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        js_path = fh.name
    try:
        result = subprocess.run(["node", "--check", js_path], capture_output=True, text=True)
    finally:
        import os

        os.unlink(js_path)
    assert result.returncode == 0, result.stderr
