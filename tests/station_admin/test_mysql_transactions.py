from __future__ import annotations

import hashlib
import os
import uuid

import pymysql
import pytest
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from partsouq_catalog.config import DB_CONFIG
from partsouq_station_admin.db import AdminDatabase
from partsouq_station_admin.query_trace import QueryTrace
from partsouq_station_admin.repository import (
    ENTITY_SPECS,
    AdminReadinessError,
    AdminRepository,
    RevisionConflictError,
)

pytestmark = pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run station-admin MySQL tests",
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
        autocommit=False,
        cursorclass=DictCursor,
    )


def test_readiness_accepts_real_migration_019_contract() -> None:
    connection = _connect()
    trace = QueryTrace()
    try:
        AdminRepository(AdminDatabase(connection, trace)).check_readiness()
        assert trace.tags[-2:] == (
            "health.backoffice-schema",
            "health.published-provenance",
        )
    except AdminReadinessError:
        pytest.fail("test database does not satisfy migration 030 readiness contract")
    finally:
        connection.close()


def test_legacy_json_null_part_overrides_fall_back_to_source_values() -> None:
    connection = _connect()
    source_id = uuid.uuid4().int % (2**63 - 1) + 1
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO admin_override_heads("
                "entity_type, identity_key, source_record_id, manual_uuid, payload_json, "
                "status, revision, base_sha256, actor, reason, created_at, updated_at"
                ") VALUES ('part_numbers', %s, %s, NULL, %s, 'active', 1, %s, "
                "'legacy-test', 'legacy null compatibility', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
                (
                    f"source:{source_id}",
                    source_id,
                    '{"number_raw":null,"name_en_raw":null}',
                    "0" * 64,
                ),
            )
            cursor.execute(
                "SELECT part_number_override, number_normalized_override, part_name_override "
                "FROM station_admin_effective_parts WHERE part_id = %s",
                (source_id,),
            )
            assert cursor.fetchone() == {
                "part_number_override": None,
                "number_normalized_override": None,
                "part_name_override": None,
            }
    finally:
        connection.rollback()
        connection.close()


