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


def test_same_group_code_with_distinct_uids_keeps_both_units_and_receipts() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()

        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        crawl = CrawlRepository(database, "group-uid-fixture")
        run_id = crawl.start_run("group-uid-fixture", fresh=True)

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
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        first_group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "ENGINE BLOCK A",
            "UID-A",
            "https://partsouq.com/en/catalog/genuine/unit?uid=UID-A",
        )
        second_group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "ENGINE BLOCK B",
            "UID-B",
            "https://partsouq.com/en/catalog/genuine/unit?uid=UID-B",
        )
        assert first_group_id != second_group_id
        assert (
            vehicles.upsert_group(
                category_id,
                "1101",
                "ENGINE BLOCK A UPDATED",
                "UID-A",
                "https://partsouq.com/en/catalog/genuine/unit?uid=UID-A",
            )
            == first_group_id
        )
        # migration 010 對無法回推出 source uid 的 legacy NULL 只能保留為
        # 空字串；它不能反過來阻擋新一輪完整 manifest 對帳。
        vehicles.upsert_group(
            category_id,
            "LEGACY",
            "LEGACY WITHOUT UID",
            None,
            None,
        )

        row = {
            "name": "ENGINE PART",
            "code": "11000",
            "note": None,
            "quantity": "01",
            "range_str": "",
            "part_from": None,
            "part_to": None,
        }
        parts.upsert_parts(first_group_id, [{**row, "part_number": "PART-A"}], run_id)
        parts.upsert_parts(second_group_id, [{**row, "part_number": "PART-B"}], run_id)
        crawl.mark_group_fetched(first_group_id, "group-uid-fixture", row_count=1)
        crawl.mark_group_fetched(second_group_id, "group-uid-fixture", row_count=1)
        database.commit()

        assert vehicles.list_group_identities_for_category(vehicle_id, "1") == {
            ("1101", "UID-A"),
            ("1101", "UID-B"),
        }
        assert crawl.fetched_group_map(vehicle_id, "group-uid-fixture") == {
            ("1", "1101", "UID-A"): 1,
            ("1", "1101", "UID-B"): 1,
        }
        assert crawl.previous_row_count_map(vehicle_id, "group-uid-fixture") == {
            ("1", "1101", "UID-A"): 1,
            ("1", "1101", "UID-B"): 1,
        }
        assert crawl.is_group_fetched(vehicle_id, "1101", "UID-A", "group-uid-fixture")
        assert crawl.is_group_fetched(vehicle_id, "1101", "UID-B", "group-uid-fixture")
        assert database._execute(
            "SELECT COUNT(*) AS n FROM parts WHERE group_id IN (%s, %s)",
            (first_group_id, second_group_id),
        ).fetchone() == {"n": 2}
    finally:
        database.rollback()
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()
        database.close()
