from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from unittest import mock

import pymysql
import pytest
from pymysql.cursors import DictCursor

from partsouq_catalog import migrations as catalog_migrations
from partsouq_catalog import scheduler
from partsouq_catalog.admission import AdmissionLockBusy
from partsouq_catalog.config import CRAWL
from partsouq_catalog.crawler import Crawler
from partsouq_catalog.db import Database
from partsouq_catalog.migrations import (
    ACTIVE_VERSIONS,
    CATALOG_MANIFEST,
    STATION_ADMIN_ASSET_HASHES,
    CatalogMigrationRunner,
    MigrationConnection,
    MigrationError,
    split_mysql_script,
)
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATHS = tuple(
    PROJECT_ROOT / "db" / filename
    for filename in ("catalog.sql", "nhtsa.sql", "admin.sql", "station_admin.sql")
)
DATABASE_PATTERN = re.compile(r"^partsouq_migration_[0-9a-f]{12}_test$")
LOCAL_DATABASE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class MigrationDatabase:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)

    def connect(self) -> MigrationConnection:
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=DictCursor,
        )


@pytest.fixture
def migration_database() -> Iterator[MigrationDatabase]:
    if os.getenv("UNIFIED_TEST_MYSQL") != "1":
        pytest.skip("set UNIFIED_TEST_MYSQL=1 to run the isolated MySQL gate")
    host = os.environ["PARTSOUQ_DB_HOST"]
    if host not in LOCAL_DATABASE_HOSTS:
        raise ValueError("migration database host must be local loopback")
    port = int(os.environ["PARTSOUQ_DB_PORT"])
    root_password = os.environ["PARTSOUQ_MYSQL_ROOT_PASSWORD"]
    database_name = f"partsouq_migration_{uuid.uuid4().hex[:12]}_test"
    if DATABASE_PATTERN.fullmatch(database_name) is None:
        raise ValueError("unsafe migration test database name")
    root = pymysql.connect(
        host=host,
        port=port,
        user="root",
        password=root_password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
    )
    try:
        with root.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE `{database_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
        database = MigrationDatabase(host, port, database_name, "root", root_password)
        _apply_schema(database)
        yield database
    finally:
        if DATABASE_PATTERN.fullmatch(database_name) is None:
            raise ValueError("unsafe migration test database cleanup name")
        with root.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database_name}`")
        root.close()


def _apply_schema(database: MigrationDatabase) -> None:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            for path in SCHEMA_PATHS:
                for statement in split_mysql_script(path.read_text(encoding="utf-8")):
                    cursor.execute(statement)
                    while cursor.nextset():
                        pass
            cursor.execute(
                "INSERT INTO brands (name, code, url) VALUES "
                "('MIGRATION-SENTINEL', 'MIGRATION-SENTINEL', 'https://example.invalid/sentinel')"
            )
    finally:
        connection.close()


def test_stale_repair_does_not_touch_near_match_rows(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    run_ids = [
        _insert_linked_run(migration_database, "no-link", link=False),
        _insert_linked_run(migration_database, "wrong-job", job_name="nhtsa"),
        _insert_linked_run(migration_database, "completed", job_status="completed"),
        _insert_linked_run(migration_database, "no-finish", finished=False),
        _insert_linked_run(migration_database, "no-exit", exit_code=None),
        _insert_linked_run(migration_database, "failed-zero", exit_code=0),
    ]

    with pytest.raises(MigrationError, match="running jobs exist in crawl_runs"):
        runner.apply()

    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            placeholders = ",".join("%s" for _ in run_ids)
            cursor.execute(
                f"SELECT status FROM crawl_runs WHERE id IN ({placeholders}) ORDER BY id",
                tuple(run_ids),
            )
            assert [row["status"] for row in cursor.fetchall()] == ["running"] * len(run_ids)
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='catalog_schema_ledger'"
            )
            ledger = cursor.fetchone()
            assert ledger and ledger["row_count"] == 0
    finally:
        connection.close()


def test_failed_zero_and_duplicate_scheduler_links_cannot_authorize_repair(
    migration_database: MigrationDatabase,
) -> None:
    failed_zero = _insert_linked_run(migration_database, "failed-zero-direct", exit_code=0)
    first_duplicate = _insert_linked_run(migration_database, "duplicate-link")
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT scheduled_job_run_id FROM crawl_runs WHERE id=%s",
                (first_duplicate,),
            )
            row = cursor.fetchone()
            assert row and row["scheduled_job_run_id"] is not None
            cursor.execute(
                "INSERT INTO crawl_runs "
                "(run_key,started_at,status,dataset_kind,target_parts,scheduled_job_run_id) "
                "VALUES ('migration-duplicate-link-2',NOW(6),'running','bounded',10000,%s)",
                (row["scheduled_job_run_id"],),
            )
            second_duplicate = cursor.lastrowid
            assert second_duplicate is not None

        catalog_migrations._repair_stale_catalog_runs(connection)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM crawl_runs WHERE id IN (%s,%s,%s) ORDER BY id",
                (failed_zero, first_duplicate, second_duplicate),
            )
            assert [row["status"] for row in cursor.fetchall()] == ["running"] * 3
        with pytest.raises(MigrationError, match="running jobs exist in crawl_runs"):
            catalog_migrations._assert_no_running_jobs(
                connection,
                allow_repairable_catalog=True,
            )
    finally:
        connection.close()


