from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pymysql
import pytest
from fastapi.testclient import TestClient
from pymysql.cursors import DictCursor

from partsouq_admin import app as data_admin_app
from partsouq_catalog.config import DB_CONFIG
from partsouq_catalog.db import Database
from partsouq_catalog.repositories import (
    BrandRepository,
    CrawlRepository,
    PartRepository,
    VehicleRepository,
)
from partsouq_station_admin.app import create_app
from partsouq_station_admin.config import AdminConfig
from partsouq_station_admin.db import AdminDatabase
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import ENTITY_SPECS, AdminRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRESH_SCHEMA_PATHS = (
    PROJECT_ROOT / "db" / "catalog.sql",
    PROJECT_ROOT / "db" / "nhtsa.sql",
    PROJECT_ROOT / "db" / "admin.sql",
    PROJECT_ROOT / "db" / "station_admin.sql",
)
MIGRATION_009_PATH = PROJECT_ROOT / "migrations" / "catalog" / "009_bounded_production_dataset.sql"
MIGRATION_011_PATH = PROJECT_ROOT / "migrations" / "catalog" / "011_part_quarantine.sql"
MIGRATION_012_PATH = PROJECT_ROOT / "migrations" / "catalog" / "012_part_quarantine_resolution.sql"
MIGRATION_013_PATH = (
    PROJECT_ROOT / "migrations" / "catalog" / "013_part_quarantine_run_key_updated_index.sql"
)
MIGRATION_014_PATH = (
    PROJECT_ROOT
    / "migrations"
    / "catalog"
    / "014_part_quarantine_run_key_resolved_updated_index.sql"
)
MIGRATION_015_PATH = (
    PROJECT_ROOT / "migrations" / "catalog" / "015_quarantine_index_contract_cleanup.sql"
)
MIGRATION_016_PATH = (
    PROJECT_ROOT / "migrations" / "catalog" / "016_published_snapshot_provenance.sql"
)
MIGRATION_019_PATH = (
    PROJECT_ROOT / "migrations" / "catalog" / "019_verified_bounded_catalog_view.sql"
)
DATABASE_NAME_PATTERN = re.compile(r"^partsouq_bounded_perf_[0-9a-f]{12}_test$")
LOCAL_DATABASE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TARGET_PARTS = 10_000
SHARED_PART_NUMBER = "PF-SHARED-001"
SHARED_PART_NORMALIZED = "PFSHARED001"
SHARED_PART_FITMENTS = 100
PAGE_SIZES = (10, 30, 200)
PERF_SAMPLES = 25
SUMMARY_P95_LIMIT_MS = 1_000.0
PAGE_P95_LIMIT_MS = 500.0


@dataclass(frozen=True, slots=True)
class PerformanceDatabase:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)

    def connect(self, *, autocommit: bool = False) -> pymysql.Connection[DictCursor]:
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            autocommit=autocommit,
            cursorclass=DictCursor,
        )

    def station_admin_config(self) -> AdminConfig:
        return AdminConfig(
            mysql_host=self.host,
            mysql_port=self.port,
            mysql_user=self.user,
            mysql_password=self.password,
            mysql_database=self.database,
            bind_host="127.0.0.1",
            bind_port=0,
            secret_key="synthetic-performance-fixture",
            page_size=30,
        )


@pytest.fixture
def performance_database() -> Iterator[PerformanceDatabase]:
    if os.getenv("UNIFIED_TEST_MYSQL") != "1":
        pytest.skip("set UNIFIED_TEST_MYSQL=1 to run the isolated MySQL gate")
    host = os.environ["PARTSOUQ_DB_HOST"]
    if host not in LOCAL_DATABASE_HOSTS:
        raise ValueError("performance database host must be local loopback")
    port = int(os.environ["PARTSOUQ_DB_PORT"])
    root_password = os.environ["PARTSOUQ_MYSQL_ROOT_PASSWORD"]
    database_name = f"partsouq_bounded_perf_{uuid.uuid4().hex[:12]}_test"
    _validate_test_database_name(database_name)
    root_connection = pymysql.connect(
        host=host,
        port=port,
        user="root",
        password=root_password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
    )
    try:
        with root_connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE `{database_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        database = PerformanceDatabase(host, port, database_name, "root", root_password)
        _apply_sql_paths(database, (FRESH_SCHEMA_PATHS[0],))
        yield database
    finally:
        _validate_test_database_name(database_name)
        with root_connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database_name}`")
        root_connection.close()


def _validate_test_database_name(database_name: str) -> None:
    if DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("performance database name must match the isolated fixture pattern")


def _apply_sql_paths(database: PerformanceDatabase, paths: tuple[Path, ...]) -> None:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            for path in paths:
                for statement in _mysql_statements(path.read_text(encoding="utf-8")):
                    cursor.execute(statement)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _mysql_statements(script: str) -> Iterator[str]:
    delimiter = ";"
    buffer: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DELIMITER "):
            if any(part.strip() for part in buffer):
                raise ValueError("DELIMITER changed before the current SQL statement ended")
            delimiter = stripped.split(maxsplit=1)[1]
            continue
        buffer.append(line)
        if stripped.endswith(delimiter):
            statement = "\n".join(buffer)
            statement = statement[: statement.rfind(delimiter)].strip()
            if statement:
                yield statement
            buffer.clear()
    if any(part.strip() for part in buffer):
        raise ValueError("SQL script ended with an incomplete statement")


def test_quarantine_index_migration_keeps_online_ddl_contract() -> None:
    script = MIGRATION_015_PATH.read_text(encoding="utf-8")
    sql_statements = tuple(_mysql_statements(script))
    alter_pattern = re.compile(
        r"\bALTER\s+TABLE\s+`?part_quarantine`?\b.*?(?=;|$)",
        re.IGNORECASE | re.DOTALL,
    )
    alter_statements = [
        match.group(0)
        for sql_statement in sql_statements
        for match in alter_pattern.finditer(sql_statement)
    ]

    assert alter_statements
    assert (
        len(
            re.findall(
                r"(?im)^\s*SET\s+SESSION\s+lock_wait_timeout\s*=\s*30\s*;",
                script,
            )
        )
        == 1
    )
    assert (
        len(
            re.findall(
                r"(?im)^\s*SET\s+SESSION\s+innodb_lock_wait_timeout\s*=\s*30\s*;",
                script,
            )
        )
        == 1
    )
    for statement in alter_statements:
        assert re.search(r"\bALGORITHM\s*=\s*INPLACE\b", statement, re.IGNORECASE)
        assert re.search(r"\bLOCK\s*=\s*NONE\b", statement, re.IGNORECASE)


def test_quarantine_migration_runs_with_schema_scoped_app_user(
    performance_database: PerformanceDatabase,
) -> None:
    username = f"psq_mig_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    root_connection = performance_database.connect(autocommit=True)
    try:
        with root_connection.cursor() as cursor:
            cursor.execute("ALTER TABLE part_quarantine DROP INDEX idx_quarantine_list")
            cursor.execute(f"CREATE USER '{username}'@'%%' IDENTIFIED BY %s", (password,))
            cursor.execute(
                f"GRANT ALL PRIVILEGES ON `{performance_database.database}`.* TO '{username}'@'%'"
            )
        app_database = PerformanceDatabase(
            performance_database.host,
            performance_database.port,
            performance_database.database,
            username,
            password,
        )
        _apply_sql_paths(app_database, (MIGRATION_015_PATH,))
        _apply_sql_paths(app_database, (MIGRATION_015_PATH,))
        _assert_quarantine_index_contract(performance_database)
    finally:
        with root_connection.cursor() as cursor:
            cursor.execute(f"DROP USER IF EXISTS '{username}'@'%'")
        root_connection.close()


