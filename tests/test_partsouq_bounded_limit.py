import hashlib
import os
import re
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from unittest import mock

import pytest

from partsouq_catalog import scheduler
from partsouq_catalog.config import CRAWL, DB_CONFIG
from partsouq_catalog.crawler import Crawler, SampleLimitReached
from partsouq_catalog.db import Database
from partsouq_catalog.evidence import (
    PARSER_CONTRACT_VERSION,
    RecordEvidence,
    public_source_url,
    replay_catalog_records,
    sanitize_parser_html,
)
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
            "note": "",
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


def _record_verified_live_evidence(
    database: Database,
    repository: CrawlRepository,
    *,
    run_id: int,
    scheduled_job_run_id: int,
    page_types: frozenset[str] | None = None,
    verify: bool = True,
) -> None:
    vehicle_key = {
        "brand": "TOYOTA",
        "model": "CAMRY",
        "name": "CAMRY",
        "model_code": "AXVA70",
        "prod_period": "01.2018 - 12.2020",
        "production_from": "2018-01",
        "production_to": "2020-12",
        "engine": None,
        "trim_name": None,
        "vid": "SITE-VID-1",
    }
    unit_html = _parts_html(10_000)
    pages: tuple[tuple[str, str, str, dict[str, object], str], ...] = (
        (
            "genuine",
            "parse_brands",
            "https://partsouq.com/en/catalog/genuine",
            {},
            '<li><a href="/en/catalog/genuine/locate?c=TOYOTA">TOYOTA</a></li>',
        ),
        (
            "locate",
            "parse_brand_index",
            "https://partsouq.com/en/catalog/genuine/locate?c=TOYOTA",
            {"brand": "TOYOTA"},
            '<a href="/en/catalog/genuine/pick?c=TOYOTA&model=CAMRY&ssd=token">CAMRY</a>',
        ),
        (
            "pick",
            "parse_vehicles",
            "https://partsouq.com/en/catalog/genuine/pick?c=TOYOTA&model=CAMRY",
            {"brand": "TOYOTA", "model": "CAMRY"},
            "<table><tr><th class='n_name'>Name</th><th class='__model'>Model</th>"
            "<th class='__prodPeriod'>Prod Period</th></tr><tr>"
            "<td><a href='/en/catalog/genuine/vehicle?c=TOYOTA&ssd=token&vid=SITE-VID-1'>"
            "CAMRY</a></td><td>AXVA70</td><td>01.2018 - 12.2020</td></tr></table>",
        ),
        (
            "vehicle",
            "parse_category_links",
            "https://partsouq.com/en/catalog/genuine/vehicle?c=TOYOTA&vid=SITE-VID-1",
            {
                "brand": "TOYOTA",
                "vehicle_key": vehicle_key,
                "expected_vid": "SITE-VID-1",
                "source_url": (
                    "https://partsouq.com/en/catalog/genuine/vehicle?c=TOYOTA&vid=SITE-VID-1"
                ),
            },
            "<html><body>ENGINE/FUEL/TOOL</body></html>",
        ),
        (
            "category",
            "parse_groups",
            ("https://partsouq.com/en/catalog/genuine/vehicle?c=TOYOTA&vid=SITE-VID-1&cid=1"),
            {
                "brand": "TOYOTA",
                "vehicle_key": vehicle_key,
                "default_cid": "1",
                "expected_vid": "SITE-VID-1",
            },
            "<a href='/en/catalog/genuine/unit?c=TOYOTA&ssd=token&vid=SITE-VID-1"
            "&cid=1&uid=10001&q='>1101: PARTIAL ENGINE ASSEMBLY</a>",
        ),
        (
            "unit",
            "parse_parts",
            (
                "https://partsouq.com/en/catalog/genuine/unit"
                "?c=TOYOTA&vid=SITE-VID-1&cid=1&uid=10001"
            ),
            {
                "group_key": {
                    "category": {
                        "vehicle": vehicle_key,
                        "cid": "1",
                        "category_name": "ENGINE/FUEL/TOOL",
                    },
                    "group_code": "1101",
                    "uid": "10001",
                }
            },
            unit_html,
        ),
    )
    for page_type, parser_name, public_url, context, html in pages:
        if page_types is not None and page_type not in page_types:
            continue
        sanitized = sanitize_parser_html(html)
        records, malformed_rows, skipped_rows = replay_catalog_records(
            sanitized.body,
            parser_name=parser_name,
            parser_version=PARSER_CONTRACT_VERSION,
            context=context,
        )
        accepted_records: list[tuple[int, RecordEvidence]] = []
        if page_type == "unit":
            part_rows = database._execute(
                "SELECT id FROM parts WHERE seen_run_id = %s ORDER BY part_number",
                (run_id,),
            ).fetchall()
            assert len(part_rows) == len(records) == 10_000
            accepted_records = [
                (int(row["id"]), record) for row, record in zip(part_rows, records, strict=True)
            ]
        repository.record_http_evidence(
            run_id,
            scheduled_job_run_id,
            page_type=page_type,
            public_url=public_source_url(public_url),
            raw_body_sha256=hashlib.sha256(html.encode()).hexdigest(),
            status_code=200,
            content_type="text/html",
            fetched_at=datetime.now(UTC).replace(tzinfo=None),
            elapsed_ms=1,
            attempt=1,
            sanitized_body=sanitized,
            parser_name=parser_name,
            parser_version=PARSER_CONTRACT_VERSION,
            parser_context=context,
            parsed_records=records,
            replayed_records=records,
            accepted_records=accepted_records,
            malformed_rows=malformed_rows,
            skipped_record_count=skipped_rows,
        )
    if verify:
        repository.verify_run_evidence(run_id)