def test_pre_009_shape_skips_linked_run_repair(
    migration_database: MigrationDatabase,
) -> None:
    legacy_name = f"partsouq_migration_{uuid.uuid4().hex[:12]}_test"
    root = pymysql.connect(
        host=migration_database.host,
        port=migration_database.port,
        user=migration_database.user,
        password=migration_database.password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
    )
    try:
        with root.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{legacy_name}`")
        legacy = MigrationDatabase(
            migration_database.host,
            migration_database.port,
            legacy_name,
            migration_database.user,
            migration_database.password,
        )
        connection = legacy.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE scheduled_job_runs (id BIGINT UNSIGNED PRIMARY KEY,"
                    "job_name VARCHAR(32),status VARCHAR(32),finished_at DATETIME NULL,"
                    "exit_code INT NULL)"
                )
                cursor.execute(
                    "CREATE TABLE crawl_runs (id INT PRIMARY KEY,status VARCHAR(16),"
                    "finished_at DATETIME NULL,error_msg TEXT NULL)"
                )
                cursor.execute("INSERT INTO crawl_runs (id,status) VALUES (1,'success')")
            catalog_migrations._repair_stale_catalog_runs(connection)
            catalog_migrations._assert_no_running_jobs(connection, allow_repairable_catalog=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT status FROM crawl_runs WHERE id=1")
                row = cursor.fetchone()
                assert row and row["status"] == "success"
        finally:
            connection.close()
    finally:
        with root.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{legacy_name}`")
        root.close()


def test_completed_scheduler_cannot_authorize_stale_repair(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    run_id = _insert_linked_run(
        migration_database, "completed-only", job_status="completed", exit_code=0
    )

    with pytest.raises(MigrationError, match="running jobs exist in crawl_runs"):
        runner.apply()

    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM crawl_runs WHERE id=%s", (run_id,))
            row = cursor.fetchone()
            assert row and row["status"] == "running"
        assert not _ledger_exists(migration_database)
    finally:
        connection.close()


def test_stale_repair_waits_for_other_writers_to_stop(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    stale_run_id = _insert_linked_run(migration_database, "with-nhtsa-writer")
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(run_key,scope_name,status,source_keys_json,started_at,updated_at) "
                "VALUES ('migration-running','all','running',JSON_ARRAY(),NOW(6),NOW(6))"
            )
        with pytest.raises(MigrationError, match="running jobs exist in nhtsa_sync_runs"):
            runner.apply()
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM crawl_runs WHERE id=%s", (stale_run_id,))
            row = cursor.fetchone()
            assert row and row["status"] == "running"
        assert not _ledger_exists(migration_database)
    finally:
        connection.close()


def test_connection_contract_and_database_lock_fail_before_ledger_write(
    migration_database: MigrationDatabase,
) -> None:
    def connect_without_autocommit() -> MigrationConnection:
        return pymysql.connect(
            host=migration_database.host,
            port=migration_database.port,
            user=migration_database.user,
            password=migration_database.password,
            database=migration_database.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
        )

    invalid_runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=connect_without_autocommit,
    )
    with pytest.raises(MigrationError, match="must use autocommit"):
        invalid_runner.apply()
    assert not _ledger_exists(migration_database)

    lock_name = catalog_migrations.catalog_schema_lock_name(migration_database.database)
    holder = migration_database.connect()
    try:
        with holder.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name,))
            acquired = cursor.fetchone()
            assert acquired and acquired["acquired"] == 1
        locked_runner = CatalogMigrationRunner(
            migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
            station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
            connection_factory=migration_database.connect,
        )
        with pytest.raises(MigrationError, match="holds the database lock"):
            locked_runner.apply()
        assert not _ledger_exists(migration_database)
    finally:
        with holder.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        holder.close()


