from __future__ import annotations

import os

import pytest

from partsouq_catalog.config import DB_CONFIG
from partsouq_catalog.db import Database
from partsouq_catalog.repositories import (
    BrandRepository,
    CrawlRepository,
    PartRepository,
    VehicleRepository,
)

pytestmark = pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL catalog tests",
)

VIN = "ZZZTEST00X0000003"
NHTSA_SOURCE_KEY = "source_id_view_fixture"


def test_full_candidate_archive_preserves_source_ids_without_formal_mapping() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        for table in (
            "admin_vehicle_mappings",
            "bounded_parts",
            "bounded_group_receipts",
            "published_parts_previous",
            "published_parts",
            "partsouq_artifact_records",
            "partsouq_http_diagnostics",
            "partsouq_http_artifacts",
            "partsouq_response_bodies",
            "crawl_state",
            "crawl_runs",
            "scheduled_job_runs",
            "brands",
        ):
            database._execute(f"DELETE FROM {table}")
        database._execute("DELETE FROM nhtsa_vin_decodes WHERE vin = %s", (VIN,))
        database._execute(
            "DELETE FROM nhtsa_source_artifacts WHERE source_key = %s",
            (NHTSA_SOURCE_KEY,),
        )
        database.commit()

        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        crawl = CrawlRepository(database, "source-id-fixture")

        run_id = crawl.start_run(
            "source-id-fixture",
            fresh=True,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        brand_id = brands.upsert_brand("TOYOTA", None)
        model_id = brands.upsert_model(brand_id, "CAMRY", "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "CAMRY",
                "model_code": "AXVA70",
                "prod_period": "01.2018 - 12.2020",
                "production_from": "2018-01",
                "production_to": "2020-12",
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
                "engine": "A25A-FKS",
                "grade": "LE",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        group_id = vehicles.upsert_group(
            category_id,
            "1502",
            "OIL FILTER",
            "SITE-UID-1",
            "https://partsouq.com/en/catalog/genuine/unit?uid=SITE-UID-1",
        )
        parts.upsert_parts(
            group_id,
            [
                {
                    "part_number": "TEST-PART-001",
                    "name": "FILTER ASSY, OIL",
                    "code": "15601",
                    "note": "",
                    "quantity": "01",
                    "range_str": "01.2018 - 12.2019",
                    "part_from": "2018-01",
                    "part_to": "2019-12",
                }
            ],
            run_id=run_id,
        )
        crawl.mark_group_fetched(
            group_id,
            crawl.run_key,
            status="done",
            row_count=1,
        )
        source_part = database._execute(
            "SELECT id FROM parts WHERE group_id = %s AND part_number = %s",
            (group_id, "TEST-PART-001"),
        ).fetchone()
        assert source_part is not None
        part_id = source_part["id"]
        assert crawl.archive_full_candidate_parts(run_id) == 1
        crawl.finish_run(run_id, "success", {"parts": 1})
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (scheduled_job_run_id,),
        )

        artifact_cursor = database._execute(
            "INSERT INTO nhtsa_source_artifacts ("
            "dataset_name, source_key, source_url, http_status, response_headers_json, "
            "sha256, stored_path, byte_count, parser_name, parser_version, status, "
            "downloaded_at, verified_at, imported_at, source_rows, new_versions) "
            "VALUES (%s, %s, %s, 200, %s, %s, %s, 2, %s, %s, %s, "
            "NOW(6), NOW(6), NOW(6), 1, 1)",
            (
                "vpic_vin_decodes",
                NHTSA_SOURCE_KEY,
                f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{VIN}?format=json",
                "{}",
                "a" * 64,
                "/tmp/source-id-view-fixture.json",
                "source_id_fixture",
                "1",
                "imported",
            ),
        )
        artifact_id = artifact_cursor.lastrowid
        database._execute(
            "INSERT INTO nhtsa_vin_decodes ("
            "vin, make_name, model_name, model_year, engine_configuration, engine_model, "
            "displacement_l, trim_name, series_name, error_code, error_text, payload_json, "
            "source_url, source_artifact_id, decoded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(6))",
            (
                VIN,
                "TOYOTA",
                "CAMRY",
                2018,
                "In-Line",
                "A25A-FKS",
                "2.5",
                "LE",
                "XV70",
                "0",
                "0 - VIN decoded clean.",
                "{}",
                f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{VIN}?format=json",
                artifact_id,
            ),
        )
        database._execute(
            "INSERT INTO admin_vehicle_mappings ("
            "vin_prefix, vin, partsouq_vehicle_id, make_name, model_name, model_year, "
            "engine, trim_name, source_name, source_reference) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                VIN[:11],
                VIN,
                vehicle_id,
                "TOYOTA",
                "CAMRY",
                2018,
                "In-Line / A25A-FKS",
                "LE",
                "test",
                "source ID view fixture",
            ),
        )
        database.commit()

        expected = {
            "part_id": part_id,
            "model_id": model_id,
            "vehicle_id": vehicle_id,
            "vehicle_vid": "SITE-VID-1",
            "category_id": category_id,
            "category_cid": "1",
            "group_id": group_id,
            "group_code": "1502",
            "group_uid": "SITE-UID-1",
            "code": "15601",
        }
        snapshot = database._execute(
            "SELECT part_id, model_id, vehicle_id, vehicle_vid, category_id, category_cid, "
            "group_id, group_code, group_uid, code FROM published_parts"
        ).fetchone()
        current_view = database._execute(
            "SELECT part_id, model_id, vehicle_id, vehicle_vid, category_id, category_cid, "
            "group_id, group_code, group_uid, code FROM v_parts"
        ).fetchone()
        mapping_view = database._execute(
            "SELECT part_id, model_id, vehicle_id, vehicle_vid, category_id, category_cid, "
            "group_id, group_code, group_uid, code, partsouq_vehicle_id "
            "FROM v_vin_part_fitments WHERE vin = %s",
            (VIN,),
        ).fetchone()

        assert snapshot is not None
        for key, value in expected.items():
            assert snapshot[key] == value
        assert current_view is None
        assert mapping_view is None
    finally:
        database.rollback()
        for table in (
            "admin_vehicle_mappings",
            "bounded_parts",
            "bounded_group_receipts",
            "published_parts_previous",
            "published_parts",
            "partsouq_artifact_records",
            "partsouq_http_diagnostics",
            "partsouq_http_artifacts",
            "partsouq_response_bodies",
            "crawl_state",
            "crawl_runs",
            "scheduled_job_runs",
            "brands",
        ):
            database._execute(f"DELETE FROM {table}")
        database._execute("DELETE FROM nhtsa_vin_decodes WHERE vin = %s", (VIN,))
        database._execute(
            "DELETE FROM nhtsa_source_artifacts WHERE source_key = %s",
            (NHTSA_SOURCE_KEY,),
        )
        database.commit()
        database.close()
