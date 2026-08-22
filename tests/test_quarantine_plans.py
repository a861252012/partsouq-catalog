from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import uuid
from collections.abc import Iterator
from unittest import mock

import pymysql
import pytest
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from partsouq_admin import app as data_admin_app
from partsouq_catalog.config import DB_CONFIG
from partsouq_station_admin.config import AdminConfig
from partsouq_station_admin.db import AdminDatabase
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import AdminRepository

RUN_KEY_RESOLVED_UPDATED_INDEX = "idx_quarantine_run_key_resolved_updated"


def _connect() -> Connection[DictCursor]:
    if os.getenv("UNIFIED_TEST_MYSQL") != "1":
        pytest.skip("set UNIFIED_TEST_MYSQL=1 to run shared MySQL quarantine plan tests")
    database_name = str(DB_CONFIG["database"])
    if not database_name.endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")
    return pymysql.connect(
        host=str(DB_CONFIG["host"]),
        port=int(DB_CONFIG["port"]),
        user=str(DB_CONFIG["user"]),
        password=str(DB_CONFIG["password"]),
        database=database_name,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
    )


def _admin_config() -> AdminConfig:
    return AdminConfig(
        mysql_host=str(DB_CONFIG["host"]),
        mysql_port=int(DB_CONFIG["port"]),
        mysql_user=str(DB_CONFIG["user"]),
        mysql_password=str(DB_CONFIG["password"]),
        mysql_database=str(DB_CONFIG["database"]),
        bind_host="127.0.0.1",
        bind_port=0,
        secret_key="plan-test-secret",
        default_actor="plan-test",
        page_size=30,
    )


def _explain_json(
    connection: Connection[DictCursor],
    sql: str,
    params: tuple[object, ...],
) -> object:
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN FORMAT=JSON " + sql, params)
        return json.loads(str((cursor.fetchone() or {}).get("EXPLAIN", "{}")))


def _explain_json_has_filesort(
    connection: Connection[DictCursor],
    sql: str,
    params: tuple[object, ...],
) -> bool:
    plan = _explain_json(connection, sql, params)
    found = False

    def visit(value: object) -> None:
        nonlocal found
        if isinstance(value, dict):
            if value.get("using_filesort") is True:
                found = True
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan)
    return found


def _explain_plan_keys(
    connection: Connection[DictCursor],
    sql: str,
    params: tuple[object, ...],
) -> set[str]:
    plan = _explain_json(connection, sql, params)
    keys: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            key = value.get("key")
            if isinstance(key, str):
                keys.add(key)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan)
    return keys


def _explain_analyze_part_quarantine_rows(
    connection: Connection[DictCursor],
    sql: str,
    params: tuple[object, ...],
) -> int:
    """Run EXPLAIN ANALYZE and return part_quarantine rows across all loops.

    Unlike EXPLAIN FORMAT=JSON's rows_examined_per_scan, rows * loops is an
    executed measurement rather than an optimizer estimate.
    """
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN ANALYZE " + sql, params)
        text = str(cursor.fetchone()["EXPLAIN"])
    examined: list[float] = []
    number = r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    for line in text.splitlines():
        if re.search(r"\bon part_quarantine\b", line) is None:
            continue
        match = re.search(
            rf"\(actual time={number}\.\.{number} rows=({number}) loops=({number})\)",
            line,
        )
        if match:
            examined.append(float(match.group(1)) * float(match.group(2)))
    return math.ceil(sum(examined)) if examined else -1


