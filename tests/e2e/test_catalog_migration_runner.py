from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
RECOVERY_MIGRATION_VERSIONS = tuple(version for version in ACTIVE_VERSIONS if version >= 24)
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


def _downgrade_nhtsa_024_schema(cursor: DictCursor) -> None:
    cursor.execute(
        "ALTER TABLE nhtsa_current_artifacts "
        "DROP FOREIGN KEY fk_nhtsa_current_published_run, "
        "DROP INDEX idx_nhtsa_current_published_run, "
        "DROP COLUMN published_run_id"
    )
    cursor.execute(
        "ALTER TABLE nhtsa_sync_runs "
        "DROP CHECK chk_nhtsa_sync_status_lease, "
        "DROP FOREIGN KEY fk_nhtsa_sync_scheduled_job, "
        "DROP INDEX idx_nhtsa_sync_lease_expiry, "
        "DROP INDEX uq_nhtsa_sync_scheduled_job, "
        "DROP INDEX uq_nhtsa_sync_lease_slot, "
        "DROP COLUMN scheduled_job_run_id, "
        "DROP COLUMN lease_slot, "
        "DROP COLUMN lease_token, "
        "DROP COLUMN heartbeat_at, "
        "DROP COLUMN lease_expires_at"
    )
    cursor.execute(
        "ALTER TABLE scheduled_job_runs "
        "DROP FOREIGN KEY fk_scheduled_job_parent, "
        "DROP INDEX uq_scheduled_job_parent_stage, "
        "DROP COLUMN parent_scheduled_job_run_id"
    )
    cursor.execute("DELETE FROM nhtsa_schema_migrations WHERE version=2")
    # 移除 024 起的全部 ledger 列（含其後版本）：單刪 24 會留下
    # 「23 → 25」斷層觸發 gap 檢查；整段移除讓第二次 apply 自 024
    # 起依序重放（各 migration 對既有 schema 冪等）。
    cursor.execute("DELETE FROM catalog_schema_ledger WHERE version >= 24")


def _insert_stale_direct_nhtsa_tuple(
    cursor: DictCursor,
    *,
    with_parent: bool,
) -> tuple[int | None, int, int]:
    parent_id: int | None = None
    if with_parent:
        cursor.execute(
            "INSERT INTO scheduled_job_runs "
            "(job_name,trigger_mode,status,started_at,output_text) "
            "VALUES ('nhtsa','daemon','running',UTC_TIMESTAMP()-INTERVAL 10 MINUTE,"
            "'original parent output')"
        )
        parent_id = int(cursor.lastrowid)
    cursor.execute(
        "INSERT INTO scheduled_job_runs "
        "(parent_scheduled_job_run_id,job_name,trigger_mode,status,started_at,output_text) "
        "VALUES (%s,'nhtsa-bulk','daemon','running',"
        "UTC_TIMESTAMP()-INTERVAL 9 MINUTE,'original child output')",
        (parent_id,),
    )
    child_id = int(cursor.lastrowid)
    cursor.execute(
        "INSERT INTO nhtsa_sync_runs "
        "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
        "lease_slot,lease_token,started_at,updated_at,heartbeat_at,"
        "lease_expires_at,error_message) VALUES ("
        "%s,%s,'all','running',JSON_ARRAY(),'writer',%s,"
        "UTC_TIMESTAMP(6)-INTERVAL 8 MINUTE,"
        "UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE,"
        "UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE,"
        "UTC_TIMESTAMP(6)-INTERVAL 4 MINUTE,'original domain error')",
        (child_id, f"stale-direct-{uuid.uuid4().hex}", "e" * 64),
    )
    return parent_id, child_id, int(cursor.lastrowid)


def _nhtsa_recovery_snapshot(
    cursor: DictCursor,
    *,
    run_id: int,
    scheduled_ids: tuple[int, ...],
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    cursor.execute(
        "SELECT scheduled_job_run_id,status,started_at,updated_at,heartbeat_at,"
        "lease_expires_at,ended_at,lease_slot,lease_token,error_message "
        "FROM nhtsa_sync_runs WHERE id=%s",
        (run_id,),
    )
    domain = cursor.fetchone()
    assert domain is not None
    placeholders = ",".join("%s" for _scheduled_id in scheduled_ids)
    cursor.execute(
        "SELECT id,parent_scheduled_job_run_id,job_name,trigger_mode,status,started_at,"
        "finished_at,exit_code,output_text FROM scheduled_job_runs "
        f"WHERE id IN ({placeholders}) ORDER BY id",
        scheduled_ids,
    )
    return dict(domain), tuple(dict(row) for row in cursor.fetchall())


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
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at) "
                "VALUES ('nhtsa-bulk','manual','running',NOW(6))"
            )
            scheduled_job_run_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
                "lease_slot,lease_token,started_at,updated_at,heartbeat_at,lease_expires_at) "
                "VALUES (%s,'migration-running','all','running',JSON_ARRAY(),'writer',%s,"
                "NOW(6),NOW(6),NOW(6),DATE_ADD(NOW(6),INTERVAL 5 MINUTE))",
                (scheduled_job_run_id, "a" * 64),
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


def test_migration_028_upgrades_sparse_fitments_from_027_and_is_repeatable(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    connection = migration_database.connect()
    vin = "ZZZTEST00X0000001"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT VIEW_DEFINITION FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='v_vin_part_fitments'"
            )
            current_view = cursor.fetchone()
            assert current_view is not None
            current_definition = str(current_view["VIEW_DEFINITION"])
            legacy_definition = current_definition.replace(
                "manual-sparse-override",
                "legacy-sparse-disabled",
            )
            assert legacy_definition != current_definition

            cursor.execute(
                "CREATE TABLE migration_028_catalog_fixture AS "
                "SELECT * FROM v_current_catalog_parts WHERE FALSE"
            )
            cursor.execute(
                "CREATE OR REPLACE VIEW v_current_catalog_parts AS "
                "SELECT * FROM migration_028_catalog_fixture"
            )
            cursor.execute(
                "INSERT INTO brands(name,code,url) VALUES "
                "('TOYOTA','MIGRATION-028','https://example.invalid/toyota')"
            )
            brand_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO models(brand_id,name,ssd,url) VALUES "
                "(%s,'CAMRY',NULL,'https://example.invalid/camry')",
                (brand_id,),
            )
            model_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO vehicles("
                "model_id,identity_hash,name,model_code,prod_period,production_from,"
                "production_to,grade,engine,vid,url) VALUES "
                "(%s,%s,'CAMRY','AXVA70','01.2018 - 12.2019','2018-01','2019-12',"
                "'XLE','A25A-FXS','VID-028','https://example.invalid/vehicle')",
                (model_id, "a" * 64),
            )
            vehicle_id = int(cursor.lastrowid)
            fixture_columns = (
                "dataset_scope,source_crawl_run_id,part_id,vehicle_id,model_id,"
                "vehicle_vid,brand,model,vehicle_name,vehicle_code,prod_period,"
                "production_from,production_to,engine,trim_name,part_name,part_number,"
                "part_number_normalized,category_id,category_cid,category_main,"
                "category_group,group_id,group_code,group_uid,part_range,part_from,part_to,"
                "source_url,note,quantity,code,snapshot_at"
            )
            cursor.execute(
                f"INSERT INTO migration_028_catalog_fixture({fixture_columns}) VALUES "
                "('full',1,101,%s,%s,'VID-028','TOYOTA','CAMRY','CAMRY','AXVA70',"
                "'01.2018 - 12.2019','2018-01','2019-12','A25A-FXS','XLE',"
                "'OIL FILTER','MIGRATION-028-PART','MIGRATION028PART',1,'CID-028',"
                "'ENGINE','OIL FILTER',1,'1502','UID-028','01.2018 - 12.2019',"
                "'2018-01','2019-12','https://example.invalid/part',NULL,'01','15601',NOW(6)),"
                "('full',1,102,%s,%s,'VID-028','TOYOTA','CAMRY','CAMRY','AXVA70',"
                "'01.2018 - 12.2019','2018-01','2019-12','SPLIT-ROW-ENGINE','XLE',"
                "'SPLIT FILTER','MIGRATION-028-SPLIT','MIGRATION028SPLIT',1,'CID-028',"
                "'ENGINE','OIL FILTER',1,'1502','UID-028','01.2018 - 12.2019',"
                "'2018-01','2019-12','https://example.invalid/split',NULL,'01','15602',NOW(6))",
                (vehicle_id, model_id, vehicle_id, model_id),
            )
            cursor.execute(
                "INSERT INTO nhtsa_source_artifacts("
                "dataset_name,source_key,source_url,http_status,response_headers_json,sha256,"
                "stored_path,byte_count,parser_name,parser_version,status,downloaded_at) VALUES "
                "('vpic_vin_decodes','migration-028-vin','https://example.invalid/vin',200,"
                "JSON_OBJECT(),%s,'/tmp/migration-028.json',1,'migration_test','1','imported',"
                "NOW(6))",
                ("b" * 64,),
            )
            artifact_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO nhtsa_vin_decodes("
                "vin,make_name,model_name,model_year,engine_configuration,engine_model,"
                "displacement_l,trim_name,series_name,error_code,error_text,payload_json,"
                "source_url,source_artifact_id,decoded_at) VALUES "
                "(%s,'TOYOTA',NULL,2018,NULL,NULL,NULL,NULL,NULL,'0',NULL,JSON_OBJECT(),"
                "'https://example.invalid/vin',%s,NOW(6))",
                (vin, artifact_id),
            )
            cursor.execute(
                "INSERT INTO admin_vehicle_mappings("
                "vin_prefix,vin,partsouq_vehicle_id,make_name,model_name,model_year,engine,"
                "trim_name,source_name,source_reference) VALUES "
                "(%s,%s,%s,'TOYOTA','CAMRY',2018,'A25A-FXS','XLE',"
                "'manual-sparse-override','migration 028 fixture')",
                (vin[:11], vin, vehicle_id),
            )
            cursor.execute("CREATE OR REPLACE VIEW v_vin_part_fitments AS " + legacy_definition)
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (28,29,30,31,32,33,34,35,36)"
            )
            cursor.execute(
                "SELECT MAX(version) AS latest_version FROM catalog_schema_ledger "
                "WHERE kind='migration'"
            )
            assert cursor.fetchone() == {"latest_version": 27}
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM v_vin_part_fitments WHERE vin=%s",
                (vin,),
            )
            assert cursor.fetchone() == {"row_count": 0}

        assert runner.apply() == (28, 29, 30, 31, 32, 33, 34, 35, 36)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE OR REPLACE VIEW v_current_catalog_parts AS "
                "SELECT * FROM migration_028_catalog_fixture"
            )
            cursor.execute(
                "SELECT VIEW_DEFINITION FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='v_vin_part_fitments'"
            )
            upgraded_view = cursor.fetchone()
            assert upgraded_view == {"VIEW_DEFINITION": current_definition}
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM v_vin_part_fitments WHERE vin=%s",
                (vin,),
            )
            assert cursor.fetchone() == {"row_count": 1}

            cursor.execute(
                "UPDATE nhtsa_vin_decodes SET engine_configuration='In-Line' WHERE vin=%s",
                (vin,),
            )
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM v_vin_part_fitments WHERE vin=%s",
                (vin,),
            )
            assert cursor.fetchone() == {"row_count": 1}
            cursor.execute(
                "UPDATE nhtsa_vin_decodes SET engine_configuration=NULL WHERE vin=%s",
                (vin,),
            )
            cursor.execute(
                "UPDATE migration_028_catalog_fixture "
                "SET production_from='2020-01',production_to='2020-12' WHERE part_id=101"
            )
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM v_vin_part_fitments WHERE vin=%s",
                (vin,),
            )
            assert cursor.fetchone() == {"row_count": 0}

            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (28,29,30,31,32,33,34,35,36)"
            )
        assert runner.apply() == (28, 29, 30, 31, 32, 33, 34, 35, 36)
        runner.check()
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE OR REPLACE VIEW v_current_catalog_parts AS "
                "SELECT * FROM migration_028_catalog_fixture"
            )
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM v_vin_part_fitments WHERE vin=%s",
                (vin,),
            )
            assert cursor.fetchone() == {"row_count": 0}
    finally:
        connection.close()


