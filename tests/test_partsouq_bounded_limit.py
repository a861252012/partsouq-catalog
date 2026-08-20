import os
import re
from unittest import mock

import pytest

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
            "part_number": f"P-{index:05d}",
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
        f'<td><a href="/en/search/all?q=P-{index:05d}">P-{index:05d}</a></td>'
        f"<td>Part {index}</td><td>11000</td><td></td><td>01</td><td></td>"
        "</tr>"
        for index in range(count)
    )
    return f"<table><tbody>{rows}</tbody></table>"


def _group() -> dict:
    return {
        "category_name": "ENGINE/FUEL/TOOL",
        "cid": "1",
        "group_code": "1101",
        "group_name": "PARTIAL ENGINE ASSEMBLY",
        "uid": "10001",
        "url": "/en/catalog/genuine/unit?uid=10001",
    }


def _bounded_config(monkeypatch, target: int = 10) -> None:
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", target)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "bounded-resume-test")
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 77)
    monkeypatch.setitem(CRAWL, "min_brands", 1)
    for key in ("start_brand", "limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, "" if key == "start_brand" else 0)


def test_bounded_retry_resumes_db_membership_and_publishes_exact_target(monkeypatch) -> None:
    _bounded_config(monkeypatch)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=4)
    instance.brands = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.run_status.return_value = "running"
    instance.crawl.count_run_parts.return_value = 8
    instance.crawl.discard_invalid_bounded_membership.return_value = 0
    instance.crawl.resumable_bounded_run_key.return_value = None
    instance.crawl.purge_legacy_vehicle_state.return_value = 0
    instance.crawl.remaining_group_count.return_value = 0
    instance.crawl.count_failures.return_value = 0
    instance._brands = mock.MagicMock(return_value=[{"name": "TOYOTA"}])

    def finish_remaining(_brand: str) -> None:
        assert instance.counts["parts"] == 8
        instance.counts["parts"] = 10
        instance._sample_limit_reached.set()
        raise SampleLimitReached

    instance.crawl_brand = mock.MagicMock(side_effect=finish_remaining)
    try:
        counts = instance.run()
    finally:
        instance.close()

    assert counts["parts"] == 10
    assert instance.last_status == "bounded_success"
    assert instance.workers == 1
    generated_run_key = instance.crawl.start_run.call_args.args[0]
    assert re.fullmatch(r"bounded-10-s\d{15}", generated_run_key)
    instance.crawl.resumable_bounded_run_key.assert_called_once_with(
        10,
        scheduled_job_run_id=77,
    )
    assert instance.crawl.start_run.call_args.kwargs == {
        "fresh": False,
        "dataset_kind": "bounded",
        "target_parts": 10,
        "scheduled_job_run_id": 77,
    }
    instance.crawl.publish_bounded_parts.assert_called_once_with(17, 10)
    instance.crawl.publish_success_parts.assert_not_called()
    assert instance.crawl.finish_run.call_args.args[:3] == (17, "bounded_success", counts)


def test_bounded_partial_group_retry_excludes_already_seen_keys(monkeypatch) -> None:
    _bounded_config(monkeypatch)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.counts["parts"] = 8
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.parts.bounded_group_context.return_value = {
        "production_from": "2018-01",
        "production_to": "2020-12",
        "source_valid": 1,
    }
    instance.parts.seen_keys_in_group.return_value = {
        (part["part_number"], part["range_str"]) for part in _parts(8)
    }
    instance.parts.upsert_parts.return_value = 2
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance.crawl.previous_row_count.return_value = 0
    instance._get = mock.MagicMock(return_value=_parts_html(10))
    try:
        truncated = instance.crawl_group("TOYOTA", 7, _group(), fetched={})
    finally:
        instance.close()

    assert truncated is False
    assert instance.counts["parts"] == 10
    written = instance.parts.upsert_parts.call_args
    assert [row["part_number"] for row in written.args[1]] == ["P-00008", "P-00009"]
    assert written.kwargs == {"complete_group": True}
    instance.crawl.mark_group_fetched.assert_called_once_with(
        41,
        "bounded-resume-test",
        status="done",
        row_count=10,
    )


def test_bounded_partial_group_retry_removes_disappeared_membership(monkeypatch) -> None:
    _bounded_config(monkeypatch)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.counts["parts"] = 8
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.parts.bounded_group_context.return_value = {
        "production_from": "2018-01",
        "production_to": "2020-12",
        "source_valid": 1,
    }
    instance.parts.seen_keys_in_group.return_value = {
        (part["part_number"], part["range_str"]) for part in _parts(8)
    }
    instance.parts.clear_seen_keys.return_value = 1
    instance.parts.upsert_parts.return_value = 2
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance.crawl.previous_row_count.return_value = 0
    instance._get = mock.MagicMock(return_value="<table></table>")
    with mock.patch(
        "partsouq_catalog.crawler.parse_parts",
        return_value=(_parts(10)[1:], 0),
    ):
        try:
            truncated = instance.crawl_group("TOYOTA", 7, _group(), fetched={})
        finally:
            instance.close()

    assert truncated is False
    assert instance.counts["parts"] == 9
    instance.parts.clear_seen_keys.assert_called_once_with(41, 17, {("P-00000", "")})
    written = instance.parts.upsert_parts.call_args
    assert [row["part_number"] for row in written.args[1]] == ["P-00008", "P-00009"]
    assert written.kwargs == {"complete_group": True}