def test_fresh_schema_replay_is_repeatable_and_fail_closed(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    stale_run_id = _insert_linked_run(migration_database, "safe-stale")

    assert runner.apply() == ACTIVE_VERSIONS
    runner.check()
    first_rows = _migration_rows(migration_database)
    assert [cast(int, row["version"]) for row in first_rows] == list(ACTIVE_VERSIONS)
    assert all(row["state"] == "applied" and row["attempt_count"] == 1 for row in first_rows)
    assert runner.apply() == ()
    assert _migration_rows(migration_database) == first_rows

    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM brands WHERE name='MIGRATION-SENTINEL'"
            )
            sentinel = cursor.fetchone()
            assert sentinel and sentinel["row_count"] == 1
            cursor.execute(
                "SELECT change_key, sha256, state, attempt_count "
                "FROM catalog_schema_ledger WHERE kind='asset'"
            )
            asset = cursor.fetchone()
            assert (
                asset
                and asset["change_key"] == "asset:station-admin"
                and asset["state"] == "applied"
            )
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME LIKE '%partsouq\\_0%'"
            )
            routines = cursor.fetchone()
            assert routines and routines["row_count"] == 0
            cursor.execute(
                "SELECT status,finished_at,error_msg FROM crawl_runs WHERE id=%s",
                (stale_run_id,),
            )
            repaired = cursor.fetchone()
            assert repaired and repaired["status"] == "interrupted"
            assert repaired["finished_at"] is not None
            assert "migration preflight" in str(repaired["error_msg"])

            latest = ACTIVE_VERSIONS[-1]
            cursor.execute(
                "UPDATE catalog_schema_ledger "
                "SET state='failed', finished_at=NOW(6), error_text='injected' "
                "WHERE change_key=%s",
                (f"migration:{latest:03d}",),
            )
        with pytest.raises(MigrationError, match="retry that exact version"):
            runner.apply()
        assert runner.apply(retry_version=latest) == (latest,)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE catalog_schema_ledger SET sha256=%s WHERE change_key='asset:station-admin'",
                ("0" * 64,),
            )
        with pytest.raises(MigrationError, match="checksum drift"):
            runner.check()
        with pytest.raises(MigrationError, match="checksum drift"):
            runner.apply()
        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW station_admin_formal_vehicle_configurations")
            cursor.execute(
                "UPDATE catalog_schema_ledger SET sha256=%s,state='applied',"
                "finished_at=NOW(6),error_text=NULL WHERE change_key='asset:station-admin'",
                (STATION_ADMIN_ASSET_HASHES[0],),
            )
        assert runner.apply() == ()
        runner.check()
        assert _view_exists(migration_database, "station_admin_formal_vehicle_configurations")

        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW station_admin_formal_vehicle_configurations")
            cursor.execute(
                "UPDATE catalog_schema_ledger SET sha256=%s,state='failed',"
                "finished_at=NOW(6),error_text='injected' "
                "WHERE change_key='asset:station-admin'",
                (STATION_ADMIN_ASSET_HASHES[0],),
            )
        with pytest.raises(MigrationError, match="asset is dirty"):
            runner.apply()
        assert runner.apply(retry_station_asset=True) == ()
        runner.check()
        assert _view_exists(migration_database, "station_admin_formal_vehicle_configurations")

        unknown_preflight_run = _insert_linked_run(migration_database, "unknown-preflight")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO catalog_schema_ledger "
                "(change_key,kind,version,filename,sha256,state,attempt_count,started_at,"
                "finished_at,error_text) VALUES "
                "('migration:099','migration',99,'099_unknown.sql',%s,'applied',1,"
                "NOW(6),NOW(6),NULL)",
                ("0" * 64,),
            )
        attempts_before = _attempt_sum(migration_database)
        with pytest.raises(MigrationError, match="unknown change"):
            runner.apply()
        assert _attempt_sum(migration_database) == attempts_before
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM crawl_runs WHERE id=%s", (unknown_preflight_run,))
            unchanged = cursor.fetchone()
            assert unchanged and unchanged["status"] == "running"
    finally:
        connection.close()


@pytest.mark.parametrize("version", (13, 14))
@pytest.mark.parametrize("state", ("applying", "failed"))
def test_superseded_dirty_retry_replays_final_index_contract(
    migration_database: MigrationDatabase,
    version: int,
    state: str,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    _, filename, sha256 = next(item for item in CATALOG_MANIFEST if item[0] == version)
    finished_at = "NULL" if state == "applying" else "NOW(6)"
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO catalog_schema_ledger "
                "(change_key,kind,version,filename,sha256,state,attempt_count,started_at,"
                "finished_at,error_text) VALUES "
                f"(%s,'migration',%s,%s,%s,%s,1,NOW(6),{finished_at},%s)",
                (
                    f"migration:{version:03d}",
                    version,
                    filename,
                    sha256,
                    state,
                    None if state == "applying" else "injected",
                ),
            )

        with pytest.raises(MigrationError, match="retry that exact version"):
            runner.apply()
        assert runner.apply(retry_version=version) == (version, 15)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version,state,attempt_count FROM catalog_schema_ledger "
                "WHERE version IN (%s,15) ORDER BY version",
                (version,),
            )
            rows = list(cursor.fetchall())
        assert rows == [
            {"version": version, "state": "applied", "attempt_count": 2},
            {"version": 15, "state": "applied", "attempt_count": 2},
        ]
    finally:
        connection.close()


def test_forward_cleanup_drops_only_exact_superseded_routines(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    connection = migration_database.connect()
    exact_name = "assert_partsouq_013_output"
    near_name = "my_partsouqX013_backup"
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version IN (18,19,20,21,22)")
            cursor.execute(f"CREATE PROCEDURE {exact_name}() SELECT 1")
            cursor.execute(f"CREATE PROCEDURE {near_name}() SELECT 1")

        assert runner.apply() == (18, 19, 20, 21, 22)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT ROUTINE_NAME FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_NAME IN (%s,%s) "
                "ORDER BY ROUTINE_NAME",
                (exact_name, near_name),
            )
            assert [row["ROUTINE_NAME"] for row in cursor.fetchall()] == [near_name]
    finally:
        connection.close()