def test_migration_029_adds_repeatable_model_scope_contract(
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
            cursor.execute("ALTER TABLE crawl_runs DROP CHECK chk_crawl_run_model_scope")
            cursor.execute(
                "ALTER TABLE crawl_runs DROP COLUMN scope_vehicle_year_floor, "
                "DROP COLUMN scope_model, DROP COLUMN scope_brand"
            )
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (29,30,31,32,33,34,35,36)"
            )

        assert runner.apply() == (29, 30, 31, 32, 33, 34, 35, 36)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='crawl_runs' "
                "AND COLUMN_NAME IN "
                "('scope_brand','scope_model','scope_vehicle_year_floor') "
                "ORDER BY ORDINAL_POSITION"
            )
            assert list(cursor.fetchall()) == [
                {
                    "COLUMN_NAME": "scope_brand",
                    "COLUMN_TYPE": "varchar(64)",
                    "IS_NULLABLE": "YES",
                },
                {
                    "COLUMN_NAME": "scope_model",
                    "COLUMN_TYPE": "varchar(128)",
                    "IS_NULLABLE": "YES",
                },
                {
                    "COLUMN_NAME": "scope_vehicle_year_floor",
                    "COLUMN_TYPE": "smallint unsigned",
                    "IS_NULLABLE": "YES",
                },
            ]
            cursor.execute(
                "INSERT INTO crawl_runs "
                "(run_key,started_at,status,dataset_kind,target_parts) "
                "VALUES ('migration-029-legacy',NOW(),'error','bounded',10000)"
            )
            cursor.execute(
                "INSERT INTO crawl_runs "
                "(run_key,started_at,status,dataset_kind,target_parts,scope_brand,"
                "scope_model,scope_vehicle_year_floor) VALUES "
                "('migration-029-scoped',NOW(),'error','bounded',10000,"
                "'TOYOTA','TACOMA',2006)"
            )
            with pytest.raises(pymysql.MySQLError) as error:
                cursor.execute(
                    "INSERT INTO crawl_runs "
                    "(run_key,started_at,status,dataset_kind,target_parts,scope_brand,"
                    "scope_model) VALUES "
                    "('migration-029-partial',NOW(),'error','bounded',10000,"
                    "'TOYOTA','TACOMA')"
                )
            assert error.value.args[0] == 3819
            for run_key, brand, model, year_floor in (
                ("migration-029-blank-brand", " ", "TACOMA", 2006),
                ("migration-029-blank-model", "TOYOTA", " ", 2006),
                ("migration-029-floor-low", "TOYOTA", "TACOMA", 1885),
                ("migration-029-floor-high", "TOYOTA", "TACOMA", 2101),
            ):
                with pytest.raises(pymysql.MySQLError) as error:
                    cursor.execute(
                        "INSERT INTO crawl_runs "
                        "(run_key,started_at,status,dataset_kind,target_parts,scope_brand,"
                        "scope_model,scope_vehicle_year_floor) "
                        "VALUES (%s,NOW(),'error','bounded',10000,%s,%s,%s)",
                        (run_key, brand, model, year_floor),
                    )
                assert error.value.args[0] == 3819

            cursor.execute("ALTER TABLE crawl_runs DROP CHECK chk_crawl_run_model_scope")
            cursor.execute(
                "ALTER TABLE crawl_runs ADD CONSTRAINT chk_crawl_run_model_scope "
                "CHECK (scope_brand IS NULL OR scope_brand IS NOT NULL)"
            )
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (29,30,31,32,33,34,35,36)"
            )

        assert runner.apply() == (29, 30, 31, 32, 33, 34, 35, 36)
        runner.check()
        with connection.cursor() as cursor:
            with pytest.raises(pymysql.MySQLError) as error:
                cursor.execute(
                    "INSERT INTO crawl_runs "
                    "(run_key,started_at,status,dataset_kind,target_parts,scope_brand,"
                    "scope_model,scope_vehicle_year_floor) VALUES "
                    "('migration-029-repaired-check',NOW(),'error','bounded',10000,"
                    "'TOYOTA','TACOMA',1885)"
                )
            assert error.value.args[0] == 3819
    finally:
        connection.close()


