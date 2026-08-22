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


def _explain_json_has_filesort(
    connection: Connection[DictCursor],
    sql: str,
    params: tuple[object, ...],
) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN FORMAT=JSON " + sql, params)
        plan = json.loads(str((cursor.fetchone() or {}).get("EXPLAIN", "{}")))
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
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN FORMAT=JSON " + sql, params)
        plan = json.loads(str((cursor.fetchone() or {}).get("EXPLAIN", "{}")))
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


@pytest.fixture
def seeded_quarantine_rows() -> Iterator[dict[str, object]]:
    """Seed one unresolved (older updated_at) and one resolved (newer
    updated_at) row so ordering/plan assertions are meaningful."""
    connection = _connect()
    suffix = uuid.uuid4().hex[:8]
    run_key = f"plan-reg-{suffix}"
    rows: dict[str, object] = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO brands(name, code, url) VALUES (%s, %s, %s)",
                (f"PLAN MOTORS {suffix}", f"PLANB{suffix}", "https://partsouq.example/plan"),
            )
            brand_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO models(brand_id, name, ssd, url, fetched_at) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP())",
                (
                    brand_id,
                    f"PLAN MODEL {suffix}",
                    f"model-{suffix}",
                    "https://partsouq.example/plan",
                ),
            )
            model_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO vehicles("
                "model_id, identity_hash, name, model_code, prod_period, "
                "production_from, production_to, engine, grade, ssd, vid, url, fetched_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())",
                (
                    model_id,
                    hashlib.sha256(f"plan-vehicle-{suffix}".encode()).hexdigest(),
                    f"PLAN VEHICLE {suffix}",
                    f"PLAN-{suffix}",
                    "01.2020 - 12.2025",
                    "2020-01",
                    "2025-12",
                    "PLAN ENGINE",
                    "PLAN TRIM",
                    f"vehicle-{suffix}",
                    f"PLANVID{suffix}",
                    "https://partsouq.example/plan",
                ),
            )
            vehicle_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO categories(vehicle_id, name, cid, fetched_at) "
                "VALUES (%s, %s, %s, UTC_TIMESTAMP())",
                (vehicle_id, f"PLAN CATEGORY {suffix}", f"PLANC{suffix}"),
            )
            category_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO groups_t("
                "category_id, code, name, uid, url, fetched_at, fetched_status, "
                "fetched_row_count, verified_row_count"
                ") VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(), %s, %s, %s)",
                (
                    category_id,
                    f"PLANG{suffix}",
                    f"PLAN GROUP {suffix}",
                    f"PLANUID{suffix}",
                    "https://partsouq.example/plan",
                    "done",
                    1000,
                    1000,
                ),
            )
            group_id = cursor.lastrowid
            unresolved_number = f"PLANQ-UNRESOLVED-{suffix}"
            resolved_number = f"PLANQ-RESOLVED-{suffix}"
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key, resolved_at, "
                "resolution, updated_at"
                ") VALUES (%s, %s, %s, %s, %s, NULL, NULL, "
                "UTC_TIMESTAMP() - INTERVAL 1 DAY)",
                (group_id, unresolved_number, "PLAN-RANGE", "nameless", run_key),
            )
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key, resolved_at, "
                "resolution, updated_at"
                ") VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(), 'resolved in plan test', "
                "UTC_TIMESTAMP())",
                (group_id, resolved_number, "PLAN-RANGE", "nameless", run_key),
            )
        rows = {
            "connection": connection,
            "run_key": run_key,
            "group_id": group_id,
            "brand_id": brand_id,
            "model_id": model_id,
            "vehicle_id": vehicle_id,
            "category_id": category_id,
            "unresolved_number": unresolved_number,
            "resolved_number": resolved_number,
        }
        yield rows
    finally:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM part_quarantine WHERE run_key = %s", (run_key,))
            cursor.execute("DELETE FROM groups_t WHERE id = %s", (rows["group_id"],))
            cursor.execute("DELETE FROM categories WHERE id = %s", (rows["category_id"],))
            cursor.execute("DELETE FROM vehicles WHERE id = %s", (rows["vehicle_id"],))
            cursor.execute("DELETE FROM models WHERE id = %s", (rows["model_id"],))
            cursor.execute("DELETE FROM brands WHERE id = %s", (rows["brand_id"],))
        connection.close()


