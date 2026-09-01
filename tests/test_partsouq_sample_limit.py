import errno
import fcntl
import os
import signal
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

from partsouq_catalog import run_crawl
from partsouq_catalog.admission import CatalogRuntimeLease, CatalogRuntimeLockBusy
from partsouq_catalog.config import CRAWL, DB_CONFIG
from partsouq_catalog.crawler import Crawler, SampleLimitReached
from partsouq_catalog.db import Database
from partsouq_catalog.repositories import (
    BrandRepository,
    CrawlRepository,
    PartRepository,
    VehicleRepository,
)


@pytest.fixture(autouse=True)
def _allow_catalog_runtime_lock(monkeypatch):
    lease = mock.MagicMock(spec=CatalogRuntimeLease)
    monkeypatch.setattr(
        run_crawl,
        "acquire_catalog_runtime_lock",
        mock.MagicMock(return_value=lease),
    )


def _parts(count: int) -> list[dict]:
    return [
        {
            "part_number": f"P{index:05d}",
            "name": f"Part {index}",
            "code": "11000",
            "note": None,
            "quantity": "01",
            "range_str": "",
            "part_from": None,
            "part_to": None,
        }
        for index in range(count)
    ]


def _parts_html(count: int) -> str:
    # 真實 unit 頁會渲染所屬 uid（身分斷言依據）；fixture 同步含
    # uid=10001（與 _group() 一致），模擬 genuine 頁面。
    rows = "".join(
        "<tr>"
        f'<td><a href="/en/search/all?q=P{index:05d}">P{index:05d}</a></td>'
        f"<td>Part {index}</td><td>11000</td><td></td><td>01</td><td></td>"
        "</tr>"
        for index in range(count)
    )
    return f'<input type="hidden" name="uid" value="10001"><table><tbody>{rows}</tbody></table>'


@pytest.fixture
def sample_crawler(monkeypatch):
    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=4)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.parts.upsert_parts.side_effect = lambda _group, rows, _run, **_kwargs: len(rows)
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "2026-08-sample-test"
    instance.crawl.previous_row_count.return_value = 0
    instance._get = mock.MagicMock(return_value="<html>unit</html>")
    yield instance
    instance.close()


def _group() -> dict:
    return {
        "category_name": "ENGINE/FUEL/TOOL",
        "cid": "1",
        "group_code": "1101",
        "group_name": "PARTIAL ENGINE ASSEMBLY",
        "uid": "10001",
        "url": "/en/catalog/genuine/unit?uid=10001",
    }


def test_exactly_1000_rows_complete_group(sample_crawler):
    sample_crawler._get.return_value = _parts_html(1000)
    fetched = {}

    truncated = sample_crawler.crawl_group("TOYOTA", 7, _group(), fetched=fetched)

    assert sample_crawler.workers == 1
    assert truncated is False
    assert sample_crawler.counts["parts"] == 1000
    assert sample_crawler._sample_limit_reached.is_set()
    written = sample_crawler.parts.upsert_parts.call_args
    assert len(written.args[1]) == 1000
    assert written.kwargs == {"complete_group": True}
    sample_crawler.crawl.mark_group_fetched.assert_called_once_with(
        41,
        "2026-08-sample-test",
        status="done",
        row_count=1000,
    )
    assert fetched == {("1", "1101", "10001"): 1000}


def test_1001_rows_are_capped_without_completing_group(sample_crawler):
    sample_crawler._get.return_value = _parts_html(1001)
    fetched = {}

    truncated = sample_crawler.crawl_group("TOYOTA", 7, _group(), fetched=fetched)

    assert truncated is True
    assert sample_crawler.counts["parts"] == 1000
    written = sample_crawler.parts.upsert_parts.call_args
    assert len(written.args[1]) == 1000
    assert written.kwargs == {"complete_group": False}
    sample_crawler.crawl.mark_group_fetched.assert_not_called()
    assert fetched == {}