def test_migration_032_backfills_only_unchanged_unique_snapshot_evidence(
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
                "CREATE TABLE migration_032_catalog_fixture AS "
                "SELECT * FROM v_current_catalog_parts WHERE FALSE"
            )
            cursor.execute(
                "CREATE OR REPLACE VIEW v_current_catalog_parts AS "
                "SELECT * FROM migration_032_catalog_fixture"
            )
            cursor.execute("DROP TRIGGER IF EXISTS prevent_bounded_parts_update")
            cursor.execute("ALTER TABLE bounded_parts DROP COLUMN evidence_record_sha256")
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version IN (32,33,34,35,36)")
            cursor.execute(
                "INSERT INTO brands(name,code,url) VALUES "
                "('TOYOTA','MIGRATION-032','https://example.invalid/toyota')"
            )
            brand_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO models(brand_id,name,ssd,url) VALUES "
                "(%s,'TACOMA',NULL,'https://example.invalid/tacoma')",
                (brand_id,),
            )
            model_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO vehicles("
                "model_id,identity_hash,name,model_code,prod_period,production_from,"
                "production_to,grade,engine,vid,url) VALUES "
                "(%s,%s,'TACOMA','N300','01.2018 - 12.2019','2018-01','2019-12',"
                "'TRD','2GR','VID-032','https://example.invalid/vehicle')",
                (model_id, "a" * 64),
            )
            vehicle_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO categories(vehicle_id,name,cid) VALUES (%s,'ENGINE','C-032')",
                (vehicle_id,),
            )
            category_id = int(cursor.lastrowid)
            source_url = (
                "https://partsouq.com/en/catalog/genuine/unit?cid=C-032&uid=UID-032&vid=VID-032"
            )
            cursor.execute(
                "INSERT INTO groups_t(category_id,code,name,uid,url) "
                "VALUES (%s,'1101','GROUP','UID-032',%s)",
                (category_id, source_url),
            )
            group_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO scheduled_job_runs("
                "job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('catalog','daemon','completed',NOW(6),NOW(6),0)"
            )
            scheduled_job_run_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO crawl_runs("
                "run_key,started_at,finished_at,status,dataset_kind,target_parts,"
                "scheduled_job_run_id) VALUES "
                "('migration-032-backfill',NOW(6),NOW(6),'interrupted','bounded',10000,%s)",
                (scheduled_job_run_id,),
            )
            crawl_run_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO parts("
                "group_id,part_number,name,code,note,quantity,range_str,part_from,part_to,"
                "seen_run_id) VALUES "
                "(%s,'P-032','PART-032','11000','NOTE','01','01.2018 - 12.2019',"
                "'2018-01','2019-12',%s)",
                (group_id, crawl_run_id),
            )
            part_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO bounded_parts("
                "part_id,crawl_run_id,vehicle_id,model_id,vehicle_vid,brand,model,"
                "vehicle_name,vehicle_code,prod_period,production_from,production_to,"
                "engine,trim_name,part_name,part_number,part_number_normalized,category_id,"
                "category_cid,category_main,category_group,group_id,group_code,group_uid,"
                "part_range,part_from,part_to,source_url,note,quantity,code,snapshot_at) VALUES "
                "(%s,%s,%s,%s,'VID-032','TOYOTA','TACOMA','TACOMA','N300',"
                "'01.2018 - 12.2019','2018-01','2019-12','2GR','TRD','PART-032',"
                "'P-032','P032',%s,'C-032','ENGINE','GROUP',%s,'1101','UID-032',"
                "'01.2018 - 12.2019','2018-01','2019-12',%s,'NOTE','01','11000',NOW())",
                (part_id, crawl_run_id, vehicle_id, model_id, category_id, group_id, source_url),
            )
            cursor.execute(
                "INSERT INTO partsouq_response_bodies("
                "body_sha256,compression,body_blob,original_bytes,stored_bytes,"
                "sanitizer_version) VALUES (%s,'zlib',%s,1,1,%s)",
                ("b" * 64, b"x", "partsouq-html-public-v2"),
            )
            cursor.execute(
                "INSERT INTO partsouq_http_artifacts("
                "crawl_run_id,scheduled_job_run_id,capture_kind,page_type,public_source_url,"
                "source_url_sha256,raw_body_sha256,body_sha256,sanitizer_version,http_status,"
                "content_type,challenge_detected,fetched_at,elapsed_ms,attempt,parser_name,"
                "parser_version,parser_context_json,parser_context_sha256,malformed_row_count,"
                "skipped_record_count,parsed_record_count,parsed_records_sha256,"
                "accepted_record_count,accepted_records_sha256,verification_status,verified_at) "
                "VALUES (%s,%s,'live_http','unit',%s,%s,%s,%s,%s,200,'text/html',0,NOW(6),"
                "1,1,'parse_parts','partsouq-catalog-parser-v1',JSON_OBJECT(),%s,0,0,1,%s,"
                "1,%s,'verified',NOW(6))",
                (
                    crawl_run_id,
                    scheduled_job_run_id,
                    source_url,
                    "c" * 64,
                    "d" * 64,
                    "b" * 64,
                    "partsouq-html-public-v2",
                    "e" * 64,
                    "f" * 64,
                    "0" * 64,
                ),
            )
            first_artifact_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO partsouq_artifact_records("
                "artifact_id,crawl_run_id,record_type,natural_key_sha256,"
                "parent_natural_key_sha256,record_sha256,accepted,part_id) VALUES "
                "(%s,%s,'part',%s,%s,%s,1,%s)",
                (first_artifact_id, crawl_run_id, "1" * 64, "2" * 64, "3" * 64, part_id),
            )

        assert runner.apply() == (32, 33, 34, 35, 36)
        runner.check()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT evidence_record_sha256 FROM bounded_parts WHERE part_id=%s",
                (part_id,),
            )
            assert cursor.fetchone() == {"evidence_record_sha256": "3" * 64}

            for statement, params in (
                (
                    "UPDATE bounded_parts SET part_name='MUTATED' WHERE part_id=%s",
                    (part_id,),
                ),
                (
                    "UPDATE bounded_parts SET vehicle_id=vehicle_id + 1 WHERE part_id=%s",
                    (part_id,),
                ),
                (
                    "UPDATE bounded_parts SET evidence_record_sha256=NULL WHERE part_id=%s",
                    (part_id,),
                ),
            ):
                with pytest.raises(pymysql.MySQLError) as error:
                    cursor.execute(statement, params)
                assert error.value.args[0] == 1644
            cursor.execute(
                "SELECT part_name,vehicle_id,evidence_record_sha256 "
                "FROM bounded_parts WHERE part_id=%s",
                (part_id,),
            )
            assert cursor.fetchone() == {
                "part_name": "PART-032",
                "vehicle_id": vehicle_id,
                "evidence_record_sha256": "3" * 64,
            }

            # 後續 raw crawl 改寫原列，migration 不得把舊 snapshot 猜測重綁。
            cursor.execute("DROP TRIGGER IF EXISTS prevent_bounded_parts_update")
            cursor.execute(
                "UPDATE bounded_parts SET evidence_record_sha256=NULL WHERE part_id=%s",
                (part_id,),
            )
            cursor.execute(
                "UPDATE parts SET name='RAW-REWRITTEN',seen_run_id=NULL WHERE id=%s",
                (part_id,),
            )
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version IN (32,33,34,35,36)")

        assert runner.apply() == (32, 33, 34, 35, 36)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT evidence_record_sha256 FROM bounded_parts WHERE part_id=%s",
                (part_id,),
            )
            assert cursor.fetchone() == {"evidence_record_sha256": None}

            # 即使 raw row 恢復，若有兩筆 accepted evidence，仍不可任選其一。
            cursor.execute(
                "UPDATE parts SET name='PART-032',seen_run_id=%s WHERE id=%s",
                (crawl_run_id, part_id),
            )
            cursor.execute(
                "INSERT INTO partsouq_response_bodies("
                "body_sha256,compression,body_blob,original_bytes,stored_bytes,"
                "sanitizer_version) VALUES (%s,'zlib',%s,1,1,%s)",
                ("4" * 64, b"y", "partsouq-html-public-v2"),
            )
            cursor.execute(
                "INSERT INTO partsouq_http_artifacts("
                "crawl_run_id,scheduled_job_run_id,capture_kind,page_type,public_source_url,"
                "source_url_sha256,raw_body_sha256,body_sha256,sanitizer_version,http_status,"
                "content_type,challenge_detected,fetched_at,elapsed_ms,attempt,parser_name,"
                "parser_version,parser_context_json,parser_context_sha256,malformed_row_count,"
                "skipped_record_count,parsed_record_count,parsed_records_sha256,"
                "accepted_record_count,accepted_records_sha256,verification_status,verified_at) "
                "VALUES (%s,%s,'live_http','unit',%s,%s,%s,%s,%s,200,'text/html',0,NOW(6),"
                "1,2,'parse_parts','partsouq-catalog-parser-v1',JSON_OBJECT(),%s,0,0,1,%s,"
                "1,%s,'verified',NOW(6))",
                (
                    crawl_run_id,
                    scheduled_job_run_id,
                    source_url,
                    "5" * 64,
                    "6" * 64,
                    "4" * 64,
                    "partsouq-html-public-v2",
                    "7" * 64,
                    "8" * 64,
                    "9" * 64,
                ),
            )
            second_artifact_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO partsouq_artifact_records("
                "artifact_id,crawl_run_id,record_type,natural_key_sha256,"
                "parent_natural_key_sha256,record_sha256,accepted,part_id) VALUES "
                "(%s,%s,'part',%s,%s,%s,1,%s)",
                (second_artifact_id, crawl_run_id, "a" * 64, "b" * 64, "c" * 64, part_id),
            )
            cursor.execute("DROP TRIGGER IF EXISTS prevent_bounded_parts_update")
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version IN (32,33,34,35,36)")

        assert runner.apply() == (32, 33, 34, 35, 36)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT evidence_record_sha256 FROM bounded_parts WHERE part_id=%s",
                (part_id,),
            )
            assert cursor.fetchone() == {"evidence_record_sha256": None}

            # 035 不重寫 immutable snapshot；它只剔除無法由原始料號重算
            # normalized number 的 legacy 列，讓整份 10,000 筆 snapshot fail closed。
            cursor.execute("DROP TRIGGER IF EXISTS prevent_bounded_parts_update")
            cursor.execute(
                "UPDATE bounded_parts SET part_number_normalized='CORRUPTED', "
                "evidence_record_sha256=%s WHERE part_id=%s",
                ("d" * 64, part_id),
            )
            cursor.execute(
                "CREATE TRIGGER prevent_bounded_parts_update "
                "BEFORE UPDATE ON bounded_parts FOR EACH ROW "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
                "'bounded_parts snapshot is immutable; publish a replacement snapshot'"
            )
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version IN (35,36)")

        assert runner.apply() == (35, 36)
        runner.check()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM bounded_parts WHERE part_id=%s", (part_id,)
            )
            assert cursor.fetchone() == {"row_count": 0}
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='prevent_bounded_parts_update'"
            )
            assert cursor.fetchone() == {"row_count": 1}
    finally:
        connection.close()