def test_migration_022_rebuilds_index_and_rejects_only_invalid_bounded_runs(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version=22")
            cursor.execute("ALTER TABLE crawl_runs DROP CHECK chk_crawl_run_verified_evidence")
            cursor.execute("ALTER TABLE crawl_runs DROP CHECK chk_crawl_run_evidence_status")
            cursor.execute(
                "ALTER TABLE crawl_runs MODIFY COLUMN evidence_status "
                "VARCHAR(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci "
                "NOT NULL DEFAULT 'missing' AFTER error_msg"
            )
            cursor.execute(
                "ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_evidence_status "
                "CHECK (evidence_status IN "
                "('missing','collecting','verified','rejected'))"
            )
            cursor.execute(
                "ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_verified_evidence "
                "CHECK (evidence_status <> 'verified' OR ("
                "evidence_manifest_sha256 REGEXP '^[0-9a-f]{64}$' "
                "AND evidence_dataset_sha256 REGEXP '^[0-9a-f]{64}$' "
                "AND evidence_artifact_count > 0 AND evidence_record_count > 0 "
                "AND evidence_original_bytes > 0 AND evidence_stored_bytes > 0 "
                "AND evidence_verified_at IS NOT NULL))"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts "
                "DROP CHECK chk_partsouq_artifact_verified_sanitizer"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts DROP CHECK chk_partsouq_artifact_status"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts MODIFY COLUMN verification_status "
                "VARCHAR(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci "
                "NOT NULL DEFAULT 'pending' AFTER accepted_records_sha256"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts ADD CONSTRAINT "
                "chk_partsouq_artifact_verified_sanitizer CHECK ("
                "verification_status <> 'verified' OR "
                "BINARY sanitizer_version = BINARY 'partsouq-html-public-v2')"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts ADD CONSTRAINT "
                "chk_partsouq_artifact_status CHECK ("
                "verification_status IN ('pending','verified','superseded','rejected') "
                "AND (verification_status <> 'verified' OR verified_at IS NOT NULL))"
            )
            cursor.execute("ALTER TABLE groups_t DROP KEY idx_group_fetched_run_key")
            cursor.execute(
                "ALTER TABLE groups_t ADD KEY idx_group_fetched_run_key (fetched_status)"
            )
            cursor.execute("INSERT INTO brands (name) VALUES ('MIGRATION-022')")
            brand_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO models (brand_id,name) VALUES (%s,'MIGRATION-022')",
                (brand_id,),
            )
            model_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO vehicles (model_id,identity_hash,name,model_code,vid) "
                "VALUES (%s,%s,'MIGRATION-022','MIGRATION-022','MIGRATION-022')",
                (model_id, "1" * 64),
            )
            vehicle_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO categories (vehicle_id,name,cid) VALUES (%s,'MIGRATION-022','22')",
                (vehicle_id,),
            )
            category_id = cursor.lastrowid

            run_ids: dict[str, int] = {}
            for run_key, dataset_kind in (
                ("migration-022-invalid", "bounded"),
                ("migration-022-valid", "bounded"),
                ("migration-022-full", "full"),
            ):
                cursor.execute(
                    "INSERT INTO crawl_runs ("
                    "run_key,started_at,finished_at,status,dataset_kind,target_parts,"
                    "evidence_status,evidence_manifest_sha256,evidence_dataset_sha256,"
                    "evidence_artifact_count,evidence_record_count,evidence_original_bytes,"
                    "evidence_stored_bytes,evidence_verified_at) VALUES "
                    "(%s,NOW(6),NOW(6),'interrupted',%s,%s,'collecting',%s,%s,1,1,1,1,NOW(6))",
                    (
                        run_key,
                        dataset_kind,
                        10_000 if dataset_kind == "bounded" else None,
                        "a" * 64,
                        "b" * 64,
                    ),
                )
                assert cursor.lastrowid is not None
                run_ids[run_key] = int(cursor.lastrowid)

            cursor.execute(
                "INSERT INTO crawl_runs ("
                "run_key,started_at,finished_at,status,dataset_kind,target_parts,"
                "evidence_status,evidence_manifest_sha256,evidence_dataset_sha256,"
                "evidence_artifact_count,evidence_record_count,evidence_original_bytes,"
                "evidence_stored_bytes,evidence_verified_at) VALUES "
                "('migration-022-status-case',NOW(6),NOW(6),'interrupted','bounded',10000,"
                "'REJECTED',%s,%s,1,1,1,1,NOW(6))",
                ("c" * 64, "d" * 64),
            )
            assert cursor.lastrowid is not None
            run_ids["migration-022-status-case"] = int(cursor.lastrowid)

            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('catalog','daemon','failed',NOW(6),NOW(6),125)"
            )
            artifact_job_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO crawl_runs ("
                "run_key,started_at,finished_at,status,dataset_kind,target_parts,"
                "scheduled_job_run_id,evidence_status,evidence_manifest_sha256,"
                "evidence_dataset_sha256,evidence_artifact_count,evidence_record_count,"
                "evidence_original_bytes,evidence_stored_bytes,evidence_verified_at) "
                "VALUES ('migration-022-artifact-case',NOW(6),NOW(6),'interrupted',"
                "'bounded',10000,%s,'collecting',%s,%s,1,1,1,1,NOW(6))",
                (artifact_job_id, "e" * 64, "f" * 64),
            )
            assert cursor.lastrowid is not None
            artifact_run_id = int(cursor.lastrowid)
            run_ids["migration-022-artifact-case"] = artifact_run_id
            cursor.execute(
                "INSERT INTO partsouq_response_bodies "
                "(body_sha256,compression,body_blob,original_bytes,stored_bytes,"
                "sanitizer_version) VALUES (%s,'zlib',%s,1,1,%s)",
                ("9" * 64, b"x", "partsouq-html-public-v2"),
            )
            cursor.execute(
                "INSERT INTO partsouq_http_artifacts ("
                "crawl_run_id,scheduled_job_run_id,capture_kind,page_type,"
                "public_source_url,source_url_sha256,raw_body_sha256,body_sha256,"
                "sanitizer_version,http_status,content_type,challenge_detected,"
                "fetched_at,elapsed_ms,attempt,parser_name,parser_version,"
                "parser_context_json,parser_context_sha256,malformed_row_count,"
                "skipped_record_count,parsed_record_count,parsed_records_sha256,"
                "accepted_record_count,accepted_records_sha256,verification_status,"
                "verified_at) VALUES ("
                "%s,%s,'live_http','genuine','https://partsouq.com/en/catalog/genuine',"
                "%s,%s,%s,%s,200,'text/html',0,NOW(6),1,1,'parse_brands',"
                "'partsouq-catalog-parser-v1',JSON_OBJECT(),%s,0,0,1,%s,0,%s,"
                "'VERIFIED',NOW(6))",
                (
                    artifact_run_id,
                    artifact_job_id,
                    "8" * 64,
                    "7" * 64,
                    "9" * 64,
                    "partsouq-html-public-v2",
                    "6" * 64,
                    "5" * 64,
                    "4" * 64,
                ),
            )
            artifact_id = cursor.lastrowid

            for code, run_key, url in (
                (
                    "2201",
                    "migration-022-invalid",
                    "https://partsouq.com/en/catalog/genuine/unit?uid=9999",
                ),
                (
                    "2202",
                    "migration-022-valid",
                    "https://partsouq.com/en/catalog/genuine/unit?cid=22&uid=2202",
                ),
                (
                    "2203",
                    "migration-022-full",
                    "https://partsouq.com/en/catalog/genuine/unit?uid=2203&token=SECRET",
                ),
            ):
                cursor.execute(
                    "INSERT INTO groups_t ("
                    "category_id,code,name,uid,url,fetched_run_key,fetched_status) "
                    "VALUES (%s,%s,'MIGRATION-022',%s,%s,%s,'done')",
                    (category_id, code, code, url, run_key),
                )

        assert runner.apply() == (22,)
        runner.check()
        assert runner.apply() == ()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns_list "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='groups_t' AND INDEX_NAME='idx_group_fetched_run_key'"
            )
            assert cursor.fetchone() == {"columns_list": "fetched_run_key"}
            cursor.execute(
                "SELECT id,evidence_status,evidence_manifest_sha256,"
                "evidence_dataset_sha256,evidence_artifact_count,evidence_record_count,"
                "evidence_original_bytes,evidence_stored_bytes,evidence_verified_at "
                "FROM crawl_runs WHERE id IN (%s,%s,%s,%s,%s) ORDER BY id",
                tuple(run_ids.values()),
            )
            rows = list(cursor.fetchall())
            cursor.execute(
                "SELECT CHARACTER_SET_NAME,COLLATION_NAME,IS_NULLABLE,COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='crawl_runs' AND COLUMN_NAME='evidence_status'"
            )
            evidence_column = cursor.fetchone()
            cursor.execute(
                "SELECT verification_status,verified_at FROM partsouq_http_artifacts WHERE id=%s",
                (artifact_id,),
            )
            artifact = cursor.fetchone()
            cursor.execute(
                "SELECT CHARACTER_SET_NAME,COLLATION_NAME,IS_NULLABLE,COLUMN_DEFAULT "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='partsouq_http_artifacts' "
                "AND COLUMN_NAME='verification_status'"
            )
            artifact_status_column = cursor.fetchone()

        assert rows[0] == {
            "id": run_ids["migration-022-invalid"],
            "evidence_status": "rejected",
            "evidence_manifest_sha256": None,
            "evidence_dataset_sha256": None,
            "evidence_artifact_count": 0,
            "evidence_record_count": 0,
            "evidence_original_bytes": 0,
            "evidence_stored_bytes": 0,
            "evidence_verified_at": None,
        }
        assert rows[1]["id"] == run_ids["migration-022-valid"]
        assert rows[1]["evidence_status"] == "collecting"
        assert rows[1]["evidence_manifest_sha256"] == "a" * 64
        assert rows[2]["id"] == run_ids["migration-022-full"]
        assert rows[2]["evidence_status"] == "collecting"
        assert rows[2]["evidence_manifest_sha256"] == "a" * 64
        assert rows[3] == {
            "id": run_ids["migration-022-status-case"],
            "evidence_status": "rejected",
            "evidence_manifest_sha256": None,
            "evidence_dataset_sha256": None,
            "evidence_artifact_count": 0,
            "evidence_record_count": 0,
            "evidence_original_bytes": 0,
            "evidence_stored_bytes": 0,
            "evidence_verified_at": None,
        }
        assert rows[4] == {
            "id": run_ids["migration-022-artifact-case"],
            "evidence_status": "rejected",
            "evidence_manifest_sha256": None,
            "evidence_dataset_sha256": None,
            "evidence_artifact_count": 0,
            "evidence_record_count": 0,
            "evidence_original_bytes": 0,
            "evidence_stored_bytes": 0,
            "evidence_verified_at": None,
        }
        assert evidence_column == {
            "CHARACTER_SET_NAME": "ascii",
            "COLLATION_NAME": "ascii_bin",
            "IS_NULLABLE": "NO",
            "COLUMN_DEFAULT": "missing",
        }
        assert artifact == {"verification_status": "rejected", "verified_at": None}
        assert artifact_status_column == {
            "CHARACTER_SET_NAME": "ascii",
            "COLLATION_NAME": "ascii_bin",
            "IS_NULLABLE": "NO",
            "COLUMN_DEFAULT": "pending",
        }
        with connection.cursor() as cursor:
            with pytest.raises(pymysql.MySQLError) as error:
                cursor.execute(
                    "UPDATE crawl_runs SET evidence_status='REJECTED' WHERE id=%s",
                    (run_ids["migration-022-valid"],),
                )
            assert error.value.args[0] == 3819
            with pytest.raises(pymysql.MySQLError) as error:
                cursor.execute(
                    "UPDATE partsouq_http_artifacts "
                    "SET verification_status='VERIFIED',verified_at=NOW(6) WHERE id=%s",
                    (artifact_id,),
                )
            assert error.value.args[0] == 3819
    finally:
        connection.close()