def test_partial_group_preserves_existing_membership():
    database = mock.MagicMock()
    database._execute.return_value.fetchall.return_value = [
        {"part_number": "P00000", "range_str": ""},
        {"part_number": "OLD", "range_str": ""},
    ]
    repository = PartRepository(database)
    repository._clear_stale_group_membership = mock.MagicMock()

    repository.upsert_parts(41, _parts(1), run_id=17, complete_group=False)

    repository._clear_stale_group_membership.assert_not_called()
    written_rows = database._executemany.call_args.args[1]
    assert written_rows[0][-1] == 17


def test_sample_run_is_not_published_or_marked_success(monkeypatch):
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "min_brands", 1)
    for key in ("start_brand", "limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, "" if key == "start_brand" else 0)

    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=4, fresh=True)
    instance.brands = mock.MagicMock()
    instance.vehicles = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.purge_legacy_vehicle_state.return_value = 0
    instance.crawl.remaining_group_count.return_value = 0
    # 預期停止會留下 pending，但不能被誤判成真正失敗。
    instance.crawl.count_errors.return_value = 2
    instance.crawl.count_failures.return_value = 0
    instance._brands = mock.MagicMock(return_value=[{"name": "TOYOTA"}])

    def stop_at_sample(_brand):
        instance.counts["parts"] = 1000
        instance._sample_limit_reached.set()
        raise SampleLimitReached

    instance.crawl_brand = mock.MagicMock(side_effect=stop_at_sample)
    try:
        instance.run()
    finally:
        instance.close()

    assert instance.last_status == "sample"
    assert instance.crawl.start_run.call_args.args[0].startswith("sample-")
    assert instance.crawl.finish_run.call_args.args[1] == "sample"
    instance.crawl.count_failures.assert_called_once()
    instance.crawl.count_errors.assert_not_called()
    instance.crawl.archive_full_candidate_parts.assert_not_called()