def test_migration_036_upgrades_legacy_current_view_and_preserves_receipt_gate(
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
                "SELECT DEFINER,SECURITY_TYPE FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='v_current_catalog_parts_evidence_base'"
            )
            legacy_metadata = cursor.fetchone()
            assert legacy_metadata is not None
            cursor.execute("SHOW CREATE VIEW v_current_catalog_parts_evidence_base")
            base_create = cursor.fetchone()
            assert base_create is not None
            legacy_create = str(base_create["Create View"]).replace(
                "`v_current_catalog_parts_evidence_base`",
                "`v_current_catalog_parts`",
                1,
            )
            assert legacy_create != str(base_create["Create View"])

            # 模擬只到 035 的資料庫：舊正式 view 保留原本 DEFINER，036 必須
            # 將它改為 base，再建立 receipt wrapper；既有 dependent view 仍要可查。
            cursor.execute("DROP VIEW v_current_catalog_parts")
            cursor.execute("DROP VIEW v_current_catalog_parts_evidence_base")
            cursor.execute(legacy_create)
            cursor.execute(
                "CREATE VIEW migration_036_legacy_dependent AS "
                "SELECT COUNT(*) AS part_count FROM v_current_catalog_parts"
            )
            cursor.execute("DROP TRIGGER IF EXISTS prevent_bounded_group_receipt_update")
            cursor.execute("DROP TRIGGER IF EXISTS prevent_bounded_group_receipt_delete")
            cursor.execute("DROP TABLE bounded_group_receipts")
            cursor.execute("ALTER TABLE bounded_parts DROP INDEX idx_bounded_run_group")
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version=36")

        assert runner.apply() == (36,)
        runner.check()
        assert runner.apply() == ()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DEFINER,SECURITY_TYPE,VIEW_DEFINITION "
                "FROM information_schema.VIEWS WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME='v_current_catalog_parts_evidence_base'"
            )
            base = cursor.fetchone()
            assert base is not None
            assert {
                "DEFINER": base["DEFINER"],
                "SECURITY_TYPE": base["SECURITY_TYPE"],
            } == legacy_metadata
            assert "verified_bounded_evidence" in str(base["VIEW_DEFINITION"]).lower()
            cursor.execute(
                "SELECT VIEW_DEFINITION FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='v_current_catalog_parts'"
            )
            wrapper = cursor.fetchone()
            assert wrapper is not None
            wrapper_definition = str(wrapper["VIEW_DEFINITION"]).lower()
            for marker in (
                "v_current_catalog_parts_evidence_base",
                "bounded_group_receipts",
                "receipt_integrity",
                "verified_bounded_group_receipts",
            ):
                assert marker in wrapper_definition
            cursor.execute("SELECT part_count FROM migration_036_legacy_dependent")
            assert cursor.fetchone() == {"part_count": 0}
            cursor.execute(
                "SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION "
                "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND TRIGGER_NAME IN "
                "('prevent_bounded_group_receipt_update','prevent_bounded_group_receipt_delete') "
                "ORDER BY TRIGGER_NAME"
            )
            assert list(cursor.fetchall()) == [
                {
                    "TRIGGER_NAME": "prevent_bounded_group_receipt_delete",
                    "EVENT_OBJECT_TABLE": "bounded_group_receipts",
                    "ACTION_TIMING": "BEFORE",
                    "EVENT_MANIPULATION": "DELETE",
                },
                {
                    "TRIGGER_NAME": "prevent_bounded_group_receipt_update",
                    "EVENT_OBJECT_TABLE": "bounded_group_receipts",
                    "ACTION_TIMING": "BEFORE",
                    "EVENT_MANIPULATION": "UPDATE",
                },
            ]

            cursor.execute("SHOW CREATE VIEW v_current_catalog_parts")
            wrapper_create = cursor.fetchone()
            assert wrapper_create is not None
            cursor.execute(
                "CREATE OR REPLACE VIEW v_current_catalog_parts AS "
                "SELECT * FROM v_current_catalog_parts_evidence_base"
            )
        with pytest.raises(
            MigrationError,
            match="bounded group receipt schema contract mismatch: formal_receipt_view_ready",
        ):
            runner.check()

        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW v_current_catalog_parts")
            cursor.execute(str(wrapper_create["Create View"]))
            cursor.execute("SHOW CREATE VIEW station_admin_vin_vehicle_mappings")
            station_view_create = cursor.fetchone()
            assert station_view_create is not None
            cursor.execute(
                "CREATE OR REPLACE VIEW station_admin_vin_vehicle_mappings AS "
                "SELECT 'legacy' AS vin"
            )
        with pytest.raises(
            MigrationError, match="station-admin VIN decode schema contract mismatch"
        ):
            runner.check()

        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW station_admin_vin_vehicle_mappings")
            cursor.execute(str(station_view_create["Create View"]))
            cursor.execute(
                "ALTER TABLE bounded_group_receipts DROP CHECK chk_bounded_group_receipt_counts"
            )
            cursor.execute(
                "ALTER TABLE bounded_group_receipts "
                "ADD CONSTRAINT chk_bounded_group_receipt_counts "
                "CHECK (accepted_part_count <= parsed_part_count AND ("
                "(status = 'partial' AND accepted_part_count = parsed_part_count) OR "
                "(status = 'done' AND accepted_part_count > 0 "
                "AND accepted_part_count < parsed_part_count)))"
            )
        with pytest.raises(
            MigrationError,
            match="bounded group receipt schema contract mismatch: receipt_checks_ready",
        ):
            runner.check()

        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM catalog_schema_ledger WHERE version=36")
        assert runner.apply() == (36,)
        runner.check()
        with connection.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            try:
                for status, parsed_count, accepted_count in (
                    ("done", 2, 1),
                    ("partial", 2, 2),
                ):
                    with pytest.raises(pymysql.MySQLError) as error:
                        cursor.execute(
                            "INSERT INTO bounded_group_receipts("
                            "crawl_run_id,group_id,source_artifact_id,status,"
                            "parsed_part_count,accepted_part_count,skipped_record_count) "
                            "VALUES (999001,999001,999001,%s,%s,%s,0)",
                            (status, parsed_count, accepted_count),
                        )
                    assert error.value.args[0] == 3819
            finally:
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        connection.close()


def test_catalog_desired_scope_sync_is_canonical_and_does_not_touch_same_scope(
    migration_database: MigrationDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    monkeypatch.setattr(scheduler, "_connect", migration_database.connect)
    monkeypatch.setattr(scheduler, "_catalog_bounded_scope", lambda: ("toyota", "tacoma", 2006))

    assert scheduler._sync_catalog_desired_bounded_scope() == ("toyota", "tacoma", 2006)
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT scope_brand,scope_model,scope_vehicle_year_floor,updated_at "
                "FROM catalog_desired_bounded_scope"
            )
            first = cursor.fetchone()
            assert first is not None
            assert first["scope_brand"] == "toyota"
            assert first["scope_model"] == "tacoma"
            assert first["scope_vehicle_year_floor"] == 2006

        assert scheduler._sync_catalog_desired_bounded_scope() == ("toyota", "tacoma", 2006)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT scope_brand,scope_model,scope_vehicle_year_floor,updated_at "
                "FROM catalog_desired_bounded_scope"
            )
            same_scope = cursor.fetchone()
            assert same_scope == first
            cursor.execute(
                "UPDATE catalog_desired_bounded_scope SET updated_at='2000-01-01 00:00:00'"
            )

        monkeypatch.setattr(
            scheduler, "_catalog_bounded_scope", lambda: ("toyota", "corolla", 2006)
        )
        assert scheduler._sync_catalog_desired_bounded_scope() == ("toyota", "corolla", 2006)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT scope_brand,scope_model,scope_vehicle_year_floor,updated_at "
                "FROM catalog_desired_bounded_scope"
            )
            changed = cursor.fetchone()
            assert changed is not None
            assert changed["scope_brand"] == "toyota"
            assert changed["scope_model"] == "corolla"
            assert changed["scope_vehicle_year_floor"] == 2006
            assert changed["updated_at"] > datetime(2000, 1, 1)
            with pytest.raises(pymysql.MySQLError):
                cursor.execute("UPDATE catalog_desired_bounded_scope SET scope_brand='TOYOTA'")
    finally:
        connection.close()