def _seed_lock_fixture(
    connection: Connection[DictCursor],
) -> tuple[dict[str, int], dict[str, object]]:
    suffix = uuid.uuid4().hex[:12]
    vin = f"ZZZ{uuid.uuid4().int % 10**14:014d}"
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO brands(name) VALUES (%s)", (f"LOCK-{suffix}",))
        brand_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO models(brand_id, name) VALUES (%s, %s)",
            (brand_id, f"MODEL-{suffix}"),
        )
        model_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO vehicles(model_id, identity_hash, name, model_code) "
            "VALUES (%s, %s, %s, %s)",
            (
                model_id,
                hashlib.sha256(suffix.encode()).hexdigest(),
                f"VEHICLE-{suffix}",
                f"CODE-{suffix}",
            ),
        )
        vehicle_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO categories(vehicle_id, name, cid) VALUES (%s, %s, %s)",
            (vehicle_id, "LOCK CATEGORY", suffix),
        )
        category_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO groups_t(category_id, code, name, uid) VALUES (%s, %s, %s, %s)",
            (category_id, suffix, "LOCK GROUP", suffix),
        )
        group_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO parts(group_id, part_number, name) VALUES (%s, %s, %s)",
            (group_id, f"LOCK-{suffix}", "LOCK SOURCE PART"),
        )
        part_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO scheduled_job_runs("
            "job_name, trigger_mode, status, started_at, finished_at, exit_code"
            ") VALUES ('catalog', 'daemon', 'completed', UTC_TIMESTAMP(6), "
            "UTC_TIMESTAMP(6), 0)"
        )
        scheduled_job_run_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO crawl_runs("
            "run_key, started_at, finished_at, status, dataset_kind, target_parts, "
            "scheduled_job_run_id, parts_ok, error_msg"
            ") VALUES (%s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 'success', 'full', NULL, "
            "%s, 1, NULL)",
            (f"lock-full-{suffix}", scheduled_job_run_id),
        )
        crawl_run_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO published_parts("
            "part_id, crawl_run_id, vehicle_id, model_id, brand, model, vehicle_name, vehicle_code, "
            "prod_period, production_from, production_to, engine, trim_name, part_name, "
            "part_number, part_number_normalized, category_main, group_id, "
            "group_code, part_range, snapshot_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '2020-01', '2025-12', %s, %s, "
            "%s, %s, %s, %s, %s, %s, '', "
            "UTC_TIMESTAMP())",
            (
                part_id,
                crawl_run_id,
                vehicle_id,
                model_id,
                f"LOCK-{suffix}",
                f"MODEL-{suffix}",
                f"VEHICLE-{suffix}",
                f"CODE-{suffix}",
                "2020-01 - 2025-12",
                "LOCK ENGINE",
                "LOCK TRIM",
                "LOCK SOURCE PART",
                f"LOCK-{suffix}",
                f"LOCK{suffix}",
                "LOCK CATEGORY",
                group_id,
                suffix,
            ),
        )
        cursor.execute(
            "INSERT INTO admin_part_translations(english_name, chinese_name, source_name) "
            "VALUES ('LOCK SOURCE PART', %s, 'lock-test')",
            (f"鎖定測試-{suffix}",),
        )
        translation_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO nhtsa_source_artifacts("
            "dataset_name, source_key, source_url, http_status, response_headers_json, "
            "sha256, stored_path, byte_count, parser_name, parser_version, status, "
            "downloaded_at, verified_at, imported_at, source_rows, new_versions"
            ") VALUES ('vpic_vin_decodes', %s, 'https://example.test/vin', 200, '{}', "
            "%s, %s, 2, 'lock-test', '1', 'imported', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), "
            "UTC_TIMESTAMP(6), 1, 1)",
            (f"lock-{suffix}", hashlib.sha256(vin.encode()).hexdigest(), f"{suffix}.json"),
        )
        artifact_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO nhtsa_vin_decodes("
            "vin, make_name, model_name, model_year, engine_configuration, engine_model, "
            "displacement_l, trim_name, error_code, payload_json, source_url, "
            "source_artifact_id, decoded_at"
            ") VALUES (%s, %s, %s, 2020, 'Inline 4', 'LOCK ENGINE', 2.0, 'LOCK TRIM', "
            "'0', '{}', 'https://example.test/vin', %s, "
            "UTC_TIMESTAMP(6))",
            (vin, f"LOCK-{suffix}", f"MODEL-{suffix}", artifact_id),
        )
        cursor.execute(
            "INSERT INTO admin_vehicle_mappings("
            "vin_prefix, vin, partsouq_vehicle_id, make_name, model_name, model_year, engine, "
            "trim_name, source_name"
            ") VALUES (%s, %s, %s, %s, %s, 2020, 'LOCK ENGINE', 'LOCK TRIM', "
            "'lock-test')",
            (vin[:11], vin, vehicle_id, f"LOCK-{suffix}", f"MODEL-{suffix}"),
        )
        mapping_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO admin_reconciliation_items(channel, subject_key) VALUES ('part', %s)",
            (f"lock-{suffix}",),
        )
        reconciliation_id = int(cursor.lastrowid)
        cursor.execute(
            "SELECT CAST(CONV(SUBSTRING(SHA2(%s, 256), 1, 15), 16, 10) AS UNSIGNED) AS id",
            (vin,),
        )
        vin_vehicle_id = int(cursor.fetchone()["id"])
    connection.commit()
    return (
        {
            "vehicle_configurations": vehicle_id,
            "taxonomy_nodes": category_id * 2,
            "diagrams": group_id,
            "part_numbers": part_id,
            "part_occurrences": part_id,
            "fitments": part_id,
            "part_term_mappings": translation_id,
            "vin_vehicle_mappings": vin_vehicle_id,
            "vin_part_fitments": mapping_id * 4294967296 + part_id,
            "reconciliation_cases": reconciliation_id,
        },
        {
            "brand_id": brand_id,
            "part_id": part_id,
            "translation_id": translation_id,
            "artifact_id": artifact_id,
            "vin": vin,
            "mapping_id": mapping_id,
            "reconciliation_id": reconciliation_id,
            "taxonomy_group_id": group_id * 2 + 1,
            "crawl_run_id": crawl_run_id,
            "scheduled_job_run_id": scheduled_job_run_id,
        },
    )