def test_partial_run_uses_independent_running_marker(monkeypatch):
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    monkeypatch.setitem(CRAWL, "start_brand", "TOYOTA")
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    for key in ("limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, 0)

    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.purge_legacy_vehicle_state.return_value = 0
    instance.crawl.remaining_group_count.return_value = 0
    instance.crawl.count_errors.return_value = 0
    instance._brands = mock.MagicMock(return_value=[{"name": "TOYOTA"}])
    instance.crawl_brand = mock.MagicMock(return_value=0)
    try:
        instance.run()
    finally:
        instance.close()

    run_key = instance.crawl.start_run.call_args.args[0]
    assert run_key.startswith("partial-")
    assert len(run_key) <= 32
    assert run_key != datetime.now().strftime("%Y-%m")
    instance.crawl.finish_run.assert_called_once_with(
        17,
        "error",
        instance.counts,
        "partial run (start/limit set: TOYOTA)",
    )


def test_crawler_rolls_back_initial_marker_before_releasing_admission(monkeypatch):
    events: list[str] = []

    @contextmanager
    def admission(_connection):
        events.append("acquired")
        try:
            yield
        finally:
            events.append("released")

    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", admission)
    monkeypatch.setitem(CRAWL, "start_brand", "")
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    for key in ("limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, 0)

    database = mock.MagicMock()
    database.rollback.side_effect = lambda: events.append("rollback")
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.side_effect = RuntimeError("marker failed")
    try:
        with pytest.raises(RuntimeError, match="marker failed"):
            instance.run()
    finally:
        instance.close()

    assert events == ["acquired", "rollback", "released"]


def test_crawler_closes_durable_marker_when_admission_release_fails(monkeypatch):
    events: list[str] = []

    @contextmanager
    def admission(_connection):
        yield
        events.append("release")
        raise RuntimeError("admission release failed")

    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", admission)
    monkeypatch.setitem(CRAWL, "start_brand", "")
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    for key in ("limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, 0)

    database = mock.MagicMock()
    database.commit.side_effect = lambda: events.append("commit")
    database.rollback.side_effect = lambda: events.append("rollback")
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.run_status.return_value = "running"
    instance.crawl.finish_run.side_effect = lambda *_args: events.append("finish-error")
    instance._brands = mock.MagicMock()
    try:
        with pytest.raises(RuntimeError, match="admission release failed"):
            instance.run()
    finally:
        instance.close()

    assert events == ["commit", "release", "rollback", "finish-error", "commit"]
    instance.crawl.finish_run.assert_called_once_with(
        17,
        "error",
        instance.counts,
        "admission release failed",
    )
    instance._brands.assert_not_called()


def test_crawler_keeps_existing_success_when_admission_release_fails(monkeypatch):
    @contextmanager
    def admission(_connection):
        yield
        raise RuntimeError("admission release failed")

    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", admission)
    monkeypatch.setitem(CRAWL, "start_brand", "")
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    for key in ("limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, 0)

    database = mock.MagicMock()
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.run_status.return_value = "success"
    instance._brands = mock.MagicMock()
    try:
        with pytest.raises(RuntimeError, match="admission release failed"):
            instance.run()
    finally:
        instance.close()

    instance.crawl.finish_run.assert_not_called()
    instance._brands.assert_not_called()


def test_brand_request_refreshes_cookie_after_running_marker_commit(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    monkeypatch.setitem(CRAWL, "start_brand", "")
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    monkeypatch.setitem(CRAWL, "min_brands", 1)
    for key in ("limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, 0)

    http = mock.MagicMock()
    http.ensure_fresh.side_effect = lambda: events.append("ensure_fresh")
    http.get_response.side_effect = lambda _url: (
        events.append("request")
        or mock.MagicMock(text='<li><a href="/en/catalog/genuine/locate?c=TOYOTA">TOYOTA</a></li>')
    )
    database = mock.MagicMock()
    database.commit.side_effect = lambda: events.append("commit")
    instance = Crawler(http, database, workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.run_status.return_value = "running"
    instance.crawl.purge_legacy_vehicle_state.return_value = 0
    instance.crawl.remaining_group_count.return_value = 0
    instance.crawl.count_errors.return_value = 1
    instance.crawl_brand = mock.MagicMock(return_value=0)
    try:
        instance.run()
    finally:
        instance.close()

    assert events.index("commit") < events.index("ensure_fresh") < events.index("request")
    http.ensure_fresh.assert_called_once_with()


def test_negative_limit_is_rejected(monkeypatch):
    monkeypatch.setitem(CRAWL, "limit_parts", -1)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)

    with pytest.raises(ValueError, match="PSQ_LIMIT_PARTS"):
        Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)


def test_min_brands_default_and_env_override(monkeypatch):
    # 2026-08-25 起站方公開品牌剩 17，預設地板下修為 15；環境變數仍可覆寫。
    read_value = "from partsouq_catalog.config import CRAWL; print(CRAWL['min_brands'])"

    monkeypatch.delenv("PSQ_MIN_BRANDS", raising=False)
    default = subprocess.run(
        [sys.executable, "-c", read_value], capture_output=True, text=True, check=True
    )
    assert default.stdout.strip() == "15"

    monkeypatch.setenv("PSQ_MIN_BRANDS", "17")
    overridden = subprocess.run(
        [sys.executable, "-c", read_value], capture_output=True, text=True, check=True
    )
    assert overridden.stdout.strip() == "17"


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL sample tests",
)
def test_mysql_partial_sample_readback(monkeypatch):
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    database = Database().connect()
    try:
        for table in (
            "bounded_parts",
            "published_parts",
            "admin_vehicle_mappings",
            "crawl_state",
            "crawl_runs",
            "brands",
        ):
            database._execute(f"DELETE FROM {table}")
        database.commit()

        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        old_crawl = CrawlRepository(database, "sample-membership-old")
        old_run_id = old_crawl.start_run("sample-membership-old", fresh=True)
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
        group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "PARTIAL ENGINE ASSEMBLY",
            "10001",
            "https://partsouq.com/en/catalog/genuine/unit?uid=10001",
        )
        parts.upsert_parts(
            group_id, [_parts(1)[0], {**_parts(1)[0], "part_number": "OLD"}], old_run_id
        )
        old_crawl.mark_group_fetched(
            group_id,
            "sample-membership-old",
            status="done",
            row_count=2,
        )
        database.commit()

        instance = Crawler(mock.MagicMock(), database, workers=4)
        sample_key = "sample-membership-new"
        sample_run_id = instance.crawl.start_run(sample_key, fresh=True)
        instance.crawl.run_key = sample_key
        instance.run_id = sample_run_id
        instance._get = mock.MagicMock(return_value=_parts_html(1001))
        try:
            assert instance.crawl_group("TOYOTA", vehicle_id, _group()) is True
        finally:
            instance.close()

        instance.crawl.seen("model", "TOYOTA::CAMRY")
        instance.crawl.finish_run(
            sample_run_id,
            "sample",
            instance.counts,
            "fixture sample; current snapshot not published",
        )
        database.commit()

        membership = database._execute(
            "SELECT part_number, seen_run_id FROM parts "
            "WHERE group_id = %s AND part_number IN ('P00000', 'OLD') ORDER BY part_number",
            (group_id,),
        ).fetchall()
        receipt = database._execute(
            "SELECT fetched_run_key, fetched_status, fetched_row_count FROM groups_t WHERE id = %s",
            (group_id,),
        ).fetchone()
        run = database._execute(
            "SELECT status, parts_ok FROM crawl_runs WHERE id = %s",
            (sample_run_id,),
        ).fetchone()
        published = database._execute("SELECT COUNT(*) AS n FROM published_parts").fetchone()

        assert instance.counts["parts"] == 1000
        assert {row["part_number"]: row["seen_run_id"] for row in membership} == {
            "OLD": old_run_id,
            "P00000": sample_run_id,
        }
        assert receipt == {
            "fetched_run_key": "sample-membership-old",
            "fetched_status": "done",
            "fetched_row_count": 2,
        }
        assert instance.crawl.count_errors(sample_key) == 1
        assert instance.crawl.count_failures(sample_key) == 0
        assert run == {"status": "sample", "parts_ok": 1000}
        assert published["n"] == 0
    finally:
        database.rollback()
        for table in (
            "bounded_parts",
            "published_parts",
            "admin_vehicle_mappings",
            "crawl_state",
            "crawl_runs",
            "brands",
        ):
            database._execute(f"DELETE FROM {table}")
        database.commit()
        database.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("success", 0),
        ("bounded_success", 0),
        ("sample", 3),
        ("bounded_under_target", 4),
        ("error", 1),
    ],
)
def test_cli_has_distinct_exit_codes(monkeypatch, tmp_path, status, expected):
    database = mock.MagicMock()
    crawler = mock.MagicMock(last_status=status)
    crawler.run.return_value = {}

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    load_cookies = mock.MagicMock(return_value={})
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    assert run_crawl.main() == expected
    load_cookies.assert_called_once_with()


def test_cli_rejects_full_scope_with_leftover_model_scope(monkeypatch, tmp_path) -> None:
    """正式 full 排程與 model scope 互斥；殘留 bounded 設定必須被拒絕。"""
    database = mock.MagicMock()
    load_cookies = mock.MagicMock(return_value={})
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 5830)
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_brand", "TOYOTA")
    monkeypatch.setitem(CRAWL, "bounded_model", "TACOMA")
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    assert run_crawl.main() == 64
    database.assert_not_called()
    load_cookies.assert_not_called()


