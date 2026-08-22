from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest import mock

import pymysql
import pytest
from fastapi.testclient import TestClient
from playwright._impl import _transport as playwright_transport
from playwright.sync_api import Browser, Error, Playwright, expect, sync_playwright
from pymysql.cursors import DictCursor
from werkzeug.serving import make_server

from partsouq_admin import app as data_admin_app
from partsouq_crawler.crawl.browser_fetcher import (
    browser_driver_environment,
    browser_process_environment,
)
from partsouq_station_admin.app import create_app
from partsouq_station_admin.config import AdminConfig
from tests.e2e.test_bounded_admin_performance import LOCAL_DATABASE_HOSTS, _mysql_statements

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
PUBLISHED_PART_NAME = "E2E SOURCE PART 0200"
PUBLISHED_PART_NUMBER = f"E2E-{TARGET_PART_ID:06d}"
NORMALIZED_PART_NAME = "E2E NORMALIZED PART 0200"
NORMALIZED_PART_NUMBER = "E2E NORMALIZED-000200"
NORMALIZED_PART_NUMBER_NORMALIZED = "E2ENORMALIZED000200"
OVERRIDE_PART_NAME = "E2E OVERRIDE PART 0200"
OVERRIDE_PART_NUMBER = "E2E OVERRIDE-000200"
OVERRIDE_PART_NUMBER_NORMALIZED = "E2EOVERRIDE000200"
UPDATED_SOURCE_CODE = "C0200-UPDATED"
VIN = "ZZZTEST00X0000003"
ACTOR = "station-admin-e2e"
ADMIN_PASSWORD = "station-admin-e2e-password"


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
            username=ACTOR,
            password=ADMIN_PASSWORD,
            require_auth=True,
            default_actor=ACTOR,
            page_size=30,
        )


@pytest.fixture
def e2e_database() -> Iterator[E2EDatabase]:
    host = os.environ["PARTSOUQ_DB_HOST"]
    if host not in LOCAL_DATABASE_HOSTS:
        raise ValueError("station admin E2E database host must be local loopback")
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