def test_published_snapshot_provenance_migration_upgrades_legacy_schema(
    performance_database: PerformanceDatabase,
) -> None:
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO published_parts("
                "part_id, vehicle_id, brand, model, vehicle_name, vehicle_code, "
                "part_name, part_number, part_number_normalized, category_main, "
                "group_code, part_range, snapshot_at"
                ") VALUES (900001, 900001, 'LEGACY', 'LEGACY MODEL', 'LEGACY VEHICLE', "
                "'LEGACY-CODE', 'LEGACY PART', 'LEGACY-PART-001', 'LEGACYPART001', "
                "'LEGACY CATEGORY', 'LEGACY-GROUP', '', UTC_TIMESTAMP())"
            )
            cursor.execute(
                "ALTER TABLE published_parts DROP INDEX idx_published_crawl_run, "
                "DROP COLUMN crawl_run_id"
            )
            cursor.execute("DROP TABLE published_parts_previous")
        _apply_sql_paths(performance_database, (MIGRATION_016_PATH,))
        _apply_sql_paths(performance_database, (MIGRATION_016_PATH,))

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_TYPE, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts' "
                "AND COLUMN_NAME = 'crawl_run_id'"
            )
            assert cursor.fetchone() == {"COLUMN_TYPE": "int", "IS_NULLABLE": "YES"}
            cursor.execute(
                "SELECT COLUMN_NAME, NON_UNIQUE, INDEX_TYPE, IS_VISIBLE "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'published_parts' "
                "AND INDEX_NAME = 'idx_published_crawl_run'"
            )
            assert cursor.fetchone() == {
                "COLUMN_NAME": "crawl_run_id",
                "NON_UNIQUE": 1,
                "INDEX_TYPE": "BTREE",
                "IS_VISIBLE": "YES",
            }
            cursor.execute(
                "SELECT REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts' "
                "AND CONSTRAINT_NAME = 'fk_published_crawl_run'"
            )
            assert cursor.fetchone() == {
                "REFERENCED_TABLE_NAME": "crawl_runs",
                "REFERENCED_COLUMN_NAME": "id",
            }
            cursor.execute(
                "SELECT COLUMN_NAME, NON_UNIQUE, INDEX_TYPE, IS_VISIBLE "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'published_parts_previous' "
                "AND INDEX_NAME = 'idx_published_crawl_run'"
            )
            assert cursor.fetchone() == {
                "COLUMN_NAME": "crawl_run_id",
                "NON_UNIQUE": 1,
                "INDEX_TYPE": "BTREE",
                "IS_VISIBLE": "YES",
            }
            cursor.execute(
                "SELECT REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'published_parts_previous' "
                "AND CONSTRAINT_NAME = 'fk_published_previous_crawl_run'"
            )
            assert cursor.fetchone() == {
                "REFERENCED_TABLE_NAME": "crawl_runs",
                "REFERENCED_COLUMN_NAME": "id",
            }
            cursor.execute(
                "SELECT part_number, crawl_run_id FROM published_parts WHERE part_id = 900001"
            )
            assert cursor.fetchone() == {
                "part_number": "LEGACY-PART-001",
                "crawl_run_id": None,
            }
            cursor.execute("SELECT COUNT(*) AS n FROM v_current_catalog_parts")
            assert cursor.fetchone() == {"n": 0}
            cursor.execute("SELECT COUNT(*) AS n FROM v_parts")
            assert cursor.fetchone() == {"n": 0}
            cursor.execute("SHOW CREATE VIEW v_current_catalog_parts")
            create_view = str(cursor.fetchone()["Create View"])
            assert "`full_run`.`status` = 'success'" in create_view
            assert "`full_scheduler_run`.`trigger_mode` = 'daemon'" in create_view
            assert "`full_scheduler_run`.`status` = 'completed'" in create_view
            assert "`full_scheduler_run`.`exit_code` = 0" in create_view
            assert "`current_run`.`target_parts` = 10000" in create_view
            assert "published_parts_previous" in create_view
            assert "count(`published_parts`" not in create_view.lower()
            assert "min(`published_parts`" not in create_view.lower()
            assert "max(`published_parts`" not in create_view.lower()
            cursor.execute("SHOW CREATE VIEW v_parts")
            assert "v_current_catalog_parts" in str(cursor.fetchone()["Create View"])

            cursor.execute(
                "INSERT INTO scheduled_job_runs(job_name, trigger_mode, status, started_at) "
                "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
            )
            running_job_id = int(cursor.lastrowid)
        with pytest.raises(pymysql.MySQLError, match="running catalog jobs exist"):
            _apply_sql_paths(performance_database, (MIGRATION_016_PATH,))
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
                "finished_at = UTC_TIMESTAMP() WHERE id = %s",
                (running_job_id,),
            )
        _apply_sql_paths(performance_database, (MIGRATION_016_PATH,))

        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE published_parts DROP FOREIGN KEY fk_published_crawl_run")
            cursor.execute(
                "ALTER TABLE published_parts DROP INDEX idx_published_crawl_run, "
                "ADD INDEX idx_published_crawl_run (crawl_run_id, part_id)"
            )
            cursor.execute("SET SESSION FOREIGN_KEY_CHECKS = 0")
            try:
                cursor.execute(
                    "UPDATE published_parts SET crawl_run_id = 2147483647 WHERE part_id = 900001"
                )
            finally:
                cursor.execute("SET SESSION FOREIGN_KEY_CHECKS = 1")
        with pytest.raises(pymysql.MySQLError, match="orphan run ids"):
            _apply_sql_paths(performance_database, (MIGRATION_016_PATH,))
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS columns_in_index FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts' "
                "AND INDEX_NAME = 'idx_published_crawl_run'"
            )
            assert cursor.fetchone() == {"columns_in_index": 2}
            cursor.execute("UPDATE published_parts SET crawl_run_id = NULL WHERE part_id = 900001")
        _apply_sql_paths(performance_database, (MIGRATION_016_PATH,))
    finally:
        connection.close()


def test_admin_schema_repairs_published_snapshot_foreign_key_contract(
    performance_database: PerformanceDatabase,
) -> None:
    _apply_sql_paths(performance_database, FRESH_SCHEMA_PATHS[1:3])
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE published_parts DROP FOREIGN KEY fk_published_crawl_run")
            cursor.execute(
                "ALTER TABLE published_parts ADD CONSTRAINT fk_published_crawl_run "
                "FOREIGN KEY (crawl_run_id) REFERENCES categories(id)"
            )
            cursor.execute(
                "ALTER TABLE published_parts_previous "
                "DROP FOREIGN KEY fk_published_previous_crawl_run"
            )
            cursor.execute(
                "ALTER TABLE published_parts_previous "
                "ADD CONSTRAINT fk_published_previous_crawl_run "
                "FOREIGN KEY (crawl_run_id) REFERENCES categories(id)"
            )

        _apply_sql_paths(performance_database, (FRESH_SCHEMA_PATHS[2],))

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TABLE_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() AND CONSTRAINT_NAME IN "
                "('fk_published_crawl_run', 'fk_published_previous_crawl_run') "
                "ORDER BY TABLE_NAME"
            )
            assert cursor.fetchall() == [
                {
                    "TABLE_NAME": "published_parts",
                    "REFERENCED_TABLE_NAME": "crawl_runs",
                    "REFERENCED_COLUMN_NAME": "id",
                },
                {
                    "TABLE_NAME": "published_parts_previous",
                    "REFERENCED_TABLE_NAME": "crawl_runs",
                    "REFERENCED_COLUMN_NAME": "id",
                },
            ]
    finally:
        connection.close()


def test_fresh_admin_schema_current_view_is_verified_bounded_only(
    performance_database: PerformanceDatabase,
) -> None:
    _apply_sql_paths(performance_database, FRESH_SCHEMA_PATHS[1:3])
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW CREATE VIEW v_current_catalog_parts")
            view_sql = str(cursor.fetchone()["Create View"]).lower()
    finally:
        connection.close()

    assert "bounded_parts" in view_sql
    assert "verified_bounded_evidence" in view_sql
    assert "verified_bounded_records" in view_sql
    assert "evidence_record_sha256" in view_sql
    assert "catalog_desired_bounded_scope" in view_sql
    assert "formal_full_parts" not in view_sql
    assert "published_parts" not in view_sql


@pytest.fixture
def quarantine_sentinel(performance_database: PerformanceDatabase) -> None:
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO brands(id, name, code) VALUES (900001, 'MIGRATION SENTINEL', 'MIG')"
            )
            cursor.execute(
                "INSERT INTO models(id, brand_id, name) "
                "VALUES (900001, 900001, 'MIGRATION SENTINEL')"
            )
            cursor.execute(
                "INSERT INTO vehicles(id, model_id, identity_hash, name, model_code) "
                "VALUES (900001, 900001, %s, 'MIGRATION SENTINEL', 'MIG-001')",
                ("9" * 64,),
            )
            cursor.execute(
                "INSERT INTO categories(id, vehicle_id, name, cid) "
                "VALUES (900001, 900001, 'MIGRATION SENTINEL', 'MIG-CID')"
            )
            cursor.execute(
                "INSERT INTO groups_t(id, category_id, code, name, uid) "
                "VALUES (900001, 900001, 'MIG', 'MIGRATION SENTINEL', 'MIG-UID')"
            )
            cursor.execute(
                "INSERT INTO part_quarantine("
                "id, group_id, part_number, range_str, reason, code, quantity, note, run_key"
                ") VALUES (900001, 900001, 'MIG-PART-001', '01.2020 - 12.2020', "
                "'nameless', 'MIG-CODE', '01', 'migration sentinel', 'migration-sentinel')"
            )
            cursor.execute(
                "INSERT INTO part_quarantine("
                "id, group_id, part_number, range_str, reason, code, quantity, note, run_key, "
                "resolved_at, resolution"
                ") VALUES (900002, 900001, 'MIG-PART-002', '01.2021 - 12.2021', "
                "'nameless', 'MIG-CODE-2', '02', 'resolved migration sentinel', "
                "'migration-sentinel', '2026-08-22 00:00:00', 'verified historical row')"
            )
    finally:
        connection.close()