def test_cli_accepts_full_scope_without_model_scope(monkeypatch, tmp_path) -> None:
    """正式 full 排程（0/0、無 model scope）通過啟動閘，進入資料庫初始化。"""
    database = mock.MagicMock()
    load_cookies = mock.MagicMock(return_value={})
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 5830)
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_brand", "")
    monkeypatch.setitem(CRAWL, "bounded_model", "")
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    try:
        run_crawl.main()
    except Exception:  # noqa: BLE001 - mock 之後的初始化本就會失敗
        pass
    database.assert_called()
    load_cookies.assert_called()


@pytest.mark.parametrize(
    ("brand", "model", "year_window"),
    (("", "TACOMA", 20), ("TOYOTA", "", 20), ("TOYOTA", "TACOMA", 0)),
)
def test_cli_rejects_incomplete_scheduled_model_scope_before_database_or_browser(
    brand: str,
    model: str,
    year_window: int,
    monkeypatch,
    tmp_path,
) -> None:
    database = mock.MagicMock()
    load_cookies = mock.MagicMock(return_value={})
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 5830)
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 10_000)
    monkeypatch.setitem(CRAWL, "bounded_brand", brand)
    monkeypatch.setitem(CRAWL, "bounded_model", model)
    monkeypatch.setitem(CRAWL, "vehicle_year_window", year_window)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    assert run_crawl.main() == 64
    database.assert_not_called()
    load_cookies.assert_not_called()