def test_apply_recovers_one_stale_catalog_daemon_after_ledger_validation(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    job_id, run_id = _insert_running_daemon(migration_database, "safe-auto-recovery")

    assert runner.apply(recover_stale_catalog_daemon_seconds=900) == ()
    runner.check()

    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,exit_code,finished_at FROM scheduled_job_runs WHERE id=%s",
                (job_id,),
            )
            job = cursor.fetchone()
            assert job and job["status"] == "failed"
            assert job["exit_code"] == catalog_migrations.STALE_SCHEDULER_EXIT_CODE
            assert job["finished_at"] is not None
            cursor.execute("SELECT status,finished_at FROM crawl_runs WHERE id=%s", (run_id,))
            run = cursor.fetchone()
            assert run and run["status"] == "interrupted"
            assert run["finished_at"] is not None
    finally:
        connection.close()


def test_apply_recovers_stale_catalog_daemon_after_terminal_full_success(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    job_id, run_id = _insert_running_daemon(
        migration_database,
        "terminal-full-success",
        crawl_status="success",
    )

    assert runner.apply(recover_stale_catalog_daemon_seconds=900) == ()
    runner.check()

    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,exit_code,finished_at FROM scheduled_job_runs WHERE id=%s",
                (job_id,),
            )
            job = cursor.fetchone()
            assert job and job["status"] == "failed"
            assert job["exit_code"] == catalog_migrations.STALE_SCHEDULER_EXIT_CODE
            assert job["finished_at"] is not None
            cursor.execute("SELECT status,finished_at FROM crawl_runs WHERE id=%s", (run_id,))
            run = cursor.fetchone()
            assert run and run["status"] == "success"
            assert run["finished_at"] is not None
    finally:
        connection.close()


def test_stale_catalog_daemon_is_unchanged_when_ledger_checksum_drifts(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    job_id, run_id = _insert_running_daemon(migration_database, "checksum-drift")
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE catalog_schema_ledger SET sha256=%s WHERE change_key='migration:001'",
                ("0" * 64,),
            )
        with pytest.raises(MigrationError, match="checksum drift"):
            runner.apply(recover_stale_catalog_daemon_seconds=900)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,exit_code,finished_at FROM scheduled_job_runs WHERE id=%s", (job_id,)
            )
            job = cursor.fetchone()
            assert job and job["status"] == "running"
            assert job["exit_code"] is None and job["finished_at"] is None
            cursor.execute("SELECT status,finished_at FROM crawl_runs WHERE id=%s", (run_id,))
            run = cursor.fetchone()
            assert run and run["status"] == "running" and run["finished_at"] is None
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
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36)"
            )
            cursor.execute(f"CREATE PROCEDURE {exact_name}() SELECT 1")
            cursor.execute(f"CREATE PROCEDURE {near_name}() SELECT 1")

        assert runner.apply() == (
            18,
            19,
            20,
            21,
            22,
            23,
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            33,
            34,
            35,
            36,
        )
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
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (22,23,24,25,26,27,28,29,30,31,32,33,34,35,36)"
            )
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

        assert runner.apply() == (22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36)
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
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36)"
            )

        assert runner.apply() == (
            20,
            21,
            22,
            23,
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            33,
            34,
            35,
            36,
        )
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
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36)"
            )
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
        assert runner.apply(retry_version=20) == (
            20,
            21,
            22,
            23,
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            33,
            34,
            35,
            36,
        )
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
                "WHERE version IN (20,21,22,23,24) ORDER BY version"
            )
            assert list(cursor.fetchall()) == [
                {"version": 20, "state": "applied", "attempt_count": 2},
                {"version": 21, "state": "applied", "attempt_count": 1},
                {"version": 22, "state": "applied", "attempt_count": 1},
                {"version": 23, "state": "applied", "attempt_count": 1},
                {"version": 24, "state": "applied", "attempt_count": 1},
            ]
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA=DATABASE() "
                "AND ROUTINE_NAME='upgrade_partsouq_020_artifact_sanitizer_version'"
            )
            assert cursor.fetchone() == {"row_count": 0}
    finally:
        connection.close()


def test_migration_check_rejects_http_diagnostic_schema_and_unique_index_drift(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    runner.check()
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics MODIFY COLUMN content_type TEXT NOT NULL"
            )
        with pytest.raises(MigrationError, match="diagnostics schema contract mismatch"):
            runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "MODIFY COLUMN content_type VARCHAR(128) NOT NULL"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "DROP INDEX uq_partsouq_diagnostic_group_reason, "
                "ADD UNIQUE KEY uq_partsouq_diagnostic_group_reason "
                "(group_id, crawl_run_id, reason)"
            )
        with pytest.raises(MigrationError, match="diagnostics schema contract mismatch"):
            runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "ADD KEY fk_partsouq_diagnostic_group (group_id)"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "DROP INDEX uq_partsouq_diagnostic_group_reason, "
                "ADD UNIQUE KEY uq_partsouq_diagnostic_group_reason "
                "(crawl_run_id, group_id, reason)"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "DROP INDEX idx_partsouq_diagnostic_body, "
                "ADD KEY idx_partsouq_diagnostic_body (body_sha256(8))"
            )
        with pytest.raises(MigrationError, match="diagnostics schema contract mismatch"):
            runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "DROP INDEX idx_partsouq_diagnostic_body, "
                "ADD KEY idx_partsouq_diagnostic_body (body_sha256)"
            )
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "DROP CHECK chk_partsouq_diagnostic_sanitizer, "
                "ADD CONSTRAINT chk_partsouq_diagnostic_sanitizer "
                "CHECK (sanitizer_version IS NOT NULL "
                "OR 'partsouq-html-public-v2' = '')"
            )
        with pytest.raises(MigrationError, match="diagnostics schema contract mismatch"):
            runner.check()
    finally:
        connection.close()


def test_migration_023_creates_http_diagnostics_from_missing_table(
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
            cursor.execute("DROP TABLE partsouq_http_diagnostics")
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (23,24,25,26,27,28,29,30,31,32,33,34,35,36)"
            )
        assert runner.apply() == (23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36)
        runner.check()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS column_count FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='partsouq_http_diagnostics'"
            )
            assert cursor.fetchone() == {"column_count": 25}
    finally:
        connection.close()


def test_migration_check_rejects_disabled_http_diagnostic_check(
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
                "ALTER TABLE partsouq_http_diagnostics "
                "ALTER CHECK chk_partsouq_diagnostic_sanitizer NOT ENFORCED"
            )
        with pytest.raises(MigrationError, match="diagnostics schema contract mismatch"):
            runner.check()
    finally:
        connection.close()


def test_migration_023_exact_postflight_marks_dirty_before_finish_and_can_retry(
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
                "ALTER TABLE partsouq_http_diagnostics MODIFY COLUMN content_type TEXT NOT NULL"
            )
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (23,24,25,26,27,28,29,30,31,32,33,34,35,36)"
            )
        with pytest.raises(MigrationError, match="migration:023 failed"):
            runner.apply()
        with connection.cursor() as cursor:
            cursor.execute("SELECT state,attempt_count FROM catalog_schema_ledger WHERE version=23")
            assert cursor.fetchone() == {"state": "failed", "attempt_count": 1}
            cursor.execute(
                "ALTER TABLE partsouq_http_diagnostics "
                "MODIFY COLUMN content_type VARCHAR(128) NOT NULL"
            )
        assert runner.apply(retry_version=23) == (
            23,
            24,
            25,
            26,
            27,
            28,
            29,
            30,
            31,
            32,
            33,
            34,
            35,
            36,
        )
        runner.check()
        with connection.cursor() as cursor:
            cursor.execute("SELECT state,attempt_count FROM catalog_schema_ledger WHERE version=23")
            assert cursor.fetchone() == {"state": "applied", "attempt_count": 2}
    finally:
        connection.close()


def test_migration_024_upgrades_legacy_nhtsa_schema_and_is_repeatable(
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
            _downgrade_nhtsa_024_schema(cursor)

        assert runner.apply() == (24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36)
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM catalog_schema_ledger WHERE version IN (24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36)"
            )
        assert runner.apply() == (24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36)
        runner.check()
    finally:
        connection.close()


def test_migration_024_recovers_one_unique_cross_second_legacy_nhtsa_run(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    base = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=10)
    run_key = f"nhtsa-bulk-{base.strftime('%Y%m%dT%H%M%SZ')}"
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            _downgrade_nhtsa_024_schema(cursor)
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','failed',%s,%s,125)",
                (base + timedelta(seconds=1), base + timedelta(seconds=4)),
            )
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(run_key,scope_name,status,source_keys_json,started_at,updated_at,error_message) "
                "VALUES (%s,'all','running',JSON_ARRAY(),%s,%s,'original failure')",
                (
                    run_key,
                    base + timedelta(seconds=2),
                    base + timedelta(seconds=3),
                ),
            )
            run_id = int(cursor.lastrowid)

        assert runner.apply(recover_stale_nhtsa_daemon_seconds=60) == RECOVERY_MIGRATION_VERSIONS
        runner.check()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,started_at,updated_at,ended_at,error_message,"
                "scheduled_job_run_id FROM nhtsa_sync_runs WHERE id=%s",
                (run_id,),
            )
            recovered = cursor.fetchone()
        assert recovered is not None
        assert recovered["status"] == "interrupted"
        assert recovered["ended_at"] >= recovered["updated_at"] >= recovered["started_at"]
        assert recovered["scheduled_job_run_id"] is None
        assert "original failure" in str(recovered["error_message"])
        assert "legacy-matched scheduler child" in str(recovered["error_message"])
    finally:
        connection.close()