def test_migration_020_backfills_and_rejects_legacy_verified_artifact(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts "
                "DROP CHECK chk_partsouq_artifact_verified_sanitizer"
            )
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name, trigger_mode, status, started_at, finished_at, exit_code) "
                "VALUES ('catalog', 'daemon', 'failed', NOW(6), NOW(6), 125)"
            )
            scheduled_job_run_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO crawl_runs "
                "(run_key, started_at, finished_at, status, dataset_kind, target_parts, "
                "scheduled_job_run_id, evidence_status) "
                "VALUES ('legacy-v1-evidence', NOW(6), NOW(6), 'interrupted', "
                "'bounded', 10000, %s, 'collecting')",
                (scheduled_job_run_id,),
            )
            crawl_run_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO partsouq_response_bodies "
                "(body_sha256, compression, body_blob, original_bytes, stored_bytes, "
                "sanitizer_version) VALUES (%s, 'zlib', %s, 1, 1, %s)",
                ("a" * 64, b"x", "partsouq-html-public-v1"),
            )
            cursor.execute(
                "INSERT INTO partsouq_http_artifacts "
                "(crawl_run_id, scheduled_job_run_id, capture_kind, page_type, "
                "public_source_url, source_url_sha256, raw_body_sha256, body_sha256, "
                "sanitizer_version, http_status, content_type, challenge_detected, "
                "fetched_at, elapsed_ms, attempt, parser_name, parser_version, "
                "parser_context_json, parser_context_sha256, malformed_row_count, "
                "skipped_record_count, parsed_record_count, parsed_records_sha256, "
                "accepted_record_count, accepted_records_sha256, verification_status, "
                "verified_at) VALUES "
                "(%s, %s, 'live_http', 'genuine', "
                "'https://partsouq.com/en/catalog/genuine', %s, %s, %s, %s, "
                "200, 'text/html', 0, NOW(6), 1, 1, 'parse_brands', "
                "'partsouq-catalog-parser-v1', JSON_OBJECT(), %s, 0, 0, 1, %s, "
                "0, %s, 'verified', NOW(6))",
                (
                    crawl_run_id,
                    scheduled_job_run_id,
                    "b" * 64,
                    "c" * 64,
                    "a" * 64,
                    "partsouq-html-public-v1",
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                ),
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts DROP CHECK chk_partsouq_artifact_sanitizer"
            )
            cursor.execute("ALTER TABLE partsouq_http_artifacts DROP COLUMN sanitizer_version")
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version IN (20,21,22)")

        assert runner.apply() == (20, 21, 22)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sanitizer_version, verification_status, verified_at "
                "FROM partsouq_http_artifacts WHERE crawl_run_id = %s",
                (crawl_run_id,),
            )
            artifact = cursor.fetchone()
            assert artifact == {
                "sanitizer_version": "partsouq-html-public-v1",
                "verification_status": "rejected",
                "verified_at": None,
            }
            cursor.execute(
                "SELECT CHARACTER_SET_NAME, COLLATION_NAME, IS_NULLABLE "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='partsouq_http_artifacts' "
                "AND COLUMN_NAME='sanitizer_version'"
            )
            assert cursor.fetchone() == {
                "CHARACTER_SET_NAME": "ascii",
                "COLLATION_NAME": "ascii_bin",
                "IS_NULLABLE": "NO",
            }
            cursor.execute(
                "SELECT evidence_status FROM crawl_runs WHERE id = %s",
                (crawl_run_id,),
            )
            assert cursor.fetchone() == {"evidence_status": "rejected"}
            cursor.execute(
                "UPDATE partsouq_http_artifacts SET sanitizer_version=%s, "
                "verification_status='verified', verified_at=NOW(6) WHERE crawl_run_id=%s",
                ("partsouq-html-public-v2", crawl_run_id),
            )
            for incompatible_version in (
                "PARTSOUQ-HTML-PUBLIC-V2",
                "partsouq-html-public-v2 ",
            ):
                with pytest.raises(pymysql.MySQLError) as error:
                    cursor.execute(
                        "UPDATE partsouq_http_artifacts SET sanitizer_version=%s "
                        "WHERE crawl_run_id=%s",
                        (incompatible_version, crawl_run_id),
                    )
                assert error.value.args[0] == 3819

            _, filename, sha256 = next(item for item in CATALOG_MANIFEST if item[0] == 20)
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts "
                "DROP CHECK chk_partsouq_artifact_verified_sanitizer"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_artifacts MODIFY COLUMN "
                "sanitizer_version VARCHAR(64) NULL AFTER body_sha256"
            )
            cursor.execute(
                "UPDATE partsouq_http_artifacts SET sanitizer_version=%s, "
                "verification_status='verified', verified_at=NOW(6) WHERE crawl_run_id=%s",
                ("partsouq-html-public-v1", crawl_run_id),
            )
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version IN (20,21,22)")
            cursor.execute(
                "INSERT INTO catalog_schema_ledger "
                "(change_key,kind,version,filename,sha256,state,attempt_count,started_at,"
                "finished_at,error_text) VALUES "
                "('migration:020','migration',20,%s,%s,'failed',1,NOW(6),NOW(6),'injected')",
                (filename, sha256),
            )
            cursor.execute(
                "CREATE PROCEDURE upgrade_partsouq_020_artifact_sanitizer_version() SELECT 1"
            )

        with pytest.raises(MigrationError, match="retry that exact version"):
            runner.apply()
        assert runner.apply(retry_version=20) == (20, 21, 22)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sanitizer_version,verification_status,verified_at "
                "FROM partsouq_http_artifacts WHERE crawl_run_id=%s",
                (crawl_run_id,),
            )
            assert cursor.fetchone() == {
                "sanitizer_version": "partsouq-html-public-v1",
                "verification_status": "rejected",
                "verified_at": None,
            }
            cursor.execute(
                "SELECT version,state,attempt_count FROM catalog_schema_ledger "
                "WHERE version IN (20,21,22) ORDER BY version"
            )
            assert list(cursor.fetchall()) == [
                {"version": 20, "state": "applied", "attempt_count": 2},
                {"version": 21, "state": "applied", "attempt_count": 1},
                {"version": 22, "state": "applied", "attempt_count": 1},
            ]
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA=DATABASE() "
                "AND ROUTINE_NAME='upgrade_partsouq_020_artifact_sanitizer_version'"
            )
            assert cursor.fetchone() == {"row_count": 0}
    finally:
        connection.close()