def test_cli_sigterm_stops_owned_browser_before_waiting_for_workers(monkeypatch, tmp_path) -> None:
    database = mock.MagicMock()
    database.connect.return_value = database
    crawler = mock.MagicMock()
    installed_handlers: dict[int, object] = {}
    events: list[str] = []

    def install_handler(signum: int, handler: object) -> None:
        installed_handlers[signum] = handler

    def run_until_sigterm() -> None:
        handler = installed_handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)

    crawler.run.side_effect = run_until_sigterm
    crawler.close.side_effect = lambda: events.append("crawler-close")
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    monkeypatch.setattr(run_crawl, "load_cookies", mock.MagicMock(return_value={}))
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr(
        run_crawl,
        "_begin_browser_shutdown",
        lambda: events.append("browser-stop"),
    )
    monkeypatch.setattr(run_crawl, "_finish_browser_shutdown", mock.MagicMock())
    monkeypatch.setattr(run_crawl.signal, "signal", install_handler)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    with pytest.raises(SystemExit) as error:
        run_crawl.main()

    assert error.value.code == 128 + signal.SIGTERM
    assert events[:2] == ["browser-stop", "crawler-close"]


def test_cli_runtime_lease_close_failure_still_releases_local_resources(
    monkeypatch, tmp_path
) -> None:
    database = mock.MagicMock()
    database.connect.return_value = database
    owner_connection = mock.MagicMock(open=True)
    database.open_owner_connection.return_value = owner_connection
    crawler = mock.MagicMock(last_status="success")
    crawler.run.return_value = {}
    lease = mock.MagicMock(spec=CatalogRuntimeLease)
    lease.close.side_effect = RuntimeError("heartbeat did not stop")
    run_crawl.acquire_catalog_runtime_lock.return_value = lease
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    monkeypatch.setattr(run_crawl, "load_cookies", mock.MagicMock(return_value={}))
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    with pytest.raises(RuntimeError, match="heartbeat did not stop"):
        run_crawl.main()

    owner_connection.close.assert_called_once_with()
    database.close.assert_called_once_with()
    with (tmp_path / "crawler.lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_cli_recover_only_runs_recover_and_exits_zero(monkeypatch, tmp_path):
    database = mock.MagicMock()
    crawler = mock.MagicMock(sample_mode=False, bounded_mode=False, part_limit=0)
    crawler.recover_null_groups.return_value = 3

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    load_cookies = mock.MagicMock(return_value={})
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--recover-only", "--workers", "1"])

    assert run_crawl.main() == 0
    crawler.recover_null_groups.assert_called_once_with()
    crawler.run.assert_not_called()


def test_cli_recover_only_exits_nonzero_on_partial_failure(monkeypatch, tmp_path):
    database = mock.MagicMock()
    crawler = mock.MagicMock(sample_mode=False, bounded_mode=False, part_limit=0)
    crawler.recover_null_groups.side_effect = RuntimeError("recover incomplete")

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    monkeypatch.setattr(run_crawl, "load_cookies", mock.MagicMock(return_value={}))
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--recover-only", "--workers", "1"])

    assert run_crawl.main() == 1
    crawler.close.assert_called_once_with()


def test_cli_recover_only_defers_on_schema_migration(monkeypatch, tmp_path):
    database = mock.MagicMock()
    crawler = mock.MagicMock(sample_mode=False, bounded_mode=False, part_limit=0)
    crawler.recover_null_groups.side_effect = run_crawl.AdmissionLockBusy("migration")

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    monkeypatch.setattr(run_crawl, "load_cookies", mock.MagicMock(return_value={}))
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--recover-only", "--workers", "1"])

    assert run_crawl.main() == 75


def test_cli_recover_only_rejects_bounded_configuration(monkeypatch, tmp_path):
    database = mock.MagicMock()
    crawler = mock.MagicMock(sample_mode=False, bounded_mode=True, part_limit=10_000)

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    monkeypatch.setattr(run_crawl, "load_cookies", mock.MagicMock(return_value={}))
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--recover-only", "--workers", "1"])

    assert run_crawl.main() == 64
    crawler.recover_null_groups.assert_not_called()


def test_cli_database_runtime_lock_blocks_second_checkout_before_browser(monkeypatch, tmp_path):
    database = mock.MagicMock()
    load_cookies = mock.MagicMock()
    run_crawl.acquire_catalog_runtime_lock.side_effect = CatalogRuntimeLockBusy("owned")

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    assert run_crawl.main() == 2
    load_cookies.assert_not_called()
    database.open_owner_connection.return_value.close.assert_called_once_with()


def test_start_run_rejects_running_scheduler_owner_takeover() -> None:
    database = mock.MagicMock()
    database._execute.return_value.fetchone.return_value = {
        "id": 18,
        "status": "running",
        "dataset_kind": "full",
        "target_parts": None,
        "scheduled_job_run_id": 5830,
        "scope_brand": None,
        "scope_model": None,
        "scope_vehicle_year_floor": None,
    }
    repository = CrawlRepository(database, "2026-08")

    with pytest.raises(RuntimeError, match="already owned by scheduler 5830"):
        repository.start_run("2026-08", scheduled_job_run_id=5831)

    database._execute.assert_called_once_with(
        "SELECT id, status, dataset_kind, target_parts, scheduled_job_run_id, "
        "scope_brand, scope_model, scope_vehicle_year_floor FROM crawl_runs "
        "WHERE run_key = %s FOR UPDATE",
        ("2026-08",),
    )


def test_database_commit_guard_fails_before_transaction_commit() -> None:
    database = Database()
    connection = mock.MagicMock()
    database._local.conn = connection
    guard = mock.MagicMock(side_effect=RuntimeError("runtime lease lost"))
    database.set_commit_guard(guard)

    with pytest.raises(RuntimeError, match="runtime lease lost"):
        database.commit()

    guard.assert_called_once_with()
    connection.commit.assert_not_called()


def test_cli_recover_only_shares_lock_with_full_crawl(monkeypatch, tmp_path):
    # recover 會改 receipt 與 membership；full crawl 持鎖時不得併行。
    shared_state = tmp_path / "shared-state"
    shared_state.mkdir()
    held_crawler_lock = open(shared_state / "crawler.lock", "a")
    fcntl.flock(held_crawler_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    crawler = mock.MagicMock()
    crawler.recover_null_groups.return_value = 0
    database = mock.MagicMock()

    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(shared_state))
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    monkeypatch.setattr(run_crawl, "load_cookies", mock.MagicMock(return_value={}))
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--recover-only", "--workers", "1"])

    try:
        assert run_crawl.main() == 2
        assert not (shared_state / "recover.lock").exists()
        crawler.recover_null_groups.assert_not_called()
    finally:
        fcntl.flock(held_crawler_lock, fcntl.LOCK_UN)
        held_crawler_lock.close()


def test_cli_uses_shared_scheduler_state_lock_across_worktrees(monkeypatch, tmp_path):
    shared_state = tmp_path / "shared-state"
    shared_state.mkdir()
    held_lock = open(shared_state / "crawler.lock", "a")
    fcntl.flock(held_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    load_cookies = mock.MagicMock()

    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(shared_state))
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path / "other-worktree" / "logs")
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    try:
        assert run_crawl.main() == 2
        load_cookies.assert_not_called()
    finally:
        fcntl.flock(held_lock, fcntl.LOCK_UN)
        held_lock.close()


