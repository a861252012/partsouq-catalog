from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pymysql
import pytest
from fastapi.testclient import TestClient

from partsouq_admin.app import app
from partsouq_catalog.config import DB_CONFIG
from partsouq_catalog.db import Database
from partsouq_catalog.parsers import parse_parts, parse_vehicles
from partsouq_catalog.repositories import (
    BrandRepository,
    CrawlRepository,
    PartRepository,
    VehicleRepository,
)
from partsouq_crawler.nhtsa.api import NhtsaApiParser
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import ApiSource
from partsouq_crawler.nhtsa.models import DownloadedArtifact
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository

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
        for table in (
            "admin_vehicle_mappings",
            "admin_part_fitments",
            "admin_part_translations",
            "admin_category_labels",
            "admin_reconciliation_items",
            "admin_crawl_requests",
            "bounded_parts",
            "published_parts_previous",
            "published_parts",
            "crawl_state",
            "crawl_runs",
            "scheduled_job_runs",
            "brands",
        ):
            cursor.execute(f"DELETE FROM {table}")
    repository.clear_for_tests()


def _vehicle_html() -> str:
    return """
    <table>
      <tr>
        <th class="__name">Name</th><th class="__model">Model</th>
        <th class="__prodPeriod">Prod Period</th><th class="__engine">Engine</th>
        <th class="__grade">Grade</th>
      </tr>
      <tr>
        <td><a href="/en/catalog/genuine/vehicle?c=TOYOTA&amp;ssd=S1&amp;vid=1">CAMRY</a></td>
        <td>AXVA70</td><td>- 12.2022</td><td>A25A-FKS</td><td>LE</td>
      </tr>
    </table>
    """


def _parts_html() -> str:
    return """
    <table><tbody>
      <tr>
        <td><a href="/en/search/all?q=TEST-PART-001">TEST-PART-001</a></td>
        <td>FILTER ASSY, OIL</td><td>15601</td><td></td><td>01</td>
        <td>01.2018 - 12.2019</td>
      </tr>
      <tr>
        <td><a href="/en/search/all?q=OUTSIDE-RANGE">OUTSIDE-RANGE</a></td>
        <td>FUTURE PART</td><td>99999</td><td></td><td>01</td>
        <td>01.2021 - 12.2022</td>
      </tr>
      <tr>
        <td><a href="/en/search/all?q=UNKNOWN-RANGE">UNKNOWN-RANGE</a></td>
        <td>UNKNOWN RANGE PART</td><td>88888</td><td></td><td>01</td>
        <td>UNKNOWN PERIOD</td>
      </tr>
    </tbody></table>
    """