def _cleanup_lock_fixture(connection: Connection[DictCursor], fixture: dict[str, object]) -> None:
    connection.rollback()
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM admin_reconciliation_items WHERE id = %s",
            (fixture["reconciliation_id"],),
        )
        cursor.execute("DELETE FROM admin_vehicle_mappings WHERE id = %s", (fixture["mapping_id"],))
        cursor.execute("DELETE FROM nhtsa_vin_decodes WHERE vin = %s", (fixture["vin"],))
        cursor.execute(
            "DELETE FROM nhtsa_source_artifacts WHERE id = %s", (fixture["artifact_id"],)
        )
        cursor.execute(
            "DELETE FROM admin_part_translations WHERE id = %s", (fixture["translation_id"],)
        )
        cursor.execute("DELETE FROM bounded_parts WHERE part_id = %s", (fixture["part_id"],))
        cursor.execute("DELETE FROM published_parts WHERE part_id = %s", (fixture["part_id"],))
        cursor.execute(
            "DELETE FROM published_parts_previous WHERE part_id = %s", (fixture["part_id"],)
        )
        cursor.execute("DELETE FROM crawl_runs WHERE id = %s", (fixture["crawl_run_id"],))
        cursor.execute(
            "DELETE FROM scheduled_job_runs WHERE id = %s",
            (fixture["scheduled_job_run_id"],),
        )
        cursor.execute("DELETE FROM brands WHERE id = %s", (fixture["brand_id"],))
    connection.commit()


def test_each_entity_locks_real_source_rows_and_blocks_crawler_update() -> None:
    locker = _connect()
    writer = _connect()
    fixture: dict[str, object] = {}
    try:
        entity_ids, fixture = _seed_lock_fixture(locker)
        locker.begin()
        repository = AdminRepository(AdminDatabase(locker, QueryTrace()))
        for entity_type, source_id in entity_ids.items():
            # 此 fixture 只有 raw full candidate。VIN fitment 僅能從已驗證的
            # current bounded snapshot 推導，因此不應在站方正式 view 出現。
            if entity_type == "vin_part_fitments":
                continue
            assert repository._locked_base(ENTITY_SPECS[entity_type], source_id) is not None
        assert (
            repository._locked_base(
                ENTITY_SPECS["vin_part_fitments"], entity_ids["vin_part_fitments"]
            )
            is None
        )
        assert (
            repository._locked_base(
                ENTITY_SPECS["taxonomy_nodes"], int(fixture["taxonomy_group_id"])
            )
            is not None
        )

        with writer.cursor() as cursor:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
        writer.begin()
        with writer.cursor() as cursor:
            with pytest.raises(pymysql.err.OperationalError) as captured:
                cursor.execute(
                    "UPDATE parts SET name = CONCAT(name, ' writer') WHERE id = %s",
                    (fixture["part_id"],),
                )
            assert captured.value.args[0] == 1205
    finally:
        writer.rollback()
        writer.close()
        if fixture:
            _cleanup_lock_fixture(locker, fixture)
        else:
            locker.rollback()
        locker.close()