def _quarantine_rows(database: PerformanceDatabase) -> tuple[dict[str, Any], ...]:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, group_id, part_number, range_str, reason, code, quantity, note, "
                "run_key, resolved_at, resolution, created_at, updated_at "
                "FROM part_quarantine ORDER BY id"
            )
            return tuple(dict(row) for row in cursor.fetchall())
    finally:
        connection.close()


EXPECTED_QUARANTINE_INDEX_COLUMNS = {
    "idx_quarantine_list": ["resolved_at", "updated_at"],
    "idx_quarantine_run_key_resolved_updated": [
        "run_key",
        "resolved_at",
        "updated_at",
    ],
}
EXPECTED_QUARANTINE_UNIQUE_COLUMNS = ["group_id", "part_number", "range_str", "reason"]
QUARANTINE_INDEX_METADATA_SQL = (
    "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, SUB_PART, "
    "COLLATION, INDEX_TYPE, IS_VISIBLE, EXPRESSION "
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


def _assert_quarantine_index_contract(database: PerformanceDatabase) -> dict[str, int]:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(QUARANTINE_INDEX_METADATA_SQL)
            index_rows = cursor.fetchall()
            cursor.execute(
                "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, SUB_PART, "
                "COLLATION, INDEX_TYPE, IS_VISIBLE, EXPRESSION "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
                "AND INDEX_NAME = 'uq_quarantine' ORDER BY SEQ_IN_INDEX"
            )
            unique_rows = cursor.fetchall()
            cursor.execute(
                "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, SUB_PART, "
                "COLLATION, INDEX_TYPE, IS_VISIBLE, EXPRESSION "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
                "AND INDEX_NAME = 'PRIMARY' ORDER BY SEQ_IN_INDEX"
            )
            primary_rows = cursor.fetchall()
            cursor.execute(
                "SELECT key_columns.ORDINAL_POSITION AS key_ordinal, "
                "key_columns.POSITION_IN_UNIQUE_CONSTRAINT AS unique_position, "
                "key_columns.COLUMN_NAME AS column_name, "
                "key_columns.REFERENCED_TABLE_SCHEMA AS referenced_schema, "
                "key_columns.REFERENCED_TABLE_NAME AS referenced_table, "
                "key_columns.REFERENCED_COLUMN_NAME AS referenced_column, "
                "referential_constraints.UNIQUE_CONSTRAINT_SCHEMA AS unique_schema, "
                "referential_constraints.UNIQUE_CONSTRAINT_NAME AS unique_name, "
                "referential_constraints.UPDATE_RULE AS update_rule, "
                "referential_constraints.DELETE_RULE AS delete_rule "
                "FROM information_schema.KEY_COLUMN_USAGE AS key_columns "
                "JOIN information_schema.REFERENTIAL_CONSTRAINTS "
                "AS referential_constraints "
                "ON referential_constraints.CONSTRAINT_SCHEMA = "
                "key_columns.CONSTRAINT_SCHEMA "
                "AND referential_constraints.CONSTRAINT_NAME = "
                "key_columns.CONSTRAINT_NAME "
                "AND referential_constraints.TABLE_NAME = key_columns.TABLE_NAME "
                "WHERE key_columns.CONSTRAINT_SCHEMA = DATABASE() "
                "AND key_columns.TABLE_SCHEMA = DATABASE() "
                "AND key_columns.TABLE_NAME = 'part_quarantine' "
                "AND key_columns.CONSTRAINT_NAME = 'fk_quarantine_group'"
            )
            fk_rows = cursor.fetchall()
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'groups_t' AND CONSTRAINT_NAME = 'PRIMARY' "
                "ORDER BY ORDINAL_POSITION"
            )
            parent_primary_columns = [str(row["COLUMN_NAME"]) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT COUNT(*) AS n FROM sys.schema_redundant_indexes "
                "WHERE table_schema = DATABASE() AND table_name = 'part_quarantine'"
            )
            redundant_count = int(cursor.fetchone()["n"])
            cursor.execute(
                "SELECT innodb_indexes.NAME, innodb_indexes.INDEX_ID "
                "FROM information_schema.INNODB_INDEXES AS innodb_indexes "
                "JOIN information_schema.INNODB_TABLES AS innodb_tables "
                "ON innodb_tables.TABLE_ID = innodb_indexes.TABLE_ID "
                "WHERE innodb_tables.NAME = CONCAT(DATABASE(), '/part_quarantine') "
                "AND innodb_indexes.NAME IN ("
                "'idx_quarantine_list', "
                "'idx_quarantine_run_key_resolved_updated'"
                ")"
            )
            index_ids = {str(row["NAME"]): int(row["INDEX_ID"]) for row in cursor.fetchall()}
    finally:
        connection.close()

    actual_columns: dict[str, list[str]] = {}
    for row in index_rows:
        index_name = str(row["INDEX_NAME"])
        actual_columns.setdefault(index_name, []).append(str(row["COLUMN_NAME"]))
        assert row["NON_UNIQUE"] == 1, index_name
        assert row["SUB_PART"] is None, index_name
        assert row["COLLATION"] == "A", index_name
        assert row["INDEX_TYPE"] == "BTREE", index_name
        assert row["IS_VISIBLE"] == "YES", index_name
        assert row["EXPRESSION"] is None, index_name
    assert actual_columns == EXPECTED_QUARANTINE_INDEX_COLUMNS
    assert [str(row["COLUMN_NAME"]) for row in unique_rows] == (EXPECTED_QUARANTINE_UNIQUE_COLUMNS)
    for row in unique_rows:
        assert row["NON_UNIQUE"] == 0
        assert row["SUB_PART"] is None
        assert row["COLLATION"] == "A"
        assert row["INDEX_TYPE"] == "BTREE"
        assert row["IS_VISIBLE"] == "YES"
        assert row["EXPRESSION"] is None
    assert primary_rows == [
        {
            "INDEX_NAME": "PRIMARY",
            "SEQ_IN_INDEX": 1,
            "COLUMN_NAME": "id",
            "NON_UNIQUE": 0,
            "SUB_PART": None,
            "COLLATION": "A",
            "INDEX_TYPE": "BTREE",
            "IS_VISIBLE": "YES",
            "EXPRESSION": None,
        }
    ]
    assert fk_rows == [
        {
            "key_ordinal": 1,
            "unique_position": 1,
            "column_name": "group_id",
            "referenced_schema": database.database,
            "referenced_table": "groups_t",
            "referenced_column": "id",
            "unique_schema": database.database,
            "unique_name": "PRIMARY",
            "update_rule": "NO ACTION",
            "delete_rule": "CASCADE",
        }
    ]
    assert parent_primary_columns == ["id"]
    assert redundant_count == 0
    assert index_ids.keys() == EXPECTED_QUARANTINE_INDEX_COLUMNS.keys()
    return index_ids


