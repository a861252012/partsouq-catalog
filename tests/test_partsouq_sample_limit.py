import os
from unittest import mock

import pytest

from partsouq_catalog import run_crawl
from partsouq_catalog.config import CRAWL, DB_CONFIG
from partsouq_catalog.crawler import Crawler, SampleLimitReached
from partsouq_catalog.db import Database
from partsouq_catalog.repositories import (
    BrandRepository,
    CrawlRepository,
    PartRepository,
    VehicleRepository,
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
    rows = "".join(
        "<tr>"
        f'<td><a href="/en/search/all?q=P{index:05d}">P{index:05d}</a></td>'
        f"<td>Part {index}</td><td>11000</td><td></td><td>01</td><td></td>"
        "</tr>"
        for index in range(count)
    )
    return f"<table><tbody>{rows}</tbody></table>"


@pytest.fixture
def sample_crawler(monkeypatch):
    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
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
    assert fetched == {("1", "1101"): 1000}


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
    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
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
    instance.crawl.publish_success_parts.assert_not_called()


def test_negative_limit_is_rejected(monkeypatch):
    monkeypatch.setitem(CRAWL, "limit_parts", -1)

    with pytest.raises(ValueError, match="PSQ_LIMIT_PARTS"):
        Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL sample tests",
)
def test_mysql_partial_sample_readback(monkeypatch):
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
    database = Database().connect()
    try:
        for table in ("published_parts", "crawl_state", "crawl_runs", "brands"):
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
        for table in ("published_parts", "crawl_state", "crawl_runs", "brands"):
            database._execute(f"DELETE FROM {table}")
        database.commit()
        database.close()


@pytest.mark.parametrize(
    ("status", "expected"),
    [("success", 0), ("sample", 3), ("error", 1)],
)
def test_cli_has_distinct_exit_codes(monkeypatch, tmp_path, status, expected):
    database = mock.MagicMock()
    crawler = mock.MagicMock(last_status=status)
    crawler.run.return_value = {}

    monkeypatch.setattr(run_crawl, "LOG_DIR", tmp_path)
    monkeypatch.setattr(run_crawl, "Database", mock.MagicMock(return_value=database))
    database.connect.return_value = database
    monkeypatch.setattr(run_crawl, "SessionManager", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "RequestGovernor", mock.MagicMock())
    monkeypatch.setattr(run_crawl, "Crawler", mock.MagicMock(return_value=crawler))
    monkeypatch.setattr("sys.argv", ["partsouq-catalog-crawl", "--workers", "1"])

    assert run_crawl.main() == expected