def test_bounded_and_sample_limits_are_mutually_exclusive(monkeypatch) -> None:
    monkeypatch.setitem(CRAWL, "limit_parts", 1)
    monkeypatch.setitem(CRAWL, "bounded_parts", 1)

    with pytest.raises(ValueError, match="mutually exclusive"):
        Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_bounded_publish_is_atomic_and_does_not_touch_full_snapshot() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
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

        manual_scheduler_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'manual', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        manual_scheduler = CrawlRepository(database, "bounded-1-manual-scheduler")
        manual_scheduler_crawl_id = manual_scheduler.start_run(
            "bounded-1-manual-scheduler",
            fresh=True,
            dataset_kind="bounded",
            target_parts=1,
            scheduled_job_run_id=manual_scheduler_run_id,
        )
        parts.upsert_parts(group_id, _parts(1), manual_scheduler_crawl_id)
        database.commit()
        with pytest.raises(RuntimeError, match="invalid scheduler provenance"):
            manual_scheduler.publish_bounded_parts(manual_scheduler_crawl_id, 1)
        database.rollback()

        manual = CrawlRepository(database, "bounded-manual-partial")
        manual_run_id = manual.start_run(
            "bounded-manual-partial",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
        )
        manual.finish_run(manual_run_id, "error", {"parts": 0}, "interrupted direct CLI")
        database.commit()
        assert (
            manual.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=scheduled_job_run_id,
            )
            is None
        )
        assert manual.resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=None,
        ) == ("bounded-manual-partial")

        first = CrawlRepository(database, "bounded-scheduled-resume")
        first_run_id = first.start_run(
            "bounded-scheduled-resume",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        parts.upsert_parts(group_id, _parts(8_000), first_run_id, complete_group=False)
        first.finish_run(first_run_id, "error", {"parts": 8_000}, "simulated interruption")
        database.commit()
        assert first.resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=scheduled_job_run_id,
        ) == ("bounded-scheduled-resume")

        retry_run_id = first.start_run(
            "bounded-scheduled-resume",
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        assert retry_run_id == first_run_id
        assert first.count_run_parts(retry_run_id) == 8_000
        parts.upsert_parts(group_id, _parts(10_000)[8_000:], retry_run_id)
        assert first.count_run_parts(retry_run_id) == 10_000
        first.publish_bounded_parts(retry_run_id, 10_000)
        first.finish_run(retry_run_id, "bounded_success", {"parts": 10_000})
        database.commit()

        published_count = database._execute(
            "SELECT COUNT(*) AS row_count FROM published_parts"
        ).fetchone()
        bounded = database._execute(
            "SELECT crawl_run_id, part_number, part_number_normalized, source_url "
            "FROM bounded_parts WHERE part_number = 'P-00000'"
        ).fetchone()
        bounded_count = database._execute(
            "SELECT COUNT(*) AS row_count, COUNT(DISTINCT crawl_run_id) AS run_count "
            "FROM bounded_parts"
        ).fetchone()
        bounded_indexes = database._execute(
            "SELECT COUNT(DISTINCT INDEX_NAME) AS index_count "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'bounded_parts' "
            "AND INDEX_NAME IN "
            "('idx_bounded_part_number_normalized', 'idx_bounded_snapshot_page')"
        ).fetchone()
        assert published_count == {"row_count": 0}
        assert bounded_count == {"row_count": 10_000, "run_count": 1}
        assert bounded_indexes == {"index_count": 2}
        assert bounded == {
            "crawl_run_id": first_run_id,
            "part_number": "P-00000",
            "part_number_normalized": "P00000",
            "source_url": "https://partsouq.com/en/catalog/genuine/unit?uid=10001",
        }

        second = CrawlRepository(database, "bounded-incomplete-next-cycle")
        second_run_id = second.start_run(
            "bounded-incomplete-next-cycle",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        parts.upsert_parts(group_id, _parts(9_999), second_run_id, complete_group=False)
        database.commit()

        with pytest.raises(RuntimeError, match="source count mismatch"):
            second.publish_bounded_parts(second_run_id, 10_000)
        database.rollback()

        parts.upsert_parts(group_id, _parts(10_000)[9_999:], second_run_id)
        second.mark_error("vehicle", "TOYOTA::CAMRY::failed", "upstream failed")
        database.commit()

        with pytest.raises(RuntimeError, match="has crawl failures: count=1"):
            second.publish_bounded_parts(second_run_id, 10_000)
        database.rollback()

        second.mark_done("vehicle", "TOYOTA::CAMRY::failed")
        database._execute(
            "UPDATE parts SET code = '' WHERE seen_run_id = %s AND part_number = 'P-09999'",
            (second_run_id,),
        )
        database.commit()

        with pytest.raises(RuntimeError, match="failed source/field quality gate"):
            second.publish_bounded_parts(second_run_id, 10_000)
        database.rollback()

        preserved = database._execute(
            "SELECT COUNT(*) AS row_count, MIN(crawl_run_id) AS min_run_id, "
            "MAX(crawl_run_id) AS max_run_id FROM bounded_parts"
        ).fetchone()
        assert preserved == {
            "row_count": 10_000,
            "min_run_id": first_run_id,
            "max_run_id": first_run_id,
        }
        assert second.discard_invalid_bounded_membership(second_run_id) == 1
        database.commit()
        assert second.count_run_parts(second_run_id) == 9_999
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


def _clear_mysql_fixture(database: Database) -> None:
    for table in (
        "bounded_parts",
        "published_parts",
        "admin_vehicle_mappings",
        "crawl_state",
        "crawl_runs",
        "scheduled_job_runs",
        "brands",
    ):
        database._execute(f"DELETE FROM {table}")
    database.commit()
