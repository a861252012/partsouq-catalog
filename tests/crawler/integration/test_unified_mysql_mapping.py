from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from partsouq_admin.app import app
from partsouq_catalog.config import DB_CONFIG
from partsouq_catalog.db import Database
from partsouq_catalog.repositories import (
    BrandRepository,
    CrawlRepository,
    PartRepository,
    VehicleRepository,
)
from partsouq_crawler.nhtsa.api import NhtsaApiParser, vin_source_key
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import ApiSource
from partsouq_crawler.nhtsa.models import DownloadedArtifact
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository
from tests.test_partsouq_bounded_limit import _parts as bounded_fixture_parts
from tests.test_partsouq_bounded_limit import _record_verified_live_evidence

pytestmark = pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL mapping tests",
)

VIN = "ZZZTEST00X0000001"


def _config(tmp_path: Path) -> NhtsaConfig:
    config = NhtsaConfig.from_env(raw_dir=tmp_path / "raw")
    if not config.mysql_database.endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")
    catalog_target = (
        str(DB_CONFIG["host"]),
        int(DB_CONFIG["port"]),
        str(DB_CONFIG["database"]),
        str(DB_CONFIG["user"]),
    )
    nhtsa_target = (
        config.mysql_host,
        config.mysql_port,
        config.mysql_database,
        config.mysql_user,
    )
    if catalog_target != nhtsa_target:
        raise ValueError("PartSouq and NHTSA tests must use the same MySQL target")
    return config


def _clear_shared_database(repository: NhtsaMySQLRepository) -> None:
    with repository.transaction() as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM admin_vehicle_mappings")
    repository.clear_for_tests()
    with repository.transaction() as connection, connection.cursor() as cursor:
        for table in (
            "admin_override_events",
            "admin_override_heads",
            "admin_crawl_request_audits",
            "admin_part_fitments",
            "admin_part_translations",
            "admin_category_labels",
            "admin_reconciliation_items",
            "admin_crawl_requests",
            "bounded_parts",
            "published_parts_previous",
            "published_parts",
            "partsouq_artifact_records",
            "partsouq_http_diagnostics",
            "partsouq_http_artifacts",
            "partsouq_response_bodies",
            "part_quarantine",
            "crawl_state",
            "crawl_runs",
            "scheduled_job_runs",
            "catalog_desired_bounded_scope",
            "brands",
        ):
            cursor.execute(f"DELETE FROM {table}")