def test_migration_024_legacy_recovery_tolerates_child_second_precision(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    base = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=10)
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            _downgrade_nhtsa_024_schema(cursor)
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','failed',%s,%s,125)",
                (base + timedelta(seconds=1), base + timedelta(seconds=4)),
            )
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(run_key,scope_name,status,source_keys_json,started_at,updated_at) "
                "VALUES (%s,'all','running',JSON_ARRAY(),%s,%s)",
                (
                    f"nhtsa-bulk-{base.strftime('%Y%m%dT%H%M%SZ')}",
                    base + timedelta(seconds=2, microseconds=250_000),
                    base + timedelta(seconds=4, microseconds=500_000),
                ),
            )
            run_id = int(cursor.lastrowid)

        assert runner.apply(recover_stale_nhtsa_daemon_seconds=60) == RECOVERY_MIGRATION_VERSIONS
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM nhtsa_sync_runs WHERE id=%s", (run_id,))
            assert cursor.fetchone() == {"status": "interrupted"}
    finally:
        connection.close()


def test_post_024_recovery_tolerates_child_second_precision(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    base = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=10)
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','failed',%s,%s,125)",
                (base, base + timedelta(seconds=2)),
            )
            child_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
                "lease_slot,lease_token,started_at,updated_at,heartbeat_at,lease_expires_at) "
                "VALUES (%s,%s,'all','running',JSON_ARRAY(),'writer',%s,%s,%s,%s,%s)",
                (
                    child_id,
                    f"stale-direct-precision-{uuid.uuid4().hex}",
                    "d" * 64,
                    base + timedelta(microseconds=250_000),
                    base + timedelta(seconds=2, microseconds=500_000),
                    base + timedelta(seconds=1, microseconds=250_000),
                    base + timedelta(seconds=3),
                ),
            )
            run_id = int(cursor.lastrowid)

        assert runner.apply(recover_stale_nhtsa_daemon_seconds=60) == ()
        with connection.cursor() as cursor:
            cursor.execute("SELECT status FROM nhtsa_sync_runs WHERE id=%s", (run_id,))
            assert cursor.fetchone() == {"status": "interrupted"}
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("offset_seconds", "should_recover"),
    ((5, True), (6, False)),
)
def test_migration_024_legacy_link_window_stops_after_five_seconds(
    migration_database: MigrationDatabase,
    offset_seconds: int,
    should_recover: bool,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    base = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=10)
    marker_time = base + timedelta(seconds=offset_seconds)
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            _downgrade_nhtsa_024_schema(cursor)
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','failed',%s,%s,125)",
                (marker_time, base + timedelta(seconds=10)),
            )
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(run_key,scope_name,status,source_keys_json,started_at,updated_at,error_message) "
                "VALUES (%s,'all','running',JSON_ARRAY(),%s,%s,'boundary error')",
                (
                    f"nhtsa-bulk-{base.strftime('%Y%m%dT%H%M%SZ')}",
                    marker_time,
                    marker_time,
                ),
            )
            run_id = int(cursor.lastrowid)

        if should_recover:
            assert (
                runner.apply(recover_stale_nhtsa_daemon_seconds=60) == RECOVERY_MIGRATION_VERSIONS
            )
        else:
            with pytest.raises(MigrationError, match="one unique recoverable scheduler child"):
                runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,ended_at,error_message FROM nhtsa_sync_runs WHERE id=%s",
                (run_id,),
            )
            run = cursor.fetchone()
            assert run is not None
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM catalog_schema_ledger WHERE version=24"
            )
            ledger = cursor.fetchone()
        if should_recover:
            assert run["status"] == "interrupted"
            assert run["ended_at"] is not None
            assert "boundary error" in str(run["error_message"])
            assert ledger == {"row_count": 1}
        else:
            assert run == {
                "status": "running",
                "ended_at": None,
                "error_message": "boundary error",
            }
            assert ledger == {"row_count": 0}
    finally:
        connection.close()


@pytest.mark.parametrize("ambiguity", ["two_children", "two_runs"])
def test_migration_024_rejects_ambiguous_legacy_nhtsa_lineage(
    migration_database: MigrationDatabase,
    ambiguity: str,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    base = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=10)
    connection = migration_database.connect()
    run_ids: list[int] = []
    try:
        with connection.cursor() as cursor:
            _downgrade_nhtsa_024_schema(cursor)
            child_starts = (
                (base + timedelta(seconds=1), base + timedelta(seconds=2))
                if ambiguity == "two_children"
                else (base + timedelta(seconds=2),)
            )
            for offset, child_started in enumerate(child_starts, start=1):
                cursor.execute(
                    "INSERT INTO scheduled_job_runs "
                    "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                    "VALUES ('nhtsa-bulk','daemon','failed',%s,%s,125)",
                    (child_started, base + timedelta(seconds=4 + offset)),
                )
            run_bases = (
                (base,) if ambiguity == "two_children" else (base, base + timedelta(seconds=1))
            )
            for run_base in run_bases:
                cursor.execute(
                    "INSERT INTO nhtsa_sync_runs "
                    "(run_key,scope_name,status,source_keys_json,started_at,updated_at) "
                    "VALUES (%s,'all','running',JSON_ARRAY(),%s,%s)",
                    (
                        f"nhtsa-bulk-{run_base.strftime('%Y%m%dT%H%M%SZ')}",
                        base + timedelta(seconds=3),
                        base + timedelta(seconds=3),
                    ),
                )
                run_ids.append(int(cursor.lastrowid))

        with pytest.raises(MigrationError, match="one unique recoverable scheduler child"):
            runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            placeholders = ",".join("%s" for _run_id in run_ids)
            cursor.execute(
                f"SELECT status FROM nhtsa_sync_runs WHERE id IN ({placeholders}) ORDER BY id",
                tuple(run_ids),
            )
            assert [row["status"] for row in cursor.fetchall()] == ["running"] * len(run_ids)
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM catalog_schema_ledger WHERE version=24"
            )
            assert cursor.fetchone() == {"row_count": 0}
    finally:
        connection.close()


def test_migration_024_does_not_recover_recent_legacy_nhtsa_run(
    migration_database: MigrationDatabase,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    base = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(seconds=30)
    connection = migration_database.connect()
    try:
        with connection.cursor() as cursor:
            _downgrade_nhtsa_024_schema(cursor)
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','failed',%s,%s,125)",
                (base + timedelta(seconds=1), base + timedelta(seconds=4)),
            )
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(run_key,scope_name,status,source_keys_json,started_at,updated_at) "
                "VALUES (%s,'all','running',JSON_ARRAY(),%s,%s)",
                (
                    f"nhtsa-bulk-{base.strftime('%Y%m%dT%H%M%SZ')}",
                    base + timedelta(seconds=2),
                    base + timedelta(seconds=3),
                ),
            )
            run_id = int(cursor.lastrowid)

        with pytest.raises(MigrationError, match="one unique recoverable scheduler child"):
            runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            cursor.execute("SELECT status,ended_at FROM nhtsa_sync_runs WHERE id=%s", (run_id,))
            assert cursor.fetchone() == {"status": "running", "ended_at": None}
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM catalog_schema_ledger WHERE version=24"
            )
            assert cursor.fetchone() == {"row_count": 0}
    finally:
        connection.close()


def test_post_024_stale_nhtsa_recovery_uses_direct_link_and_clears_lease(
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
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','failed',"
                "UTC_TIMESTAMP()-INTERVAL 5 MINUTE,"
                "UTC_TIMESTAMP()-INTERVAL 90 SECOND,125)"
            )
            scheduled_job_run_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
                "lease_slot,lease_token,started_at,updated_at,heartbeat_at,"
                "lease_expires_at) VALUES ("
                "%s,'direct-link-does-not-use-timestamp','all','running',JSON_ARRAY(),"
                "'writer',%s,UTC_TIMESTAMP()-INTERVAL 5 MINUTE,"
                "UTC_TIMESTAMP()-INTERVAL 3 MINUTE,"
                "UTC_TIMESTAMP()-INTERVAL 3 MINUTE,"
                "UTC_TIMESTAMP()-INTERVAL 2 MINUTE)",
                (scheduled_job_run_id, "a" * 64),
            )
            run_id = int(cursor.lastrowid)

        assert runner.apply(recover_stale_nhtsa_daemon_seconds=60) == ()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,ended_at,lease_slot,lease_token,lease_expires_at,"
                "scheduled_job_run_id,error_message FROM nhtsa_sync_runs WHERE id=%s",
                (run_id,),
            )
            recovered = cursor.fetchone()
        assert recovered is not None
        assert recovered["status"] == "interrupted"
        assert recovered["ended_at"] is not None
        assert recovered["lease_slot"] is None
        assert recovered["lease_token"] is None
        assert recovered["lease_expires_at"] is None
        assert recovered["scheduled_job_run_id"] == scheduled_job_run_id
        assert "directly linked scheduler child" in recovered["error_message"]
    finally:
        connection.close()