@pytest.mark.parametrize(
    ("mutated_index", "mutation_sql"),
    (
        pytest.param(
            "idx_quarantine_run_key_resolved_updated",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_run_key_resolved_updated, "
            "ADD KEY idx_quarantine_run_key_resolved_updated "
            "(run_key, updated_at, resolved_at)",
            id="run-key-wrong-order",
        ),
        pytest.param(
            "idx_quarantine_list",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_list, "
            "ADD KEY idx_quarantine_list (updated_at, resolved_at)",
            id="list-wrong-order",
        ),
        pytest.param(
            "idx_quarantine_run_key_resolved_updated",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_run_key_resolved_updated, "
            "ADD UNIQUE KEY idx_quarantine_run_key_resolved_updated "
            "(run_key, resolved_at, updated_at)",
            id="unique",
        ),
        pytest.param(
            "idx_quarantine_run_key_resolved_updated",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_run_key_resolved_updated, "
            "ADD KEY idx_quarantine_run_key_resolved_updated "
            "(run_key(32), resolved_at, updated_at)",
            id="prefix",
        ),
        pytest.param(
            "idx_quarantine_run_key_resolved_updated",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_run_key_resolved_updated, "
            "ADD KEY idx_quarantine_run_key_resolved_updated "
            "(run_key, resolved_at, updated_at DESC)",
            id="descending",
        ),
        pytest.param(
            "idx_quarantine_run_key_resolved_updated",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_run_key_resolved_updated, "
            "ADD KEY idx_quarantine_run_key_resolved_updated "
            "(run_key, resolved_at, updated_at, ((LENGTH(run_key))))",
            id="functional-key-part",
        ),
        pytest.param(
            "idx_quarantine_list",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_list, "
            "ADD UNIQUE KEY idx_quarantine_list (resolved_at, updated_at)",
            id="list-unique",
        ),
        pytest.param(
            "idx_quarantine_list",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_list, "
            "ADD KEY idx_quarantine_list (resolved_at, updated_at DESC)",
            id="list-descending",
        ),
        pytest.param(
            "idx_quarantine_list",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_list, "
            "ADD KEY idx_quarantine_list "
            "(resolved_at, updated_at, ((LENGTH(reason))))",
            id="list-functional-key-part",
        ),
        pytest.param(
            "idx_quarantine_list",
            (
                "ALTER TABLE part_quarantine ADD COLUMN spatial_probe POINT NULL SRID 0",
                "UPDATE part_quarantine SET spatial_probe = ST_SRID(POINT(0, 0), 0)",
                "ALTER TABLE part_quarantine "
                "MODIFY COLUMN spatial_probe POINT NOT NULL SRID 0, "
                "DROP KEY idx_quarantine_list, "
                "ADD SPATIAL KEY idx_quarantine_list (spatial_probe)",
            ),
            id="list-spatial",
        ),
        pytest.param(
            "idx_quarantine_run_key_resolved_updated",
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_run_key_resolved_updated, "
            "DROP KEY idx_quarantine_list",
            id="missing-indexes",
        ),
    ),
)
@pytest.mark.usefixtures("quarantine_sentinel")
def test_quarantine_index_migrations_repair_existing_volume(
    performance_database: PerformanceDatabase,
    mutated_index: str,
    mutation_sql: str | tuple[str, ...],
) -> None:
    _assert_quarantine_index_contract(performance_database)
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            mutation_statements = (mutation_sql,) if isinstance(mutation_sql, str) else mutation_sql
            for statement in mutation_statements:
                cursor.execute(statement)
            cursor.execute(QUARANTINE_INDEX_METADATA_SQL)
            mutated_metadata = cursor.fetchall()
            mutated_rows = [row for row in mutated_metadata if row["INDEX_NAME"] == mutated_index]
            cursor.execute(
                "SELECT innodb_indexes.NAME, innodb_indexes.INDEX_ID "
                "FROM information_schema.INNODB_INDEXES AS innodb_indexes "
                "JOIN information_schema.INNODB_TABLES AS innodb_tables "
                "ON innodb_tables.TABLE_ID = innodb_indexes.TABLE_ID "
                "WHERE innodb_tables.NAME = CONCAT(DATABASE(), '/part_quarantine') "
                "AND innodb_indexes.NAME IN ("
                "'idx_quarantine_list', "
                "'idx_quarantine_run_key_resolved_updated'"
                ")"
            )
            mutated_index_ids = {
                str(row["NAME"]): int(row["INDEX_ID"]) for row in cursor.fetchall()
            }
            spatial_state = None
            if isinstance(mutation_sql, tuple) and any(
                "spatial_probe" in statement for statement in mutation_sql
            ):
                cursor.execute(
                    "SELECT DATA_TYPE, IS_NULLABLE, SRS_ID "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
                    "AND COLUMN_NAME = 'spatial_probe'"
                )
                spatial_column = cursor.fetchone()
                cursor.execute(
                    "SELECT id, ST_AsText(spatial_probe) AS point_text, "
                    "ST_SRID(spatial_probe) AS srid "
                    "FROM part_quarantine ORDER BY id"
                )
                spatial_state = (spatial_column, cursor.fetchall())
    finally:
        connection.close()

    mutated_contract_is_valid = [str(row["COLUMN_NAME"]) for row in mutated_rows] == (
        EXPECTED_QUARANTINE_INDEX_COLUMNS[mutated_index]
    ) and all(
        row["NON_UNIQUE"] == 1
        and row["SUB_PART"] is None
        and row["COLLATION"] == "A"
        and row["INDEX_TYPE"] == "BTREE"
        and row["IS_VISIBLE"] == "YES"
        and row["EXPRESSION"] is None
        for row in mutated_rows
    )
    assert not mutated_contract_is_valid, "mutation must break the target index contract"
    before_rows = _quarantine_rows(performance_database)

    _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))
    after_index_ids = _assert_quarantine_index_contract(performance_database)
    for index_name, index_id in mutated_index_ids.items():
        if index_name != mutated_index:
            assert after_index_ids[index_name] == index_id
    assert _quarantine_rows(performance_database) == before_rows
    if spatial_state is not None:
        connection = performance_database.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DATA_TYPE, IS_NULLABLE, SRS_ID "
                    "FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'part_quarantine' "
                    "AND COLUMN_NAME = 'spatial_probe'"
                )
                spatial_column = cursor.fetchone()
                cursor.execute(
                    "SELECT id, ST_AsText(spatial_probe) AS point_text, "
                    "ST_SRID(spatial_probe) AS srid "
                    "FROM part_quarantine ORDER BY id"
                )
                assert (spatial_column, cursor.fetchall()) == spatial_state
        finally:
            connection.close()


@pytest.mark.parametrize("index_name", tuple(EXPECTED_QUARANTINE_INDEX_COLUMNS))
@pytest.mark.usefixtures("quarantine_sentinel")
def test_quarantine_index_visibility_repair_does_not_rebuild(
    performance_database: PerformanceDatabase,
    index_name: str,
) -> None:
    before_ids = _assert_quarantine_index_contract(performance_database)
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE part_quarantine ALTER INDEX {index_name} INVISIBLE")
    finally:
        connection.close()
    before_rows = _quarantine_rows(performance_database)

    _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))
    after_ids = _assert_quarantine_index_contract(performance_database)
    assert after_ids == before_ids
    assert _quarantine_rows(performance_database) == before_rows


@pytest.mark.usefixtures("quarantine_sentinel")
def test_quarantine_index_migration_is_idempotent_after_legacy_replay(
    performance_database: PerformanceDatabase,
) -> None:
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE part_quarantine ADD KEY idx_quarantine_group (group_id)")
    finally:
        connection.close()
    _apply_sql_paths(
        performance_database,
        (MIGRATION_011_PATH, MIGRATION_012_PATH, MIGRATION_013_PATH),
    )

    connection = performance_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(QUARANTINE_INDEX_METADATA_SQL)
            legacy_index_names = {str(row["INDEX_NAME"]) for row in cursor.fetchall()}
    finally:
        connection.close()
    assert {
        "idx_quarantine_group",
        "idx_quarantine_resolved",
        "idx_quarantine_run_key_updated",
    }.issubset(legacy_index_names)
    before_rows = _quarantine_rows(performance_database)

    _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))
    first_index_ids = _assert_quarantine_index_contract(performance_database)
    assert _quarantine_rows(performance_database) == before_rows
    _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))
    assert _assert_quarantine_index_contract(performance_database) == first_index_ids
    assert _quarantine_rows(performance_database) == before_rows

    _apply_sql_paths(
        performance_database,
        (
            MIGRATION_011_PATH,
            MIGRATION_012_PATH,
            MIGRATION_013_PATH,
            MIGRATION_014_PATH,
            MIGRATION_015_PATH,
        ),
    )
    assert _assert_quarantine_index_contract(performance_database) == first_index_ids
    assert _quarantine_rows(performance_database) == before_rows


def test_quarantine_migrations_build_final_contract_from_pre_011_schema(
    performance_database: PerformanceDatabase,
) -> None:
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE part_quarantine")
    finally:
        connection.close()

    _apply_sql_paths(
        performance_database,
        (MIGRATION_011_PATH, MIGRATION_012_PATH, MIGRATION_015_PATH),
    )
    _assert_quarantine_index_contract(performance_database)