def test_e2e_database_rejects_remote_host_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PARTSOUQ_DB_HOST", "mysql.example.test")
    connect = mock.MagicMock()
    monkeypatch.setattr(pymysql, "connect", connect)
    fixture = e2e_database.__wrapped__()

    with pytest.raises(ValueError, match="must be local loopback"):
        next(fixture)

    connect.assert_not_called()


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
    )
    try:
        with connection.cursor() as cursor:
            for schema_path in SCHEMA_PATHS:
                for statement in _mysql_statements(schema_path.read_text(encoding="utf-8")):
                    cursor.execute(statement)
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
                "production_from, production_to, engine, grade, ssd, vid, url, fetched_at"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())",
                (
                    model_id,
                    hashlib.sha256(b"station-admin-e2e-vehicle").hexdigest(),
                    "E2E VEHICLE",
                    "E2E-1000",
                    "01.2020 - 12.2025",
                    "2020-01",
                    "2025-12",
                    "E2E ENGINE",
                    "E2E TRIM",
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
            cursor.execute(
                "INSERT INTO crawl_runs(run_key, started_at, finished_at, status, parts_ok) "
                "VALUES (%s, UTC_TIMESTAMP(), UTC_TIMESTAMP(), 'sample', 1000)",
                ("e2e-sample",),
            )
            run_id = cursor.lastrowid
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
                    run_id,
                )
                for index in range(1, 1001)
            ]
            cursor.executemany(
                "INSERT INTO parts("
                "group_id, part_number, name, code, note, quantity, range_str, "
                "part_from, part_to, url, seen_run_id"
                ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
            cursor.execute(
                "INSERT INTO scheduled_job_runs("
                "job_name, trigger_mode, status, started_at, finished_at, exit_code"
                ") VALUES ('catalog', 'daemon', 'completed', UTC_TIMESTAMP(6), "
                "UTC_TIMESTAMP(6), 0)"
            )
            scheduled_job_run_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO crawl_runs("
                "run_key, started_at, finished_at, status, dataset_kind, target_parts, "
                "scheduled_job_run_id, parts_ok, error_msg"
                ") VALUES ('e2e-formal-full', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), "
                "'success', 'full', NULL, %s, 1, NULL)",
                (scheduled_job_run_id,),
            )
            formal_crawl_run_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO published_parts("
                "part_id, crawl_run_id, vehicle_id, model_id, vehicle_vid, brand, model, vehicle_name, "
                "vehicle_code, prod_period, production_from, production_to, engine, trim_name, "
                "part_name, part_number, part_number_normalized, category_id, category_cid, "
                "category_main, "
                "category_group, group_id, group_code, group_uid, part_range, part_from, "
                "part_to, source_url, note, quantity, code, snapshot_at"
                ") SELECT p.id, %s, v.id, m.id, v.vid, b.name, m.name, v.name, v.model_code, "
                "v.prod_period, v.production_from, v.production_to, v.engine, v.grade, "
                "p.name, p.part_number, "
                "UPPER(REGEXP_REPLACE(p.part_number, '[[:space:]-]+', '')), "
                "c.id, c.cid, c.name, g.name, g.id, g.code, g.uid, "
                "p.range_str, p.part_from, p.part_to, p.url, p.note, p.quantity, p.code, "
                "UTC_TIMESTAMP() FROM parts AS p "
                "JOIN groups_t AS g ON g.id = p.group_id "
                "JOIN categories AS c ON c.id = g.category_id "
                "JOIN vehicles AS v ON v.id = c.vehicle_id "
                "JOIN models AS m ON m.id = v.model_id "
                "JOIN brands AS b ON b.id = m.brand_id WHERE p.id = %s",
                (formal_crawl_run_id, TARGET_PART_ID),
            )
            cursor.execute(
                "UPDATE parts SET part_number = %s, name = %s WHERE id = %s",
                (NORMALIZED_PART_NUMBER, NORMALIZED_PART_NAME, TARGET_PART_ID),
            )
            cursor.execute(
                "INSERT INTO nhtsa_source_artifacts("
                "dataset_name, source_key, source_url, http_status, response_headers_json, "
                "sha256, stored_path, byte_count, parser_name, parser_version, status, "
                "downloaded_at, verified_at, imported_at, source_rows, new_versions"
                ") VALUES ('vpic_vin_decodes', 'e2e-vin', %s, 200, '{}', %s, %s, 2, "
                "'e2e', '1', 'imported', UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), 1, 1)",
                ("https://vpic.example/e2e", "a" * 64, "e2e-vin.json"),
            )
            artifact_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO nhtsa_vin_decodes("
                "vin, make_name, model_name, model_year, engine_configuration, engine_model, "
                "displacement_l, trim_name, error_code, payload_json, source_url, "
                "source_artifact_id, decoded_at"
                ") VALUES (%s, 'E2E MOTORS', 'E2E MODEL', 2022, 'INLINE', 'E2E ENGINE', "
                "2.0, 'E2E TRIM', '0', '{}', %s, %s, UTC_TIMESTAMP())",
                (VIN, "https://vpic.example/e2e", artifact_id),
            )
            cursor.execute(
                "INSERT INTO admin_vehicle_mappings("
                "vin_prefix, vin, partsouq_vehicle_id, make_name, model_name, model_year, "
                "engine, trim_name, source_name"
                ") VALUES (%s, %s, %s, 'E2E MOTORS', 'E2E MODEL', 2022, "
                "'INLINE / E2E ENGINE', 'E2E TRIM', 'e2e')",
                (VIN[:11], VIN, vehicle_id),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def test_station_admin_e2e_fixture_requires_auth_and_formal_provenance(
    e2e_database: E2EDatabase,
) -> None:
    config = e2e_database.admin_config()
    assert config.require_auth is True
    assert config.auth_required is True

    connection = e2e_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT dataset_scope, source_crawl_run_id "
                "FROM v_current_catalog_parts WHERE part_id = %s",
                (TARGET_PART_ID,),
            )
            published = cursor.fetchone()
        assert published is not None
        assert published["dataset_scope"] == "full"
        assert int(published["source_crawl_run_id"]) > 0
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


@contextmanager
def _running_admin_server(database: E2EDatabase) -> Iterator[str]:
    import uvicorn

    config = uvicorn.Config(
        data_admin_app.app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    started = False
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("uvicorn server thread exited before startup completed")
            if time.monotonic() > deadline:
                raise TimeoutError("uvicorn server failed to start within 30 seconds")
            time.sleep(0.02)
        started = True
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if not started and thread.is_alive():
            raise RuntimeError("uvicorn server still running after failed startup")


def _launch_chromium(playwright: Playwright) -> Browser:
    _assert_browser_launch_environment()
    channel = os.getenv("STATION_ADMIN_E2E_BROWSER_CHANNEL", "").strip()
    browser_environment = browser_process_environment()
    try:
        if channel:
            return playwright.chromium.launch(
                headless=True,
                channel=channel,
                env=browser_environment,
            )
        return playwright.chromium.launch(headless=True, env=browser_environment)
    except Error as error:
        pytest.fail(
            "STATION_ADMIN_E2E=1 requires a launchable Chromium browser; "
            "install it with `playwright install chromium` or set "
            "STATION_ADMIN_E2E_BROWSER_CHANNEL",
            pytrace=False,
        )
        raise AssertionError from error


def _assert_browser_launch_environment() -> None:
    if sys.platform == "darwin" and os.getenv("CODEX_SANDBOX"):
        raise RuntimeError(
            "refusing to launch macOS Chromium inside the Codex sandbox; "
            "run this E2E command from a normal Aqua terminal or an explicitly "
            "approved unsandboxed runner"
        )


@contextmanager
def _playwright_runtime() -> Iterator[Playwright]:
    # async/sync Playwright 會先啟動 node driver；preflight 必須在進入
    # sync_playwright context 前執行，否則 Chromium launch guard 已太晚。
    _assert_browser_launch_environment()
    manager = sync_playwright()
    try:
        with browser_driver_environment():
            playwright = manager.start()
    except BaseException as error:
        try:
            manager.__exit__(*sys.exc_info())
        except BaseException as cleanup_error:
            error.add_note(
                "Playwright cleanup failed after driver startup error: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        raise
    try:
        yield playwright
    finally:
        playwright.stop()


def test_browser_launch_preflight_rejects_macos_codex_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeChromium:
        def __init__(self) -> None:
            self.launch_called = False

        def launch(self, **_kwargs: object) -> None:
            self.launch_called = True

    class FakePlaywright:
        def __init__(self) -> None:
            self.chromium = FakeChromium()

    playwright = FakePlaywright()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")

    with pytest.raises(RuntimeError, match="refusing to launch macOS Chromium"):
        _launch_chromium(cast(Playwright, playwright))
    assert playwright.chromium.launch_called is False


def test_playwright_node_is_not_started_inside_macos_codex_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_start = mock.Mock()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.setattr(sys.modules[__name__], "sync_playwright", sync_start)

    with (
        pytest.raises(RuntimeError, match="refusing to launch macOS Chromium"),
        _playwright_runtime(),
    ):
        pytest.fail("Playwright context must not be entered")
    sync_start.assert_not_called()


def test_playwright_node_starts_without_application_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_environment: dict[str, str] = {}
    stopped = False

    class FakePlaywright:
        def stop(self) -> None:
            nonlocal stopped
            stopped = True

    class FakeManager:
        def start(self) -> FakePlaywright:
            node_environment.update(cast(dict[str, str], playwright_transport.get_driver_env()))
            return FakePlaywright()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "database-secret")
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "admin-secret")
    monkeypatch.setattr(sys.modules[__name__], "sync_playwright", FakeManager)

    with _playwright_runtime():
        assert os.environ["PARTSOUQ_DB_PASSWORD"] == "database-secret"
        assert os.environ["PARTSOUQ_ADMIN_TOKEN"] == "admin-secret"

    assert "PARTSOUQ_DB_PASSWORD" not in node_environment
    assert "PARTSOUQ_ADMIN_TOKEN" not in node_environment
    assert stopped is True


def test_playwright_manager_stops_when_driver_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = False

    class FakeManager:
        def start(self) -> None:
            raise RuntimeError("driver start failed")

        def __exit__(self, *_args: object) -> None:
            nonlocal stopped
            stopped = True

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys.modules[__name__], "sync_playwright", FakeManager)

    with pytest.raises(RuntimeError, match="driver start failed"), _playwright_runtime():
        pytest.fail("Playwright runtime must not yield after a start failure")

    assert stopped is True