def _publish_fixture_vin(
    repository: NhtsaMySQLRepository,
    tmp_path: Path,
) -> None:
    source = ApiSource(
        key=vin_source_key(VIN),
        dataset_name="vpic_vin_decodes",
        url=f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{VIN}?format=json",
    )
    payload = {
        "VIN": VIN,
        "Make": "TOYOTA",
        "Model": "CAMRY",
        "ModelYear": "2018",
        "EngineConfiguration": "In-Line",
        "EngineModel": "A25A-FKS",
        "DisplacementL": "2.5",
        "Trim": "LE",
        "Series": "XV70",
        "ErrorCode": "0",
        "ErrorText": "0 - VIN decoded clean.",
    }
    body = json.dumps(
        {"Count": 1, "Message": "Results returned successfully", "Results": [payload]}
    ).encode()
    document = NhtsaApiParser().parse(body, source)
    sha256 = hashlib.sha256(body).hexdigest()
    raw_path = tmp_path / f"{sha256}.json"
    raw_path.write_bytes(body)
    with repository.transaction() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO scheduled_job_runs(job_name, trigger_mode, status, started_at)
            VALUES ('nhtsa-vin', 'daemon', 'running', UTC_TIMESTAMP())
            """
        )
        scheduled_job_run_id = int(cursor.lastrowid)
    lease = repository.start_run(
        "mapping-vin-fixture",
        "api-vin",
        (source.key,),
        scheduled_job_run_id=scheduled_job_run_id,
        expected_job_name="nhtsa-vin",
    )
    artifact_id = repository.create_artifact(
        lease,
        dataset_name=source.dataset_name,
        source_key=source.key,
        source_url=source.url,
        download=DownloadedArtifact(
            http_status=200,
            response_headers={"content-type": "application/json"},
            path=raw_path,
            sha256=sha256,
            byte_count=len(body),
        ),
        parser_name="test_vin_fixture",
        parser_version="1",
    )
    repository.store_member(lease, artifact_id, document.member)
    repository.reset_artifact_import(lease, artifact_id)
    new_versions = repository.insert_records(lease, artifact_id, document.records)
    repository.complete_artifact(
        lease,
        artifact_id,
        source_rows=1,
        new_versions=new_versions,
        rejected_rows=0,
    )
    repository.complete_run_and_publish_vin_decode(
        lease,
        artifact_id,
        VIN,
        payload,
        downloaded=1,
        reused=0,
        source_rows=1,
        new_versions=new_versions,
        rejected_rows=0,
    )


def test_part_and_vin_are_mapped_through_verified_bounded_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式 VIN mapping 只可讀完整驗證且 scope 相符的 10,000 筆 snapshot。"""
    config = _config(tmp_path)
    repository = NhtsaMySQLRepository.create(config)
    database = Database().connect()
    try:
        catalog_connection = database._thread_conn()
        catalog_database = catalog_connection.db
        if isinstance(catalog_database, bytes):
            catalog_database = catalog_database.decode()
        nhtsa_database = repository.connection.db
        if isinstance(nhtsa_database, bytes):
            nhtsa_database = nhtsa_database.decode()
        if catalog_database != config.mysql_database or nhtsa_database != config.mysql_database:
            raise ValueError("connected MySQL databases do not match the guarded test database")

        _clear_shared_database(repository)
        scope_brand = "TOYOTA"
        scope_model = "CAMRY"
        scope_year_floor = 2018
        database._execute(
            "INSERT INTO catalog_desired_bounded_scope "
            "(singleton_id, scope_brand, scope_model, scope_vehicle_year_floor, updated_at) "
            "VALUES (1, %s, %s, %s, UTC_TIMESTAMP(6)) AS new "
            "ON DUPLICATE KEY UPDATE scope_brand = new.scope_brand, "
            "scope_model = new.scope_model, "
            "scope_vehicle_year_floor = new.scope_vehicle_year_floor, "
            "updated_at = new.updated_at",
            (scope_brand.casefold(), scope_model.casefold(), scope_year_floor),
        )
        scheduler_run_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
                "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
            ).lastrowid
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        brand_id = brands.upsert_brand(scope_brand, "https://partsouq.com/en/catalog/genuine")
        model_id = brands.upsert_model(brand_id, scope_model, "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": scope_model,
                "model_code": "AXVA70",
                "prod_period": "01.2018 - 12.2020",
                "production_from": "2018-01",
                "production_to": "2020-12",
                "engine": "A25A-FKS",
                "grade": "LE",
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "PARTIAL ENGINE ASSEMBLY",
            "10001",
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&cid=1&uid=10001&vid=SITE-VID-1",
        )
        crawl = CrawlRepository(database, "verified-bounded-vin-mapping")
        run_id = crawl.start_run(
            "verified-bounded-vin-mapping",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduler_run_id,
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_year_floor,
        )
        assert parts.upsert_parts(group_id, bounded_fixture_parts(10_000), run_id) == 10_000
        database.commit()
        _record_verified_live_evidence(
            database,
            crawl,
            run_id=run_id,
            scheduled_job_run_id=scheduler_run_id,
            vehicle_engine="A25A-FKS",
            vehicle_trim_name="LE",
        )
        assert crawl.publish_bounded_parts(run_id, 10_000) == 10_000
        crawl.finish_run(run_id, "bounded_success", {"parts": 10_000})
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (scheduler_run_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 10_000}

        published_part = database._execute(
            "SELECT part_id, part_name FROM v_current_catalog_parts WHERE part_number = 'P-00000'"
        ).fetchone()
        assert published_part is not None
        database._execute(
            "UPDATE parts SET name = 'RAW DATA MUST NOT LEAK' WHERE id = %s",
            (published_part["part_id"],),
        )
        database.commit()
        assert database._execute(
            "SELECT part_name FROM v_current_catalog_parts WHERE part_id = %s",
            (published_part["part_id"],),
        ).fetchone() == {"part_name": "Part 0"}

        _publish_fixture_vin(repository, tmp_path)
        monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "mapping-test-token")
        headers = {"X-Admin-Token": "mapping-test-token"}
        with TestClient(app) as client:
            assert client.get(f"/api/vins/{VIN}/vehicle-candidates").status_code == 401

            candidates = client.get(
                f"/api/vins/{VIN}/vehicle-candidates",
                headers=headers,
            )
            assert candidates.status_code == 200
            assert candidates.json() == [
                {
                    "partsouq_vehicle_id": vehicle_id,
                    "partsouq_brand": "TOYOTA",
                    "partsouq_model": "CAMRY",
                    "vehicle_name": "CAMRY",
                    "vehicle_code": "AXVA70",
                    "catalog_dataset_scope": "bounded",
                    "catalog_crawl_run_id": run_id,
                    "prod_period": "01.2018 - 12.2020",
                    "production_from": "2018-01",
                    "production_to": "2020-12",
                    "engine": "A25A-FKS",
                    "trim_name": "LE",
                    "nhtsa_engine_configuration": "In-Line",
                    "nhtsa_engine_model": "A25A-FKS",
                    "nhtsa_displacement_l": "2.500000000",
                    "nhtsa_trim_name": "LE",
                    "candidate_status": "exact",
                    "candidate_reason": "normalized_make_model_year_engine_trim_in_current_range",
                }
            ]
            before_confirmation = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert before_confirmation.status_code == 200
            assert before_confirmation.json() == []

            mapping = client.post(
                "/api/vin-vehicle-mappings",
                headers=headers,
                json={"vin": VIN, "partsouq_vehicle_id": vehicle_id},
            )
            assert mapping.status_code == 201
            assert mapping.json()["vin"] == VIN

            active_mapping = client.get(
                f"/api/vin-vehicle-mappings?vin={VIN}",
                headers=headers,
            )
            assert active_mapping.status_code == 200
            assert active_mapping.json()[0]["vehicle_mapping_status"] == "confirmed"
            station_mapping = database._execute(
                "SELECT mapping_status FROM station_admin_vin_vehicle_mappings WHERE vin = %s",
                (VIN,),
            ).fetchone()
            assert station_mapping == {"mapping_status": "confirmed"}

            fitment_started_at = time.monotonic()
            fitments = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert fitments.status_code == 200
            assert time.monotonic() - fitment_started_at < 20
            fitment_rows = fitments.json()
            assert len(fitment_rows) == 10_000
            first_fitment = next(row for row in fitment_rows if row["part_number"] == "P-00000")
            assert first_fitment["part_name"] == "Part 0"
            assert first_fitment["catalog_dataset_scope"] == "bounded"
            assert first_fitment["catalog_crawl_run_id"] == run_id
            assert first_fitment["vehicle_mapping_status"] == "confirmed"
            assert first_fitment["fitment_status"] == "compatible_by_model_year_engine_trim"

            summary = client.get("/api/database-summary", headers=headers)
            assert summary.status_code == 200
            summary_body = summary.json()
            # `_test` 的 fixture 可驗證 mapping 資料流，但不得冒充正式 live catalog。
            assert summary_body["bounded_ready"] is False
            assert "bounded_non_live_data_marker" in summary_body["bounded"]["blocking_reasons"]
            assert summary_body["mappings"] == {
                "total": 1,
                "manual": 0,
                "confirmed": 1,
                "stale": 0,
                "unconfirmed_vin_decodes": 0,
            }
    finally:
        database.close()
        _clear_shared_database(repository)
        repository.close()