@pytest.mark.parametrize(
    ("source_table", "locked_field"),
    (
        ("published_parts", "published_part_id"),
        ("published_parts_previous", "previous_part_id"),
        ("bounded_parts", "bounded_part_id"),
    ),
)
def test_vin_part_fitment_locks_the_physical_snapshot_source(
    source_table: str, locked_field: str
) -> None:
    locker = _connect()
    writer = _connect()
    fixture: dict[str, object] = {}
    try:
        entity_ids, fixture = _seed_lock_fixture(locker)
        if source_table == "published_parts_previous":
            with locker.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO published_parts_previous "
                    "SELECT * FROM published_parts WHERE part_id = %s",
                    (fixture["part_id"],),
                )
                cursor.execute(
                    "DELETE FROM published_parts WHERE part_id = %s",
                    (fixture["part_id"],),
                )
            locker.commit()
        elif source_table == "bounded_parts":
            with locker.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO bounded_parts("
                    "part_id, crawl_run_id, vehicle_id, model_id, vehicle_vid, brand, model, "
                    "vehicle_name, vehicle_code, prod_period, production_from, production_to, "
                    "engine, trim_name, part_name, part_number, part_number_normalized, "
                    "category_id, category_cid, category_main, category_group, group_id, "
                    "group_code, group_uid, part_range, source_url, code, snapshot_at"
                    ") SELECT p.part_id, p.crawl_run_id, p.vehicle_id, p.model_id, 'LOCK-VID', "
                    "p.brand, p.model, p.vehicle_name, p.vehicle_code, p.prod_period, "
                    "p.production_from, p.production_to, p.engine, p.trim_name, p.part_name, "
                    "p.part_number, p.part_number_normalized, c.id, c.cid, p.category_main, "
                    "'LOCK GROUP', g.id, p.group_code, g.uid, p.part_range, "
                    "'https://example.test/lock', 'LOCK-CODE', p.snapshot_at "
                    "FROM published_parts AS p "
                    "JOIN groups_t AS g ON g.id = p.group_id "
                    "JOIN categories AS c ON c.id = g.category_id "
                    "WHERE p.part_id = %s",
                    (fixture["part_id"],),
                )
                cursor.execute(
                    "DELETE FROM published_parts WHERE part_id = %s",
                    (fixture["part_id"],),
                )
            locker.commit()

        source_id = entity_ids["vin_part_fitments"]
        locker.begin()
        repository = AdminRepository(AdminDatabase(locker, QueryTrace()))
        lock_sql, lock_params = repository._source_lock_query(
            ENTITY_SPECS["vin_part_fitments"], source_id
        )
        locked = repository.database.fetch_all(
            "write.lock-source.vin_part_fitments", lock_sql, lock_params
        )
        assert len(locked) == 1
        assert locked[0][locked_field] == fixture["part_id"]

        with writer.cursor() as cursor:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
        writer.begin()
        with writer.cursor() as cursor:
            with pytest.raises(pymysql.err.OperationalError) as captured:
                cursor.execute(
                    f"DELETE FROM {source_table} WHERE part_id = %s",
                    (fixture["part_id"],),
                )
            assert captured.value.args[0] == 1205
    finally:
        writer.rollback()
        writer.close()
        if fixture:
            _cleanup_lock_fixture(locker, fixture)
        else:
            locker.rollback()
        locker.close()