def _bounded_config(monkeypatch, target: int = 10) -> None:
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", target)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "bounded-resume-test")
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 77)
    monkeypatch.setitem(CRAWL, "min_brands", 1)
    for key in ("start_brand", "limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, "" if key == "start_brand" else 0)


def test_bounded_retry_resumes_db_membership_and_publishes_exact_target(monkeypatch) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
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


def test_bounded_resume_failure_closes_durable_running_marker(monkeypatch) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    _bounded_config(monkeypatch)
    events: list[str] = []
    database = mock.MagicMock()
    database.commit.side_effect = lambda: events.append("commit")
    database.rollback.side_effect = lambda: events.append("rollback")
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.count_run_parts.side_effect = RuntimeError("resume read failed")
    instance.crawl.finish_run.side_effect = lambda *_args: events.append("finish-error")
    try:
        with pytest.raises(RuntimeError, match="resume read failed"):
            instance.run()
    finally:
        instance.close()

    assert instance.last_status == "error"
    assert events == ["commit", "rollback", "finish-error", "commit"]
    instance.crawl.finish_run.assert_called_once_with(
        17,
        "error",
        instance.counts,
        "resume read failed",
    )


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
        return_value=(_parts(10)[1:], 0, 0, []),
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


def test_bounded_run_publishes_despite_quarantined_rows(monkeypatch) -> None:
    """「忽略 + 紀錄」政策（使用者決定）：quarantine 列是紀錄、不阻擋
    發布 —— bounded run 達到 target 且無 crawl_state failure 時照常
    bounded_success，即使存在 quarantine 列。"""
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    _bounded_config(monkeypatch)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.brands = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.run_status.return_value = "running"
    instance.crawl.count_run_parts.return_value = 10
    instance.crawl.discard_invalid_bounded_membership.return_value = 0
    instance.crawl.resumable_bounded_run_key.return_value = None
    instance.crawl.purge_legacy_vehicle_state.return_value = 0
    instance.crawl.remaining_group_count.return_value = 0
    instance.crawl.count_failures.return_value = 0
    instance.crawl.count_quarantined.return_value = 3
    instance._brands = mock.MagicMock(return_value=[{"name": "TOYOTA"}])
    instance.crawl_brand = mock.MagicMock(return_value=0)
    try:
        counts = instance.run()
    finally:
        instance.close()

    assert instance.last_status == "bounded_success"
    instance.crawl.publish_bounded_parts.assert_called_once_with(17, 10)
    assert instance.crawl.finish_run.call_args.args[:3] == (17, "bounded_success", counts)


def test_bounded_and_sample_limits_are_mutually_exclusive(monkeypatch) -> None:
    monkeypatch.setitem(CRAWL, "limit_parts", 1)
    monkeypatch.setitem(CRAWL, "bounded_parts", 1)

    with pytest.raises(ValueError, match="mutually exclusive"):
        Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)