def _seed_chain(
    connection: Connection[DictCursor],
    prefix: str,
    suffix: str,
    cursor_row: dict[str, object],
) -> None:
    """Insert brands -> models -> vehicles -> categories -> groups_t and record
    every generated id immediately so a mid-seed failure still allows
    conditional cleanup of the rows already inserted."""
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO brands(name, code, url) VALUES (%s, %s, %s)",
            (
                f"{prefix} MOTORS {suffix}",
                f"{prefix}B{suffix}",
                f"https://partsouq.example/{prefix}",
            ),
        )
        cursor_row["brand_id"] = cursor.lastrowid
        cursor.execute(
            "INSERT INTO models(brand_id, name, ssd, url, fetched_at) "
            "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP())",
            (
                cursor_row["brand_id"],
                f"{prefix} MODEL {suffix}",
                f"model-{suffix}",
                f"https://partsouq.example/{prefix}",
            ),
        )
        cursor_row["model_id"] = cursor.lastrowid
        cursor.execute(
            "INSERT INTO vehicles("
            "model_id, identity_hash, name, model_code, prod_period, "
            "production_from, production_to, engine, grade, ssd, vid, url, fetched_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())",
            (
                cursor_row["model_id"],
                hashlib.sha256(f"{prefix}-vehicle-{suffix}".encode()).hexdigest(),
                f"{prefix} VEHICLE {suffix}",
                f"{prefix}-{suffix}",
                "01.2020 - 12.2025",
                "2020-01",
                "2025-12",
                f"{prefix} ENGINE",
                f"{prefix} TRIM",
                f"vehicle-{suffix}",
                f"{prefix}VID{suffix}",
                f"https://partsouq.example/{prefix}",
            ),
        )
        cursor_row["vehicle_id"] = cursor.lastrowid
        cursor.execute(
            "INSERT INTO categories(vehicle_id, name, cid, fetched_at) "
            "VALUES (%s, %s, %s, UTC_TIMESTAMP())",
            (cursor_row["vehicle_id"], f"{prefix} CATEGORY {suffix}", f"{prefix}C{suffix}"),
        )
        cursor_row["category_id"] = cursor.lastrowid
        cursor.execute(
            "INSERT INTO groups_t("
            "category_id, code, name, uid, url, fetched_at, fetched_status, "
            "fetched_row_count, verified_row_count"
            ") VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(), %s, %s, %s)",
            (
                cursor_row["category_id"],
                f"{prefix}G{suffix}",
                f"{prefix} GROUP {suffix}",
                f"{prefix}UID{suffix}",
                f"https://partsouq.example/{prefix}",
                "done",
                1000,
                1000,
            ),
        )
        cursor_row["group_id"] = cursor.lastrowid


def _cleanup_chain(connection: Connection[DictCursor], rows: dict[str, object]) -> None:
    """Delete seeded rows child-first; every statement is conditional on the
    id having been produced, so a partial seed never leaves orphaned rows.
    Each statement is isolated: a failed DELETE records the error and the
    remaining tables are still cleaned up. Cleanup errors are re-raised only
    when no original exception is in flight; otherwise they are attached to
    the original error as a note instead of masking it."""
    cleanup_errors: list[str] = []
    statements: list[tuple[str, str, tuple[object, ...]]] = []
    if "group_id" in rows:
        statements.append(
            (
                "part_quarantine",
                "DELETE FROM part_quarantine WHERE group_id = %s",
                (rows["group_id"],),
            )
        )
        statements.append(("groups_t", "DELETE FROM groups_t WHERE id = %s", (rows["group_id"],)))
    if "category_id" in rows:
        statements.append(
            ("categories", "DELETE FROM categories WHERE id = %s", (rows["category_id"],))
        )
    if "vehicle_id" in rows:
        statements.append(("vehicles", "DELETE FROM vehicles WHERE id = %s", (rows["vehicle_id"],)))
    if "model_id" in rows:
        statements.append(("models", "DELETE FROM models WHERE id = %s", (rows["model_id"],)))
    if "brand_id" in rows:
        statements.append(("brands", "DELETE FROM brands WHERE id = %s", (rows["brand_id"],)))
    with connection.cursor() as cursor:
        for table, statement, params in statements:
            try:
                cursor.execute(statement, params)
            except Exception as error:
                cleanup_errors.append(f"{table}: {error}")
    if cleanup_errors:
        message = "cleanup failed: " + "; ".join(cleanup_errors)
        original_error = sys.exception()
        if original_error is not None:
            original_error.add_note(message)
        else:
            raise RuntimeError(message)