def _publish_fixture_vin(
    repository: NhtsaMySQLRepository,
    tmp_path: Path,
) -> None:
    source = ApiSource(
        key=f"vpic_vin_{VIN}",
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
    artifact_id = repository.create_artifact(
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
    repository.store_member(artifact_id, document.member)
    repository.reset_artifact_import(artifact_id)
    new_versions = repository.insert_records(artifact_id, document.records)
    repository.complete_artifact(
        artifact_id,
        source_rows=1,
        new_versions=new_versions,
        rejected_rows=0,
    )
    repository.publish_vin_decode(artifact_id, source.key, payload)


def test_part_and_vin_are_mapped_through_shared_mysql_and_admin_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        vehicles = parse_vehicles(_vehicle_html(), "TOYOTA")
        parts, malformed = parse_parts(_parts_html())
        assert malformed == 0
        assert vehicles[0]["production_from"] is None
        assert vehicles[0]["production_to"] == "2022-12"
        assert parts[0]["part_from"] == "2018-01"
        assert parts[0]["part_to"] == "2019-12"

        brands = BrandRepository(database)
        vehicle_repository = VehicleRepository(database)
        part_repository = PartRepository(database)
        scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        crawl_repository = CrawlRepository(database, "mapping-fixture")
        run_id = crawl_repository.start_run(
            "mapping-fixture",
            fresh=True,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        brand_id = brands.upsert_brand("TOYOTA", "https://partsouq.com/en/catalog/genuine")
        model_id = brands.upsert_model(brand_id, "CAMRY", "S1", None)
        vehicle_id = vehicle_repository.upsert_vehicle(model_id, vehicles[0])
        alternate_vehicle = dict(vehicles[0])
        alternate_vehicle["model_code"] = "AXVA70-ALT"
        alternate_vehicle["engine"] = "A25A-FXS"
        alternate_vehicle["grade"] = "XLE"
        alternate_vehicle_id = vehicle_repository.upsert_vehicle(model_id, alternate_vehicle)
        category_id = vehicle_repository.upsert_category(vehicle_id, "ENGINE", "1")
        group_id = vehicle_repository.upsert_group(
            category_id,
            "1502",
            "OIL FILTER",
            "U1",
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&vid=1&cid=1&uid=U1",
        )
        assert part_repository.upsert_parts(group_id, parts, run_id=run_id) == 3
        alternate_category_id = vehicle_repository.upsert_category(
            alternate_vehicle_id,
            "ENGINE",
            "1",
        )
        alternate_group_id = vehicle_repository.upsert_group(
            alternate_category_id,
            "1502",
            "OIL FILTER",
            "U2",
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&vid=2&cid=1&uid=U2",
        )
        alternate_part = dict(parts[0])
        alternate_part["part_number"] = "ALT-PART-002"
        alternate_part["name"] = "ALTERNATE FILTER"
        assert (
            part_repository.upsert_parts(
                alternate_group_id,
                [alternate_part],
                run_id=run_id,
            )
            == 1
        )
        assert crawl_repository.publish_success_parts(run_id) == 4
        crawl_repository.finish_run(
            run_id,
            "success",
            {"brands": 1, "models": 1, "vehicles": 2, "groups": 2, "parts": 4},
        )
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (scheduled_job_run_id,),
        )
        database.commit()

        with pytest.raises(pymysql.MySQLError):
            database._execute(
                "UPDATE vehicles SET production_from = '2101-01' WHERE id = %s",
                (vehicle_id,),
            )

        _publish_fixture_vin(repository, tmp_path)
        monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "mapping-test-token")
        headers = {"X-Admin-Token": "mapping-test-token"}
        with TestClient(app) as client:
            unauthorized = client.get(f"/api/vins/{VIN}/vehicle-candidates")
            assert unauthorized.status_code == 401

            candidates = client.get(
                f"/api/vins/{VIN}/vehicle-candidates",
                headers=headers,
            )
            assert candidates.status_code == 200
            candidates_by_id = {row["partsouq_vehicle_id"]: row for row in candidates.json()}
            assert set(candidates_by_id) == {vehicle_id}
            assert candidates_by_id[vehicle_id]["nhtsa_engine_model"] == "A25A-FKS"
            assert candidates_by_id[vehicle_id]["nhtsa_displacement_l"] == "2.500000000"
            assert candidates_by_id[vehicle_id]["nhtsa_trim_name"] == "LE"
            assert (
                candidates_by_id[vehicle_id]["candidate_reason"]
                == "normalized_make_model_year_engine_trim_in_current_range"
            )
            assert candidates_by_id[vehicle_id]["catalog_dataset_scope"] == "full"
            assert candidates_by_id[vehicle_id]["catalog_crawl_run_id"] == run_id

            first_manual_mapping = client.post(
                "/api/vehicle-mappings",
                headers=headers,
                json={
                    "vin_prefix": "ZZZ",
                    "make_name": "Example",
                    "model_name": "Model",
                    "engine": "A:B",
                    "trim_name": "C",
                },
            )
            second_manual_mapping = client.post(
                "/api/vehicle-mappings",
                headers=headers,
                json={
                    "vin_prefix": "ZZZ",
                    "make_name": "Example",
                    "model_name": "Model",
                    "engine": "A",
                    "trim_name": "B:C",
                },
            )
            assert first_manual_mapping.status_code == 201
            assert second_manual_mapping.status_code == 201

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

            first_vehicle_parts = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert first_vehicle_parts.status_code == 200
            assert {row["part_number"] for row in first_vehicle_parts.json()} == {"TEST-PART-001"}
            assert first_vehicle_parts.json()[0]["vehicle_mapping_status"] == "confirmed"
            assert first_vehicle_parts.json()[0]["fitment_status"] == (
                "compatible_by_model_year_engine_trim"
            )

            exact_summary = client.get("/api/database-summary")
            assert exact_summary.status_code == 200
            assert exact_summary.json()["mappings"]["confirmed"] == 1
            assert exact_summary.json()["mappings"]["stale"] == 0

            corrected_mapping = client.put(
                f"/api/vin-vehicle-mappings/{mapping.json()['id']}",
                headers=headers,
                json={
                    "vin": VIN,
                    "partsouq_vehicle_id": alternate_vehicle_id,
                    "source_name": "manual-corrected",
                    "source_reference": "fixture re-confirmation",
                    "expected_updated_at": mapping.json()["updated_at"],
                },
            )
            assert corrected_mapping.status_code == 409
            assert "引擎與 Trim 相符" in corrected_mapping.json()["detail"]

            override_mapping = client.put(
                f"/api/vin-vehicle-mappings/{mapping.json()['id']}",
                headers=headers,
                json={
                    "vin": VIN,
                    "partsouq_vehicle_id": alternate_vehicle_id,
                    "allow_name_override": True,
                    "source_reference": "fixture engine and trim override evidence",
                    "expected_updated_at": mapping.json()["updated_at"],
                },
            )
            assert override_mapping.status_code == 200
            assert override_mapping.json()["partsouq_vehicle_id"] == alternate_vehicle_id
            assert override_mapping.json()["source_name"] == "manual-name-override"

            stale_mapping = client.put(
                f"/api/vin-vehicle-mappings/{mapping.json()['id']}",
                headers=headers,
                json={
                    "vin": VIN,
                    "partsouq_vehicle_id": alternate_vehicle_id,
                    "allow_name_override": True,
                    "source_reference": "stale fixture edit",
                    "expected_updated_at": mapping.json()["updated_at"],
                },
            )
            assert stale_mapping.status_code == 409
            assert "其他使用者更新" in stale_mapping.json()["detail"]

            duplicate = client.post(
                "/api/vin-vehicle-mappings",
                headers=headers,
                json={"vin": VIN, "partsouq_vehicle_id": vehicle_id},
            )
            assert duplicate.status_code == 409

            response = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert response.status_code == 200
            fitments = response.json()
            assert len(fitments) == 1
            assert {row["part_number"] for row in fitments} == {"ALT-PART-002"}
            assert fitments[0]["part_number"] == "ALT-PART-002"
            assert fitments[0]["part_name"] == "ALTERNATE FILTER"
            assert fitments[0]["make_name"] == "TOYOTA"
            assert fitments[0]["model_name"] == "CAMRY"
            assert fitments[0]["partsouq_brand"] == "TOYOTA"
            assert fitments[0]["partsouq_model"] == "CAMRY"
            assert fitments[0]["model_year"] == 2018
            assert fitments[0]["engine_model"] == "A25A-FKS"
            assert fitments[0]["displacement_l"] == "2.500000000"
            assert fitments[0]["nhtsa_trim_name"] == "LE"
            assert fitments[0]["fitment_from"] == "2018-01"
            assert fitments[0]["fitment_to"] == "2019-12"
            assert fitments[0]["vehicle_mapping_status"] == "confirmed_manual_override"
            assert fitments[0]["fitment_status"] == "manual_vehicle_override"

            database._execute(
                "UPDATE published_parts SET production_from = %s, production_to = %s "
                "WHERE vehicle_id = %s",
                ("2020-01", "2020-12", alternate_vehicle_id),
            )
            database.commit()
            out_of_range_mapping = client.get(
                f"/api/vin-vehicle-mappings?vin={VIN}",
                headers=headers,
            )
            assert out_of_range_mapping.status_code == 200
            assert out_of_range_mapping.json()[0]["vehicle_mapping_status"] == "stale"
            out_of_range_parts = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert out_of_range_parts.status_code == 200
            assert out_of_range_parts.json() == []
            database._execute(
                "UPDATE published_parts SET production_from = %s, production_to = %s "
                "WHERE vehicle_id = %s",
                ("2018-01", "2020-12", alternate_vehicle_id),
            )
            database.commit()

            part = client.get("/api/parts/TEST-PART-001/fitments")
            assert part.status_code == 200
            assert part.json()["catalog"][0]["partsouq_vehicle_id"] == vehicle_id
            assert part.json()["catalog"][0]["part_range"] == "01.2018 - 12.2019"

            normalized_part = client.get("/api/parts/TESTPART001/fitments")
            assert normalized_part.status_code == 200
            assert normalized_part.json()["catalog"][0]["partsouq_vehicle_id"] == vehicle_id

            active_mapping = client.get(
                f"/api/vin-vehicle-mappings?vin={VIN}",
                headers=headers,
            )
            assert active_mapping.status_code == 200
            assert active_mapping.json()[0]["vehicle_mapping_status"] == (
                "confirmed_manual_override"
            )

            database._execute(
                "UPDATE nhtsa_vin_decodes SET model_name = %s WHERE vin = %s",
                ("CAMRY UPDATED", VIN),
            )
            database.commit()
            changed_decode_mapping = client.get(
                f"/api/vin-vehicle-mappings?vin={VIN}",
                headers=headers,
            )
            assert changed_decode_mapping.status_code == 200
            assert changed_decode_mapping.json()[0]["vehicle_mapping_status"] == "stale"
            changed_decode_parts = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert changed_decode_parts.status_code == 200
            assert changed_decode_parts.json() == []

            reconfirmed_mapping = client.put(
                f"/api/vin-vehicle-mappings/{mapping.json()['id']}",
                headers=headers,
                json={
                    "vin": VIN,
                    "partsouq_vehicle_id": alternate_vehicle_id,
                    "allow_name_override": True,
                    "source_reference": "fixture changed decode review",
                    "expected_updated_at": override_mapping.json()["updated_at"],
                },
            )
            assert reconfirmed_mapping.status_code == 200
            assert reconfirmed_mapping.json()["model_name"] == "CAMRY UPDATED"
            assert reconfirmed_mapping.json()["source_name"] == "manual-name-override"
            recovered_parts = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert {row["part_number"] for row in recovered_parts.json()} == {"ALT-PART-002"}

            database._execute("DELETE FROM admin_vehicle_mappings WHERE vin = %s", (VIN,))
            database._execute(
                "UPDATE nhtsa_vin_decodes SET model_name = %s WHERE vin = %s",
                ("CAMRY", VIN),
            )
            untrusted_bounded_run = database._execute(
                "INSERT INTO crawl_runs(run_key, started_at, finished_at, status, "
                "dataset_kind, target_parts, parts_ok) VALUES "
                "(%s, UTC_TIMESTAMP(), UTC_TIMESTAMP(), 'bounded_success', 'bounded', 3, 3)",
                ("bounded-3-untrusted",),
            )
            untrusted_bounded_run_id = int(untrusted_bounded_run.lastrowid)
            database._execute(
                "INSERT INTO bounded_parts("
                "part_id, crawl_run_id, vehicle_id, model_id, vehicle_vid, brand, model, "
                "vehicle_name, vehicle_code, prod_period, production_from, production_to, "
                "engine, trim_name, part_name, part_number, part_number_normalized, "
                "category_id, category_cid, category_main, category_group, group_id, "
                "group_code, group_uid, part_range, part_from, part_to, source_url, note, "
                "quantity, code, snapshot_at) SELECT part_id, %s, vehicle_id, model_id, "
                "vehicle_vid, brand, model, vehicle_name, vehicle_code, prod_period, "
                "production_from, production_to, engine, trim_name, part_name, part_number, "
                "part_number_normalized, category_id, category_cid, category_main, "
                "category_group, group_id, group_code, group_uid, part_range, part_from, "
                "part_to, source_url, note, quantity, code, snapshot_at FROM published_parts "
                "WHERE part_number <> 'OUTSIDE-RANGE'",
                (untrusted_bounded_run_id,),
            )
            database.commit()

            full_precedence = client.get(
                f"/api/vins/{VIN}/vehicle-candidates",
                headers=headers,
            )
            assert full_precedence.status_code == 200
            assert {row["catalog_dataset_scope"] for row in full_precedence.json()} == {"full"}

            database._execute("DELETE FROM published_parts")
            database.commit()
            untrusted_candidates = client.get(
                f"/api/vins/{VIN}/vehicle-candidates",
                headers=headers,
            )
            assert untrusted_candidates.status_code == 200
            assert untrusted_candidates.json() == []

            scheduler_run_id = int(
                database._execute(
                    "INSERT INTO scheduled_job_runs("
                    "job_name, trigger_mode, status, started_at) "
                    "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
                ).lastrowid
            )
            bounded_crawl = CrawlRepository(database, "bounded-10000-mapping-daemon")
            bounded_run_id = bounded_crawl.start_run(
                "bounded-10000-mapping-daemon",
                fresh=True,
                dataset_kind="bounded",
                target_parts=10_000,
                scheduled_job_run_id=scheduler_run_id,
            )
            bounded_fixture_parts = [parts[0]] + [
                {
                    "part_number": f"BOUND-{index:05d}",
                    "name": f"BOUNDED MAPPING PART {index:05d}",
                    "code": f"B{index:05d}",
                    "note": "synthetic integration fixture; not live crawl evidence",
                    "quantity": "01",
                    "range_str": "01.2019 - 12.2020",
                    "part_from": "2019-01",
                    "part_to": "2020-12",
                }
                for index in range(1, 10_000)
            ]
            part_repository.upsert_parts(
                group_id,
                bounded_fixture_parts,
                run_id=bounded_run_id,
            )
            assert bounded_crawl.count_run_parts(bounded_run_id) == 10_000
            # 這段只驗證 full/bounded mapping 的讀取優先序；synthetic fixture
            # 不得偽造 live HTTP evidence 或呼叫正式發布入口。
            database._execute("DELETE FROM bounded_parts")
            database._execute(
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
                "categories.id, categories.cid, categories.name, groups_t.name, "
                "groups_t.id, groups_t.code, groups_t.uid, parts.range_str, "
                "parts.part_from, parts.part_to, groups_t.url, parts.note, "
                "parts.quantity, parts.code, UTC_TIMESTAMP() FROM parts "
                "JOIN groups_t ON groups_t.id = parts.group_id "
                "JOIN categories ON categories.id = groups_t.category_id "
                "JOIN vehicles ON vehicles.id = categories.vehicle_id "
                "JOIN models ON models.id = vehicles.model_id "
                "JOIN brands ON brands.id = models.brand_id "
                "WHERE parts.seen_run_id = %s",
                (bounded_run_id, bounded_run_id),
            )
            bounded_crawl.finish_run(
                bounded_run_id,
                "bounded_success",
                {
                    "brands": 1,
                    "models": 1,
                    "vehicles": 1,
                    "groups": 1,
                    "parts": 10_000,
                    "parts_new": 9_999,
                },
            )
            database._execute(
                "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
                "finished_at = UTC_TIMESTAMP() WHERE id = %s",
                (scheduler_run_id,),
            )
            database.commit()

            bounded_candidates = client.get(
                f"/api/vins/{VIN}/vehicle-candidates",
                headers=headers,
            )
            assert bounded_candidates.status_code == 200
            bounded_by_id = {row["partsouq_vehicle_id"]: row for row in bounded_candidates.json()}
            assert bounded_by_id == {}

            bounded_mapping = client.post(
                "/api/vin-vehicle-mappings",
                headers=headers,
                json={"vin": VIN, "partsouq_vehicle_id": vehicle_id},
            )
            assert bounded_mapping.status_code == 409
            assert (
                "不是品牌、型號、年份、引擎與 Trim 相符的候選" in (bounded_mapping.json()["detail"])
            )

            bounded_active_mapping = client.get(
                f"/api/vin-vehicle-mappings?vin={VIN}",
                headers=headers,
            )
            assert bounded_active_mapping.status_code == 200
            assert bounded_active_mapping.json() == []

            bounded_summary = client.get("/api/database-summary")
            assert bounded_summary.status_code == 200
            assert bounded_summary.json()["bounded_ready"] is False
            assert bounded_summary.json()["bounded"]["blocking_reasons"] == [
                "bounded_non_live_data_marker",
                "bounded_live_mapping_evidence_not_verified",
            ]
            assert bounded_summary.json()["mappings"]["confirmed"] == 0
            assert bounded_summary.json()["mappings"]["stale"] == 0

            bounded_parts = client.get(f"/api/vins/{VIN}/parts", headers=headers)
            assert bounded_parts.status_code == 200
            assert bounded_parts.json() == []
    finally:
        database.close()
        _clear_shared_database(repository)
        repository.close()
