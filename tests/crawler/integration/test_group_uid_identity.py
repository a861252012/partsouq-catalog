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
            "UID-A": {"1101"},
            "UID-B": {"1101"},
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


def test_group_code_transition_reconciles_by_uid() -> None:
    """圖片-only（code 空）↔ 文字（code 有值）跨月轉換：同 uid 就地
    更新 code，不產生重複列 —— 否則 closure 對帳永久失敗、brick 車型。"""
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
        brand_id = brands.upsert_brand("MITSUBISHI", None)
        model_id = brands.upsert_model(brand_id, "L300", "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "L300",
                "model_code": "P35W",
                "prod_period": "",
                "production_from": None,
                "production_to": None,
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")

        image_id = vehicles.upsert_group(category_id, "", "", "UID-IMG", "url-img")
        assert (
            vehicles.upsert_group(category_id, "0901", "ENGINE BLOCK", "UID-IMG", "url-text")
            == image_id
        )
        row = database._execute(
            "SELECT code, name, url FROM groups_t WHERE id = %s", (image_id,)
        ).fetchone()
        assert row == {"code": "0901", "name": "ENGINE BLOCK", "url": "url-text"}
        assert vehicles.list_group_identities_for_category(vehicle_id, "1") == {"UID-IMG": {"0901"}}
        assert database._execute(
            "SELECT COUNT(*) AS n FROM groups_t WHERE category_id = %s", (category_id,)
        ).fetchone() == {"n": 1}
    finally:
        database.rollback()
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()
        database.close()


def test_legacy_uid_empty_groups_are_not_reconciled_by_uid() -> None:
    """uid 為空的 legacy 列只能靠 (code, uid) 唯一鍵，不得被 by-uid 對帳
    錯改（否則同 uid 空字串的 legacy 列會互相覆寫 code）。"""
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
        brand_id = brands.upsert_brand("LEGACY-BRAND", None)
        model_id = brands.upsert_model(brand_id, "LEGACY-MODEL", "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "LEGACY",
                "model_code": "X",
                "prod_period": "",
                "production_from": None,
                "production_to": None,
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        first_id = vehicles.upsert_group(category_id, "LEGACY", "LEGACY ROW", None, None)
        second_id = vehicles.upsert_group(category_id, "LEGACY2", "LEGACY ROW 2", None, None)
        assert first_id != second_id
        assert (
            vehicles.upsert_group(category_id, "LEGACY", "LEGACY ROW UPDATED", None, None)
            == first_id
        )
        rows = {
            r["code"]: r["name"]
            for r in database._execute(
                "SELECT code, name FROM groups_t WHERE category_id = %s", (category_id,)
            ).fetchall()
        }
        assert rows == {"LEGACY": "LEGACY ROW UPDATED", "LEGACY2": "LEGACY ROW 2"}
    finally:
        database.rollback()
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()
        database.close()


def test_same_uid_variant_text_groups_are_not_merged() -> None:
    """同 (cid, uid) 但 code 不同的文字列是變體專屬資料：by-uid 對帳
    只升級空 code 列，不得互相覆寫或合併。"""
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
        brand_id = brands.upsert_brand("VARIANT-BRAND", None)
        model_id = brands.upsert_model(brand_id, "VARIANT-MODEL", "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "VARIANT",
                "model_code": "X",
                "prod_period": "",
                "production_from": None,
                "production_to": None,
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        a_id = vehicles.upsert_group(category_id, "0902", "VARIANT A", "UID-V", "url-a")
        b_id = vehicles.upsert_group(category_id, "0903", "VARIANT B", "UID-V", "url-b")
        assert a_id != b_id
        assert vehicles.upsert_group(category_id, "0903", "VARIANT B NEW", "UID-V", "url-b") == b_id
        rows = {
            r["code"]: (r["name"], r["id"])
            for r in database._execute(
                "SELECT code, name, id FROM groups_t WHERE category_id = %s", (category_id,)
            ).fetchall()
        }
        assert rows == {
            "0902": ("VARIANT A", a_id),
            "0903": ("VARIANT B NEW", b_id),
        }
    finally:
        database.rollback()
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()
        database.close()


def test_image_upgrade_skipped_when_text_row_exists() -> None:
    """text→image→text 共存後：image 月不會覆寫既有文字列，文字月
    不會試圖升級而撞唯一鍵（db._execute 對 IntegrityError 會回滾整個
    transaction，此路徑必須完全不觸發例外），且同交易內先前寫入不被丟棄。"""
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
        brand_id = brands.upsert_brand("COEXIST-BRAND", None)
        model_id = brands.upsert_model(brand_id, "COEXIST-MODEL", "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "COEXIST",
                "model_code": "X",
                "prod_period": "",
                "production_from": None,
                "production_to": None,
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL RENAMED", "1")

        text_id = vehicles.upsert_group(category_id, "0901", "BRAKE", "UID-C", "url-text")
        image_id = vehicles.upsert_group(category_id, "", "", "UID-C", "url-img")
        assert image_id != text_id
        assert (
            vehicles.upsert_group(category_id, "0901", "BRAKE NEW", "UID-C", "url-text") == text_id
        )
        database.commit()

        text_row = database._execute(
            "SELECT code, name FROM groups_t WHERE id = %s", (text_id,)
        ).fetchone()
        assert text_row == {"code": "0901", "name": "BRAKE NEW"}
        cat_name = database._execute(
            "SELECT name FROM categories WHERE id = %s", (category_id,)
        ).fetchone()["name"]
        assert cat_name == "ENGINE/FUEL/TOOL RENAMED"
    finally:
        database.rollback()
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()
        database.close()


def test_text_group_not_overwritten_by_image_only_month() -> None:
    """文字月已取得的 code/name 不得被後續圖片-only 月覆寫成空值。"""
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
        brand_id = brands.upsert_brand("TEXT-IMG-BRAND", None)
        model_id = brands.upsert_model(brand_id, "TEXT-IMG-MODEL", "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "TEXT-IMG",
                "model_code": "X",
                "prod_period": "",
                "production_from": None,
                "production_to": None,
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        text_id = vehicles.upsert_group(category_id, "0901", "BRAKE", "UID-T", "url-text")
        image_id = vehicles.upsert_group(category_id, "", "", "UID-T", "url-img")
        assert image_id != text_id
        text_row = database._execute(
            "SELECT code, name FROM groups_t WHERE id = %s", (text_id,)
        ).fetchone()
        assert text_row == {"code": "0901", "name": "BRAKE"}
        assert vehicles.list_group_identities_for_category(vehicle_id, "1") == {
            "UID-T": {"0901", ""}
        }
    finally:
        database.rollback()
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()
        database.close()


def test_identity_cache_path_matches_no_cache_path() -> None:
    """SOL review P2：preload_group_identity + upsert_group(identity=...)
    的快取路徑必須與逐次 SELECT 的無快取路徑行為一致（image→text 就地
    升級、不長重複列、變體不 merge、快取就地更新）。"""
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
        brand_id = brands.upsert_brand("CACHE-BRAND", None)
        model_id = brands.upsert_model(brand_id, "CACHE-MODEL", "MODEL-SSD", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "CACHE",
                "model_code": "X",
                "prod_period": "",
                "production_from": None,
                "production_to": None,
                "vid": "SITE-VID-1",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")

        identity = vehicles.preload_group_identity(category_id)
        assert identity.by_key == {}
        assert identity.image_by_uid == {}

        # 1) image-only 插入 → 快取同步
        image_id = vehicles.upsert_group(category_id, "", "", "UID-C", "url-img", identity=identity)
        assert identity.image_by_uid["UID-C"] == image_id
        assert identity.by_key[("", "UID-C")] == image_id

        # 2) 同 uid 文字組經快取路徑就地升級同一列（不長重複列）
        text_id = vehicles.upsert_group(
            category_id, "0901", "BRAKE", "UID-C", "url-text", identity=identity
        )
        assert text_id == image_id
        assert "UID-C" not in identity.image_by_uid
        assert identity.by_key[("0901", "UID-C")] == image_id
        assert (
            database._execute(
                "SELECT COUNT(*) AS n FROM groups_t WHERE category_id = %s", (category_id,)
            ).fetchone()["n"]
            == 1
        )

        # 3) 同 uid 第二個文字變體（不同 code）→ 新列，不 merge
        variant_id = vehicles.upsert_group(
            category_id, "0902", "BRAKE V2", "UID-C", "url-v2", identity=identity
        )
        assert variant_id != text_id
        assert identity.by_key[("0902", "UID-C")] == variant_id
        assert (
            database._execute(
                "SELECT COUNT(*) AS n FROM groups_t WHERE category_id = %s", (category_id,)
            ).fetchone()["n"]
            == 2
        )

        # 4) 重複 upsert 同一 (code, uid) → 同一列、快取仍一致
        again = vehicles.upsert_group(
            category_id, "0901", "BRAKE NEW", "UID-C", "url-text", identity=identity
        )
        assert again == text_id
        assert identity.by_key[("0901", "UID-C")] == text_id
        database.commit()

        # 5) 全新 preload 與快取路徑在「真實存在的列」上一致；唯一差異是
        # 快取在升級後保留 ("", uid) 的 stale 項目（文件已註明無害）。
        fresh = vehicles.preload_group_identity(category_id)
        assert fresh.image_by_uid == identity.image_by_uid
        for key, row_id in fresh.by_key.items():
            assert identity.by_key[key] == row_id
        assert ("", "UID-C") not in fresh.by_key
        assert ("", "UID-C") in identity.by_key
    finally:
        database.rollback()
        database._execute("DELETE FROM crawl_state")
        database._execute("DELETE FROM crawl_runs")
        database._execute("DELETE FROM brands")
        database.commit()
        database.close()