@pytest.fixture
def seeded_quarantine_rows() -> Iterator[dict[str, object]]:
    """Seed one unresolved (older updated_at) and one resolved (newer
    updated_at) row under a unique run_key so ordering/plan assertions are
    meaningful for both the unfiltered and run_key-filtered paths."""
    connection = _connect()
    suffix = uuid.uuid4().hex[:8]
    run_key = f"plan-reg-{suffix}"
    rows: dict[str, object] = {"connection": connection, "run_key": run_key}
    try:
        _seed_chain(connection, "PLANQ", suffix, rows)
        unresolved_number = f"PLANQ-UNRESOLVED-{suffix}"
        resolved_number = f"PLANQ-RESOLVED-{suffix}"
        rows["unresolved_number"] = unresolved_number
        rows["resolved_number"] = resolved_number
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key, resolved_at, "
                "resolution, updated_at"
                ") VALUES (%s, %s, %s, %s, %s, NULL, NULL, "
                "UTC_TIMESTAMP() - INTERVAL 1 DAY)",
                (rows["group_id"], unresolved_number, "PLAN-RANGE", "nameless", run_key),
            )
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key, resolved_at, "
                "resolution, updated_at"
                ") VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(), 'resolved in plan test', "
                "UTC_TIMESTAMP())",
                (rows["group_id"], resolved_number, "PLAN-RANGE", "nameless", run_key),
            )
        yield rows
    finally:
        try:
            _cleanup_chain(connection, rows)
        finally:
            connection.close()


@pytest.fixture
def seeded_skewed_quarantine_rows() -> Iterator[dict[str, object]]:
    """Seed 2000 resolved + 1 unresolved row under one run_key: the skewed
    scenario from SOL review round 8 where a run_key index lacking resolved_at
    forces the unresolved query to scan every resolved row."""
    connection = _connect()
    suffix = uuid.uuid4().hex[:8]
    run_key = f"plan-skew-{suffix}"
    rows: dict[str, object] = {"connection": connection, "run_key": run_key}
    try:
        _seed_chain(connection, "PLANS", suffix, rows)
        rows["open_number"] = f"PLANS-OPEN-{suffix}"
        with connection.cursor() as cursor:
            for i in range(2000):
                cursor.execute(
                    "INSERT INTO part_quarantine("
                    "group_id, part_number, range_str, reason, run_key, resolved_at, "
                    "resolution, updated_at"
                    ") VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(), 'resolved', "
                    "UTC_TIMESTAMP())",
                    (
                        rows["group_id"],
                        f"PLANS-RESOLVED-{i:04d}-{suffix}",
                        "PLAN-RANGE",
                        "nameless",
                        run_key,
                    ),
                )
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key, resolved_at, "
                "resolution, updated_at"
                ") VALUES (%s, %s, %s, %s, %s, NULL, NULL, "
                "UTC_TIMESTAMP() - INTERVAL 1 DAY)",
                (rows["group_id"], rows["open_number"], "PLAN-RANGE", "nameless", run_key),
            )
        yield rows
    finally:
        try:
            _cleanup_chain(connection, rows)
        finally:
            connection.close()


def _admin_list_quarantine_sql(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    run_key: str | None,
) -> tuple[str, tuple[object, ...]]:
    captured: list[tuple[str, tuple[object, ...]]] = []

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        if sql.startswith("SELECT COUNT"):
            return [{"n": 2}]
        captured.append((sql, params))
        return []

    monkeypatch.setattr(data_admin_app, "_fetch_all", capture_all)
    data_admin_app.list_quarantine(state=state, run_key=run_key, page=1, page_size=10)
    list_calls = [call for call in captured if call[0].startswith("SELECT part_quarantine")]
    assert len(list_calls) == 1
    return list_calls[0]