def test_post_024_stale_nhtsa_recovery_repairs_running_child_and_parent_atomically(
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
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,output_text) "
                "VALUES ('nhtsa','daemon','running',UTC_TIMESTAMP()-INTERVAL 10 MINUTE,"
                "'parent output')"
            )
            parent_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(parent_scheduled_job_run_id,job_name,trigger_mode,status,started_at,output_text) "
                "VALUES (%s,'nhtsa-bulk','daemon','running',"
                "UTC_TIMESTAMP()-INTERVAL 9 MINUTE,'child output')",
                (parent_id,),
            )
            child_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
                "lease_slot,lease_token,started_at,updated_at,heartbeat_at,"
                "lease_expires_at,error_message) VALUES ("
                "%s,%s,'all','running',JSON_ARRAY(),'writer',%s,"
                "UTC_TIMESTAMP(6)-INTERVAL 9 MINUTE,"
                "UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE,"
                "UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE,"
                "UTC_TIMESTAMP(6)-INTERVAL 4 MINUTE,'original domain error')",
                (child_id, f"parent-hard-kill-{uuid.uuid4().hex}", "c" * 64),
            )
            run_id = int(cursor.lastrowid)

        assert runner.apply(recover_stale_nhtsa_daemon_seconds=60) == ()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,started_at,updated_at,ended_at,lease_slot,lease_token,"
                "lease_expires_at,error_message FROM nhtsa_sync_runs WHERE id=%s",
                (run_id,),
            )
            domain = cursor.fetchone()
            cursor.execute(
                "SELECT id,status,finished_at,exit_code,output_text FROM scheduled_job_runs "
                "WHERE id IN (%s,%s) ORDER BY id",
                (parent_id, child_id),
            )
            scheduled = list(cursor.fetchall())
        assert domain is not None
        assert domain["status"] == "interrupted"
        assert domain["ended_at"] >= domain["updated_at"] >= domain["started_at"]
        assert domain["lease_slot"] is None
        assert domain["lease_token"] is None
        assert domain["lease_expires_at"] is None
        assert "original domain error" in str(domain["error_message"])
        assert [row["status"] for row in scheduled] == ["failed", "failed"]
        assert [row["exit_code"] for row in scheduled] == [125, 125]
        assert all(row["finished_at"] is not None for row in scheduled)
        assert "composite parent interrupted" in str(scheduled[0]["output_text"])
        assert "expired NHTSA lease recovered" in str(scheduled[1]["output_text"])
    finally:
        connection.close()


@pytest.mark.parametrize("recent_marker", ["child", "parent"])
def test_post_024_stale_nhtsa_recovery_rejects_recent_failed_marker(
    migration_database: MigrationDatabase,
    recent_marker: str,
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
            parent_id, child_id, run_id = _insert_stale_direct_nhtsa_tuple(
                cursor,
                with_parent=recent_marker == "parent",
            )
            marker_id = child_id if recent_marker == "child" else parent_id
            assert marker_id is not None
            cursor.execute(
                "UPDATE scheduled_job_runs SET status='failed',"
                "finished_at=UTC_TIMESTAMP()-INTERVAL 30 SECOND,exit_code=125 WHERE id=%s",
                (marker_id,),
            )
            scheduled_ids = (child_id,) if parent_id is None else (parent_id, child_id)
            before = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=scheduled_ids,
            )

        with pytest.raises(MigrationError, match="one unique recoverable scheduler child"):
            runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            after = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=scheduled_ids,
            )
        assert after == before
    finally:
        connection.close()


@pytest.mark.parametrize("mutation", ["child_identity", "parent_link"])
def test_post_024_stale_nhtsa_exact_recovery_rolls_back_after_candidate_mutation(
    migration_database: MigrationDatabase,
    mutation: str,
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
            parent_id, child_id, run_id = _insert_stale_direct_nhtsa_tuple(
                cursor,
                with_parent=True,
            )
            assert parent_id is not None
        candidates = catalog_migrations._repairable_stale_nhtsa_runs(connection, 60)
        assert [int(row["run_id"]) for row in candidates] == [run_id]

        with connection.cursor() as cursor:
            scheduled_ids = [parent_id, child_id]
            if mutation == "child_identity":
                cursor.execute(
                    "UPDATE scheduled_job_runs SET job_name='nhtsa-vin' WHERE id=%s",
                    (child_id,),
                )
            else:
                cursor.execute(
                    "INSERT INTO scheduled_job_runs "
                    "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                    "VALUES ('nhtsa','daemon','failed',"
                    "UTC_TIMESTAMP()-INTERVAL 12 MINUTE,"
                    "UTC_TIMESTAMP()-INTERVAL 11 MINUTE,125)"
                )
                alternate_parent_id = int(cursor.lastrowid)
                scheduled_ids.append(alternate_parent_id)
                cursor.execute(
                    "UPDATE scheduled_job_runs SET parent_scheduled_job_run_id=%s WHERE id=%s",
                    (alternate_parent_id, child_id),
                )
            exact_scheduled_ids = tuple(scheduled_ids)
            domain_before, scheduled_before = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=exact_scheduled_ids,
            )

        with pytest.raises(MigrationError, match="scheduler marker changed during recovery"):
            catalog_migrations._repair_stale_nhtsa_runs(connection, candidates)

        with connection.cursor() as cursor:
            domain_after, scheduled_after = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=exact_scheduled_ids,
            )
        assert domain_after == domain_before
        assert scheduled_after == scheduled_before
    finally:
        connection.close()


@pytest.mark.parametrize("blocker", ["scheduler", "admin"])
def test_post_024_stale_nhtsa_recovery_preflight_blocker_leaves_candidate_untouched(
    migration_database: MigrationDatabase,
    blocker: str,
) -> None:
    runner = CatalogMigrationRunner(
        migrations_dir=PROJECT_ROOT / "migrations" / "catalog",
        station_schema_path=PROJECT_ROOT / "db" / "station_admin.sql",
        connection_factory=migration_database.connect,
    )
    assert runner.apply() == ACTIVE_VERSIONS
    connection = migration_database.connect()
    blocker_table = "scheduled_job_runs" if blocker == "scheduler" else "admin_crawl_requests"
    try:
        with connection.cursor() as cursor:
            parent_id, child_id, run_id = _insert_stale_direct_nhtsa_tuple(
                cursor,
                with_parent=True,
            )
            assert parent_id is not None
            if blocker == "scheduler":
                cursor.execute(
                    "INSERT INTO scheduled_job_runs "
                    "(job_name,trigger_mode,status,started_at) "
                    "VALUES ('unrelated','manual','running',"
                    "UTC_TIMESTAMP()-INTERVAL 10 MINUTE)"
                )
            else:
                cursor.execute(
                    "INSERT INTO admin_crawl_requests "
                    "(job_name,status,started_at) VALUES "
                    "('unrelated','running',UTC_TIMESTAMP()-INTERVAL 10 MINUTE)"
                )
            blocker_id = int(cursor.lastrowid)
            before = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=(parent_id, child_id),
            )
            cursor.execute(
                "SELECT state,attempt_count,started_at,finished_at,error_text "
                "FROM catalog_schema_ledger WHERE version=24"
            )
            ledger_before = cursor.fetchone()

        with pytest.raises(MigrationError, match=f"running jobs exist in {blocker_table}"):
            runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            after = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=(parent_id, child_id),
            )
            cursor.execute(
                f"SELECT status FROM {blocker_table} WHERE id=%s",
                (blocker_id,),
            )
            blocker_after = cursor.fetchone()
            cursor.execute(
                "SELECT state,attempt_count,started_at,finished_at,error_text "
                "FROM catalog_schema_ledger WHERE version=24"
            )
            ledger_after = cursor.fetchone()
        assert after == before
        assert blocker_after == {"status": "running"}
        assert ledger_after == ledger_before
    finally:
        connection.close()