@pytest.mark.usefixtures("quarantine_sentinel")
def test_quarantine_migration_recovers_after_failed_preflight(
    performance_database: PerformanceDatabase,
) -> None:
    before_rows = _quarantine_rows(performance_database)
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION restrict_fk_on_non_standard_key = OFF")
            cursor.execute("ALTER TABLE part_quarantine DROP FOREIGN KEY fk_quarantine_group")
            cursor.execute("ALTER TABLE parts DROP FOREIGN KEY fk_part_group")
            cursor.execute(
                "ALTER TABLE groups_t ADD COLUMN shard INT NOT NULL DEFAULT 0, "
                "DROP PRIMARY KEY, ADD PRIMARY KEY (id, shard)"
            )
            cursor.execute(
                "ALTER TABLE part_quarantine "
                "ADD CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id) "
                "REFERENCES groups_t(id) ON DELETE CASCADE"
            )
    finally:
        connection.close()

    with pytest.raises(
        pymysql.MySQLError,
        match="migration 015: quarantine key/FK/data preflight failed",
    ):
        _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))

    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA = DATABASE() "
                "AND ROUTINE_NAME = "
                "'upgrade_partsouq_015_quarantine_index_contract_cleanup'"
            )
            assert cursor.fetchone()["n"] == 1
            cursor.execute("ALTER TABLE part_quarantine DROP FOREIGN KEY fk_quarantine_group")
            cursor.execute(
                "ALTER TABLE groups_t DROP PRIMARY KEY, ADD PRIMARY KEY (id), DROP COLUMN shard"
            )
            cursor.execute(
                "ALTER TABLE part_quarantine "
                "ADD CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id) "
                "REFERENCES groups_t(id) ON DELETE CASCADE"
            )
            cursor.execute(
                "ALTER TABLE parts "
                "ADD CONSTRAINT fk_part_group FOREIGN KEY (group_id) "
                "REFERENCES groups_t(id) ON DELETE CASCADE"
            )
    finally:
        connection.close()

    _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))
    _assert_quarantine_index_contract(performance_database)
    assert _quarantine_rows(performance_database) == before_rows

    connection = performance_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA = DATABASE() "
                "AND ROUTINE_NAME LIKE '%partsouq_015%'"
            )
            assert cursor.fetchone()["n"] == 0
    finally:
        connection.close()


def test_station_health_fails_closed_when_backoffice_schema_is_missing(
    performance_database: PerformanceDatabase,
) -> None:
    station_app = create_app(performance_database.station_admin_config())
    station_app.testing = True

    with pytest.raises(pymysql.MySQLError) as error:
        station_app.test_client().get("/health")

    assert error.value.args[0] == 1146
    _apply_sql_paths(performance_database, FRESH_SCHEMA_PATHS[1:])

    response = station_app.test_client().get("/health")

    assert response.status_code == 200
    assert response.json == {"entities": 10, "status": "ok"}


def test_data_admin_health_fails_closed_when_backoffice_schema_is_missing(
    performance_database: PerformanceDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_catalog_database(monkeypatch, performance_database)
    with pytest.raises(pymysql.MySQLError) as error:
        data_admin_app.health()

    assert error.value.args[0] == 1146
    _apply_sql_paths(performance_database, FRESH_SCHEMA_PATHS[1:])
    assert data_admin_app.health() == {"status": "ok"}


@pytest.mark.parametrize(
    ("object_type", "object_name"),
    (
        ("TABLE", "admin_override_events"),
        ("TABLE", "admin_crawl_requests"),
        ("TABLE", "admin_crawl_request_audits"),
        ("TABLE", "scheduled_job_runs"),
        ("TABLE", "nhtsa_current_artifacts"),
        ("TABLE", "nhtsa_source_artifacts"),
        ("VIEW", "station_admin_historical_sample_part_numbers"),
        ("VIEW", "station_admin_historical_sample_part_occurrences"),
        ("VIEW", "station_admin_historical_sample_fitments"),
    ),
)
def test_station_health_fails_closed_when_backoffice_object_is_missing(
    performance_database: PerformanceDatabase,
    object_type: str,
    object_name: str,
) -> None:
    _apply_sql_paths(performance_database, FRESH_SCHEMA_PATHS[1:])
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION FOREIGN_KEY_CHECKS = 0")
            try:
                cursor.execute(f"DROP {object_type} {object_name}")
            finally:
                cursor.execute("SET SESSION FOREIGN_KEY_CHECKS = 1")
    finally:
        connection.close()

    station_app = create_app(performance_database.station_admin_config())
    station_app.testing = True
    with pytest.raises(pymysql.MySQLError) as error:
        station_app.test_client().get("/health")

    assert error.value.args[0] in {1146, 1356}


@pytest.mark.parametrize(
    ("object_type", "object_name"),
    (
        ("TABLE", "nhtsa_sync_runs"),
        ("VIEW", "station_admin_effective_parts"),
    ),
)
def test_data_admin_health_fails_closed_when_schema_object_is_missing(
    performance_database: PerformanceDatabase,
    monkeypatch: pytest.MonkeyPatch,
    object_type: str,
    object_name: str,
) -> None:
    _apply_sql_paths(performance_database, FRESH_SCHEMA_PATHS[1:])
    _configure_catalog_database(monkeypatch, performance_database)
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            # nhtsa_sync_runs 被 nhtsa_current_artifacts 的 published_run FK
            # 引用；health() 的 fail-closed 語意只需要「物件不存在」，
            # 因此這裡合法地暫時關閉 FK 檢查再 DROP。
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            cursor.execute(f"DROP {object_type} {object_name}")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
    finally:
        connection.close()

    with pytest.raises(pymysql.MySQLError) as error:
        data_admin_app.health()

    assert error.value.args[0] == 1146


@pytest.mark.parametrize(
    ("mutation_sql", "expected_error"),
    (
        pytest.param(
            "ALTER TABLE part_quarantine DROP KEY uq_quarantine",
            "migration 015: quarantine key/FK/data preflight failed",
            id="missing-unique-contract",
        ),
        pytest.param(
            (
                "ALTER TABLE part_quarantine DROP KEY uq_quarantine",
                "ALTER TABLE part_quarantine "
                "ADD UNIQUE KEY uq_quarantine "
                "(group_id, part_number, range_str, code)",
            ),
            "migration 015: quarantine key/FK/data preflight failed",
            id="wrong-unique-contract",
        ),
        pytest.param(
            "ALTER TABLE part_quarantine DROP FOREIGN KEY fk_quarantine_group",
            "migration 015: quarantine key/FK/data preflight failed",
            id="missing-foreign-key",
        ),
        pytest.param(
            (
                "ALTER TABLE part_quarantine DROP FOREIGN KEY fk_quarantine_group",
                "ALTER TABLE part_quarantine "
                "ADD CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id) "
                "REFERENCES groups_t(id) ON DELETE RESTRICT",
            ),
            "migration 015: quarantine key/FK/data preflight failed",
            id="wrong-delete-rule",
        ),
        pytest.param(
            (
                "ALTER TABLE part_quarantine DROP FOREIGN KEY fk_quarantine_group",
                "ALTER TABLE part_quarantine "
                "ADD CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id) "
                "REFERENCES groups_t(id) ON UPDATE CASCADE ON DELETE CASCADE",
            ),
            "migration 015: quarantine key/FK/data preflight failed",
            id="wrong-update-rule",
        ),
        pytest.param(
            (
                "ALTER TABLE part_quarantine DROP FOREIGN KEY fk_quarantine_group",
                "ALTER TABLE part_quarantine "
                "ADD CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id) "
                "REFERENCES categories(id) ON DELETE CASCADE",
            ),
            "migration 015: quarantine key/FK/data preflight failed",
            id="wrong-referenced-table",
        ),
        pytest.param(
            (
                "SET SESSION restrict_fk_on_non_standard_key = OFF",
                "ALTER TABLE part_quarantine DROP FOREIGN KEY fk_quarantine_group",
                "ALTER TABLE parts DROP FOREIGN KEY fk_part_group",
                "ALTER TABLE groups_t ADD COLUMN shard INT NOT NULL DEFAULT 0, "
                "DROP PRIMARY KEY, ADD PRIMARY KEY (id, shard)",
                "ALTER TABLE part_quarantine "
                "ADD CONSTRAINT fk_quarantine_group FOREIGN KEY (group_id) "
                "REFERENCES groups_t(id) ON DELETE CASCADE",
            ),
            "migration 015: quarantine key/FK/data preflight failed",
            id="composite-parent-primary",
        ),
        pytest.param(
            "ALTER TABLE part_quarantine DROP PRIMARY KEY, ADD PRIMARY KEY (id, group_id)",
            "migration 015: quarantine key/FK/data preflight failed",
            id="composite-quarantine-primary",
        ),
        pytest.param(
            "ALTER TABLE part_quarantine DROP PRIMARY KEY, ADD PRIMARY KEY (id DESC)",
            "migration 015: quarantine key/FK/data preflight failed",
            id="descending-quarantine-primary",
        ),
        pytest.param(
            (
                "SET FOREIGN_KEY_CHECKS = 0",
                "INSERT INTO part_quarantine("
                "id, group_id, part_number, range_str, reason, run_key"
                ") VALUES (900003, 999999, 'MIG-ORPHAN', '', 'nameless', "
                "'migration-sentinel')",
                "SET FOREIGN_KEY_CHECKS = 1",
            ),
            "migration 015: quarantine key/FK/data preflight failed",
            id="orphan-quarantine-row",
        ),
        pytest.param(
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_run_key_resolved_updated, "
            "ADD FULLTEXT KEY idx_quarantine_run_key_resolved_updated (note)",
            "migration 015: FULLTEXT drift requires table rebuild",
            id="target-fulltext-drift",
        ),
        pytest.param(
            "ALTER TABLE part_quarantine "
            "DROP KEY idx_quarantine_group, "
            "ADD FULLTEXT KEY idx_quarantine_group (note)",
            "migration 015: FULLTEXT drift requires table rebuild",
            id="obsolete-fulltext-drift",
        ),
        pytest.param(
            (
                "ALTER TABLE part_quarantine "
                "DROP KEY idx_quarantine_run_key_resolved_updated, "
                "ADD FULLTEXT KEY idx_quarantine_run_key_resolved_updated (note)",
                "ALTER TABLE part_quarantine "
                "DROP KEY idx_quarantine_run_key_resolved_updated, "
                "ADD KEY idx_quarantine_run_key_resolved_updated "
                "(run_key, resolved_at, updated_at)",
            ),
            "migration 015: hidden FTS artifacts require table rebuild",
            id="orphan-hidden-fts",
        ),
    ),
)
@pytest.mark.usefixtures("quarantine_sentinel")
def test_quarantine_index_migration_fails_closed_before_changes(
    performance_database: PerformanceDatabase,
    mutation_sql: str | tuple[str, ...],
    expected_error: str,
) -> None:
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE part_quarantine "
                "ADD KEY idx_quarantine_group (group_id), "
                "ADD KEY idx_quarantine_resolved (run_key, resolved_at)"
            )
            cursor.execute("ALTER TABLE part_quarantine ALTER INDEX idx_quarantine_list INVISIBLE")
            mutation_statements = (mutation_sql,) if isinstance(mutation_sql, str) else mutation_sql
            for statement in mutation_statements:
                cursor.execute(statement)
    finally:
        connection.close()

    schema_snapshots: list[dict[str, Any]] = []
    for execute_migration in (False, True):
        if execute_migration:
            with pytest.raises(pymysql.MySQLError, match=re.escape(expected_error)):
                _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))
        connection = performance_database.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW CREATE TABLE part_quarantine")
                quarantine_create = cursor.fetchone()
                cursor.execute("SHOW CREATE TABLE groups_t")
                groups_create = cursor.fetchone()
                cursor.execute("SHOW CREATE TABLE parts")
                parts_create = cursor.fetchone()
                cursor.execute(
                    "SELECT innodb_indexes.NAME, innodb_indexes.INDEX_ID, "
                    "innodb_indexes.TYPE, innodb_indexes.N_FIELDS, "
                    "innodb_indexes.PAGE_NO, innodb_indexes.SPACE "
                    "FROM information_schema.INNODB_INDEXES AS innodb_indexes "
                    "JOIN information_schema.INNODB_TABLES AS innodb_tables "
                    "ON innodb_tables.TABLE_ID = innodb_indexes.TABLE_ID "
                    "WHERE innodb_tables.NAME = CONCAT(DATABASE(), '/part_quarantine') "
                    "ORDER BY innodb_indexes.NAME, innodb_indexes.INDEX_ID"
                )
                index_metadata = cursor.fetchall()
                cursor.execute(
                    "SELECT TABLE_ID, NAME, SPACE "
                    "FROM information_schema.INNODB_TABLES "
                    "WHERE NAME LIKE CONCAT(DATABASE(), '/FTS_%') "
                    "ORDER BY NAME"
                )
                fts_tables = cursor.fetchall()
                cursor.execute("SELECT * FROM groups_t WHERE id = 900001")
                sentinel_groups = cursor.fetchall()
        finally:
            connection.close()
        schema_snapshots.append(
            {
                "quarantine_create": quarantine_create,
                "groups_create": groups_create,
                "parts_create": parts_create,
                "index_metadata": index_metadata,
                "fts_tables": fts_tables,
                "sentinel_groups": sentinel_groups,
                "quarantine_rows": _quarantine_rows(performance_database),
            }
        )

    assert schema_snapshots[1] == schema_snapshots[0]