def test_migration_runner_operates_with_compose_application_user(
    migration_database: MigrationDatabase,
) -> None:
    user = os.environ["PARTSOUQ_DB_USER"]
    password = os.environ["PARTSOUQ_DB_PASSWORD"]
    if re.fullmatch(r"[A-Za-z0-9_.-]+", user) is None:
        raise ValueError("unsafe application database user")
    root = migration_database.connect()
    granted_host: str | None = None
    try:
        with root.cursor() as cursor:
            cursor.execute("SELECT Host FROM mysql.user WHERE User=%s ORDER BY Host", (user,))
            account = cursor.fetchone()
            assert account is not None, f"MySQL account {user!r} does not exist"
            host = str(account["Host"])
            if re.fullmatch(r"[A-Za-z0-9_.%-]+", host) is None:
                raise ValueError("unsafe application database account host")
            cursor.execute(
                f"GRANT ALL PRIVILEGES ON `{migration_database.database}`.* TO '{user}'@'{host}'"
            )
            granted_host = host

        application_database = MigrationDatabase(
            migration_database.host,
            migration_database.port,
            migration_database.database,
            user,
            password,
        )
        runner = CatalogMigrationRunner(
            migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
            station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
            connection_factory=application_database.connect,
        )
        assert runner.apply() == ACTIVE_VERSIONS
        runner.check()
    finally:
        if granted_host is not None:
            with root.cursor() as cursor:
                cursor.execute(
                    f"REVOKE ALL PRIVILEGES ON `{migration_database.database}`.* "
                    f"FROM '{user}'@'{granted_host}'"
                )
        root.close()


