from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterator

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

pytestmark = pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL quarantine plan tests",
)

RUN_KEY_RESOLVED_UPDATED_INDEX = "idx_quarantine_run_key_resolved_updated"


def _connect() -> Connection[DictCursor]:
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


def _explain_part_quarantine_rows_examined(
    connection: Connection[DictCursor],
    sql: str,
    params: tuple[object, ...],
) -> int:
    plan = _explain_json(connection, sql, params)
    rows: list[int] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("table_name") == "part_quarantine":
                examined = value.get("rows_examined_per_scan")
                if isinstance(examined, (int, float)):
                    rows.append(int(examined))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan)
    return max(rows) if rows else -1


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
    id having been produced, so a partial seed never masks the original error
    and never leaves orphaned rows."""
    with connection.cursor() as cursor:
        if "group_id" in rows:
            cursor.execute("DELETE FROM part_quarantine WHERE group_id = %s", (rows["group_id"],))
            cursor.execute("DELETE FROM groups_t WHERE id = %s", (rows["group_id"],))
        if "category_id" in rows:
            cursor.execute("DELETE FROM categories WHERE id = %s", (rows["category_id"],))
        if "vehicle_id" in rows:
            cursor.execute("DELETE FROM vehicles WHERE id = %s", (rows["vehicle_id"],))
        if "model_id" in rows:
            cursor.execute("DELETE FROM models WHERE id = %s", (rows["model_id"],))
        if "brand_id" in rows:
            cursor.execute("DELETE FROM brands WHERE id = %s", (rows["brand_id"],))


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
    connection = seeded_quarantine_rows["connection"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(DISTINCT INDEX_NAME) AS n FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
            "AND INDEX_NAME = %s",
            (RUN_KEY_RESOLVED_UPDATED_INDEX,),
        )
        assert cursor.fetchone()["n"] == 1
        cursor.execute(
            "SELECT COUNT(DISTINCT INDEX_NAME) AS n FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
            "AND INDEX_NAME = 'idx_quarantine_run_key_updated'"
        )
        assert cursor.fetchone()["n"] == 0


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


def test_admin_all_state_run_key_path_uses_filesort(
    seeded_quarantine_rows: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = seeded_quarantine_rows["connection"]
    run_key = str(seeded_quarantine_rows["run_key"])
    sql, params = _admin_list_quarantine_sql(monkeypatch, "all", run_key)
    assert "FORCE INDEX" not in sql
    assert "STRAIGHT_JOIN" not in sql
    assert _explain_json_has_filesort(connection, sql, params)


def test_station_admin_all_state_run_key_path_uses_filesort(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    connection = seeded_quarantine_rows["connection"]
    run_key = str(seeded_quarantine_rows["run_key"])
    sql, params = _station_list_quarantine_sql("all", run_key)
    assert "FORCE INDEX" not in sql
    assert "STRAIGHT_JOIN" not in sql
    assert _explain_json_has_filesort(connection, sql, params)


def test_admin_all_state_keeps_unresolved_first(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    for run_key in (None, str(seeded_quarantine_rows["run_key"])):
        rows = data_admin_app.list_quarantine(state="all", run_key=run_key, page=1, page_size=50)
        part_numbers = [row["part_number"] for row in rows["items"]]
        assert seeded_quarantine_rows["unresolved_number"] in part_numbers
        assert seeded_quarantine_rows["resolved_number"] in part_numbers
        assert part_numbers.index(
            str(seeded_quarantine_rows["unresolved_number"])
        ) < part_numbers.index(str(seeded_quarantine_rows["resolved_number"]))


def test_station_admin_all_state_keeps_unresolved_first(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    database = AdminDatabase.connect(_admin_config(), QueryTrace())
    try:
        for run_key in (None, str(seeded_quarantine_rows["run_key"])):
            page = AdminRepository(database).list_quarantine(
                state="all", run_key=run_key, page=1, limit=50
            )
            part_numbers = [row["part_number"] for row in page["items"]]
            assert seeded_quarantine_rows["unresolved_number"] in part_numbers
            assert seeded_quarantine_rows["resolved_number"] in part_numbers
            assert part_numbers.index(
                str(seeded_quarantine_rows["unresolved_number"])
            ) < part_numbers.index(str(seeded_quarantine_rows["resolved_number"]))
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
    assert _explain_part_quarantine_rows_examined(connection, sql, params) == 1


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
    assert _explain_part_quarantine_rows_examined(connection, sql, params) == 1
