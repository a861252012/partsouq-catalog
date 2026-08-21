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


def test_quarantine_list_all_state_has_no_resolved_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def capture_one(sql: str, params: tuple[object, ...] = ()) -> dict:
        captured.append(sql)
        return {"n": 0}

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        return []

    monkeypatch.setattr(admin_app, "_fetch_one", capture_one)
    monkeypatch.setattr(admin_app, "_fetch_all", capture_all)

    admin_app.list_quarantine(state="all", page=1, page_size=10)

    assert "resolved_at IS NULL" not in captured[0]


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
