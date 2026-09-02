from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime

import pytest

from partsouq_catalog.config import DB_CONFIG
from partsouq_catalog.db import Database
from partsouq_catalog.evidence import (
    PARSER_CONTRACT_VERSION,
    public_source_url,
    replay_catalog_records,
    sanitize_parser_html,
)
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

        # 正式 full archive 需先封存可重放的 live HTTP 證據；fixture 的
        # 識別欄位（vehicle／group／part）必須與上述 upsert 一致。
        vehicle_key = {
            "brand": "TOYOTA",
            "model": "CAMRY",
            "name": "CAMRY",
            "model_code": "AXVA70",
            "prod_period": "01.2018 - 12.2020",
            "production_from": "2018-01",
            "production_to": "2020-12",
            "engine": "A25A-FKS",
            "trim_name": "LE",
            "vid": "SITE-VID-1",
        }
        group_key = {
            "category": {
                "vehicle": vehicle_key,
                "cid": "1",
                "category_name": "ENGINE/FUEL/TOOL",
            },
            "group_code": "1502",
            "uid": "SITE-UID-1",
        }
        unit_url = "https://partsouq.com/en/catalog/genuine/unit?uid=SITE-UID-1"
        unit_html = (
            '<input type="hidden" name="uid" value="SITE-UID-1">'
            "<table><tbody><tr>"
            '<td><a href="/en/search/all?q=TEST-PART-001">TEST-PART-001</a></td>'
            "<td>FILTER ASSY, OIL</td><td>15601</td><td></td><td>01</td>"
            "<td>01.2018 - 12.2019</td>"
            "</tr></tbody></table>"
        )
        pages = (
            (
                "genuine",
                "parse_brands",
                "https://partsouq.com/en/catalog/genuine",
                {},
                '<li><a href="/en/catalog/genuine/locate?c=TOYOTA">TOYOTA</a></li>',
            ),
            (
                "locate",
                "parse_brand_index",
                "https://partsouq.com/en/catalog/genuine/locate?c=TOYOTA",
                {"brand": "TOYOTA"},
                '<a href="/en/catalog/genuine/pick?c=TOYOTA&model=CAMRY&ssd=token">CAMRY</a>',
            ),
            (
                "pick",
                "parse_vehicles",
                "https://partsouq.com/en/catalog/genuine/pick?c=TOYOTA&model=CAMRY",
                {"brand": "TOYOTA", "model": "CAMRY"},
                "<table><tr><th class='n_name'>Name</th><th class='__model'>Model</th>"
                "<th class='__prodPeriod'>Prod Period</th><th class='__engine'>Engine</th>"
                "<th class='__grade'>Grade</th></tr><tr>"
                "<td><a href='/en/catalog/genuine/vehicle?c=TOYOTA&ssd=token&vid=SITE-VID-1'>"
                "CAMRY</a></td><td>AXVA70</td><td>01.2018 - 12.2020</td>"
                "<td>A25A-FKS</td><td>LE</td></tr></table>",
            ),
            (
                "vehicle",
                "parse_category_links",
                "https://partsouq.com/en/catalog/genuine/vehicle?c=TOYOTA&vid=SITE-VID-1",
                {
                    "brand": "TOYOTA",
                    "vehicle_key": vehicle_key,
                    "expected_vid": "SITE-VID-1",
                    "source_url": (
                        "https://partsouq.com/en/catalog/genuine/vehicle?c=TOYOTA&vid=SITE-VID-1"
                    ),
                },
                "<html><body>ENGINE/FUEL/TOOL</body></html>",
            ),
            (
                "category",
                "parse_groups",
                ("https://partsouq.com/en/catalog/genuine/vehicle?c=TOYOTA&vid=SITE-VID-1&cid=1"),
                {
                    "brand": "TOYOTA",
                    "vehicle_key": vehicle_key,
                    "default_cid": "1",
                    "expected_vid": "SITE-VID-1",
                },
                "<a href='/en/catalog/genuine/unit?c=TOYOTA&ssd=token&vid=SITE-VID-1"
                "&cid=1&uid=SITE-UID-1&q='>1502: OIL FILTER</a>",
            ),
            (
                "unit",
                "parse_parts",
                unit_url,
                {"group_key": group_key},
                unit_html,
            ),
        )
        for page_type, parser_name, public_url, context, html in pages:
            sanitized = sanitize_parser_html(html)
            records, malformed_rows, skipped_rows = replay_catalog_records(
                sanitized.body,
                parser_name=parser_name,
                parser_version=PARSER_CONTRACT_VERSION,
                context=context,
            )
            accepted_records: list[tuple[int, object]] = []
            if page_type == "unit":
                assert len(records) == 1
                accepted_records = [(part_id, records[0])]
            crawl.record_http_evidence(
                run_id,
                scheduled_job_run_id,
                page_type=page_type,
                public_url=public_source_url(public_url),
                raw_body_sha256=hashlib.sha256(html.encode()).hexdigest(),
                status_code=200,
                content_type="text/html",
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
                elapsed_ms=1,
                attempt=1,
                sanitized_body=sanitized,
                parser_name=parser_name,
                parser_version=PARSER_CONTRACT_VERSION,
                parser_context=context,
                parsed_records=records,
                replayed_records=records,
                accepted_records=accepted_records,
                malformed_rows=malformed_rows,
                skipped_record_count=skipped_rows,
            )
        database.commit()
        crawl.verify_run_evidence_full(run_id)
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
        # migration 039 切換閘：本測試的 full run 已滿足
        # 「success、證據已封存、daemon completed exit=0、單一 linked crawl」，
        # current view 讀全量快照，輸出與 published_parts 相同的身分欄位。
        assert current_view is not None
        for key, value in expected.items():
            assert current_view[key] == value
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