def test_vin_view_keeps_raw_candidate_mapping_stale_and_shows_decode_warnings() -> None:
    connection = _connect()
    fixture: dict[str, object] = {}
    try:
        _, fixture = _seed_lock_fixture(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT mapping_status, decode_status, model_name, "
                "partsouq_vehicle_configuration_id "
                "FROM station_admin_vin_vehicle_mappings WHERE vin = %s",
                (fixture["vin"],),
            )
            mapped = cursor.fetchone()
            assert mapped is not None
            # `published_parts` 是 raw full candidate，不是正式 current catalog；
            # 同一 mapping 不得因為 raw row 存在而被誤標為 confirmed。
            assert mapped["mapping_status"] == "stale"
            assert mapped["decode_status"] == "decoded"
            assert mapped["partsouq_vehicle_configuration_id"] is not None

            cursor.execute(
                "UPDATE nhtsa_vin_decodes SET engine_configuration = NULL, "
                "displacement_l = NULL WHERE vin = %s",
                (fixture["vin"],),
            )
            cursor.execute(
                "SELECT mapping_status FROM station_admin_vin_vehicle_mappings WHERE vin = %s",
                (fixture["vin"],),
            )
            optional_uncomparable_fields = cursor.fetchone()
            assert optional_uncomparable_fields is not None
            assert optional_uncomparable_fields["mapping_status"] == "stale"
            cursor.execute(
                "UPDATE nhtsa_vin_decodes SET engine_configuration = 'Inline 4', "
                "displacement_l = 2.0 WHERE vin = %s",
                (fixture["vin"],),
            )

            cursor.execute(
                "UPDATE nhtsa_vin_decodes SET model_name = CONCAT(model_name, '-STALE') "
                "WHERE vin = %s",
                (fixture["vin"],),
            )
            cursor.execute(
                "SELECT mapping_status, decode_status "
                "FROM station_admin_vin_vehicle_mappings WHERE vin = %s",
                (fixture["vin"],),
            )
            stale = cursor.fetchone()
            assert stale is not None
            assert stale["mapping_status"] == "stale"
            assert stale["decode_status"] == "decoded"

            cursor.execute(
                "UPDATE nhtsa_vin_decodes SET model_name = %s, error_code = '8' WHERE vin = %s",
                (mapped["model_name"], fixture["vin"]),
            )
            cursor.execute(
                "SELECT mapping_status, decode_status "
                "FROM station_admin_vin_vehicle_mappings WHERE vin = %s",
                (fixture["vin"],),
            )
            warning = cursor.fetchone()
            assert warning is not None
            assert warning["mapping_status"] == "stale"
            assert warning["decode_status"] == "decoded_with_warning"

            cursor.execute(
                "DELETE FROM admin_vehicle_mappings WHERE id = %s",
                (fixture["mapping_id"],),
            )
            cursor.execute(
                "SELECT mapping_status, decode_status, "
                "partsouq_vehicle_configuration_id "
                "FROM station_admin_vin_vehicle_mappings WHERE vin = %s",
                (fixture["vin"],),
            )
            unmapped = cursor.fetchone()
            assert unmapped is not None
            assert unmapped["mapping_status"] == "unmapped"
            assert unmapped["decode_status"] == "decoded_with_warning"
            assert unmapped["partsouq_vehicle_configuration_id"] is None
        connection.rollback()
    finally:
        if fixture:
            _cleanup_lock_fixture(connection, fixture)
        else:
            connection.rollback()
        connection.close()


def test_quarantine_resolve_uses_occurrence_key_and_is_single_use() -> None:
    connection = _connect()
    fixture: dict[str, object] = {}
    quarantine_id = 0
    try:
        _, fixture = _seed_lock_fixture(connection)
        group_id = (int(fixture["taxonomy_group_id"]) - 1) // 2
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key"
                ") VALUES (%s, %s, '', 'nameless', 'bounded-current')",
                (group_id, f"Q-{uuid.uuid4().hex[:12]}"),
            )
            quarantine_id = int(cursor.lastrowid)
        connection.commit()
        repository = AdminRepository(AdminDatabase(connection, QueryTrace()))

        with pytest.raises(RevisionConflictError, match="已更新"):
            repository.resolve_quarantine(
                quarantine_id,
                "stale",
                expected_run_key="bounded-old",
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT resolved_at, resolution FROM part_quarantine WHERE id = %s",
                (quarantine_id,),
            )
            assert cursor.fetchone() == {"resolved_at": None, "resolution": None}

        repository.resolve_quarantine(
            quarantine_id,
            "verified",
            expected_run_key="bounded-current",
        )
        with pytest.raises(RevisionConflictError, match="已更新"):
            repository.resolve_quarantine(
                quarantine_id,
                "duplicate",
                expected_run_key="bounded-current",
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT resolved_at, resolution FROM part_quarantine WHERE id = %s",
                (quarantine_id,),
            )
            resolved = cursor.fetchone()
            assert resolved is not None
            assert resolved["resolved_at"] is not None
            assert resolved["resolution"] == "verified"
    finally:
        connection.rollback()
        if quarantine_id:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM part_quarantine WHERE id = %s", (quarantine_id,))
            connection.commit()
        if fixture:
            _cleanup_lock_fixture(connection, fixture)
        connection.close()