def test_bounded_publish_rejects_non_formal_target_before_query() -> None:
    database = mock.MagicMock()

    with pytest.raises(ValueError, match="exactly 10000"):
        CrawlRepository(database, "bounded-invalid-target").publish_bounded_parts(17, 9_999)

    database._execute.assert_not_called()


def test_scheduled_bounded_resume_requires_a_finished_failed_prior_attempt() -> None:
    database = mock.MagicMock()
    database._execute.return_value.fetchone.return_value = None

    assert (
        CrawlRepository(database, "bounded-resume-contract").resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=77,
        )
        is None
    )

    query, params = database._execute.call_args.args
    assert "cr.status IN ('running', 'error', 'interrupted')" in query
    assert "previous_job.id = current_job.id" in query
    assert "previous_job.status = 'failed'" in query
    assert "previous_job.finished_at IS NOT NULL" in query
    assert "previous_job.exit_code IS NOT NULL" in query
    assert "previous_job.exit_code <> 0" in query
    assert params == (10_000, 77)


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
            target_parts=10_000,
            scheduled_job_run_id=manual_scheduler_run_id,
        )
        parts.upsert_parts(group_id, _parts(1), manual_scheduler_crawl_id)
        database.commit()
        with pytest.raises(RuntimeError, match="invalid scheduler provenance"):
            manual_scheduler.publish_bounded_parts(manual_scheduler_crawl_id, 10_000)
        database.rollback()

        manual = CrawlRepository(database, "bounded-manual-partial")
        manual_run_id = manual.start_run(
            "bounded-manual-partial",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
        )
        manual.finish_run(
            manual_run_id,
            "interrupted",
            {"parts": 0},
            "interrupted direct CLI",
        )
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
        _record_verified_live_evidence(
            database,
            first,
            run_id=first_run_id,
            scheduled_job_run_id=int(scheduled_job_run_id),
            page_types=frozenset({"genuine", "locate", "pick", "vehicle", "category"}),
            verify=False,
        )
        first.finish_run(
            first_run_id,
            "interrupted",
            {"parts": 8_000},
            "simulated scheduler interruption",
        )
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 125, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (scheduled_job_run_id,),
        )
        retry_scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        database.commit()
        assert first.resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=retry_scheduled_job_run_id,
        ) == ("bounded-scheduled-resume")

        retry_run_id = first.start_run(
            "bounded-scheduled-resume",
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=retry_scheduled_job_run_id,
        )
        assert retry_run_id == first_run_id
        assert first.count_run_parts(retry_run_id) == 8_000
        parts.upsert_parts(group_id, _parts(10_000)[8_000:], retry_run_id)
        assert first.count_run_parts(retry_run_id) == 10_000
        _record_verified_live_evidence(
            database,
            first,
            run_id=retry_run_id,
            scheduled_job_run_id=int(retry_scheduled_job_run_id),
            page_types=frozenset({"unit"}),
        )
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

        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 125, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (retry_scheduled_job_run_id,),
        )
        database.commit()
        assert scheduler._recover_interrupted_job_runs("catalog") is True
        recovered_scheduler = database._execute(
            "SELECT status, exit_code FROM scheduled_job_runs WHERE id = %s",
            (retry_scheduled_job_run_id,),
        ).fetchone()
        assert recovered_scheduler == {"status": "completed", "exit_code": 0}
        audit = first.audit_run_evidence(retry_run_id)
        assert audit["verified"] is True
        artifact_lineage = database._execute(
            "SELECT scheduled_job_run_id, COUNT(*) AS artifact_count "
            "FROM partsouq_http_artifacts WHERE crawl_run_id = %s "
            "AND verification_status = 'verified' "
            "GROUP BY scheduled_job_run_id ORDER BY scheduled_job_run_id",
            (retry_run_id,),
        ).fetchall()
        assert artifact_lineage == [
            {"scheduled_job_run_id": scheduled_job_run_id, "artifact_count": 5},
            {"scheduled_job_run_id": retry_scheduled_job_run_id, "artifact_count": 1},
        ]
        current_bounded = database._execute(
            "SELECT COUNT(*) AS row_count, COUNT(DISTINCT dataset_scope) AS scope_count, "
            "MIN(dataset_scope) AS dataset_scope FROM v_current_catalog_parts"
        ).fetchone()
        assert current_bounded == {
            "row_count": 10_000,
            "scope_count": 1,
            "dataset_scope": "bounded",
        }

        history_scheduler_id = database._execute(
            "INSERT INTO scheduled_job_runs "
            "(job_name, trigger_mode, status, started_at, finished_at, exit_code) "
            "VALUES ('catalog', 'daemon', 'failed', "
            "UTC_TIMESTAMP() - INTERVAL 1 HOUR, UTC_TIMESTAMP(), 125)"
        ).lastrowid
        history_run_id = database._execute(
            "INSERT INTO crawl_runs "
            "(run_key, started_at, finished_at, status, dataset_kind, target_parts, "
            "scheduled_job_run_id, error_msg) VALUES "
            "('bounded-history-artifacts', UTC_TIMESTAMP() - INTERVAL 1 HOUR, "
            "UTC_TIMESTAMP(), 'interrupted', 'bounded', 10000, %s, 'history fixture')",
            (history_scheduler_id,),
        ).lastrowid
        source_artifact = database._execute(
            "SELECT id FROM partsouq_http_artifacts WHERE crawl_run_id = %s "
            "AND page_type = 'unit' ORDER BY id LIMIT 1",
            (retry_run_id,),
        ).fetchone()
        assert source_artifact is not None
        database._executemany(
            "INSERT INTO partsouq_http_artifacts("
            "crawl_run_id, scheduled_job_run_id, capture_kind, page_type, public_source_url, "
            "source_url_sha256, raw_body_sha256, body_sha256, http_status, content_type, "
            "challenge_detected, fetched_at, elapsed_ms, attempt, parser_name, parser_version, "
            "parser_context_json, parser_context_sha256, malformed_row_count, "
            "skipped_record_count, parsed_record_count, parsed_records_sha256, "
            "accepted_record_count, accepted_records_sha256, verification_status, verified_at"
            ") SELECT %s, %s, capture_kind, page_type, public_source_url, source_url_sha256, "
            "%s, body_sha256, http_status, content_type, challenge_detected, fetched_at, "
            "elapsed_ms, attempt, parser_name, parser_version, parser_context_json, "
            "parser_context_sha256, malformed_row_count, skipped_record_count, "
            "parsed_record_count, parsed_records_sha256, accepted_record_count, "
            "accepted_records_sha256, verification_status, verified_at "
            "FROM partsouq_http_artifacts WHERE id = %s",
            (
                (
                    history_run_id,
                    history_scheduler_id,
                    hashlib.sha256(f"bounded-history-{index}".encode()).hexdigest(),
                    source_artifact["id"],
                )
                for index in range(2_000)
            ),
        )
        database._execute(
            "INSERT INTO partsouq_artifact_records("
            "artifact_id, crawl_run_id, record_type, natural_key_sha256, "
            "parent_natural_key_sha256, record_sha256, accepted, part_id"
            ") SELECT history.id, %s, seed.record_type, seed.natural_key_sha256, "
            "seed.parent_natural_key_sha256, seed.record_sha256, seed.accepted, seed.part_id "
            "FROM partsouq_http_artifacts AS history CROSS JOIN ("
            "SELECT record_type, natural_key_sha256, parent_natural_key_sha256, "
            "record_sha256, accepted, part_id FROM partsouq_artifact_records "
            "WHERE artifact_id = %s AND accepted = 1 ORDER BY part_id LIMIT 1"
            ") AS seed WHERE history.crawl_run_id = %s",
            (history_run_id, source_artifact["id"], history_run_id),
        )
        database.commit()
        view_plan_row = database._execute(
            "EXPLAIN ANALYZE SELECT COUNT(*) FROM v_current_catalog_parts"
        ).fetchone()
        assert view_plan_row is not None
        view_plan = str(view_plan_row["EXPLAIN"])
        record_lookup = re.search(
            r"idx_partsouq_record_run_accepted.*?actual time=[^\n]*?rows=([0-9.]+) "
            r"loops=([0-9.]+)",
            view_plan,
        )
        assert record_lookup is not None, view_plan
        assert float(record_lookup[1]) * float(record_lookup[2]) <= 10_000, view_plan
        assert "scan on artifact" not in view_plan.lower(), view_plan

        formal_queries = {
            "first_page": (
                "SELECT part_id, part_number FROM v_current_catalog_parts "
                "ORDER BY part_number_normalized, part_id LIMIT 200",
                200,
            ),
            "last_page": (
                "SELECT part_id, part_number FROM v_current_catalog_parts "
                "ORDER BY part_number_normalized, part_id LIMIT 200 OFFSET 9800",
                200,
            ),
            "exact_part_number": (
                "SELECT part_id, part_number FROM v_current_catalog_parts "
                "WHERE part_number_normalized = 'P09999' "
                "ORDER BY part_number_normalized, part_id LIMIT 200",
                1,
            ),
        }
        formal_p95_ms: dict[str, float] = {}
        for query_name, (query, expected_rows) in formal_queries.items():
            query_plan_row = database._execute(f"EXPLAIN ANALYZE {query}").fetchone()
            assert query_plan_row is not None
            query_plan = str(query_plan_row["EXPLAIN"])
            assert "scan on artifact" not in query_plan.lower(), query_plan
            for _ in range(3):
                assert len(database._execute(query).fetchall()) == expected_rows
            samples_ms = []
            for _ in range(20):
                started_at = time.perf_counter()
                rows = database._execute(query).fetchall()
                samples_ms.append((time.perf_counter() - started_at) * 1_000)
                assert len(rows) == expected_rows
            formal_p95_ms[query_name] = sorted(samples_ms)[18]
        assert all(duration < 500 for duration in formal_p95_ms.values()), formal_p95_ms
        print(
            "verified formal 10k p95 ms: "
            + ", ".join(
                f"{query_name}={duration:.2f}" for query_name, duration in formal_p95_ms.items()
            )
        )

        database._execute(
            "UPDATE crawl_runs SET evidence_status = 'missing' WHERE id = %s",
            (retry_run_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}
        assert database._execute("SELECT COUNT(*) AS row_count FROM v_parts").fetchone() == {
            "row_count": 0
        }
        database._execute(
            "UPDATE crawl_runs SET evidence_status = 'verified' WHERE id = %s",
            (retry_run_id,),
        )
        mutated_artifact = database._execute(
            "SELECT id, fetched_at FROM partsouq_http_artifacts WHERE crawl_run_id = %s "
            "AND verification_status = 'verified' ORDER BY id LIMIT 1",
            (retry_run_id,),
        ).fetchone()
        assert mutated_artifact is not None
        database._execute(
            "UPDATE partsouq_http_artifacts SET capture_kind = 'fixture' WHERE id = %s",
            (mutated_artifact["id"],),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}
        database._execute(
            "UPDATE partsouq_http_artifacts SET capture_kind = 'live_http' WHERE id = %s",
            (mutated_artifact["id"],),
        )
        database.commit()
        assert first.audit_run_evidence(retry_run_id)["verified"] is True
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 10_000}

        database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0 WHERE id = %s",
            (scheduled_job_run_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}
        assert database._execute("SELECT COUNT(*) AS row_count FROM v_parts").fetchone() == {
            "row_count": 0
        }
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 125 WHERE id = %s",
            (scheduled_job_run_id,),
        )
        database._execute(
            "UPDATE scheduled_job_runs SET trigger_mode = 'manual' WHERE id = %s",
            (scheduled_job_run_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}
        database._execute(
            "UPDATE scheduled_job_runs SET trigger_mode = 'daemon' WHERE id = %s",
            (scheduled_job_run_id,),
        )
        database.commit()
        assert first.audit_run_evidence(retry_run_id)["verified"] is True
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 10_000}

        database._execute(
            "UPDATE partsouq_http_artifacts AS artifact "
            "JOIN scheduled_job_runs AS job ON job.id = artifact.scheduled_job_run_id "
            "SET artifact.fetched_at = job.finished_at + INTERVAL 6 MINUTE "
            "WHERE artifact.id = %s",
            (mutated_artifact["id"],),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}
        database._execute(
            "UPDATE partsouq_http_artifacts SET fetched_at = %s WHERE id = %s",
            (mutated_artifact["fetched_at"], mutated_artifact["id"]),
        )
        database.commit()
        assert first.audit_run_evidence(retry_run_id)["verified"] is True
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 10_000}

        database._execute(
            "INSERT INTO published_parts("
            "part_id, vehicle_id, brand, model, vehicle_name, vehicle_code, part_name, "
            "part_number, part_number_normalized, category_main, group_code, part_range, "
            "snapshot_at) VALUES (2000000000, 2000000000, 'LEGACY', 'LEGACY MODEL', "
            "'LEGACY VEHICLE', 'LEGACY-CODE', 'LEGACY PART', 'LEGACY-PART-001', "
            "'LEGACYPART001', 'LEGACY CATEGORY', 'LEGACY-GROUP', '', UTC_TIMESTAMP())"
        )
        database.commit()
        assert (
            database._execute(
                "SELECT COUNT(*) AS row_count, COUNT(DISTINCT dataset_scope) AS scope_count, "
                "MIN(dataset_scope) AS dataset_scope FROM v_current_catalog_parts"
            ).fetchone()
            == current_bounded
        )
        assert database._execute("SELECT COUNT(*) AS row_count FROM v_parts").fetchone() == {
            "row_count": 10_000
        }
        database._execute("DELETE FROM published_parts")
        next_scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        database.commit()

        second = CrawlRepository(database, "bounded-incomplete-next-cycle")
        second_run_id = second.start_run(
            "bounded-incomplete-next-cycle",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=next_scheduled_job_run_id,
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
        _record_verified_live_evidence(
            database,
            second,
            run_id=second_run_id,
            scheduled_job_run_id=int(next_scheduled_job_run_id),
        )
        database._execute(
            "UPDATE parts SET code = '' WHERE seen_run_id = %s AND part_number = 'P-09999'",
            (second_run_id,),
        )
        database.commit()

        with pytest.raises(RuntimeError, match="evidence payload hash mismatch"):
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


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL full snapshot tests",
)
def test_mysql_full_publish_rejects_non_full_and_invalid_source_atomically() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
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

        unscheduled = CrawlRepository(database, "full-publish-unscheduled")
        unscheduled_run_id = unscheduled.start_run("full-publish-unscheduled", fresh=True)
        parts.upsert_parts(group_id, _parts(1), unscheduled_run_id)
        database.commit()
        with pytest.raises(RuntimeError, match="invalid scheduler provenance"):
            unscheduled.publish_success_parts(unscheduled_run_id)
        database.rollback()

        manual_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'manual', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        manual = CrawlRepository(database, "full-publish-manual")
        manual_run_id = manual.start_run(
            "full-publish-manual",
            fresh=True,
            scheduled_job_run_id=manual_job_id,
        )
        parts.upsert_parts(group_id, _parts(1), manual_run_id)
        database.commit()
        with pytest.raises(RuntimeError, match="invalid scheduler provenance"):
            manual.publish_success_parts(manual_run_id)
        database.rollback()

        failed_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        failed = CrawlRepository(database, "full-publish-failed")
        failed_run_id = failed.start_run(
            "full-publish-failed",
            fresh=True,
            scheduled_job_run_id=failed_job_id,
        )
        parts.upsert_parts(group_id, _parts(1), failed_run_id)
        failed.finish_run(failed_run_id, "error", {"parts": 1}, "upstream failed")
        database.commit()
        with pytest.raises(RuntimeError, match="not a matching running full crawl"):
            failed.publish_success_parts(failed_run_id)
        database.rollback()

        shared_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        shared_first = CrawlRepository(database, "full-publish-shared-first")
        shared_first_id = shared_first.start_run(
            "full-publish-shared-first",
            fresh=True,
            scheduled_job_run_id=shared_job_id,
        )
        CrawlRepository(database, "full-publish-shared-second").start_run(
            "full-publish-shared-second",
            fresh=True,
            scheduled_job_run_id=shared_job_id,
        )
        parts.upsert_parts(group_id, _parts(1), shared_first_id)
        database.commit()
        with pytest.raises(RuntimeError, match="invalid scheduler provenance"):
            shared_first.publish_success_parts(shared_first_id)
        database.rollback()

        scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        first = CrawlRepository(database, "full-publish-valid")
        first_run_id = first.start_run(
            "full-publish-valid",
            fresh=True,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        parts.upsert_parts(group_id, _parts(1), first_run_id)
        assert first.publish_success_parts(first_run_id) == 1
        published_provenance = database._execute(
            "SELECT crawl_run_id FROM published_parts"
        ).fetchone()
        assert published_provenance == {"crawl_run_id": first_run_id}
        first.finish_run(first_run_id, "success", {"parts": 1})
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}

        database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', finished_at = UTC_TIMESTAMP(), "
            "exit_code = 1 WHERE id = %s",
            (scheduled_job_run_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}

        database._execute(
            "UPDATE scheduled_job_runs SET exit_code = 0 WHERE id = %s",
            (scheduled_job_run_id,),
        )
        database.commit()
        current_provenance = database._execute(
            "SELECT dataset_scope, source_crawl_run_id FROM v_current_catalog_parts"
        ).fetchone()
        assert current_provenance == {
            "dataset_scope": "full",
            "source_crawl_run_id": first_run_id,
        }
        expected_snapshot = database._execute(
            "SELECT part_number, part_name, code FROM published_parts"
        ).fetchone()

        sample = CrawlRepository(database, "full-publish-sample")
        sample_run_id = sample.start_run(
            "full-publish-sample",
            fresh=True,
            dataset_kind="sample",
            target_parts=1,
        )
        parts.upsert_parts(group_id, [{**_parts(1)[0], "name": "SAMPLE PART"}], sample_run_id)
        database.commit()
        with pytest.raises(RuntimeError, match="not a matching running full crawl"):
            sample.publish_success_parts(sample_run_id)
        database.rollback()
        assert (
            database._execute("SELECT part_number, part_name, code FROM published_parts").fetchone()
            == expected_snapshot
        )

        invalid_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        invalid = CrawlRepository(database, "full-publish-invalid")
        invalid_run_id = invalid.start_run(
            "full-publish-invalid",
            fresh=True,
            scheduled_job_run_id=invalid_job_id,
        )
        parts.upsert_parts(group_id, _parts(1), invalid_run_id)
        invalid.mark_error("vehicle", "TOYOTA::CAMRY", "upstream failed")
        database.commit()
        with pytest.raises(RuntimeError, match="has crawl failures: count=1"):
            invalid.publish_success_parts(invalid_run_id)
        database.rollback()

        invalid.mark_done("vehicle", "TOYOTA::CAMRY")
        invalid.seen("vehicle", "TOYOTA::CAMRY::pending")
        database.commit()
        with pytest.raises(RuntimeError, match="incomplete crawl state: count=1"):
            invalid.publish_success_parts(invalid_run_id)
        database.rollback()

        invalid.mark_done("vehicle", "TOYOTA::CAMRY::pending")
        database._execute(
            "UPDATE groups_t SET url = %s WHERE id = %s",
            ("https://example.com/en/catalog/genuine/unit?uid=10001", group_id),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="failed source/field quality gate"):
            invalid.publish_success_parts(invalid_run_id)
        database.rollback()

        database._execute(
            "UPDATE groups_t SET url = %s WHERE id = %s",
            ("https://partsouq.com/en/catalog/genuine/unit?uid=10001", group_id),
        )
        database._execute(
            "UPDATE parts SET part_from = NULL, part_to = '2017-12' WHERE seen_run_id = %s",
            (invalid_run_id,),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="failed source/field quality gate"):
            invalid.publish_success_parts(invalid_run_id)
        database.rollback()

        database._execute(
            "UPDATE parts SET part_to = NULL, code = '' WHERE seen_run_id = %s",
            (invalid_run_id,),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="failed source/field quality gate"):
            invalid.publish_success_parts(invalid_run_id)
        database.rollback()
        assert (
            database._execute("SELECT part_number, part_name, code FROM published_parts").fetchone()
            == expected_snapshot
        )

        failed_replacement_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        failed_replacement = CrawlRepository(database, "full-publish-replacement-failed")
        failed_replacement_run_id = failed_replacement.start_run(
            "full-publish-replacement-failed",
            fresh=True,
            scheduled_job_run_id=failed_replacement_job_id,
        )
        parts.upsert_parts(
            group_id,
            [{**_parts(1)[0], "name": "FAILED REPLACEMENT"}],
            failed_replacement_run_id,
        )
        assert failed_replacement.publish_success_parts(failed_replacement_run_id) == 1
        failed_replacement.finish_run(
            failed_replacement_run_id,
            "success",
            {"parts": 1},
        )
        database.commit()

        assert database._execute(
            "SELECT crawl_run_id, part_name FROM published_parts_previous"
        ).fetchone() == {
            "crawl_run_id": first_run_id,
            "part_name": expected_snapshot["part_name"],
        }
        assert database._execute(
            "SELECT source_crawl_run_id, part_name FROM v_current_catalog_parts"
        ).fetchone() == {
            "source_crawl_run_id": first_run_id,
            "part_name": expected_snapshot["part_name"],
        }
        assert database._execute("SELECT part_name FROM v_parts").fetchone() == {
            "part_name": expected_snapshot["part_name"]
        }

        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 1, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (failed_replacement_job_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT source_crawl_run_id FROM v_current_catalog_parts"
        ).fetchone() == {"source_crawl_run_id": first_run_id}

        resumed_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        resumed = CrawlRepository(database, "full-publish-resumed-count")
        resumed_run_id = resumed.start_run(
            "full-publish-resumed-count",
            fresh=True,
            scheduled_job_run_id=resumed_job_id,
        )
        parts.upsert_parts(
            group_id,
            [{**_parts(1)[0], "name": "RESUMED SNAPSHOT"}],
            resumed_run_id,
        )
        assert resumed.publish_success_parts(resumed_run_id) == 1
        resumed.finish_run(resumed_run_id, "success", {"parts": 999})
        database.commit()
        assert database._execute(
            "SELECT source_crawl_run_id FROM v_current_catalog_parts"
        ).fetchone() == {"source_crawl_run_id": first_run_id}

        database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (resumed_job_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT source_crawl_run_id, part_name FROM v_current_catalog_parts"
        ).fetchone() == {
            "source_crawl_run_id": resumed_run_id,
            "part_name": "RESUMED SNAPSHOT",
        }
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_quarantine_records_nameless_rows_without_blocking() -> None:
    """「忽略 + 紀錄」政策（使用者決定）：quarantine 列是紀錄、不阻擋
    發布 —— 組照常標 done、進 fetched map；count_quarantined 可供
    運維查詢（resolved_at 填上後不再計入）。"""
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
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
        crawl = CrawlRepository(database, "bounded-mysql-gate")
        run_key = "bounded-mysql-gate"
        database.commit()

        # 組含無名稱列：quarantine 記錄 + 照常標 done（不阻擋發布）
        parts.quarantine_parts(group_id, run_key, [_parts(1)[0]])
        crawl.mark_group_fetched(group_id, run_key, status="done", row_count=1)
        database.commit()
        assert crawl.count_quarantined(run_key) == 1
        assert crawl.fetched_group_map(vehicle_id, run_key) == {("1", "1101", "10001"): 1}
        assert crawl.is_group_fetched(vehicle_id, "1101", "10001", run_key) is True

        # 運維標記處置：resolved_at 填上後 count_quarantined 不再計入
        database._execute(
            "UPDATE part_quarantine SET resolved_at = NOW(), resolution = %s WHERE group_id = %s",
            ("verified removed from site", group_id),
        )
        database.commit()
        assert crawl.count_quarantined(run_key) == 0

        # SOL review P1：同一料號在**後續 run 再次出現**時必須重開
        # 處置狀態 —— resolved_at / resolution 清空、新的 run_key 計入
        # count_quarantined，不能藏在舊的「已處置」紀錄下。
        second_run_key = "bounded-mysql-gate-2"
        parts.quarantine_parts(group_id, second_run_key, [_parts(1)[0]])
        database.commit()
        row = database._execute(
            "SELECT run_key, resolved_at, resolution FROM part_quarantine "
            "WHERE group_id = %s AND part_number = %s",
            (group_id, "P-00000"),
        ).fetchone()
        assert row["run_key"] == second_run_key
        assert row["resolved_at"] is None
        assert row["resolution"] is None
        assert crawl.count_quarantined(second_run_key) == 1
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


def _clear_mysql_fixture(database: Database) -> None:
    for table in (
        "bounded_parts",
        "published_parts_previous",
        "published_parts",
        "partsouq_http_artifacts",
        "partsouq_response_bodies",
        "admin_vehicle_mappings",
        "crawl_state",
        "crawl_runs",
        "scheduled_job_runs",
        "brands",
    ):
        database._execute(f"DELETE FROM {table}")
    database.commit()