def test_cli_rejects_symlinked_crawler_lock_without_touching_target_or_running_work(
    monkeypatch, tmp_path
):
    log_dir = tmp_path / "logs"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    external = tmp_path / "external-lock-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    (state_dir / "crawler.lock").symlink_to(external)
    database = mock.Mock()
    load_cookies = mock.Mock(return_value={})
    session_manager = mock.Mock()
    request_governor = mock.Mock()
    crawler = mock.Mock()
    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(state_dir))
    monkeypatch.setattr(run_crawl, "LOG_DIR", log_dir)
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr(run_crawl, "SessionManager", session_manager)
    monkeypatch.setattr(run_crawl, "RequestGovernor", request_governor)
    monkeypatch.setattr(run_crawl, "Crawler", crawler)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    with pytest.raises(OSError, match="refusing symlinked state file"):
        run_crawl.main()

    database.assert_not_called()
    load_cookies.assert_not_called()
    session_manager.assert_not_called()
    request_governor.assert_not_called()
    crawler.assert_not_called()
    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_cli_rejects_symlinked_crawl_log_without_touching_target_or_running_work(
    monkeypatch, tmp_path
):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    external = tmp_path / "external-log-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    (log_dir / "crawl.log").symlink_to(external)
    database = mock.Mock()
    load_cookies = mock.Mock(return_value={})
    session_manager = mock.Mock()
    request_governor = mock.Mock()
    crawler = mock.Mock()
    monkeypatch.delenv("PSQ_SCHEDULER_STATE_DIR", raising=False)
    monkeypatch.setattr(run_crawl, "LOG_DIR", log_dir)
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr(run_crawl, "SessionManager", session_manager)
    monkeypatch.setattr(run_crawl, "RequestGovernor", request_governor)
    monkeypatch.setattr(run_crawl, "Crawler", crawler)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    with pytest.raises(OSError, match="refusing symlinked state file"):
        run_crawl.main()

    database.assert_not_called()
    load_cookies.assert_not_called()
    session_manager.assert_not_called()
    request_governor.assert_not_called()
    crawler.assert_not_called()
    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_cli_rejects_symlinked_runtime_log_ancestor_before_any_write_or_work(monkeypatch, tmp_path):
    external = tmp_path / "external-logs"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    alias = tmp_path / "logs-alias"
    alias.symlink_to(external, target_is_directory=True)
    database = mock.Mock()
    monkeypatch.setattr(run_crawl, "LOG_DIR", alias / "runtime")
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    with pytest.raises(OSError, match="refusing symlinked private state path"):
        run_crawl.main()

    database.assert_not_called()
    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