def test_quarantine_index_preflight(seeded_quarantine_rows: dict[str, object]) -> None:
    connection = seeded_quarantine_rows["connection"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(DISTINCT INDEX_NAME) AS n FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
            "AND INDEX_NAME = 'idx_quarantine_run_key_updated'"
        )
        assert cursor.fetchone()["n"] == 1


def test_admin_unresolved_queries_have_no_filesort(
    seeded_quarantine_rows: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = seeded_quarantine_rows["connection"]
    run_key = str(seeded_quarantine_rows["run_key"])
    captured: list[tuple[str, tuple[object, ...]]] = []

    def capture_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
        if sql.startswith("SELECT COUNT"):
            return [{"n": 2}]
        captured.append((sql, params))
        return []

    monkeypatch.setattr(data_admin_app, "_fetch_all", capture_all)

    data_admin_app.list_quarantine(state="unresolved", page=1, page_size=10)
    data_admin_app.list_quarantine(state="unresolved", run_key=run_key, page=1, page_size=10)

    for sql, params in captured:
        assert not _explain_json_has_filesort(connection, sql, params), sql
    assert "idx_quarantine_list" in _explain_plan_keys(connection, captured[0][0], captured[0][1])
    assert "idx_quarantine_run_key_updated" in _explain_plan_keys(
        connection, captured[1][0], captured[1][1]
    )


def test_station_admin_unresolved_queries_have_no_filesort(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    connection = seeded_quarantine_rows["connection"]
    run_key = str(seeded_quarantine_rows["run_key"])
    database = AdminDatabase.connect(
        AdminConfig(
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
        ),
        QueryTrace(),
    )
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
        repository.list_quarantine(state="unresolved", page=1, limit=10)
        repository.list_quarantine(state="unresolved", run_key=run_key, page=1, limit=10)
    finally:
        database.close()

    assert len(captured) == 2
    for sql, params in captured:
        assert not _explain_json_has_filesort(connection, sql, params), sql
    assert "idx_quarantine_list" in _explain_plan_keys(connection, captured[0][0], captured[0][1])
    assert "idx_quarantine_run_key_updated" in _explain_plan_keys(
        connection, captured[1][0], captured[1][1]
    )


def test_admin_all_state_keeps_unresolved_first(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    rows = data_admin_app.list_quarantine(state="all", page=1, page_size=50)
    part_numbers = [row["part_number"] for row in rows["items"]]
    assert seeded_quarantine_rows["unresolved_number"] in part_numbers
    assert seeded_quarantine_rows["resolved_number"] in part_numbers
    assert part_numbers.index(
        str(seeded_quarantine_rows["unresolved_number"])
    ) < part_numbers.index(str(seeded_quarantine_rows["resolved_number"]))


def test_station_admin_all_state_keeps_unresolved_first(
    seeded_quarantine_rows: dict[str, object],
) -> None:
    database = AdminDatabase.connect(
        AdminConfig(
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
        ),
        QueryTrace(),
    )
    try:
        page = AdminRepository(database).list_quarantine(state="all", page=1, limit=50)
    finally:
        database.close()
    part_numbers = [row["part_number"] for row in page["items"]]
    assert seeded_quarantine_rows["unresolved_number"] in part_numbers
    assert seeded_quarantine_rows["resolved_number"] in part_numbers
    assert part_numbers.index(
        str(seeded_quarantine_rows["unresolved_number"])
    ) < part_numbers.index(str(seeded_quarantine_rows["resolved_number"]))