def test_browser_launch_does_not_receive_application_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_options: dict[str, object] = {}

    class FakeChromium:
        def launch(self, **kwargs: object) -> object:
            launch_options.update(kwargs)
            return object()

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("STATION_ADMIN_E2E_BROWSER_CHANNEL", raising=False)
    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "database-secret")
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "admin-secret")

    _launch_chromium(cast(Playwright, FakePlaywright()))

    browser_environment = launch_options["env"]
    assert isinstance(browser_environment, dict)
    assert "PARTSOUQ_DB_PASSWORD" not in browser_environment
    assert "PARTSOUQ_ADMIN_TOKEN" not in browser_environment


def test_admin_quarantine_loads_without_token_then_refreshes_with_token(
    e2e_database: E2EDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_config = {
        "host": e2e_database.host,
        "port": e2e_database.port,
        "user": e2e_database.user,
        "password": e2e_database.password,
        "database": e2e_database.database,
    }
    for key, value in database_config.items():
        monkeypatch.setitem(data_admin_app.DB_CONFIG, key, value)
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "e2e-admin-token")
    connection = e2e_database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key"
                ") SELECT id, 'E2E-Q0001', 'E2E-RANGE', 'nameless', 'e2e-run' "
                "FROM groups_t WHERE code = 'E2E1' LIMIT 1"
            )
            cursor.execute(
                "INSERT INTO part_quarantine("
                "group_id, part_number, range_str, reason, run_key, "
                "resolved_at, resolution"
                ") SELECT id, 'E2E-Q0002', 'E2E-RANGE', 'nameless', 'e2e-run', "
                "UTC_TIMESTAMP(), 'resolved in e2e' "
                "FROM groups_t WHERE code = 'E2E1' LIMIT 1"
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    with (
        _running_admin_server(e2e_database) as admin_url,
        _playwright_runtime() as playwright,
    ):
        browser = _launch_chromium(playwright)
        try:
            page = browser.new_page()
            console_errors: list[str] = []
            page.on(
                "pageerror",
                lambda error: console_errors.append(str(error)),
            )
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )

            page.goto(admin_url)
            expect(page.locator("#quarantine-table-body")).to_contain_text("無紀錄")
            assert not any("is not iterable" in text for text in console_errors)
            expect(page.locator("#quarantine-page-number")).to_have_value("1")
            expect(page.locator("#quarantine-total-pages")).to_have_text("共 0 頁")

            page.locator("#token").fill("e2e-admin-token")
            page.locator("#refresh").click()
            expect(page.locator("#quarantine-table-body")).to_contain_text("E2E-Q0001")
            assert not any("422" in text for text in console_errors)
            expect(page.locator("#quarantine-page-number")).to_have_value("1")
            expect(page.locator("#quarantine-total-pages")).to_have_text("共 1 頁")
            expect(page.locator("#quarantine-range-label")).to_have_text("顯示 1 到 1，共 1 筆")
            page.locator("#quarantine-page-size").select_option("30")
            page.locator("#quarantine-run-key").fill("missing-run")
            page.locator("#quarantine-refresh").click()
            expect(page.locator("#quarantine-table-body")).to_contain_text("無紀錄")

            page.locator("#quarantine-run-key").fill("e2e-run")
            page.locator("#quarantine-refresh").click()
            resolve_button = page.locator("#quarantine-table-body").get_by_role(
                "button", name="標記處置"
            )
            expect(resolve_button).to_be_visible()
            page.once("dialog", lambda dialog: dialog.accept("verified in browser e2e"))
            resolve_button.click()
            expect(page.locator("#quarantine-table-body")).to_contain_text("無紀錄")

            connection = e2e_database.connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT resolved_at, resolution FROM part_quarantine "
                        "WHERE part_number = 'E2E-Q0001'"
                    )
                    resolved = cursor.fetchone()
            finally:
                connection.close()
            assert resolved is not None
            assert resolved["resolved_at"] is not None
            assert resolved["resolution"] == "verified in browser e2e"

            page.locator("#quarantine-state").select_option("all")
            page.locator("#quarantine-refresh").click()
            expect(page.locator("#quarantine-table-body")).to_contain_text("E2E-Q0002")
            expect(page.locator("#quarantine-total-pages")).to_have_text("共 1 頁")
            newly_resolved_row = page.locator("#quarantine-table-body tr").filter(
                has_text="E2E-Q0001"
            )
            resolved_row = page.locator("#quarantine-table-body tr").filter(has_text="E2E-Q0002")
            expect(newly_resolved_row.locator("td")).to_have_count(8)
            expect(resolved_row.locator("td")).to_have_count(8)
            expect(newly_resolved_row).to_contain_text("verified in browser e2e")

            page.locator("#token").fill("")
            page.locator("#quarantine-refresh").click()
            expect(page.locator("#quarantine-table-body")).to_contain_text("無紀錄")
            expect(page.locator("#quarantine-page-size")).to_have_value("50")
            expect(page.locator("#quarantine-total-pages")).to_have_text("共 0 頁")
            expect(page.locator("#quarantine-range-label")).to_have_text("顯示 0 到 0，共 0 筆")
        finally:
            browser.close()