def _station_list_quarantine_sql(state: str, run_key: str | None) -> tuple[str, tuple[object, ...]]:
    database = AdminDatabase.connect(_admin_config(), QueryTrace())
    captured: list[tuple[str, tuple[object, ...]]] = []
    original_fetch_all = database.fetch_all

    def capture_fetch_all(
        tag: str,
        sql: str,
        params: tuple[object, ...] | None = None,
    ) -> list[dict[str, object]]:
        if tag == "quarantine.list":
            captured.append((sql, tuple(params or ())))
        return original_fetch_all(tag, sql, params)

    database.fetch_all = capture_fetch_all  # type: ignore[method-assign]
    repository = AdminRepository(database)
    try:
        repository.list_quarantine(state=state, run_key=run_key, page=1, limit=10)
    finally:
        database.close()
    assert len(captured) == 1
    return captured[0]


def test_quarantine_index_preflight(seeded_quarantine_rows: dict[str, object]) -> None:
    """Both FORCE INDEX names used by app.py and repository.py must exist
    with their exact ordered, non-unique, unprefixed, ascending BTREE shape
    and stay visible; all superseded quarantine indexes must be gone."""
    connection = seeded_quarantine_rows["connection"]
    expected = {
        "idx_quarantine_list": ["resolved_at", "updated_at"],
        RUN_KEY_RESOLVED_UPDATED_INDEX: ["run_key", "resolved_at", "updated_at"],
    }
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE, SUB_PART, COLLATION, "
            "INDEX_TYPE, IS_VISIBLE, EXPRESSION "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
            "AND INDEX_NAME IN ("
            "'idx_quarantine_list', "
            "'idx_quarantine_run_key_resolved_updated', "
            "'idx_quarantine_run_key_updated', "
            "'idx_quarantine_resolved', "
            "'idx_quarantine_group'"
            ") ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        )
        index_rows = cursor.fetchall()

    actual: dict[str, list[str]] = {}
    for row in index_rows:
        index_name = str(row["INDEX_NAME"])
        actual.setdefault(index_name, []).append(str(row["COLUMN_NAME"]))
        assert row["NON_UNIQUE"] == 1, index_name
        assert row["SUB_PART"] is None, index_name
        assert row["COLLATION"] == "A", index_name
        assert row["INDEX_TYPE"] == "BTREE", index_name
        assert row["IS_VISIBLE"] == "YES", index_name
        assert row["EXPRESSION"] is None, index_name
    assert actual == expected


def test_explain_analyze_uses_total_executed_part_quarantine_rows() -> None:
    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {
        "EXPLAIN": (
            "-> Index lookup on groups_t (actual time=0.01..0.02 rows=1 loops=1)\n"
            "    -> Filter: (part_quarantine.resolved_at is null) "
            "(actual time=0.01..0.02 rows=99 loops=1)\n"
            "    -> Index lookup on part_quarantine "
            "(actual time=934e-6..1.2e-3 rows=5e-1 loops=3.4e1)\n"
            "    -> Table scan on part_quarantine "
            "(actual time=0.01..0.02 rows=1 loops=2)"
        )
    }

    examined = _explain_analyze_part_quarantine_rows(
        connection,
        "SELECT * FROM part_quarantine WHERE run_key = %s",
        ("run-1",),
    )

    assert examined == 19
    cursor.execute.assert_called_once_with(
        "EXPLAIN ANALYZE SELECT * FROM part_quarantine WHERE run_key = %s",
        ("run-1",),
    )


def test_cleanup_chain_raises_when_no_original_error() -> None:
    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = RuntimeError("delete failed")

    with pytest.raises(RuntimeError, match="cleanup failed: part_quarantine: delete failed"):
        _cleanup_chain(connection, {"group_id": 1})

    assert cursor.execute.call_count == 2