def test_migration_lock_defers_every_writer_before_running_marker(
    migration_database: MigrationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = migration_database.connect()
    lock_name = catalog_migrations.catalog_schema_lock_name(migration_database.database)
    monkeypatch.setattr(scheduler, "_connect", migration_database.connect)
    popen = mock.MagicMock()
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)
    catalog_db = Database()
    catalog_connection = pymysql.connect(
        host=migration_database.host,
        port=migration_database.port,
        user=migration_database.user,
        password=migration_database.password,
        database=migration_database.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
    )
    catalog_db._local.conn = catalog_connection
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    crawler = Crawler(mock.MagicMock(), catalog_db, workers=1)
    nhtsa = NhtsaMySQLRepository(migration_database.connect())
    try:
        with holder.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name,))
            row = cursor.fetchone()
            assert row and row["acquired"] == 1
            cursor.execute(
                "INSERT INTO admin_crawl_requests (job_name,requested_scope,status) "
                "VALUES ('nhtsa-vin','ZZZTEST00X0000001','pending')"
            )
            request_id = cursor.lastrowid
            assert request_id is not None

        assert scheduler._run("catalog", ["python", "-m", "crawler"]) == 75
        popen.assert_not_called()
        with pytest.raises(AdmissionLockBusy):
            scheduler._claim_request(request_id)
        with pytest.raises(AdmissionLockBusy):
            nhtsa.start_run("nhtsa-lock-test", "all", ("fixture",))
        with pytest.raises(AdmissionLockBusy):
            crawler.run()

        with holder.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM scheduled_job_runs WHERE status='running'"
            )
            scheduled = cursor.fetchone()
            assert scheduled and scheduled["row_count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM nhtsa_sync_runs WHERE status='running'"
            )
            nhtsa_runs = cursor.fetchone()
            assert nhtsa_runs and nhtsa_runs["row_count"] == 0
            cursor.execute("SELECT status FROM admin_crawl_requests WHERE id=%s", (request_id,))
            request = cursor.fetchone()
            assert request and request["status"] == "pending"
    finally:
        with holder.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        crawler.close()
        catalog_db.close()
        nhtsa.close()
        holder.close()


