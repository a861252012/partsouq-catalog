from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pymysql
import pytest
from playwright.sync_api import Browser, Error, Playwright, expect, sync_playwright
from pymysql.constants import CLIENT
from pymysql.cursors import DictCursor
from werkzeug.serving import make_server

from partsouq_station_admin.app import create_app
from partsouq_station_admin.config import AdminConfig

pytestmark = pytest.mark.skipif(
    os.getenv("STATION_ADMIN_E2E") != "1",
    reason="set STATION_ADMIN_E2E=1 to run the station-admin browser E2E gate",
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATHS = (
    PROJECT_ROOT / "db" / "catalog.sql",
    PROJECT_ROOT / "db" / "nhtsa.sql",
    PROJECT_ROOT / "db" / "admin.sql",
    PROJECT_ROOT / "db" / "station_admin.sql",
)
DATABASE_NAME_PATTERN = re.compile(r"^[a-z0-9_]+_test$")
TARGET_PART_ID = 200
SOURCE_PART_NAME = "E2E SOURCE PART 0200"
OVERRIDE_PART_NAME = "E2E OVERRIDE PART 0200"
ACTOR = "station-admin-e2e"


@dataclass(frozen=True, slots=True)
class E2EDatabase:
    host: str
    port: int
    database: str
    user: str
    password: str

    def connect(self) -> pymysql.Connection[DictCursor]:
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
        )

    def admin_config(self) -> AdminConfig:
        return AdminConfig(
            mysql_host=self.host,
            mysql_port=self.port,
            mysql_user=self.user,
            mysql_password=self.password,
            mysql_database=self.database,
            bind_host="127.0.0.1",
            bind_port=0,
            secret_key="station-admin-e2e-secret",
            default_actor=ACTOR,
            page_size=50,
        )