def _read_database_evidence(database: E2EDatabase) -> dict[str, object]:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT part_number, name, code FROM parts WHERE id = %s", (TARGET_PART_ID,)
            )
            source = cursor.fetchone()
            cursor.execute(
                "SELECT part_number, part_name FROM published_parts WHERE part_id = %s",
                (TARGET_PART_ID,),
            )
            published = cursor.fetchone()
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
        return {"source": source, "published": published, "head": head, "events": events}
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


def _update_source_code(database: E2EDatabase) -> None:
    connection = database.connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE parts SET code = %s WHERE id = %s",
                (UPDATED_SOURCE_CODE, TARGET_PART_ID),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def test_station_admin_part_lifecycle_through_real_browser_and_mysql(
    e2e_database: E2EDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_config = {
        "host": e2e_database.host,
        "port": e2e_database.port,
        "user": e2e_database.user,
        "password": e2e_database.password,
        "database": e2e_database.database,
    }
    for key, value in database_config.items():
        monkeypatch.setitem(data_admin_app.DB_CONFIG, key, value)
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "e2e-admin-token")
    headers = {"X-Admin-Token": "e2e-admin-token"}
    try:
        with (
            _running_server(e2e_database) as base_url,
            TestClient(data_admin_app.app) as data_client,
            _playwright_runtime() as playwright,
        ):
            browser = _launch_chromium(playwright)
            try:
                page = browser.new_page()
                page.goto(f"{base_url}/entities/part_numbers")
                expect(page).to_have_url(re.compile(r"/login\?next="))
                expect(page.get_by_role("heading", name="站方後台登入")).to_be_visible()

                page.get_by_label("帳號").fill(ACTOR)
                page.get_by_label("密碼").fill("wrong-password")
                page.get_by_role("button", name="登入").click()
                expect(page.get_by_text("帳號或密碼錯誤。", exact=True)).to_be_visible()

                page.get_by_label("帳號").fill(ACTOR)
                page.get_by_label("密碼").fill(ADMIN_PASSWORD)
                page.get_by_role("button", name="登入").click()
                expect(page).to_have_url(re.compile(r"/entities/part_numbers$"))

                page.goto(base_url)
                current_card = page.locator(".summary-grid > div").filter(has_text="目前正式資料列")
                normalized_history_card = page.locator(".summary-grid > div").filter(
                    has_text="normalized 歷史總列數"
                )
                expect(current_card).to_contain_text("1")
                expect(normalized_history_card).to_contain_text("1000")

                before_override = _read_pre_override_api_evidence(data_client, headers)
                _assert_snapshot_boundary(before_override)

                page.goto(f"{base_url}/entities/part_numbers")
                page.get_by_label("資料來源").select_option("historical_sample")
                page.get_by_role("button", name="查詢").click()
                expect(page.get_by_text("顯示 1 到 30，共 1000 筆記錄", exact=True)).to_be_visible()

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

                page.goto(
                    f"{base_url}/entities/part_numbers?"
                    f"q={NORMALIZED_PART_NUMBER_NORMALIZED}&dataset=historical_sample&pageSize=200"
                )
                expect(page.get_by_text("顯示 1 到 1，共 1 筆記錄", exact=True)).to_be_visible()
                page.get_by_role("link", name=f"source:{TARGET_PART_ID}").click()
                expect(page.get_by_role("heading", name=re.compile(r"source:200"))).to_be_visible()
                expect(page.get_by_text(NORMALIZED_PART_NAME, exact=True)).to_be_visible()

                page.get_by_role("link", name="編輯覆寫").click()
                page.locator('input[name="field__number_raw"]').fill(OVERRIDE_PART_NUMBER)
                page.locator('input[name="field__name_en_raw"]').fill(OVERRIDE_PART_NAME)
                page.locator('select[name="field__is_assembly_inferred"]').select_option("1")
                expect(page.get_by_label("操作者")).to_have_value(ACTOR)
                expect(page.get_by_label("操作者")).to_have_attribute("readonly", "")
                page.get_by_label("修改原因").fill("E2E update")
                page.get_by_role("button", name="儲存新版本").click()
                expect(
                    page.get_by_text("已新增一筆覆寫版本；來源型錄資料未被修改。", exact=True)
                ).to_be_visible()
                expect(page.get_by_text(OVERRIDE_PART_NAME, exact=True)).to_be_visible()
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 1")

                effective = _read_effective_api_evidence(data_client, headers)
                _assert_effective_part_visible(effective)

                page.get_by_role("link", name="編輯覆寫").click()
                page.locator('select[name="field__is_assembly_inferred"]').select_option("")
                page.get_by_label("修改原因").fill("E2E clear boolean override")
                page.get_by_role("button", name="儲存新版本").click()
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 2")

                page.get_by_role("link", name="編輯覆寫").click()
                _update_source_code(e2e_database)
                page.get_by_label("修改原因").fill("E2E stale source")
                page.get_by_role("button", name="儲存新版本").click()
                expect(
                    page.get_by_text(
                        "爬蟲來源資料已更新；請重新載入後再套用人工修改",
                        exact=True,
                    )
                ).to_be_visible()

                page.goto(f"{base_url}/entities/part_numbers/source:{TARGET_PART_ID}")
                page.get_by_role("link", name="編輯覆寫").click()
                page.get_by_label("修改原因").fill("E2E rebase after source refresh")
                page.get_by_role("button", name="儲存新版本").click()
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 3")

                page.get_by_label("原因").fill("E2E retire")
                page.get_by_role("button", name="停用").click()
                expect(page.locator(".page-title p")).to_contain_text("狀態 retired")
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 4")
                retired = _read_effective_api_evidence(data_client, headers)
                assert retired == {"published": [], "sample": [], "fitments": [], "vin": []}

                page.get_by_label("原因").fill("E2E restore")
                page.get_by_role("button", name="恢復").click()
                expect(page.locator(".page-title p")).to_contain_text("狀態 active")
                expect(page.locator(".page-title p")).to_contain_text("覆寫版本 5")
                restored = _read_effective_api_evidence(data_client, headers)
                _assert_effective_part_visible(restored)

                rejected_logout = page.request.post(
                    f"{base_url}/logout",
                    form={},
                    fail_on_status_code=False,
                )
                assert rejected_logout.status == 400
                page.get_by_role("button", name="登出").click()
                expect(page.get_by_role("heading", name="站方後台登入")).to_be_visible()
                page.goto(f"{base_url}/entities/part_numbers")
                expect(page).to_have_url(re.compile(r"/login\?next="))
            finally:
                browser.close()

        evidence = _read_database_evidence(e2e_database)
        assert evidence["source"] == {
            "part_number": NORMALIZED_PART_NUMBER,
            "name": NORMALIZED_PART_NAME,
            "code": UPDATED_SOURCE_CODE,
        }
        assert evidence["published"] == {
            "part_number": PUBLISHED_PART_NUMBER,
            "part_name": PUBLISHED_PART_NAME,
        }
        head = evidence["head"]
        assert isinstance(head, dict)
        assert head["status"] == "active"
        assert head["revision"] == 5
        assert head["actor"] == ACTOR
        assert head["reason"] == "E2E restore"
        assert len(head["base_sha256"]) == 64
        payload = json.loads(head["payload_json"])
        assert payload == {
            "name_en_raw": OVERRIDE_PART_NAME,
            "number_normalized": OVERRIDE_PART_NUMBER_NORMALIZED,
            "number_raw": OVERRIDE_PART_NUMBER,
        }

        events = evidence["events"]
        assert isinstance(events, list)
        assert [(event["action"], event["revision"]) for event in events] == [
            ("update", 1),
            ("update", 2),
            ("update", 3),
            ("retire", 4),
            ("restore", 5),
        ]
        assert [event["actor"] for event in events] == [ACTOR] * 5
        assert [event["reason"] for event in events] == [
            "E2E update",
            "E2E clear boolean override",
            "E2E rebase after source refresh",
            "E2E retire",
            "E2E restore",
        ]
        assert json.loads(events[0]["before_json"])["name_en_raw"] == NORMALIZED_PART_NAME
        assert json.loads(events[0]["after_json"])["name_en_raw"] == OVERRIDE_PART_NAME
        assert json.loads(events[0]["after_json"])["is_assembly_inferred"] is True
        assert json.loads(events[1]["after_json"])["is_assembly_inferred"] == 0
    finally:
        _cleanup_override(e2e_database)