def _configure_catalog_database(
    monkeypatch: pytest.MonkeyPatch,
    database: PerformanceDatabase,
) -> None:
    for key, value in {
        "host": database.host,
        "port": database.port,
        "user": database.user,
        "password": database.password,
        "database": database.database,
    }.items():
        monkeypatch.setitem(DB_CONFIG, key, value)


@pytest.mark.parametrize(
    "index_name",
    ("idx_quarantine_list", "idx_quarantine_run_key_resolved_updated"),
)
def test_health_endpoints_fail_closed_when_quarantine_index_is_missing(
    performance_database: PerformanceDatabase,
    monkeypatch: pytest.MonkeyPatch,
    index_name: str,
) -> None:
    _configure_catalog_database(monkeypatch, performance_database)
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE part_quarantine DROP KEY {index_name}")
    finally:
        connection.close()

    try:
        with pytest.raises(pymysql.OperationalError) as fastapi_error:
            data_admin_app.health()
        assert fastapi_error.value.args[0] == 1176

        station_app = create_app(performance_database.station_admin_config())
        station_app.testing = True
        with pytest.raises(pymysql.OperationalError) as station_error:
            station_app.test_client().get("/health")
        assert station_error.value.args[0] == 1176
    finally:
        _apply_sql_paths(performance_database, (MIGRATION_015_PATH,))