def test_cli_rejects_symlinked_scheduler_state_ancestor_before_crawler_work(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    external = tmp_path / "external-state"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    alias = tmp_path / "state-alias"
    alias.symlink_to(external, target_is_directory=True)
    database = mock.Mock()
    load_cookies = mock.Mock()
    monkeypatch.setattr(run_crawl, "LOG_DIR", log_dir)
    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(alias / "scheduler"))
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    with pytest.raises(OSError, match="refusing symlinked private state path"):
        run_crawl.main()

    database.assert_not_called()
    load_cookies.assert_not_called()
    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


def test_cli_propagates_non_contention_lock_error_before_crawler_work(monkeypatch, tmp_path):
    database = mock.Mock()
    load_cookies = mock.Mock()
    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(run_crawl, "Database", database)
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr(
        run_crawl.fcntl,
        "flock",
        mock.Mock(side_effect=OSError(errno.EIO, "I/O error")),
    )
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    with pytest.raises(OSError) as error:
        run_crawl.main()

    assert error.value.errno == errno.EIO
    database.assert_not_called()
    load_cookies.assert_not_called()


def test_direct_cli_defers_when_schema_migration_has_admission_lock(monkeypatch, tmp_path):
    database = mock.MagicMock()
    crawler = mock.MagicMock()
    crawler.run.side_effect = run_crawl.AdmissionLockBusy("migration")

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    load_cookies = mock.MagicMock(return_value={})
    browser_session = mock.MagicMock()
    monkeypatch.setattr(run_crawl, "load_cookies", load_cookies)
    monkeypatch.setattr("partsouq_catalog.http_client.get_session", browser_session)
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    assert run_crawl.main() == 75
    load_cookies.assert_called_once_with()
    browser_session.assert_not_called()
