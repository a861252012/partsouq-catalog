from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
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

pytestmark = pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run the isolated 10,000-row performance gate",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRESH_SCHEMA_PATHS = (
    PROJECT_ROOT / "db" / "catalog.sql",
    PROJECT_ROOT / "db" / "nhtsa.sql",
    PROJECT_ROOT / "db" / "admin.sql",
    PROJECT_ROOT / "db" / "station_admin.sql",
)
MIGRATION_009_PATH = PROJECT_ROOT / "migrations" / "catalog" / "009_bounded_production_dataset.sql"
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
        _apply_sql_paths(database, FRESH_SCHEMA_PATHS)
        _apply_sql_paths(database, (MIGRATION_009_PATH,))
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


def _seed_synthetic_bounded_dataset(database: PerformanceDatabase) -> dict[str, int]:
    _validate_test_database_name(database.database)
    catalog_database = Database().connect()
    try:
        scheduler_run_id = catalog_database._execute(
            "INSERT INTO scheduled_job_runs("
            "job_name, trigger_mode, status, started_at, output_text"
            ") VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP(), %s)",
            ("SYNTHETIC PERFORMANCE FIXTURE; NOT LIVE CRAWL EVIDENCE",),
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
        assert crawl.publish_bounded_parts(crawl_run_id, TARGET_PARTS) == TARGET_PARTS
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


def _fetch_one(database: PerformanceDatabase, sql: str) -> dict[str, Any]:
    connection = database.connect(autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
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
    _configure_catalog_database(monkeypatch, performance_database)
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
        "(SELECT COUNT(*) FROM published_parts) AS published_rows, "
        "(SELECT COUNT(*) FROM crawl_runs WHERE status = 'sample') AS sample_runs, "
        "(SELECT COUNT(*) FROM admin_override_heads) AS override_rows",
    )
    assert direct == {
        "bounded_rows": TARGET_PARTS,
        "current_rows": TARGET_PARTS,
        "current_bounded_rows": TARGET_PARTS,
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
    with TestClient(data_admin_app.app) as data_client:
        data_queries.clear()
        summary = data_client.get("/api/database-summary")
        _assert_data_response(summary, 8, data_queries)
        summary_json = summary.json()
        assert summary_json["bounded_ready"] is False
        assert "bounded_non_live_data_marker" in summary_json["bounded"]["blocking_reasons"]
        assert summary_json["bounded"]["fitment_rows"] == TARGET_PARTS
        assert summary_json["bounded"]["unique_part_numbers"] == (
            TARGET_PARTS - SHARED_PART_FITMENTS + 1
        )
        assert summary_json["bounded"]["unique_vehicles"] == seeded["vehicles"]
        assert summary_json["bounded"]["crawl_run_id"] == seeded["crawl_run_id"]
        assert summary_json["bounded"]["scheduler"]["run_id"] == seeded["scheduler_run_id"]
        assert summary_json["bounded"]["source_provenance"]["raw_http_artifact_status"] == (
            "not_persisted_by_catalog_crawler"
        )
        assert summary_json["production_ready"] is False
        data_admin_report["synthetic_readiness"] = {
            "bounded_ready": summary_json["bounded_ready"],
            "blocking_reasons": summary_json["bounded"]["blocking_reasons"],
        }
        summary_queries = tuple(data_queries)

        def summary_request() -> None:
            data_queries.clear()
            response = data_client.get("/api/database-summary")
            _assert_data_response(response, 8, data_queries)

        data_admin_report["summary"] = _measure(summary_request)

        for page_size in PAGE_SIZES:
            total_pages = math.ceil(TARGET_PARTS / page_size)
            for page_name, page in (("first", 1), ("last", total_pages)):
                path = f"/api/bounded-parts?page={page}&pageSize={page_size}"

                def bounded_request(path: str = path) -> None:
                    data_queries.clear()
                    response = data_client.get(path)
                    _assert_data_response(response, 2, data_queries)

                data_queries.clear()
                response = data_client.get(path)
                _assert_data_response(response, 2, data_queries)
                payload = response.json()
                assert payload["total"] == TARGET_PARTS
                assert payload["pageSize"] == page_size
                assert len(payload["items"]) == min(
                    page_size,
                    TARGET_PARTS - ((page - 1) * page_size),
                )
                if page_size == 200:
                    data_admin_report[f"bounded_200_{page_name}_explain"] = [
                        _explain_plan(performance_database, sql, params)
                        for sql, params in data_queries
                    ]
                data_admin_report[f"bounded_{page_size}_{page_name}"] = _measure(bounded_request)

        data_queries.clear()
        exact = data_client.get(
            "/api/bounded-parts",
            params={"part_number": SHARED_PART_NORMALIZED, "pageSize": 30},
        )
        _assert_data_response(exact, 2, data_queries)
        assert exact.json()["total"] == SHARED_PART_FITMENTS
        assert len(exact.json()["items"]) == 30
        assert exact.json()["items"][0]["part_number"] == SHARED_PART_NUMBER
        exact_queries = tuple(data_queries)
        data_queries.clear()
        exact_last = data_client.get(
            "/api/bounded-parts",
            params={"part_number": SHARED_PART_NORMALIZED, "pageSize": 30, "page": 4},
        )
        _assert_data_response(exact_last, 2, data_queries)
        assert exact_last.json()["total"] == SHARED_PART_FITMENTS
        assert len(exact_last.json()["items"]) == 10

        def exact_request() -> None:
            data_queries.clear()
            response = data_client.get(
                "/api/bounded-parts",
                params={"part_number": SHARED_PART_NORMALIZED, "pageSize": 30},
            )
            _assert_data_response(response, 2, data_queries)
            assert response.json()["total"] == SHARED_PART_FITMENTS

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
        total_pages = math.ceil(TARGET_PARTS / page_size)
        for page_name, page in (("first", 1), ("last", total_pages)):
            path = f"/entities/part_numbers?dataset=formal&page={page}&pageSize={page_size}"

            def station_request(path: str = path) -> None:
                response = station_client.get(path)
                assert response.status_code == 200
                assert response.headers["X-Admin-Query-Count"] == "4"

            response = station_client.get(path)
            assert response.status_code == 200
            assert response.headers["X-Admin-Query-Count"] == "4"
            assert f"共 {TARGET_PARTS} 筆記錄".encode() in response.data
            station_admin_report[f"formal_{page_size}_{page_name}"] = _measure(station_request)

    def station_exact_request() -> None:
        response = station_client.get(
            f"/entities/part_numbers?dataset=formal&q={SHARED_PART_NORMALIZED}&pageSize=30"
        )
        assert response.status_code == 200
        assert response.headers["X-Admin-Query-Count"] == "4"
        assert SHARED_PART_NUMBER.encode() in response.data
        assert f"共 {SHARED_PART_FITMENTS} 筆記錄".encode() in response.data

    station_exact_request()
    station_exact_last = station_client.get(
        f"/entities/part_numbers?dataset=formal&q={SHARED_PART_NORMALIZED}&pageSize=30&page=4"
    )
    assert station_exact_last.status_code == 200
    assert station_exact_last.headers["X-Admin-Query-Count"] == "4"
    assert f"顯示 91 到 100，共 {SHARED_PART_FITMENTS} 筆記錄".encode() in (station_exact_last.data)
    station_admin_report["formal_exact_normalized"] = _measure(station_exact_request)

    summary_p95 = float(data_admin_report["summary"]["p95_ms"])
    assert summary_p95 < SUMMARY_P95_LIMIT_MS
    for section in (data_admin_report, station_admin_report):
        for name, metrics in section.items():
            if name in {"summary", "synthetic_readiness"} or name.endswith("_explain"):
                continue
            assert float(metrics["p95_ms"]) < PAGE_P95_LIMIT_MS

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    assert all(
        any(
            table.get("table") == "candidate"
            and table.get("key") == "idx_bounded_part_number_normalized"
            and table.get("access_type") == "ref"
            and int(table.get("rows_examined_per_scan") or TARGET_PARTS) <= SHARED_PART_FITMENTS
            for table in plan["tables"]
        )
        for plan in exact_explain
    )