def test_post_024_stale_nhtsa_recovery_preserves_malformed_causal_times(
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
            _parent_id, child_id, run_id = _insert_stale_direct_nhtsa_tuple(
                cursor,
                with_parent=False,
            )
            cursor.execute(
                "UPDATE nhtsa_sync_runs SET "
                "started_at=UTC_TIMESTAMP(6)-INTERVAL 4 MINUTE,"
                "updated_at=UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE,"
                "heartbeat_at=UTC_TIMESTAMP(6)-INTERVAL 5 MINUTE,"
                "lease_expires_at=UTC_TIMESTAMP(6)-INTERVAL 3 MINUTE "
                "WHERE id=%s",
                (run_id,),
            )
            before = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=(child_id,),
            )

        with pytest.raises(MigrationError, match="one unique recoverable scheduler child"):
            runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            after = _nhtsa_recovery_snapshot(
                cursor,
                run_id=run_id,
                scheduled_ids=(child_id,),
            )
        assert after == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    "unsafe_state",
    ["active_lease", "recent_heartbeat", "active_sibling", "wrong_trigger"],
)
def test_post_024_stale_nhtsa_recovery_rejects_active_or_ambiguous_lineage(
    migration_database: MigrationDatabase,
    unsafe_state: str,
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
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at) "
                "VALUES ('nhtsa','daemon','running',UTC_TIMESTAMP()-INTERVAL 10 MINUTE)"
            )
            parent_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(parent_scheduled_job_run_id,job_name,trigger_mode,status,started_at) "
                "VALUES (%s,'nhtsa-bulk','daemon','running',"
                "UTC_TIMESTAMP()-INTERVAL 9 MINUTE)",
                (parent_id,),
            )
            child_id = int(cursor.lastrowid)
            if unsafe_state == "wrong_trigger":
                cursor.execute(
                    "UPDATE scheduled_job_runs SET trigger_mode='manual' WHERE id=%s",
                    (child_id,),
                )
            heartbeat_interval = "30 SECOND" if unsafe_state == "recent_heartbeat" else "5 MINUTE"
            expiry_expression = (
                "UTC_TIMESTAMP(6)+INTERVAL 5 MINUTE"
                if unsafe_state == "active_lease"
                else "UTC_TIMESTAMP(6)-INTERVAL 10 SECOND"
                if unsafe_state == "recent_heartbeat"
                else "UTC_TIMESTAMP(6)-INTERVAL 4 MINUTE"
            )
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
                "lease_slot,lease_token,started_at,updated_at,heartbeat_at,lease_expires_at) "
                "VALUES (%s,%s,'all','running',JSON_ARRAY(),'writer',%s,"
                "UTC_TIMESTAMP(6)-INTERVAL 9 MINUTE,"
                f"UTC_TIMESTAMP(6)-INTERVAL {heartbeat_interval},"
                f"UTC_TIMESTAMP(6)-INTERVAL {heartbeat_interval},{expiry_expression})",
                (child_id, f"unsafe-{unsafe_state}-{uuid.uuid4().hex}", "d" * 64),
            )
            run_id = int(cursor.lastrowid)
            sibling_id: int | None = None
            if unsafe_state == "active_sibling":
                cursor.execute(
                    "INSERT INTO scheduled_job_runs "
                    "(parent_scheduled_job_run_id,job_name,trigger_mode,status,started_at) "
                    "VALUES (%s,'nhtsa-api','daemon','running',"
                    "UTC_TIMESTAMP()-INTERVAL 8 MINUTE)",
                    (parent_id,),
                )
                sibling_id = int(cursor.lastrowid)

        with pytest.raises(MigrationError, match="one unique recoverable scheduler child"):
            runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,lease_slot,lease_token FROM nhtsa_sync_runs WHERE id=%s",
                (run_id,),
            )
            assert cursor.fetchone() == {
                "status": "running",
                "lease_slot": "writer",
                "lease_token": "d" * 64,
            }
            scheduled_ids = [parent_id, child_id]
            if sibling_id is not None:
                scheduled_ids.append(sibling_id)
            placeholders = ",".join("%s" for _scheduled_id in scheduled_ids)
            cursor.execute(
                f"SELECT status FROM scheduled_job_runs WHERE id IN ({placeholders}) ORDER BY id",
                tuple(scheduled_ids),
            )
            assert [row["status"] for row in cursor.fetchall()] == ["running"] * len(scheduled_ids)
    finally:
        connection.close()


def test_post_024_stale_nhtsa_recovery_rejects_timestamp_match_without_direct_link(
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
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','failed','2026-08-23 10:00:00',"
                "'2026-08-23 10:01:00',125)"
            )
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code) "
                "VALUES ('nhtsa-bulk','daemon','completed','2026-08-23 09:59:00',"
                "'2026-08-23 10:01:00',0)"
            )
            linked_completed_job_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
                "lease_slot,lease_token,started_at,updated_at,heartbeat_at,"
                "lease_expires_at) VALUES ("
                "%s,'nhtsa-bulk-20260823T100000Z','all','running',JSON_ARRAY(),"
                "'writer',%s,'2026-08-23 10:00:01','2026-08-23 10:00:01',"
                "'2026-08-23 10:00:01','2026-08-23 10:01:01')",
                (linked_completed_job_id, "b" * 64),
            )
            run_id = int(cursor.lastrowid)

        with pytest.raises(
            MigrationError,
            match="running NHTSA jobs do not have one unique recoverable scheduler child each",
        ):
            runner.apply(recover_stale_nhtsa_daemon_seconds=60)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,lease_slot,lease_token,scheduled_job_run_id "
                "FROM nhtsa_sync_runs WHERE id=%s",
                (run_id,),
            )
            untouched = cursor.fetchone()
        assert untouched == {
            "status": "running",
            "lease_slot": "writer",
            "lease_token": "b" * 64,
            "scheduled_job_run_id": linked_completed_job_id,
        }
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("running_lease_requirement", "lease_expiry_comparison"),
    (
        (
            "AND lease_slot IS NOT NULL AND BINARY lease_slot = BINARY 'writer'",
            ">=",
        ),
        ("AND BINARY lease_slot = BINARY 'writer'", ">"),
    ),
)
def test_migration_check_rejects_mutated_nhtsa_lease_check(
    migration_database: MigrationDatabase,
    running_lease_requirement: str,
    lease_expiry_comparison: str,
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
            cursor.execute("ALTER TABLE nhtsa_sync_runs DROP CHECK chk_nhtsa_sync_status_lease")
            cursor.execute(
                "ALTER TABLE nhtsa_sync_runs "
                "ADD CONSTRAINT chk_nhtsa_sync_status_lease CHECK (("
                "BINARY status = BINARY 'running' "
                "AND scheduled_job_run_id IS NOT NULL "
                f"{running_lease_requirement} "
                "AND lease_token IS NOT NULL "
                "AND lease_token REGEXP '^[0-9a-f]{64}$' "
                "AND heartbeat_at IS NOT NULL "
                "AND lease_expires_at IS NOT NULL "
                f"AND lease_expires_at {lease_expiry_comparison} heartbeat_at "
                "AND ended_at IS NULL) OR ("
                "BINARY status IN ("
                "BINARY 'completed', BINARY 'failed', BINARY 'interrupted') "
                "AND lease_slot IS NULL AND lease_token IS NULL "
                "AND lease_expires_at IS NULL AND ended_at IS NOT NULL))"
            )
        with pytest.raises(
            MigrationError,
            match="NHTSA run lease schema contract mismatch: checks",
        ):
            runner.check()
    finally:
        connection.close()


def test_mysql_rejects_running_nhtsa_lease_without_slot(
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
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at) "
                "VALUES ('nhtsa-bulk','manual','running',UTC_TIMESTAMP(6))"
            )
            scheduled_job_run_id = cursor.lastrowid
            with pytest.raises(
                pymysql.MySQLError,
                match="chk_nhtsa_sync_status_lease",
            ):
                cursor.execute(
                    "INSERT INTO nhtsa_sync_runs "
                    "(scheduled_job_run_id,run_key,scope_name,status,source_keys_json,"
                    "lease_slot,lease_token,started_at,updated_at,heartbeat_at,"
                    "lease_expires_at) VALUES ("
                    "%s,'migration-null-lease-slot','all','running',JSON_ARRAY(),"
                    "NULL,%s,UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),"
                    "DATE_ADD(UTC_TIMESTAMP(6),INTERVAL 5 MINUTE))",
                    (scheduled_job_run_id, "a" * 64),
                )
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM nhtsa_sync_runs "
                "WHERE run_key='migration-null-lease-slot'"
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
            cursor.execute(
                "SELECT @@GLOBAL.log_bin_trust_function_creators AS trust_function_creators"
            )
            trust_row = cursor.fetchone()
            assert trust_row == {"trust_function_creators": 1}, (
                "the application-user migration gate requires log_bin_trust_function_creators=1"
            )
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
    monkeypatch.setitem(CRAWL, "bounded_brand", "")
    monkeypatch.setitem(CRAWL, "bounded_model", "")
    monkeypatch.setitem(CRAWL, "vehicle_year_window", 0)
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
            nhtsa.start_run(
                "nhtsa-lock-test",
                "all",
                ("fixture",),
                scheduled_job_run_id=1,
                expected_job_name="nhtsa-bulk",
            )
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


def _insert_running_daemon(
    database: MigrationDatabase,
    suffix: str,
    *,
    crawl_status: str = "running",
) -> tuple[int, int]:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name,trigger_mode,status,started_at,finished_at,exit_code,output_text) "
                "VALUES ('catalog','daemon','running','2026-01-01 00:00:00',NULL,NULL,'fixture')"
            )
            job_id = cursor.lastrowid
            assert job_id is not None
            cursor.execute(
                "INSERT INTO crawl_runs "
                "(run_key,started_at,finished_at,status,dataset_kind,target_parts,"
                "scheduled_job_run_id) VALUES (%s,'2026-01-01 00:00:00',%s,%s,"
                "'full',NULL,%s)",
                (
                    f"migration-{suffix}",
                    "2026-01-01 00:01:00" if crawl_status == "success" else None,
                    crawl_status,
                    job_id,
                ),
            )
            run_id = cursor.lastrowid
            assert run_id is not None
            return int(job_id), int(run_id)
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