def _read_effective_api_evidence(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, list[dict[str, object]]]:
    published = client.get(
        "/api/parts",
        params={"part_number": OVERRIDE_PART_NUMBER_NORMALIZED, "pageSize": 200},
    )
    assert published.status_code == 200
    sample = client.get("/api/sample-parts", params={"pageSize": 200})
    assert sample.status_code == 200
    fitments = client.get(f"/api/parts/{OVERRIDE_PART_NUMBER_NORMALIZED}/fitments")
    assert fitments.status_code == 200
    vin = client.get(f"/api/vins/{VIN}/parts", headers=headers)
    assert vin.status_code == 200
    return {
        "published": published.json()["items"],
        "sample": [row for row in sample.json()["items"] if row["part_id"] == TARGET_PART_ID],
        "fitments": fitments.json()["catalog"],
        "vin": vin.json(),
    }


def _read_pre_override_api_evidence(
    client: TestClient,
    headers: dict[str, str],
) -> dict[str, list[dict[str, object]]]:
    published = client.get(
        "/api/parts",
        params={"part_number": PUBLISHED_PART_NUMBER, "pageSize": 200},
    )
    assert published.status_code == 200
    unpublished_search = client.get(
        "/api/parts",
        params={"part_number": NORMALIZED_PART_NUMBER_NORMALIZED, "pageSize": 200},
    )
    assert unpublished_search.status_code == 200
    sample = client.get("/api/sample-parts", params={"pageSize": 200})
    assert sample.status_code == 200
    fitments = client.get(f"/api/parts/{PUBLISHED_PART_NUMBER}/fitments")
    assert fitments.status_code == 200
    vin = client.get(f"/api/vins/{VIN}/parts", headers=headers)
    assert vin.status_code == 200
    return {
        "published": published.json()["items"],
        "unpublished_search": unpublished_search.json()["items"],
        "sample": [row for row in sample.json()["items"] if row["part_id"] == TARGET_PART_ID],
        "fitments": fitments.json()["catalog"],
        "vin": vin.json(),
    }


def _assert_snapshot_boundary(evidence: dict[str, list[dict[str, object]]]) -> None:
    assert evidence["unpublished_search"] == []
    for key in ("published", "fitments", "vin"):
        rows = evidence[key]
        assert len(rows) == 1
        assert rows[0]["part_number"] == PUBLISHED_PART_NUMBER
        assert rows[0]["part_name"] == PUBLISHED_PART_NAME
        assert rows[0]["station_override_revision"] == 0
    sample = evidence["sample"]
    assert len(sample) == 1
    assert sample[0]["part_number"] == NORMALIZED_PART_NUMBER
    assert sample[0]["part_name"] == NORMALIZED_PART_NAME
    assert sample[0]["station_override_revision"] == 0


def _assert_effective_part_visible(
    evidence: dict[str, list[dict[str, object]]],
) -> None:
    for rows in evidence.values():
        assert len(rows) == 1
        assert rows[0]["part_number"] == OVERRIDE_PART_NUMBER
        assert rows[0]["part_name"] == OVERRIDE_PART_NAME
        assert rows[0]["station_override_revision"] in {1, 5}