def _seed_synthetic_bounded_dataset(database: PerformanceDatabase) -> dict[str, int]:
    _validate_test_database_name(database.database)
    catalog_database = Database().connect()
    try:
        scheduler_output = (
            "catalog receipt accepted without fixture marker\n" * 20_000
            + "SYNTHETIC PERFORMANCE FIXTURE; NOT LIVE CRAWL EVIDENCE"
        )
        scheduler_run_id = catalog_database._execute(
            "INSERT INTO scheduled_job_runs("
            "job_name, trigger_mode, status, started_at, output_text"
            ") VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP(), %s)",
            (scheduler_output,),
        ).lastrowid
        crawl = CrawlRepository(catalog_database, "bounded-10000-synthetic-perf")
        crawl_run_id = crawl.start_run(
            "bounded-10000-synthetic-perf",
            fresh=True,
            dataset_kind="bounded",
            target_parts=TARGET_PARTS,
            scheduled_job_run_id=scheduler_run_id,
        )
        brands = BrandRepository(catalog_database)
        vehicles = VehicleRepository(catalog_database)
        parts = PartRepository(catalog_database)
        part_index = 0
        vehicle_index = 0
        group_count = 0
        for brand_index in range(10):
            brand_name = f"PERF BRAND {brand_index:02d}"
            brand_id = brands.upsert_brand(
                brand_name,
                f"https://partsouq.com/en/catalog/genuine?c=PERF{brand_index:02d}",
            )
            for model_index in range(2):
                model_name = f"PERF MODEL {brand_index:02d}-{model_index:02d}"
                model_id = brands.upsert_model(
                    brand_id,
                    model_name,
                    f"PERF-MODEL-SSD-{brand_index:02d}-{model_index:02d}",
                    "https://partsouq.com/en/catalog/genuine/locate?c=PERF",
                )
                for model_vehicle_index in range(5):
                    vehicle_index += 1
                    production_from_year = 2015 + model_vehicle_index
                    production_to_year = 2020 + model_vehicle_index
                    vehicle_id = vehicles.upsert_vehicle(
                        model_id,
                        {
                            "name": f"PERF VEHICLE {vehicle_index:03d}",
                            "description": "synthetic performance fixture",
                            "model_code": f"PERF-{vehicle_index:03d}",
                            "options": "PERFORMANCE",
                            "prod_period": (f"01.{production_from_year} - 12.{production_to_year}"),
                            "production_from": f"{production_from_year}-01",
                            "production_to": f"{production_to_year}-12",
                            "grade": f"TRIM-{model_vehicle_index}",
                            "market": "TEST",
                            "engine": f"ENGINE-{model_vehicle_index}",
                            "transmission": "AUTOMATIC",
                            "body_style": "SEDAN",
                            "ssd": f"PERF-VEHICLE-SSD-{vehicle_index:03d}",
                            "vid": f"PERFVID{vehicle_index:06d}",
                            "url": (
                                "https://partsouq.com/en/catalog/genuine/vehicle"
                                f"?c=PERF&vid=PERFVID{vehicle_index:06d}"
                            ),
                        },
                    )
                    category_id = vehicles.upsert_category(
                        vehicle_id,
                        f"PERF MAIN CATEGORY {vehicle_index:03d}",
                        f"CID{vehicle_index:06d}",
                    )
                    group_count += 1
                    group_id = vehicles.upsert_group(
                        category_id,
                        f"G{group_count:06d}",
                        f"PERF CATEGORY GROUP {group_count:03d}",
                        f"UID{group_count:06d}",
                        (
                            "https://partsouq.com/en/catalog/genuine/unit"
                            f"?c=PERF&vid=PERFVID{vehicle_index:06d}"
                            f"&cid=CID{vehicle_index:06d}&uid=UID{group_count:06d}"
                        ),
                    )
                    fixture_parts = []
                    for group_part_index in range(100):
                        part_index += 1
                        fixture_parts.append(
                            {
                                "part_number": (
                                    SHARED_PART_NUMBER
                                    if group_part_index == 0
                                    else f"PF-{part_index:06d}"
                                ),
                                "name": (
                                    "SHARED PERFORMANCE PART"
                                    if group_part_index == 0
                                    else f"PERFORMANCE PART {part_index:06d}"
                                ),
                                "code": f"C{part_index:06d}",
                                "note": "synthetic performance fixture",
                                "quantity": "01",
                                "range_str": "01.2018 - 12.2025",
                                "part_from": "2018-01",
                                "part_to": "2025-12",
                            }
                        )
                    parts.upsert_parts(group_id, fixture_parts, crawl_run_id)
        assert part_index == TARGET_PARTS
        # 這是隔離效能 fixture，不可偽造成 live HTTP evidence。直接建立
        # snapshot，正式發布仍只能走 CrawlRepository 的 evidence gate。
        catalog_database._execute(
            "INSERT INTO bounded_parts ("
            "part_id, crawl_run_id, vehicle_id, model_id, vehicle_vid, "
            "brand, model, vehicle_name, vehicle_code, prod_period, "
            "production_from, production_to, engine, trim_name, part_name, "
            "part_number, part_number_normalized, category_id, category_cid, "
            "category_main, category_group, group_id, group_code, group_uid, "
            "part_range, part_from, part_to, source_url, note, quantity, code, snapshot_at) "
            "SELECT parts.id, %s, vehicles.id, models.id, vehicles.vid, brands.name, "
            "models.name, vehicles.name, vehicles.model_code, vehicles.prod_period, "
            "vehicles.production_from, vehicles.production_to, vehicles.engine, "
            "vehicles.grade, parts.name, parts.part_number, "
            "UPPER(REGEXP_REPLACE(parts.part_number, '[[:space:]-]+', '')), "
            "categories.id, categories.cid, categories.name, groups_t.name, groups_t.id, "
            "groups_t.code, groups_t.uid, parts.range_str, parts.part_from, parts.part_to, "
            "groups_t.url, parts.note, parts.quantity, parts.code, NOW() "
            "FROM parts "
            "JOIN groups_t ON groups_t.id = parts.group_id "
            "JOIN categories ON categories.id = groups_t.category_id "
            "JOIN vehicles ON vehicles.id = categories.vehicle_id "
            "JOIN models ON models.id = vehicles.model_id "
            "JOIN brands ON brands.id = models.brand_id "
            "WHERE parts.seen_run_id = %s",
            (crawl_run_id, crawl_run_id),
        )
        snapshot = catalog_database._execute(
            "SELECT COUNT(*) AS row_count FROM bounded_parts WHERE crawl_run_id = %s",
            (crawl_run_id,),
        ).fetchone()
        assert snapshot == {"row_count": TARGET_PARTS}
        crawl.finish_run(
            crawl_run_id,
            "bounded_success",
            {
                "brands": 10,
                "models": 20,
                "vehicles": vehicle_index,
                "groups": group_count,
                "parts": TARGET_PARTS,
                "parts_new": TARGET_PARTS,
            },
        )
        catalog_database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (scheduler_run_id,),
        )
        catalog_database.commit()
        return {
            "crawl_run_id": int(crawl_run_id),
            "scheduler_run_id": int(scheduler_run_id),
            "vehicles": vehicle_index,
        }
    except BaseException:
        catalog_database.rollback()
        raise
    finally:
        catalog_database.close()


def _fetch_one(
    database: PerformanceDatabase,
    sql: str,
    params: tuple[object, ...] = (),
) -> dict[str, Any]:
    connection = database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return dict(cursor.fetchone() or {})
    finally:
        connection.close()


def _measure(call: Any) -> dict[str, float]:
    for _ in range(5):
        call()
    samples = []
    for _ in range(PERF_SAMPLES):
        started = time.perf_counter()
        call()
        samples.append((time.perf_counter() - started) * 1_000)
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
        "max_ms": round(ordered[-1], 2),
    }


def _assert_data_response(response: Any, expected_queries: int, calls: list[Any]) -> None:
    assert response.status_code == 200
    assert len(calls) == expected_queries