def test_cleanup_chain_preserves_original_error_with_note() -> None:
    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = [
        RuntimeError("quarantine delete failed"),
        RuntimeError("group delete failed"),
        None,
        None,
        None,
        None,
    ]
    rows = {
        "group_id": 1,
        "category_id": 2,
        "vehicle_id": 3,
        "model_id": 4,
        "brand_id": 5,
    }

    with pytest.raises(ValueError, match="seed failed") as captured:
        try:
            raise ValueError("seed failed")
        finally:
            _cleanup_chain(connection, rows)

    assert cursor.execute.call_args_list == [
        mock.call("DELETE FROM part_quarantine WHERE group_id = %s", (1,)),
        mock.call("DELETE FROM groups_t WHERE id = %s", (1,)),
        mock.call("DELETE FROM categories WHERE id = %s", (2,)),
        mock.call("DELETE FROM vehicles WHERE id = %s", (3,)),
        mock.call("DELETE FROM models WHERE id = %s", (4,)),
        mock.call("DELETE FROM brands WHERE id = %s", (5,)),
    ]
    assert captured.value.__notes__ == [
        "cleanup failed: part_quarantine: quarantine delete failed; groups_t: group delete failed"
    ]


def test_admin_unresolved_queries_have_no_filesort(
    seeded_quarantine_rows: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = seeded_quarantine_rows["connection"]
    run_key = str(seeded_quarantine_rows["run_key"])

    plain_sql, plain_params = _admin_list_quarantine_sql(monkeypatch, "unresolved", None)
    run_key_sql, run_key_params = _admin_list_quarantine_sql(monkeypatch, "unresolved", run_key)

    assert not _explain_json_has_filesort(connection, plain_sql, plain_params)
    assert not _explain_json_has_filesort(connection, run_key_sql, run_key_params)
    assert "idx_quarantine_list" in _explain_plan_keys(connection, plain_sql, plain_params)
    assert RUN_KEY_RESOLVED_UPDATED_INDEX in _explain_plan_keys(
        connection, run_key_sql, run_key_params
    )


def test_station_admin_unresolved_queries_have_no_filesort(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    connection = seeded_quarantine_rows["connection"]
    run_key = str(seeded_quarantine_rows["run_key"])

    plain_sql, plain_params = _station_list_quarantine_sql("unresolved", None)
    run_key_sql, run_key_params = _station_list_quarantine_sql("unresolved", run_key)

    assert not _explain_json_has_filesort(connection, plain_sql, plain_params)
    assert not _explain_json_has_filesort(connection, run_key_sql, run_key_params)
    assert "idx_quarantine_list" in _explain_plan_keys(connection, plain_sql, plain_params)
    assert RUN_KEY_RESOLVED_UPDATED_INDEX in _explain_plan_keys(
        connection, run_key_sql, run_key_params
    )


def test_admin_all_state_run_key_does_not_force_plan(
    seeded_quarantine_rows: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state=all keeps unresolved-first ordering on a low-frequency history
    view; the design accepts a filesort here, so this pins only the absence
    of FORCE INDEX / STRAIGHT_JOIN hints and the ordering semantics — not
    the presence of filesort, which may legitimately disappear as data
    volume grows or the optimizer improves."""
    run_key = str(seeded_quarantine_rows["run_key"])
    sql, params = _admin_list_quarantine_sql(monkeypatch, "all", run_key)
    assert "FORCE INDEX" not in sql
    assert "STRAIGHT_JOIN" not in sql


def test_station_admin_all_state_run_key_does_not_force_plan(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    """state=all keeps unresolved-first ordering on a low-frequency history
    view; the design accepts a filesort here, so this pins only the absence
    of FORCE INDEX / STRAIGHT_JOIN hints and the ordering semantics — not
    the presence of filesort, which may legitimately disappear as data
    volume grows or the optimizer improves."""
    run_key = str(seeded_quarantine_rows["run_key"])
    sql, params = _station_list_quarantine_sql("all", run_key)
    assert "FORCE INDEX" not in sql
    assert "STRAIGHT_JOIN" not in sql


def test_admin_all_state_keeps_unresolved_first(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    expected = [
        seeded_quarantine_rows["unresolved_number"],
        seeded_quarantine_rows["resolved_number"],
    ]
    for run_key in (None, str(seeded_quarantine_rows["run_key"])):
        rows = data_admin_app.list_quarantine(
            state="all",
            run_key=run_key,
            page=1,
            page_size=200,
        )
        part_numbers = [
            row["part_number"] for row in rows["items"] if row["part_number"] in expected
        ]
        assert part_numbers == expected


def test_admin_quarantine_resolve_rejects_stale_run_then_commits_current_run(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    connection = seeded_quarantine_rows["connection"]
    run_key = str(seeded_quarantine_rows["run_key"])
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM part_quarantine WHERE part_number = %s",
            (seeded_quarantine_rows["unresolved_number"],),
        )
        row_id = int(cursor.fetchone()["id"])

    with pytest.raises(data_admin_app.HTTPException) as exc_info:
        data_admin_app.resolve_quarantine(
            row_id,
            data_admin_app.QuarantineResolveInput(
                expected_run_key=f"{run_key}-stale",
                resolution="stale request",
            ),
        )
    assert exc_info.value.status_code == 409

    with connection.cursor() as cursor:
        cursor.execute("SELECT resolved_at FROM part_quarantine WHERE id = %s", (row_id,))
        assert cursor.fetchone()["resolved_at"] is None

    resolved = data_admin_app.resolve_quarantine(
        row_id,
        data_admin_app.QuarantineResolveInput(
            expected_run_key=run_key,
            resolution="verified current run",
        ),
    )
    assert resolved["resolved_at"] is not None
    assert resolved["resolution"] == "verified current run"


def test_station_admin_all_state_keeps_unresolved_first(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    database = AdminDatabase.connect(_admin_config(), QueryTrace())
    try:
        expected = [
            seeded_quarantine_rows["unresolved_number"],
            seeded_quarantine_rows["resolved_number"],
        ]
        for run_key in (None, str(seeded_quarantine_rows["run_key"])):
            page = AdminRepository(database).list_quarantine(
                state="all",
                run_key=run_key,
                page=1,
                limit=200,
            )
            part_numbers = [
                row["part_number"] for row in page["items"] if row["part_number"] in expected
            ]
            assert part_numbers == expected
    finally:
        database.close()


def test_admin_unresolved_run_key_skewed_data_scans_only_open_rows(
    seeded_skewed_quarantine_rows: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = seeded_skewed_quarantine_rows["connection"]
    run_key = str(seeded_skewed_quarantine_rows["run_key"])
    rows = data_admin_app.list_quarantine(state="unresolved", run_key=run_key, page=1, page_size=50)
    part_numbers = [row["part_number"] for row in rows["items"]]
    assert part_numbers == [str(seeded_skewed_quarantine_rows["open_number"])]
    sql, params = _admin_list_quarantine_sql(monkeypatch, "unresolved", run_key)
    assert not _explain_json_has_filesort(connection, sql, params)
    assert _explain_analyze_part_quarantine_rows(connection, sql, params) == 1


def test_station_admin_unresolved_run_key_skewed_data_scans_only_open_rows(
    seeded_skewed_quarantine_rows: dict[str, object],
) -> None:
    connection = seeded_skewed_quarantine_rows["connection"]
    run_key = str(seeded_skewed_quarantine_rows["run_key"])
    database = AdminDatabase.connect(_admin_config(), QueryTrace())
    try:
        page = AdminRepository(database).list_quarantine(
            state="unresolved", run_key=run_key, page=1, limit=50
        )
    finally:
        database.close()
    part_numbers = [row["part_number"] for row in page["items"]]
    assert part_numbers == [str(seeded_skewed_quarantine_rows["open_number"])]
    sql, params = _station_list_quarantine_sql("unresolved", run_key)
    assert not _explain_json_has_filesort(connection, sql, params)
    assert _explain_analyze_part_quarantine_rows(connection, sql, params) == 1