@pytest.fixture
def e2e_database() -> Iterator[E2EDatabase]:
    host = os.environ["PARTSOUQ_DB_HOST"]
    port = int(os.environ["PARTSOUQ_DB_PORT"])
    root_password = os.environ["PARTSOUQ_MYSQL_ROOT_PASSWORD"]
    database_name = f"partsouq_station_admin_{uuid.uuid4().hex[:12]}_test"
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
        database = E2EDatabase(host, port, database_name, "root", root_password)
        _apply_schemas(database)
        _seed_parts(database)
        yield database
    finally:
        _validate_test_database_name(database_name)
        with root_connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database_name}`")
        root_connection.close()


def _validate_test_database_name(database_name: str) -> None:
    if DATABASE_NAME_PATTERN.fullmatch(database_name) is None:
        raise ValueError("E2E database name must be a safe identifier ending in _test")


def _apply_schemas(database: E2EDatabase) -> None:
    connection = pymysql.connect(
        host=database.host,
        port=database.port,
        user=database.user,
        password=database.password,
        database=database.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
        client_flag=CLIENT.MULTI_STATEMENTS,
    )
    try:
        with connection.cursor() as cursor:
            for schema_path in SCHEMA_PATHS:
                cursor.execute(schema_path.read_text(encoding="utf-8"))
                while cursor.nextset():
                    pass
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _seed_parts(database: E2EDatabase) -> None:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO brands(name, code, url) VALUES (%s, %s, %s)",
                ("E2E MOTORS", "E2E", "https://partsouq.example/e2e"),
            )
            brand_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO models(brand_id, name, ssd, url, fetched_at) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP())",
                (brand_id, "E2E MODEL", "model-secret", "https://partsouq.example/e2e/model"),
            )
            model_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO vehicles("
                "model_id, identity_hash, name, model_code, prod_period, "
                "production_from, production_to, engine, ssd, vid, url, fetched_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())",
                (
                    model_id,
                    hashlib.sha256(b"station-admin-e2e-vehicle").hexdigest(),
                    "E2E VEHICLE",
                    "E2E-1000",
                    "01.2020 - 12.2025",
                    "2020-01",
                    "2025-12",
                    "E2E ENGINE",
                    "vehicle-secret",
                    "E2E-VID",
                    "https://partsouq.example/e2e/vehicle?ssd=vehicle-secret",
                ),
            )
            vehicle_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO categories(vehicle_id, name, cid, fetched_at) "
                "VALUES (%s, %s, %s, UTC_TIMESTAMP())",
                (vehicle_id, "E2E MAIN CATEGORY", "E2E-CID"),
            )
            category_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO groups_t("
                "category_id, code, name, uid, url, fetched_at, fetched_status, "
                "fetched_row_count, verified_row_count"
                ") VALUES (%s, %s, %s, %s, %s, UTC_TIMESTAMP(), %s, %s, %s)",
                (
                    category_id,
                    "E2E1",
                    "E2E GROUP",
                    "E2E-UID",
                    "https://partsouq.example/e2e/group?ssd=group-secret",
                    "done",
                    1000,
                    1000,
                ),
            )
            group_id = cursor.lastrowid
            rows = [
                (
                    group_id,
                    f"E2E-{index:06d}",
                    f"E2E SOURCE PART {index:04d}",
                    f"C{index:04d}",
                    "fixture",
                    "01",
                    "01.2020 - 12.2025",
                    "2020-01",
                    "2025-12",
                    f"https://partsouq.example/e2e/part/{index}?ssd=part-secret",
                )
                for index in range(1, 1001)
            ]
            cursor.executemany(
                "INSERT INTO parts("
                "group_id, part_number, name, code, note, quantity, range_str, "
                "part_from, part_to, url"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def _running_server(database: E2EDatabase) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, create_app(database.admin_config()), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _launch_chromium(playwright: Playwright) -> Browser:
    channel = os.getenv("STATION_ADMIN_E2E_BROWSER_CHANNEL", "").strip()
    try:
        if channel:
            return playwright.chromium.launch(headless=True, channel=channel)
        return playwright.chromium.launch(headless=True)
    except Error as error:
        pytest.fail(
            "STATION_ADMIN_E2E=1 requires a launchable Chromium browser; "
            "install it with `playwright install chromium` or set "
            "STATION_ADMIN_E2E_BROWSER_CHANNEL",
            pytrace=False,
        )
        raise AssertionError from error


def _read_database_evidence(database: E2EDatabase) -> dict[str, object]:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM parts WHERE id = %s", (TARGET_PART_ID,))
            source = cursor.fetchone()
            cursor.execute(
                "SELECT id, payload_json, status, revision, actor, reason, base_sha256 "
                "FROM admin_override_heads "
                "WHERE entity_type = 'part_numbers' AND identity_key = %s",
                (f"source:{TARGET_PART_ID}",),
            )
            head = cursor.fetchone()
            cursor.execute(
                "SELECT action, revision, actor, reason, before_json, after_json "
                "FROM admin_override_events "
                "WHERE entity_type = 'part_numbers' AND identity_key = %s "
                "ORDER BY revision",
                (f"source:{TARGET_PART_ID}",),
            )
            events = cursor.fetchall()
        return {"source": source, "head": head, "events": events}
    finally:
        connection.close()


def _cleanup_override(database: E2EDatabase) -> None:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM admin_override_heads "
                "WHERE entity_type = 'part_numbers' AND identity_key = %s FOR UPDATE",
                (f"source:{TARGET_PART_ID}",),
            )
            heads = cursor.fetchall()
            for head in heads:
                cursor.execute(
                    "DELETE FROM admin_override_events WHERE head_id = %s",
                    (head["id"],),
                )
                cursor.execute(
                    "DELETE FROM admin_override_heads WHERE id = %s",
                    (head["id"],),
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def test_station_admin_part_lifecycle_through_real_browser_and_mysql(
    e2e_database: E2EDatabase,
) -> None:
    try:
        with _running_server(e2e_database) as base_url, sync_playwright() as playwright:
            browser = _launch_chromium(playwright)
            try:
                page = browser.new_page()
                page.goto(base_url)
                normalized_card = page.locator(".summary-grid > div").filter(
                    has_text="PartSouq normalized rows"
                )
                distinct_card = page.locator(".summary-grid > div").filter(
                    has_text="PartSouq distinct part numbers"
                )
                expect(normalized_card).to_contain_text("1000")
                expect(distinct_card).to_contain_text("1000")

                page.goto(f"{base_url}/entities/part_numbers")

                page.get_by_label("每頁").select_option("200")
                page.get_by_role("button", name="查詢").click()
                expect(page).to_have_url(re.compile(r"pageSize=200"))
                expect(
                    page.get_by_text("顯示 1 到 200，共 1000 筆記錄", exact=True)
                ).to_be_visible()
                expect(page.get_by_text("頁，共 5 頁", exact=False)).to_be_visible()

                page.get_by_role("link", name="末頁").click()
                expect(page.get_by_label("頁碼")).to_have_value("5")
                expect(
                    page.get_by_text("顯示 801 到 1000，共 1000 筆記錄", exact=True)
                ).to_be_visible()

                page.get_by_role("link", name=f"source:{TARGET_PART_ID}").click()
                expect(page.get_by_role("heading", name=re.compile(r"source:200"))).to_be_visible()
                expect(page.get_by_text(SOURCE_PART_NAME, exact=True)).to_be_visible()

                page.get_by_role("link", name="編輯覆寫").click()
                page.locator('input[name="field__name_en_raw"]').fill(OVERRIDE_PART_NAME)
                page.get_by_label("操作者").fill(ACTOR)
                page.get_by_label("修改原因").fill("E2E update")
                page.get_by_role("button", name="儲存新版本").click()
                expect(
                    page.get_by_text("已新增一筆覆寫版本；來源型錄資料未被修改。", exact=True)
                ).to_be_visible()
                expect(page.get_by_text(OVERRIDE_PART_NAME, exact=True)).to_be_visible()
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 1")

                page.get_by_label("操作者").fill(ACTOR)
                page.get_by_label("原因").fill("E2E retire")
                page.get_by_role("button", name="停用").click()
                expect(page.locator(".page-title p")).to_contain_text("狀態 retired")
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 2")

                page.get_by_label("操作者").fill(ACTOR)
                page.get_by_label("原因").fill("E2E restore")
                page.get_by_role("button", name="恢復").click()
                expect(page.locator(".page-title p")).to_contain_text("狀態 active")
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 3")
            finally:
                browser.close()

        evidence = _read_database_evidence(e2e_database)
        assert evidence["source"] == {"name": SOURCE_PART_NAME}
        head = evidence["head"]
        assert isinstance(head, dict)
        assert head["status"] == "active"
        assert head["revision"] == 3
        assert head["actor"] == ACTOR
        assert head["reason"] == "E2E restore"
        assert len(head["base_sha256"]) == 64
        payload = json.loads(head["payload_json"])
        assert payload["name_en_raw"] == OVERRIDE_PART_NAME

        events = evidence["events"]
        assert isinstance(events, list)
        assert [(event["action"], event["revision"]) for event in events] == [
            ("update", 1),
            ("retire", 2),
            ("restore", 3),
        ]
        assert [event["actor"] for event in events] == [ACTOR, ACTOR, ACTOR]
        assert [event["reason"] for event in events] == [
            "E2E update",
            "E2E retire",
            "E2E restore",
        ]
        assert json.loads(events[0]["before_json"])["name_en_raw"] == SOURCE_PART_NAME
        assert json.loads(events[0]["after_json"])["name_en_raw"] == OVERRIDE_PART_NAME
    finally:
        _cleanup_override(e2e_database)