def test_writer_marker_is_durable_and_blocks_migration_after_short_lock(
    migration_database: MigrationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler, "_connect", migration_database.connect)
    first_id = scheduler._record_start("catalog")
    second_id = scheduler._record_start("nhtsa")
    assert first_id != second_id

    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    with pytest.raises(MigrationError, match="running jobs exist in scheduled_job_runs"):
        runner.apply()
    assert not _ledger_exists(migration_database)

    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,status FROM scheduled_job_runs WHERE id IN (%s,%s) ORDER BY id",
                (first_id, second_id),
            )
            assert [row["status"] for row in cursor.fetchall()] == ["running", "running"]
            cursor.execute(
                "DELETE FROM scheduled_job_runs WHERE id IN (%s,%s)",
                (first_id, second_id),
            )
    finally:
        connection.close()


def test_mysql_releases_admission_lock_when_owner_connection_closes(
    migration_database: MigrationDatabase,
) -> None:
    lock_name = catalog_migrations.catalog_schema_lock_name(migration_database.database)
    owner = migration_database.connect()
    with owner.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name,))
        row = cursor.fetchone()
        assert row and row["acquired"] == 1
    owner.close()

    successor = migration_database.connect()
    try:
        with successor.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name,))
            row = cursor.fetchone()
            assert row and row["acquired"] == 1
            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
    finally:
        successor.close()


def _insert_linked_run(
    database: MigrationDatabase,
    suffix: str,
    *,
    link: bool = True,
    job_name: str = "catalog",
    job_status: str = "failed",
    finished: bool = True,
    exit_code: int | None = -2,
) -> int:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code,output_text) "
                "VALUES (%s,'manual',%s,'2026-01-01 00:00:00',%s,%s,'fixture')",
                (
                    job_name,
                    job_status,
                    "2026-01-01 00:01:00" if finished else None,
                    exit_code,
                ),
            )
            job_id = cursor.lastrowid
            assert job_id is not None
            cursor.execute(
                "INSERT INTO crawl_runs "
                "(run_key,started_at,status,dataset_kind,target_parts,scheduled_job_run_id,"
                "error_msg) VALUES (%s,'2026-01-01 00:00:00','running','bounded',10000,%s,%s)",
                (
                    f"migration-{suffix}",
                    job_id if link else None,
                    "original crawler error",
                ),
            )
            run_id = cursor.lastrowid
            assert run_id is not None
            return run_id
    finally:
        connection.close()


def _migration_rows(database: MigrationDatabase) -> list[dict[str, object]]:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version, filename, sha256, state, attempt_count "
                "FROM catalog_schema_ledger WHERE kind='migration' ORDER BY version"
            )
            return list(cursor.fetchall())
    finally:
        connection.close()


def _attempt_sum(database: MigrationDatabase) -> int:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(attempt_count),0) AS attempt_sum FROM catalog_schema_ledger"
            )
            row = cursor.fetchone()
            assert row is not None
            return cast(int, row["attempt_sum"])
    finally:
        connection.close()


def _ledger_exists(database: MigrationDatabase) -> bool:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='catalog_schema_ledger'"
            )
            row = cursor.fetchone()
            return bool(row and row["row_count"] == 1)
    finally:
        connection.close()


def _view_exists(database: MigrationDatabase, view: str) -> bool:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
                (view,),
            )
            row = cursor.fetchone()
            return bool(row and row["row_count"] == 1)
    finally:
        connection.close()