def _explain_plan(
    database: PerformanceDatabase,
    sql: str,
    params: tuple[object, ...],
) -> dict[str, object]:
    connection = database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN FORMAT=JSON " + sql, params)
            plan = json.loads(str((cursor.fetchone() or {}).get("EXPLAIN", "{}")))
    finally:
        connection.close()
    keys: set[str] = set()
    tables: list[dict[str, object]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            key = value.get("key")
            if isinstance(key, str):
                keys.add(key)
            table_name = value.get("table_name")
            if isinstance(table_name, str):
                tables.append(
                    {
                        "table": table_name,
                        "access_type": value.get("access_type"),
                        "key": key,
                        "rows_examined_per_scan": value.get("rows_examined_per_scan"),
                    }
                )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(plan)
    return {"keys": sorted(keys), "tables": tables}


def test_synthetic_bounded_dataset_admin_performance_gate(
    performance_database: PerformanceDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _apply_sql_paths(
        performance_database,
        (*FRESH_SCHEMA_PATHS[1:], MIGRATION_009_PATH, MIGRATION_019_PATH),
    )
    _configure_catalog_database(monkeypatch, performance_database)
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "performance-admin-token")
    data_admin_headers = {"X-Admin-Token": "performance-admin-token"}
    setup_started = time.perf_counter()
    seeded = _seed_synthetic_bounded_dataset(performance_database)
    setup_ms = round((time.perf_counter() - setup_started) * 1_000, 2)

    direct = _fetch_one(
        performance_database,
        "SELECT "
        "(SELECT COUNT(*) FROM bounded_parts) AS bounded_rows, "
        "(SELECT COUNT(*) FROM v_current_catalog_parts) AS current_rows, "
        "(SELECT COUNT(*) FROM v_current_catalog_parts "
        " WHERE dataset_scope = 'bounded') AS current_bounded_rows, "
        "(SELECT COUNT(*) FROM v_parts) AS compatibility_rows, "
        "(SELECT COUNT(*) FROM published_parts) AS published_rows, "
        "(SELECT COUNT(*) FROM crawl_runs WHERE status = 'sample') AS sample_runs, "
        "(SELECT COUNT(*) FROM admin_override_heads) AS override_rows",
    )
    assert direct == {
        "bounded_rows": TARGET_PARTS,
        "current_rows": 0,
        "current_bounded_rows": 0,
        "compatibility_rows": 0,
        "published_rows": 0,
        "sample_runs": 0,
        "override_rows": 0,
    }
    shared_fitments = _fetch_one(
        performance_database,
        "SELECT COUNT(*) AS fitment_rows, COUNT(DISTINCT vehicle_id) AS vehicles, "
        "COUNT(DISTINCT CONCAT(production_from, ':', production_to)) AS year_ranges "
        "FROM bounded_parts WHERE part_number_normalized = 'PFSHARED001'",
    )
    assert shared_fitments == {
        "fitment_rows": SHARED_PART_FITMENTS,
        "vehicles": SHARED_PART_FITMENTS,
        "year_ranges": 5,
    }
    formal_part_spec = replace(
        ENTITY_SPECS["part_numbers"],
        table="station_admin_formal_part_numbers",
    )
    candidate_sql, candidate_params = AdminRepository._source_search_candidates(
        formal_part_spec,
        [f"{SHARED_PART_NORMALIZED}%"] * 3,
        seeded["crawl_run_id"],
    )
    connection = performance_database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(candidate_sql, candidate_params)
            candidate_ids = {int(row["id"]) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT part_id AS id FROM bounded_parts "
                "WHERE crawl_run_id = %s AND (brand LIKE %s "
                "OR part_number_normalized LIKE %s OR part_name LIKE %s)",
                (
                    seeded["crawl_run_id"],
                    f"{SHARED_PART_NORMALIZED}%",
                    f"{SHARED_PART_NORMALIZED}%",
                    f"{SHARED_PART_NORMALIZED}%",
                ),
            )
            expected_ids = {int(row["id"]) for row in cursor.fetchall()}
    finally:
        connection.close()
    assert candidate_ids == expected_ids
    assert len(candidate_ids) == SHARED_PART_FITMENTS
    station_candidate_explain = _explain_plan(
        performance_database,
        candidate_sql,
        tuple(candidate_params),
    )
    assert any(
        table.get("key") == "idx_bounded_part_number_normalized"
        and int(table.get("rows_examined_per_scan") or TARGET_PARTS) <= SHARED_PART_FITMENTS
        for table in station_candidate_explain["tables"]
    )

    snapshot_target = _fetch_one(
        performance_database,
        "SELECT part_id, part_name FROM bounded_parts "
        "WHERE part_number_normalized = %s ORDER BY part_id LIMIT 1",
        (SHARED_PART_NORMALIZED,),
    )
    switched_name = f"{snapshot_target['part_name']} SWITCHED"
    reader_connection = performance_database.connect(autocommit=True)
    writer_connection = performance_database.connect(autocommit=True)
    reader = AdminDatabase(reader_connection, QueryTrace())
    try:
        with reader.transaction(read_only=True):
            before_switch = reader.fetch_one(
                "test.snapshot.before",
                "SELECT part_name FROM bounded_parts WHERE part_id = %s",
                (snapshot_target["part_id"],),
            )
            with writer_connection.cursor() as cursor:
                with pytest.raises(pymysql.MySQLError) as error:
                    cursor.execute(
                        "UPDATE bounded_parts SET part_name = %s WHERE part_id = %s",
                        (switched_name, snapshot_target["part_id"]),
                    )
                assert error.value.args[0] == 1644
            during_switch = reader.fetch_one(
                "test.snapshot.during",
                "SELECT part_name FROM bounded_parts WHERE part_id = %s",
                (snapshot_target["part_id"],),
            )
        assert before_switch == during_switch == {"part_name": snapshot_target["part_name"]}
        assert reader.fetch_one(
            "test.snapshot.after",
            "SELECT part_name FROM bounded_parts WHERE part_id = %s",
            (snapshot_target["part_id"],),
        ) == {"part_name": snapshot_target["part_name"]}
    finally:
        writer_connection.close()
        reader_connection.close()

    data_queries: list[tuple[str, tuple[object, ...]]] = []
    original_fetch_all = data_admin_app._fetch_all

    def recording_fetch_all(
        sql: str,
        params: tuple[object, ...] = (),
    ) -> list[dict[str, Any]]:
        data_queries.append((sql, params))
        return original_fetch_all(sql, params)

    monkeypatch.setattr(data_admin_app, "_fetch_all", recording_fetch_all)
    data_admin_report: dict[str, Any] = {}
    station_admin_report: dict[str, Any] = {}
    report: dict[str, Any] = {
        "fixture": "synthetic/performance only; not live crawl evidence",
        "live_crawl_evidence": False,
        "deployment_p95_evidence": False,
        "measurement_scope": "in-process; single-threaded; warm-cache",
        "migration_009_scope": "fresh-schema idempotency only",
        "database": performance_database.database,
        "setup_ms": setup_ms,
        "data_admin": data_admin_report,
        "station_admin": station_admin_report,
    }
    station_admin_report["bounded_candidate_explain"] = station_candidate_explain
    with TestClient(data_admin_app.app) as data_client:
        data_queries.clear()
        summary = data_client.get("/api/database-summary", headers=data_admin_headers)
        _assert_data_response(summary, 8, data_queries)
        summary_json = summary.json()
        assert summary_json["bounded_ready"] is False
        assert "bounded_non_live_data_marker" in summary_json["bounded"]["blocking_reasons"]
        assert summary_json["bounded"]["fitment_rows"] == 0
        assert summary_json["bounded"]["unique_part_numbers"] == 0
        assert summary_json["bounded"]["unique_vehicles"] == 0
        assert summary_json["current_catalog"]["fitment_rows"] == 0
        assert summary_json["bounded"]["crawl_run_id"] == seeded["crawl_run_id"]
        assert summary_json["bounded"]["scheduler"]["run_id"] == seeded["scheduler_run_id"]
        assert summary_json["bounded"]["source_provenance"]["raw_http_artifact_status"] == (
            "not_verified"
        )
        assert summary_json["production_ready"] is False
        data_admin_report["synthetic_readiness"] = {
            "bounded_ready": summary_json["bounded_ready"],
            "blocking_reasons": summary_json["bounded"]["blocking_reasons"],
        }
        summary_queries = tuple(data_queries)

        def summary_request() -> None:
            data_queries.clear()
            response = data_client.get("/api/database-summary", headers=data_admin_headers)
            _assert_data_response(response, 8, data_queries)

        data_admin_report["summary"] = _measure(summary_request)

        for page_size in PAGE_SIZES:
            for page_name, page in (("first", 1), ("out_of_range", 2)):
                path = f"/api/bounded-parts?page={page}&pageSize={page_size}"

                def bounded_request(path: str = path) -> None:
                    data_queries.clear()
                    response = data_client.get(path, headers=data_admin_headers)
                    _assert_data_response(response, 2, data_queries)

                data_queries.clear()
                response = data_client.get(path, headers=data_admin_headers)
                _assert_data_response(response, 2, data_queries)
                payload = response.json()
                assert payload["total"] == 0
                assert payload["pageSize"] == page_size
                assert payload["items"] == []
                if page_size == 200:
                    data_admin_report[f"formal_fail_closed_200_{page_name}_explain"] = [
                        _explain_plan(performance_database, sql, params)
                        for sql, params in data_queries
                    ]
                data_admin_report[f"formal_fail_closed_{page_size}_{page_name}"] = _measure(
                    bounded_request
                )

        data_queries.clear()
        exact = data_client.get(
            "/api/bounded-parts",
            params={"part_number": SHARED_PART_NORMALIZED, "pageSize": 30},
            headers=data_admin_headers,
        )
        _assert_data_response(exact, 2, data_queries)
        assert exact.json()["total"] == 0
        assert exact.json()["items"] == []
        exact_queries = tuple(data_queries)
        data_queries.clear()
        exact_last = data_client.get(
            "/api/bounded-parts",
            params={"part_number": SHARED_PART_NORMALIZED, "pageSize": 30, "page": 4},
            headers=data_admin_headers,
        )
        _assert_data_response(exact_last, 2, data_queries)
        assert exact_last.json()["total"] == 0
        assert exact_last.json()["items"] == []

        def exact_request() -> None:
            data_queries.clear()
            response = data_client.get(
                "/api/bounded-parts",
                params={"part_number": SHARED_PART_NORMALIZED, "pageSize": 30},
                headers=data_admin_headers,
            )
            _assert_data_response(response, 2, data_queries)
            assert response.json()["total"] == 0

        data_admin_report["bounded_exact_normalized"] = _measure(exact_request)

    data_admin_report["summary_main_explain"] = _explain_plan(
        performance_database, *summary_queries[0]
    )
    exact_explain = [
        _explain_plan(performance_database, sql, params) for sql, params in exact_queries
    ]
    data_admin_report["bounded_exact_explain"] = exact_explain

    station_app = create_app(performance_database.station_admin_config())
    station_app.testing = True
    station_client = station_app.test_client()
    for page_size in PAGE_SIZES:
        path = f"/entities/part_numbers?dataset=formal&page=1&pageSize={page_size}"

        def station_request(path: str = path) -> None:
            response = station_client.get(path)
            assert response.status_code == 200
            assert response.headers["X-Admin-Query-Count"] == "4"
            assert "共 0 筆記錄".encode() in response.data

        station_request()
        station_admin_report[f"formal_fail_closed_{page_size}"] = _measure(station_request)

    def station_exact_request() -> None:
        response = station_client.get(
            f"/entities/part_numbers?dataset=formal&q={SHARED_PART_NORMALIZED}&pageSize=30"
        )
        assert response.status_code == 200
        assert response.headers["X-Admin-Query-Count"] == "5"
        assert SHARED_PART_NUMBER.encode() not in response.data
        assert "共 0 筆記錄".encode() in response.data

    station_exact_request()
    station_admin_report["formal_fail_closed_exact"] = _measure(station_exact_request)

    summary_p95 = float(data_admin_report["summary"]["p95_ms"])
    assert summary_p95 < SUMMARY_P95_LIMIT_MS
    for section in (data_admin_report, station_admin_report):
        for name, metrics in section.items():
            if name in {"summary", "synthetic_readiness"} or name.endswith("_explain"):
                continue
            assert float(metrics["p95_ms"]) < PAGE_P95_LIMIT_MS, f"{name}: {metrics}"

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
