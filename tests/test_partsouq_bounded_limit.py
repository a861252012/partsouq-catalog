import hashlib
import os
import re
import socket
import threading
import time
from concurrent.futures import Future
from contextlib import nullcontext
from datetime import UTC, datetime
from unittest import mock

import pymysql
import pytest

from partsouq_catalog import scheduler
from partsouq_catalog.admission import (
    CATALOG_RUNTIME_SHUTDOWN_SECONDS,
    CatalogRuntimeLease,
    CatalogRuntimeLockBusy,
    acquire_catalog_runtime_lock,
)
from partsouq_catalog.config import CRAWL, DB_CONFIG
from partsouq_catalog.crawler import Crawler, SampleLimitReached
from partsouq_catalog.db import Database
from partsouq_catalog.evidence import (
    PARSER_CONTRACT_VERSION,
    SANITIZER_VERSION,
    CatalogHttpResponse,
    RecordEvidence,
    public_source_url,
    replay_catalog_records,
    restore_sanitized_body,
    sanitize_parser_html,
)
from partsouq_catalog.http_client import NotFoundError
from partsouq_catalog.repositories import (
    BrandRepository,
    CrawlRepository,
    PartRepository,
    VehicleRepository,
)


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL runtime-lock tests",
)
def test_mysql_catalog_runtime_lock_is_shared_across_connections() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    owner_database = Database()
    contender_database = Database()
    owner_connection = owner_database.open_owner_connection()
    contender_connection = contender_database.open_owner_connection()
    try:
        owner_lease = acquire_catalog_runtime_lock(owner_connection)
        with pytest.raises(CatalogRuntimeLockBusy):
            acquire_catalog_runtime_lock(contender_connection)

        owner_lease.close()
        owner_connection.close()
        contender_lease = acquire_catalog_runtime_lock(contender_connection)
        assert contender_lease.lock_name == owner_lease.lock_name
        contender_lease.close()
    finally:
        try:
            if owner_connection.open:
                owner_connection.close()
        finally:
            if contender_connection.open:
                contender_connection.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL runtime-lock tests",
)
def test_mysql_catalog_runtime_lease_fails_closed_after_owner_timeout() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    owner_connection = Database().open_owner_connection()
    contender_connection = Database().open_owner_connection()
    owner_lease = acquire_catalog_runtime_lock(owner_connection)
    contender_lease = None
    try:
        with owner_connection.cursor() as cursor:
            cursor.execute("SET SESSION wait_timeout = 1")
        time.sleep(2)
        contender_lease = acquire_catalog_runtime_lock(contender_connection)

        with pytest.raises(RuntimeError, match="ownership check failed"):
            owner_lease.assert_owned()
    finally:
        owner_lease.close()
        if contender_lease is not None:
            contender_lease.close()
        for connection in (owner_connection, contender_connection):
            try:
                if connection.open:
                    connection.close()
            except Exception:
                pass


def test_catalog_runtime_lease_close_interrupts_blocked_heartbeat() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args) -> None:
            entered.set()
            assert release.wait(timeout=10)
            raise RuntimeError("connection closed")

    class BlockingConnection:
        def __init__(self) -> None:
            self.close_calls = 0
            self.open = True

        def cursor(self) -> BlockingCursor:
            return BlockingCursor()

        def close(self) -> None:
            self.close_calls += 1
            self.open = False
            release.set()

    connection = BlockingConnection()
    lease = CatalogRuntimeLease(
        connection,  # type: ignore[arg-type]
        "runtime-lock",
        17,
        heartbeat_seconds=0.01,
        shutdown_seconds=0.01,
    )
    lease.start()
    assert entered.wait(timeout=10)

    lease.close()

    assert not lease._thread.is_alive()
    assert connection.close_calls == 1
    assert not connection.open


def test_catalog_runtime_lease_read_timeout_bounds_socket_heartbeat() -> None:
    entered = threading.Event()
    client, server = socket.socketpair()
    client.settimeout(0.05)

    class SocketCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args) -> None:
            entered.set()
            client.recv(1)

    class SocketConnection:
        open = True
        close_calls = 0

        def cursor(self) -> SocketCursor:
            return SocketCursor()

        def close(self) -> None:
            self.close_calls += 1
            self.open = False
            client.close()

    connection = SocketConnection()
    lease = CatalogRuntimeLease(
        connection,  # type: ignore[arg-type]
        "runtime-lock",
        18,
        heartbeat_seconds=0.001,
        shutdown_seconds=0.5,
    )
    try:
        lease.start()
        assert entered.wait(timeout=10)

        lease.close()

        assert not lease._thread.is_alive()
        assert connection.close_calls == 0
    finally:
        client.close()
        server.close()


def test_owner_connection_read_timeout_fits_lease_shutdown_budget(monkeypatch) -> None:
    connect = mock.MagicMock(return_value=mock.MagicMock())
    monkeypatch.setattr("partsouq_catalog.db.pymysql.connect", connect)

    Database().open_owner_connection()

    assert connect.call_args.kwargs["read_timeout"] <= CATALOG_RUNTIME_SHUTDOWN_SECONDS


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


def _parts_html(count: int, part_name: str | None = None) -> str:
    # 真實 unit 頁會渲染所屬 uid（身分斷言依據）；fixture 同步含
    # uid=10001（與 _group() 一致），模擬 genuine 頁面。
    rows = "".join(
        "<tr>"
        f'<td><a href="/en/search/all?q=P-{index:05d}">P-{index:05d}</a></td>'
        f"<td>{part_name or f'Part {index}'}</td><td>11000</td><td></td><td>01</td><td></td>"
        "</tr>"
        for index in range(count)
    )
    return f'<input type="hidden" name="uid" value="10001"><table><tbody>{rows}</tbody></table>'


def _group() -> dict:
    return {
        "category_name": "ENGINE/FUEL/TOOL",
        "cid": "1",
        "group_code": "1101",
        "group_name": "PARTIAL ENGINE ASSEMBLY",
        "uid": "10001",
        "url": "/en/catalog/genuine/unit?uid=10001",
    }


def _catalog_response(
    html: str,
    *,
    status_code: int = 200,
    url: str = "https://partsouq.com/en/catalog/genuine/unit?uid=10001&ssd=secret",
) -> CatalogHttpResponse:
    return CatalogHttpResponse(
        final_url=url,
        status_code=status_code,
        content_type="text/html",
        raw_body_sha256=hashlib.sha256(html.encode()).hexdigest(),
        text=html,
        fetched_at=datetime.now(UTC).replace(tzinfo=None),
        elapsed_ms=25,
        attempt=1,
    )


def _record_verified_live_evidence(
    database: Database,
    repository: CrawlRepository,
    *,
    run_id: int,
    scheduled_job_run_id: int,
    page_types: frozenset[str] | None = None,
    verify: bool = True,
    vehicle_engine: str | None = None,
    vehicle_trim_name: str | None = None,
    unit_part_count: int = 10_000,
    unit_part_name: str | None = None,
    record_group_receipt: bool = True,
) -> None:
    vehicle_key = {
        "brand": "TOYOTA",
        "model": "CAMRY",
        "name": "CAMRY",
        "model_code": "AXVA70",
        "prod_period": "01.2018 - 12.2020",
        "production_from": "2018-01",
        "production_to": "2020-12",
        # parse_vehicles() 對已存在但空白的 Engine／Grade 欄位會保留空字串。
        # fixture 的下游 category／unit evidence 必須與 pick 頁產生相同 natural key。
        "engine": vehicle_engine or "",
        "trim_name": vehicle_trim_name or "",
        "vid": "SITE-VID-1",
    }
    unit_html = _parts_html(unit_part_count, unit_part_name)
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
            "<th class='__prodPeriod'>Prod Period</th><th class='__engine'>Engine</th>"
            "<th class='__grade'>Grade</th></tr><tr>"
            "<td><a href='/en/catalog/genuine/vehicle?c=TOYOTA&ssd=token&vid=SITE-VID-1'>"
            "CAMRY</a></td><td>AXVA70</td><td>01.2018 - 12.2020</td>"
            f"<td>{vehicle_engine or ''}</td><td>{vehicle_trim_name or ''}</td></tr></table>",
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
            assert len(records) == unit_part_count
            accepted_records = [
                (int(row["id"]), record) for row, record in zip(part_rows, records, strict=False)
            ]
        artifact_id = repository.record_http_evidence(
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
        if page_type == "unit" and record_group_receipt:
            source_groups = database._execute(
                "SELECT group_id, COUNT(*) AS accepted_part_count "
                "FROM parts WHERE seen_run_id = %s GROUP BY group_id",
                (run_id,),
            ).fetchall()
            assert len(source_groups) == 1
            source_group = source_groups[0]
            accepted_part_count = int(source_group["accepted_part_count"])
            assert accepted_part_count == len(accepted_records)
            repository.record_bounded_group_receipt(
                run_id,
                int(source_group["group_id"]),
                artifact_id,
                status="done" if accepted_part_count == len(records) else "partial",
                parsed_part_count=len(records),
                accepted_part_count=accepted_part_count,
                skipped_record_count=skipped_rows,
            )
    if verify:
        repository.verify_run_evidence(run_id)


def _bounded_config(monkeypatch, target: int = 10) -> None:
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", target)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "bounded-resume-test")
    monkeypatch.setitem(CRAWL, "bounded_brand", "")
    monkeypatch.setitem(CRAWL, "bounded_model", "")
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 77)
    monkeypatch.setitem(CRAWL, "min_brands", 1)
    for key in ("start_brand", "limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, "" if key == "start_brand" else 0)


def _set_desired_bounded_scope(
    database: Database,
    brand: str,
    model: str,
    vehicle_year_floor: int,
) -> None:
    """模擬 scheduler 在 scoped bounded run 前已完成 scope activation。"""
    database._execute(
        "INSERT INTO catalog_desired_bounded_scope "
        "(singleton_id, scope_brand, scope_model, scope_vehicle_year_floor, updated_at) "
        "VALUES (1, %s, %s, %s, UTC_TIMESTAMP(6)) AS new "
        "ON DUPLICATE KEY UPDATE scope_brand = new.scope_brand, "
        "scope_model = new.scope_model, "
        "scope_vehicle_year_floor = new.scope_vehicle_year_floor, "
        "updated_at = new.updated_at",
        (brand.casefold(), model.casefold(), vehicle_year_floor),
    )
    database.commit()


def test_model_scope_requires_brand_model_pair_and_formal_target(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    monkeypatch.setitem(CRAWL, "bounded_brand", "TOYOTA")
    monkeypatch.setitem(CRAWL, "vehicle_year_window", 20)

    with pytest.raises(ValueError, match="must be set together"):
        Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)

    monkeypatch.setitem(CRAWL, "bounded_model", "TACOMA")
    monkeypatch.setitem(CRAWL, "bounded_parts", 9_999)
    with pytest.raises(ValueError, match="PSQ_BOUNDED_PARTS=10000"):
        Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)


def test_model_scope_allows_open_ended_vehicle_and_skips_known_old_vehicle(
    monkeypatch,
) -> None:
    _bounded_config(monkeypatch, target=10_000)
    monkeypatch.setitem(CRAWL, "bounded_brand", "TOYOTA")
    monkeypatch.setitem(CRAWL, "bounded_model", "TACOMA")
    monkeypatch.setitem(CRAWL, "vehicle_year_window", 20)
    monkeypatch.setattr("partsouq_catalog.crawler._vehicle_year_window_floor", lambda: 2006)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.brands = mock.MagicMock()
    instance.brands.upsert_model.return_value = 31
    instance.crawl = mock.MagicMock()
    instance.crawl.is_done.return_value = False
    instance._fetch = mock.MagicMock(return_value=("<html>pick</html>", None))
    open_ended = {
        "name": "TACOMA",
        "model_code": "GRN305",
        "prod_period": "08.2015 -",
        "production_from": "2015-08",
        "production_to": None,
        "vid": "OPEN",
        "ssd": "OPEN-SSD",
    }
    known_old = {
        "name": "TACOMA",
        "model_code": "VZN150",
        "prod_period": "01.2000 - 12.2005",
        "production_from": "2000-01",
        "production_to": "2005-12",
        "vid": "OLD",
        "ssd": "OLD-SSD",
    }
    instance.crawl_vehicle = mock.MagicMock()

    with mock.patch(
        "partsouq_catalog.crawler.parse_vehicles",
        return_value=([open_ended, known_old], 0),
    ):
        try:
            result = instance.crawl_model(
                "TOYOTA",
                17,
                {"name": "TACOMA", "ssd": "MODEL-SSD", "url": "/pick"},
            )
        finally:
            instance.close()

    assert result == (0, True)
    instance.crawl_vehicle.assert_called_once_with("TOYOTA", 31, open_ended, "TACOMA")


def test_crawl_model_commits_pick_evidence_before_vehicle_worker(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    events: list[str] = []
    database = mock.MagicMock()
    database.commit.side_effect = lambda: events.append("commit")
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.brands = mock.MagicMock()
    instance.brands.upsert_model.return_value = 31
    instance.crawl = mock.MagicMock()
    instance.crawl.is_done.return_value = False
    instance._fetch = mock.MagicMock(return_value=("<html>pick</html>", object()))
    instance._capture_http_evidence = mock.MagicMock(
        side_effect=lambda *args, **kwargs: events.append("pick-evidence")
    )
    vehicle = {
        "name": "CAMRY",
        "model_code": "AXVA70",
        "prod_period": "01.2018 - 12.2020",
        "production_from": "2018-01",
        "production_to": "2020-12",
        "vid": "SITE-VID-1",
        "ssd": "VEHICLE-SSD",
    }

    def crawl_vehicle(*_args) -> None:
        assert events[-1] == "commit"

    instance.crawl_vehicle = mock.MagicMock(side_effect=crawl_vehicle)
    with mock.patch(
        "partsouq_catalog.crawler.parse_vehicles",
        return_value=([vehicle], 0),
    ):
        try:
            result = instance.crawl_model(
                "TOYOTA",
                17,
                {"name": "CAMRY", "ssd": "MODEL-SSD", "url": "/pick"},
            )
        finally:
            instance.close()

    assert result == (0, True)
    assert events == ["commit", "pick-evidence", "commit", "commit", "commit"]
    instance.crawl.mark_error.assert_not_called()


@pytest.mark.parametrize("limit_setting", ("bounded_parts", "limit_parts"))
def test_limited_crawl_does_not_start_a_second_vehicle_after_limit(
    monkeypatch,
    limit_setting: str,
) -> None:
    _bounded_config(monkeypatch, target=0)
    monkeypatch.setitem(CRAWL, limit_setting, 10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=4)
    instance._pool.shutdown(wait=True)

    class ImmediateExecutor:
        def submit(self, function, *args):
            future: Future[None] = Future()
            try:
                future.set_result(function(*args))
            except BaseException as error:
                future.set_exception(error)
            return future

        def shutdown(self, *_args, **_kwargs) -> None:
            pass

    instance._pool = ImmediateExecutor()  # type: ignore[assignment]
    instance.brands = mock.MagicMock()
    instance.brands.upsert_model.return_value = 31
    instance.crawl = mock.MagicMock()
    instance.crawl.is_done.return_value = False
    instance._fetch = mock.MagicMock(return_value=("<html>pick</html>", None))
    vehicles = [
        {
            "name": f"CAMRY-{index}",
            "model_code": f"AXVA7{index}",
            "prod_period": "01.2018 - 12.2020",
            "vid": f"SITE-VID-{index}",
            "ssd": f"VEHICLE-SSD-{index}",
        }
        for index in range(2)
    ]
    started: list[str] = []

    def crawl_vehicle(_brand, _model_id, vehicle, _model_name) -> None:
        started.append(vehicle["name"])
        instance._sample_limit_reached.set()
        raise SampleLimitReached

    instance.crawl_vehicle = mock.MagicMock(side_effect=crawl_vehicle)
    with mock.patch(
        "partsouq_catalog.crawler.parse_vehicles",
        return_value=(vehicles, 0),
    ):
        try:
            with pytest.raises(SampleLimitReached):
                instance.crawl_model(
                    "TOYOTA",
                    17,
                    {"name": "CAMRY", "ssd": "MODEL-SSD", "url": "/pick"},
                )
        finally:
            instance.close()

    assert started == ["CAMRY-0"]
    instance.crawl.mark_error.assert_not_called()


def test_crawl_model_rolls_back_failed_vehicle_worker(monkeypatch) -> None:
    _bounded_config(monkeypatch)
    database = mock.MagicMock()
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.brands = mock.MagicMock()
    instance.brands.upsert_model.return_value = 31
    instance.crawl = mock.MagicMock()
    instance.crawl.is_done.return_value = False
    instance._fetch = mock.MagicMock(return_value=("<html>pick</html>", None))
    instance.crawl_vehicle = mock.MagicMock(side_effect=RuntimeError("vehicle failed"))
    vehicle = {
        "name": "CAMRY",
        "model_code": "AXVA70",
        "prod_period": "01.2018 - 12.2020",
        "vid": "SITE-VID-1",
        "ssd": "VEHICLE-SSD",
    }
    with mock.patch(
        "partsouq_catalog.crawler.parse_vehicles",
        return_value=([vehicle], 0),
    ):
        try:
            result = instance.crawl_model(
                "TOYOTA",
                17,
                {"name": "CAMRY", "ssd": "MODEL-SSD", "url": "/pick"},
            )
        finally:
            instance.close()

    assert result == (1, True)
    database.rollback.assert_called_once_with()
    instance.crawl.mark_error.assert_called_once()


def test_crawl_model_rolls_back_failed_late_state_commit(monkeypatch) -> None:
    _bounded_config(monkeypatch)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    thread_state = threading.local()
    release_late_vehicle = threading.Event()
    late_vehicle_started = threading.Event()
    late_rollback = threading.Event()
    database = mock.MagicMock()

    def commit() -> None:
        if not getattr(thread_state, "late_vehicle", False):
            return
        thread_state.commit_count = getattr(thread_state, "commit_count", 0) + 1
        if thread_state.commit_count == 2:
            raise RuntimeError("late state commit failed")

    def rollback() -> None:
        if getattr(thread_state, "late_vehicle", False):
            thread_state.dirty = False
            late_rollback.set()

    database.commit.side_effect = commit
    database.rollback.side_effect = rollback
    instance = Crawler(mock.MagicMock(), database, workers=4)
    instance.brands = mock.MagicMock()
    instance.brands.upsert_model.return_value = 31
    instance.crawl = mock.MagicMock()
    instance.crawl.is_done.return_value = False
    instance._fetch = mock.MagicMock(return_value=("<html>pick</html>", None))
    vehicles = [
        {
            "name": f"CAMRY-{index}",
            "model_code": f"AXVA7{index}",
            "prod_period": "01.2018 - 12.2020",
            "vid": f"SITE-VID-{index}",
            "ssd": f"VEHICLE-SSD-{index}",
        }
        for index in range(4)
    ]

    def crawl_vehicle(_brand, _model_id, vehicle, _model_name) -> None:
        if vehicle["name"] != "CAMRY-3":
            assert late_vehicle_started.wait(timeout=3)
            raise RuntimeError("vehicle failed")
        thread_state.late_vehicle = True
        thread_state.dirty = True
        late_vehicle_started.set()
        assert release_late_vehicle.wait(timeout=3)

    instance.crawl_vehicle = mock.MagicMock(side_effect=crawl_vehicle)
    release_timer = threading.Timer(1.0, release_late_vehicle.set)
    release_timer.start()
    try:
        with mock.patch(
            "partsouq_catalog.crawler.parse_vehicles",
            return_value=(vehicles, 0),
        ):
            result = instance.crawl_model(
                "TOYOTA",
                17,
                {"name": "CAMRY", "ssd": "MODEL-SSD", "url": "/pick"},
            )
        assert late_rollback.wait(timeout=3)
    finally:
        release_late_vehicle.set()
        release_timer.cancel()
        instance.close()

    assert result == (3, True)
    assert late_rollback.is_set()


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
        scope_brand=None,
        scope_model=None,
        scope_vehicle_year_floor=None,
    )
    assert instance.crawl.start_run.call_args.kwargs == {
        "fresh": False,
        "dataset_kind": "bounded",
        "target_parts": 10,
        "scheduled_job_run_id": 77,
        "scope_brand": None,
        "scope_model": None,
        "scope_vehicle_year_floor": None,
    }
    instance.crawl.publish_bounded_parts.assert_called_once_with(17, 10)
    instance.crawl.archive_full_candidate_parts.assert_not_called()
    assert instance.crawl.finish_run.call_args.args[:3] == (17, "bounded_success", counts)


def test_scoped_bounded_run_accepts_current_17_brand_index(monkeypatch) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    _bounded_config(monkeypatch, target=10_000)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")
    monkeypatch.setitem(CRAWL, "bounded_brand", "TOYOTA")
    monkeypatch.setitem(CRAWL, "bounded_model", "TACOMA")
    monkeypatch.setitem(CRAWL, "vehicle_year_window", 20)
    monkeypatch.setitem(CRAWL, "min_brands", 15)
    monkeypatch.setattr("partsouq_catalog.crawler._vehicle_year_window_floor", lambda: 2006)

    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.run_status.return_value = "running"
    instance.crawl.count_run_parts.return_value = 0
    instance.crawl.discard_invalid_bounded_membership.return_value = 0
    instance.crawl.resumable_bounded_run_key.return_value = None
    instance.crawl.purge_legacy_vehicle_state.return_value = 0
    instance.crawl.count_failures.return_value = 0
    instance.crawl.count_quarantined.return_value = 0
    brands = [{"name": "TOYOTA"}, *({"name": f"BRAND-{index:02d}"} for index in range(16))]
    instance._brands = mock.MagicMock(return_value=brands)

    def reach_target(brand: str) -> int:
        assert brand == "TOYOTA"
        instance.counts["parts"] = 10_000
        return 0

    instance.crawl_brand = mock.MagicMock(side_effect=reach_target)
    try:
        counts = instance.run()
    finally:
        instance.close()

    assert counts["parts"] == 10_000
    assert instance.last_status == "bounded_success"
    instance.crawl_brand.assert_called_once_with("TOYOTA")
    instance.crawl.publish_bounded_parts.assert_called_once_with(17, 10_000)


def test_closed_under_target_bounded_run_is_rejected_and_next_attempt_rewalks(
    monkeypatch,
) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    _bounded_config(monkeypatch)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")

    first = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    first.brands = mock.MagicMock()
    first.crawl = mock.MagicMock()
    first.crawl.start_run.return_value = 17
    first.crawl.run_status.return_value = "running"
    first.crawl.count_run_parts.return_value = 0
    first.crawl.discard_invalid_bounded_membership.return_value = 0
    first.crawl.resumable_bounded_run_key.return_value = None
    first.crawl.purge_legacy_vehicle_state.return_value = 0
    first.crawl.remaining_group_count.return_value = 0
    first.crawl.count_failures.return_value = 0
    first._brands = mock.MagicMock(return_value=[{"name": "TOYOTA"}])

    def finish_under_target(_brand: str) -> int:
        first.counts["parts"] = 5
        return 0

    first.crawl_brand = mock.MagicMock(side_effect=finish_under_target)
    try:
        first.run()
    finally:
        first.close()

    first_run_key = first.crawl.start_run.call_args.args[0]
    first.crawl.reject_run_evidence.assert_called_once_with(17)
    first.crawl.finish_run.assert_called_once_with(
        17,
        "error",
        first.counts,
        "bounded run did not reach exact target: 5/10",
    )
    assert first.last_status == "bounded_under_target"
    assert first.crawl.method_calls.index(mock.call.reject_run_evidence(17)) < (
        first.crawl.method_calls.index(
            mock.call.finish_run(
                17,
                "error",
                first.counts,
                "bounded run did not reach exact target: 5/10",
            )
        )
    )

    time.sleep(0.002)
    second = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    second.brands = mock.MagicMock()
    second.crawl = mock.MagicMock()
    second.crawl.start_run.return_value = 18
    second.crawl.run_status.return_value = "running"
    second.crawl.count_run_parts.return_value = 0
    second.crawl.discard_invalid_bounded_membership.return_value = 0
    second.crawl.resumable_bounded_run_key.return_value = None
    second.crawl.purge_legacy_vehicle_state.return_value = 0
    second.crawl.remaining_group_count.return_value = 0
    second.crawl.count_failures.return_value = 0
    second.crawl.count_quarantined.return_value = 0
    second._brands = mock.MagicMock(return_value=[{"name": "TOYOTA"}])

    def reach_target(_brand: str) -> int:
        second.counts["parts"] = 10
        return 0

    second.crawl_brand = mock.MagicMock(side_effect=reach_target)
    try:
        second.run()
    finally:
        second.close()

    second_run_key = second.crawl.start_run.call_args.args[0]
    assert second_run_key != first_run_key
    second.crawl_brand.assert_called_once_with("TOYOTA")
    second.crawl.publish_bounded_parts.assert_called_once_with(18, 10)


def test_formal_bounded_discard_rejects_logical_run_before_more_http(monkeypatch) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    _bounded_config(monkeypatch, target=10_000)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.start_run.return_value = 17
    instance.crawl.run_status.return_value = "running"
    instance.crawl.resumable_bounded_run_key.return_value = "bounded-resume"
    instance.crawl.count_run_parts.side_effect = (10_000, 9_999)
    instance.crawl.discard_invalid_bounded_membership.return_value = 1

    try:
        counts = instance.run()
    finally:
        instance.close()

    assert counts["parts"] == 9_999
    assert instance.last_status == "bounded_under_target"
    instance.crawl.reject_run_evidence.assert_called_once_with(17)
    instance.crawl.finish_run.assert_called_once_with(
        17,
        "error",
        counts,
        "bounded run discarded 1 invalid membership row(s); a new logical run is required",
    )
    instance.http.ensure_fresh.assert_not_called()
    instance.crawl.publish_bounded_parts.assert_not_called()


def test_bounded_resume_failure_closes_durable_running_marker(monkeypatch) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    _bounded_config(monkeypatch)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")
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
    assert events == ["commit", "commit", "rollback", "finish-error", "commit"]
    instance.crawl.finish_run.assert_called_once_with(
        17,
        "error",
        instance.counts,
        "resume read failed",
    )


def test_incompatible_resume_rejection_commits_before_new_marker(monkeypatch) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    _bounded_config(monkeypatch)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")
    events: list[str] = []
    database = mock.MagicMock()
    database.commit.side_effect = lambda: events.append("commit-rejection")
    database.rollback.side_effect = lambda: events.append("rollback-new-marker")
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.crawl = mock.MagicMock()
    instance.crawl.resumable_bounded_run_key.side_effect = lambda *_args, **_kwargs: (
        events.append("reject-old-run") or None
    )

    def fail_new_marker(*_args, **_kwargs) -> None:
        assert events[-1] == "commit-rejection"
        events.append("start-new-marker")
        raise RuntimeError("new marker failed")

    instance.crawl.start_run.side_effect = fail_new_marker
    try:
        with pytest.raises(RuntimeError, match="new marker failed"):
            instance.run()
    finally:
        instance.close()

    assert events == [
        "reject-old-run",
        "commit-rejection",
        "start-new-marker",
        "rollback-new-marker",
    ]


@pytest.mark.parametrize(
    "raw_url",
    (
        "/en/catalog/genuine/unit?uid=10001",
        "//partsouq.com/en/catalog/genuine/unit?uid=10001",
        "http://partsouq.com/en/catalog/genuine/unit/?uid=10001",
    ),
)
def test_bounded_partial_group_retry_excludes_already_seen_keys(monkeypatch, raw_url: str) -> None:
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
    group = _group()
    group["url"] = raw_url
    try:
        truncated = instance.crawl_group("TOYOTA", 7, group, fetched={})
    finally:
        instance.close()

    assert truncated is False
    assert instance.counts["parts"] == 10
    written = instance.parts.upsert_parts.call_args
    assert [row["part_number"] for row in written.args[1]] == ["P-00008", "P-00009"]
    assert written.kwargs == {"complete_group": True}
    assert instance.vehicles.upsert_group.call_args.args[4] == (
        "https://partsouq.com/en/catalog/genuine/unit?uid=10001"
    )
    instance._get.assert_called_once_with("https://partsouq.com/en/catalog/genuine/unit?uid=10001")
    instance.crawl.mark_group_fetched.assert_called_once_with(
        41,
        "bounded-resume-test",
        status="done",
        row_count=10,
    )


def test_bounded_all_seen_group_reuses_membership_and_records_receipt(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    accepted_parts = _parts(10)
    instance.parts.bounded_group_context.return_value = {
        "production_from": "2018-01",
        "production_to": "2020-12",
        "source_valid": 1,
    }
    instance.parts.seen_keys_in_group.return_value = {
        (part["part_number"], part["range_str"]) for part in accepted_parts
    }
    instance.parts.part_ids_for_evidence.return_value = list(enumerate(accepted_parts, start=1))
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance.crawl.previous_row_count.return_value = 0
    response = _catalog_response(_parts_html(10))
    instance._fetch = mock.MagicMock(return_value=(response.text, response))
    events: list[str] = []
    instance._capture_http_evidence = mock.MagicMock(
        side_effect=lambda *_args, **_kwargs: events.append("artifact") or 901
    )
    instance.crawl.record_bounded_group_receipt.side_effect = lambda *_args, **_kwargs: (
        events.append("receipt")
    )
    instance.crawl.mark_group_fetched.side_effect = lambda *_args, **_kwargs: events.append("done")
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "CAMRY",
        "name": "CAMRY",
        "model_code": "AXVA70",
        "vid": "SITE-VID-1",
    }
    try:
        assert (
            instance.crawl_group(
                "TOYOTA",
                7,
                _group(),
                fetched={},
                evidence_vehicle_key=evidence_vehicle_key,
            )
            is False
        )
    finally:
        instance.close()

    instance.parts.upsert_parts.assert_not_called()
    instance.parts.part_ids_for_evidence.assert_called_once_with(41, accepted_parts)
    instance._capture_http_evidence.assert_called_once()
    instance.crawl.record_bounded_group_receipt.assert_called_once_with(
        17,
        41,
        901,
        status="done",
        parsed_part_count=10,
        accepted_part_count=10,
        skipped_record_count=0,
    )
    instance.crawl.mark_group_fetched.assert_called_once_with(
        41,
        "bounded-resume-test",
        status="done",
        row_count=10,
    )
    assert events == ["artifact", "receipt", "done"]


def test_bounded_all_seen_group_quarantines_quality_rejected_named_parts(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    valid, rejected = _parts(2)
    rejected["code"] = ""
    instance.parts = mock.MagicMock()
    instance.parts.bounded_group_context.return_value = {
        "production_from": "2018-01",
        "production_to": "2020-12",
        "source_valid": 1,
    }
    instance.parts.seen_keys_in_group.return_value = {(valid["part_number"], valid["range_str"])}
    instance.parts.part_ids_for_evidence.return_value = [(1, valid)]
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance.crawl.previous_row_count.return_value = 0
    response = _catalog_response(_parts_html(2))
    instance._fetch = mock.MagicMock(return_value=(response.text, response))
    instance._capture_http_evidence = mock.MagicMock(return_value=901)
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "CAMRY",
        "name": "CAMRY",
        "model_code": "AXVA70",
        "vid": "SITE-VID-1",
    }

    with mock.patch(
        "partsouq_catalog.crawler.parse_parts", return_value=([valid, rejected], 0, 0, [])
    ):
        try:
            assert (
                instance.crawl_group(
                    "TOYOTA",
                    7,
                    _group(),
                    fetched={},
                    evidence_vehicle_key=evidence_vehicle_key,
                )
                is False
            )
        finally:
            instance.close()

    instance.parts.upsert_parts.assert_not_called()
    instance.crawl.record_bounded_group_receipt.assert_called_once_with(
        17,
        41,
        901,
        status="partial",
        parsed_part_count=2,
        accepted_part_count=1,
        skipped_record_count=0,
    )
    instance.parts.quarantine_parts.assert_called_once_with(
        41,
        "bounded-resume-test",
        [rejected],
        reason="bounded_quality_gate",
    )


def test_bounded_quality_gate_quarantines_all_excluded_named_parts(monkeypatch) -> None:
    _bounded_config(monkeypatch)
    database = mock.MagicMock()
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.parts.bounded_group_context.return_value = {
        "production_from": "2018-01",
        "production_to": "2020-12",
        "source_valid": 0,
    }
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance.crawl.previous_row_count.return_value = 0
    instance._get = mock.MagicMock(return_value=_parts_html(2))

    try:
        with pytest.raises(RuntimeError, match="all named part rows failed"):
            instance.crawl_group("TOYOTA", 7, _group(), fetched={})
    finally:
        instance.close()

    instance.parts.quarantine_parts.assert_called_once_with(
        41,
        "bounded-resume-test",
        _parts(2),
        reason="bounded_quality_gate",
    )
    instance.parts.upsert_parts.assert_not_called()
    instance.crawl.mark_group_fetched.assert_not_called()
    assert database.commit.call_count == 2


def test_bounded_quality_gate_quarantines_mixed_rejected_named_parts(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    valid, rejected = _parts(2)
    rejected["code"] = ""
    instance.parts = mock.MagicMock()
    instance.parts.bounded_group_context.return_value = {
        "production_from": "2018-01",
        "production_to": "2020-12",
        "source_valid": 1,
    }
    instance.parts.seen_keys_in_group.return_value = set()
    instance.parts.part_ids_for_evidence.side_effect = lambda _group_id, records: list(
        enumerate(records, start=1)
    )
    instance.parts.upsert_parts.return_value = 1
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance.crawl.previous_row_count.return_value = 0
    response = _catalog_response(_parts_html(2))
    instance._fetch = mock.MagicMock(return_value=(response.text, response))
    instance._capture_http_evidence = mock.MagicMock(return_value=901)
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "CAMRY",
        "name": "CAMRY",
        "model_code": "AXVA70",
        "vid": "SITE-VID-1",
    }

    with mock.patch(
        "partsouq_catalog.crawler.parse_parts", return_value=([valid, rejected], 0, 0, [])
    ):
        try:
            assert (
                instance.crawl_group(
                    "TOYOTA",
                    7,
                    _group(),
                    fetched={},
                    evidence_vehicle_key=evidence_vehicle_key,
                )
                is False
            )
        finally:
            instance.close()

    instance.parts.upsert_parts.assert_called_once_with(41, [valid], 17, complete_group=True)
    instance.crawl.record_bounded_group_receipt.assert_called_once_with(
        17,
        41,
        901,
        status="partial",
        parsed_part_count=2,
        accepted_part_count=1,
        skipped_record_count=0,
    )
    instance.parts.quarantine_parts.assert_called_once_with(
        41,
        "bounded-resume-test",
        [rejected],
        reason="bounded_quality_gate",
    )
    instance.crawl.mark_group_fetched.assert_called_once_with(
        41,
        "bounded-resume-test",
        status="done",
        row_count=2,
    )


def test_official_error_page_returned_as_http_200_remains_fail_closed(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    monkeypatch.setitem(CRAWL, "block_breather", 0)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    html = (
        "<html><head><title>PartSouq - Error</title></head>"
        "<body><h1>Error 404</h1><p>Invalid parameter</p></body></html>"
    )
    instance._fetch = mock.MagicMock(return_value=(html, _catalog_response(html)))
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "1000",
        "name": "TOYOTA1000",
        "model_code": "KP30-",
        "vid": "SITE-VID-1",
    }
    try:
        with pytest.raises(RuntimeError, match="parsed 0 parts"):
            instance.crawl_group(
                "TOYOTA",
                7,
                _group(),
                fetched={},
                evidence_vehicle_key=evidence_vehicle_key,
            )
    finally:
        instance.close()

    instance.parts.clear_group_membership.assert_not_called()
    instance.crawl.mark_group_fetched.assert_not_called()
    diagnostic = instance.crawl.record_http_diagnostic.call_args
    assert diagnostic.args[:3] == (17, 77, 41)
    assert diagnostic.kwargs["reason"] == "empty_parse"
    assert diagnostic.kwargs["status_code"] == 200
    assert "ssd=" not in diagnostic.kwargs["public_url"]


def test_unrecognized_empty_http_200_page_remains_fail_closed(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    monkeypatch.setitem(CRAWL, "block_breather", 0)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    html = (
        "<html><head><title>PartSouq - Error</title></head>"
        "<body><p>Unexpected response</p></body></html>"
    )
    instance._fetch = mock.MagicMock(return_value=(html, _catalog_response(html)))
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "1000",
        "name": "TOYOTA1000",
        "model_code": "KP30-",
        "vid": "SITE-VID-1",
    }
    try:
        with pytest.raises(RuntimeError, match="parsed 0 parts"):
            instance.crawl_group(
                "TOYOTA",
                7,
                _group(),
                fetched={},
                evidence_vehicle_key=evidence_vehicle_key,
            )
    finally:
        instance.close()

    instance.parts.clear_group_membership.assert_not_called()
    instance.crawl.mark_group_fetched.assert_not_called()
    assert instance.crawl.record_http_diagnostic.call_args.kwargs["reason"] == "empty_parse"


def test_http_404_records_diagnostic_and_marks_only_current_run_not_found(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.counts["parts"] = 3
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.parts.clear_group_membership.return_value = 3
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    html = "<html><title>PartSouq - Error</title><p>Error 404</p></html>"
    response = _catalog_response(html, status_code=404)
    instance._fetch = mock.MagicMock(side_effect=NotFoundError("http 404", response))
    fetched: dict[tuple[str, str, str], int] = {}
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "1000",
        "name": "TOYOTA1000",
        "model_code": "KP30-",
        "vid": "SITE-VID-1",
    }
    try:
        assert (
            instance.crawl_group(
                "TOYOTA",
                7,
                _group(),
                fetched=fetched,
                evidence_vehicle_key=evidence_vehicle_key,
            )
            is False
        )
    finally:
        instance.close()

    diagnostic = instance.crawl.record_http_diagnostic.call_args
    assert diagnostic.args[:3] == (17, 77, 41)
    assert diagnostic.kwargs["reason"] == "http_not_found"
    assert diagnostic.kwargs["status_code"] == 404
    assert "ssd=" not in diagnostic.kwargs["public_url"]
    instance.crawl.supersede_http_evidence.assert_called_once()
    instance.parts.clear_group_membership.assert_called_once_with(41, 17)
    instance.crawl.mark_group_fetched.assert_called_once_with(
        41,
        "bounded-resume-test",
        status="not_found",
    )
    assert instance.counts["parts"] == 0
    assert fetched == {("1", "1101", "10001"): 0}


def test_formal_not_found_without_response_envelope_remains_fail_closed(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance._fetch = mock.MagicMock(side_effect=NotFoundError("http 404"))
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "1000",
        "name": "TOYOTA1000",
        "model_code": "KP30-",
        "vid": "SITE-VID-1",
    }
    try:
        with pytest.raises(RuntimeError, match="missing its response envelope"):
            instance.crawl_group(
                "TOYOTA",
                7,
                _group(),
                fetched={},
                evidence_vehicle_key=evidence_vehicle_key,
            )
    finally:
        instance.close()

    instance.crawl.record_http_diagnostic.assert_not_called()
    instance.parts.clear_group_membership.assert_not_called()
    instance.crawl.mark_group_fetched.assert_not_called()


def test_http_diagnostic_rejects_secret_bearing_content_type() -> None:
    database = mock.MagicMock()
    repository = CrawlRepository(database)

    with pytest.raises(ValueError, match="HTTP metadata"):
        repository.record_http_diagnostic(
            17,
            23,
            41,
            public_url=public_source_url(
                "https://partsouq.com/en/catalog/genuine/unit?uid=10001&ssd=secret"
            ),
            raw_body_sha256="a" * 64,
            status_code=200,
            content_type="text/html ssd=SECRET",
            fetched_at=datetime.now(UTC).replace(tzinfo=None),
            elapsed_ms=1,
            attempt=1,
            sanitized_body=sanitize_parser_html("<html><body>empty</body></html>"),
            parser_name="parse_parts",
            parser_version=PARSER_CONTRACT_VERSION,
            parser_context={},
            reason="empty_parse",
        )

    database._execute.assert_not_called()


def test_formal_not_found_with_mismatched_unit_url_remains_fail_closed(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    html = "<html><p>Error 404</p></html>"
    mismatched = _catalog_response(
        html,
        status_code=404,
        url="https://partsouq.com/en/catalog/genuine/unit?uid=99999&ssd=secret",
    )
    instance._fetch = mock.MagicMock(side_effect=NotFoundError("http 404", mismatched))
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "1000",
        "name": "TOYOTA1000",
        "model_code": "KP30-",
        "vid": "SITE-VID-1",
    }
    try:
        with pytest.raises(RuntimeError, match="does not match its group"):
            instance.crawl_group(
                "TOYOTA",
                7,
                _group(),
                fetched={},
                evidence_vehicle_key=evidence_vehicle_key,
            )
    finally:
        instance.close()

    instance.crawl.record_http_diagnostic.assert_not_called()
    instance.parts.clear_group_membership.assert_not_called()
    instance.crawl.mark_group_fetched.assert_not_called()


def test_http_404_keeps_diagnostic_when_terminal_transaction_rolls_back(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    events: list[str] = []
    database = mock.MagicMock()
    database.commit.side_effect = lambda: events.append("commit")
    database.rollback.side_effect = lambda: events.append("rollback")
    instance = Crawler(mock.MagicMock(), database, workers=1)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "bounded-resume-test"
    instance.crawl.supersede_http_evidence.side_effect = RuntimeError("terminal write failed")
    html = "<html><title>PartSouq - Error</title><p>Error 404</p></html>"
    instance._fetch = mock.MagicMock(
        side_effect=NotFoundError("http 404", _catalog_response(html, status_code=404))
    )
    evidence_vehicle_key = {
        "brand": "TOYOTA",
        "model": "1000",
        "name": "TOYOTA1000",
        "model_code": "KP30-",
        "vid": "SITE-VID-1",
    }
    try:
        with pytest.raises(RuntimeError, match="terminal write failed"):
            instance.crawl_group(
                "TOYOTA",
                7,
                _group(),
                fetched={},
                evidence_vehicle_key=evidence_vehicle_key,
            )
    finally:
        instance.close()

    # group upsert + diagnostic 各自提交；終態交易失敗只 rollback 後半段。
    assert events == ["commit", "commit", "rollback"]
    instance.crawl.record_http_diagnostic.assert_called_once()
    instance.parts.clear_group_membership.assert_not_called()
    instance.crawl.mark_group_fetched.assert_not_called()


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
    instance._get = mock.MagicMock(
        return_value='<input type="hidden" name="uid" value="10001"><table></table>'
    )
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
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")
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


def test_scheduled_bounded_run_rejects_operator_supplied_run_key(monkeypatch) -> None:
    _bounded_config(monkeypatch, target=10_000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.crawl = mock.MagicMock()
    try:
        with pytest.raises(
            ValueError,
            match="PSQ_BOUNDED_RUN_KEY cannot be set for a bounded run",
        ):
            instance.run()
    finally:
        instance.close()

    instance.crawl.start_run.assert_not_called()


def test_direct_bounded_run_rejects_operator_supplied_run_key(monkeypatch) -> None:
    """Direct bounded 也禁止 explicit key：否則會繞過 resolver 的
    exhausted_with_failures／evidence gate 重開已耗盡且有錯誤的 run。"""
    _bounded_config(monkeypatch, target=10)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.crawl = mock.MagicMock()
    try:
        with pytest.raises(
            ValueError,
            match="PSQ_BOUNDED_RUN_KEY cannot be set for a bounded run",
        ):
            instance.run()
    finally:
        instance.close()

    instance.crawl.start_run.assert_not_called()


@pytest.mark.parametrize("run_key", ("", "operator-key"))
def test_formal_bounded_run_requires_scheduler_before_db_writes(monkeypatch, run_key: str) -> None:
    _bounded_config(monkeypatch, target=10_000)
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)
    monkeypatch.setitem(CRAWL, "bounded_run_key", run_key)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.crawl = mock.MagicMock()
    try:
        with pytest.raises(
            ValueError,
            match=r"formal evidence runs \(bounded or full\) require the daemon scheduler",
        ):
            instance.run()
    finally:
        instance.close()

    instance.crawl.start_run.assert_not_called()
    instance.http.ensure_fresh.assert_not_called()


def test_bounded_publish_rejects_non_formal_target_before_query() -> None:
    database = mock.MagicMock()

    with pytest.raises(ValueError, match="exactly 10000"):
        CrawlRepository(database, "bounded-invalid-target").publish_bounded_parts(17, 9_999)

    database._execute.assert_not_called()


def test_scoped_membership_only_rejects_known_production_end_before_floor() -> None:
    database = mock.MagicMock()
    database._execute.return_value.rowcount = 0

    CrawlRepository(database, "scope-membership").discard_invalid_bounded_membership(
        17,
        scope_brand="TOYOTA",
        scope_model="TACOMA",
        scope_vehicle_year_floor=2006,
    )

    query, params = database._execute.call_args.args
    assert "CAST(LOWER(TRIM(brand.name)) AS BINARY) <> CAST(%s AS BINARY)" in query
    assert "CAST(LOWER(TRIM(model.name)) AS BINARY) <> CAST(%s AS BINARY)" in query
    assert "OR vehicle.production_to IS NULL" not in query
    assert (
        "OR (vehicle.production_to IS NOT NULL "
        "AND CAST(LEFT(vehicle.production_to, 4) AS UNSIGNED) < %s)"
    ) in query
    assert params == (17, "toyota", "tacoma", 2006)


@pytest.mark.parametrize(
    ("scoped_count", "reaches_snapshot_mutation"),
    ((10_000, True), (9_999, False)),
)
def test_scoped_publish_allows_open_ended_and_rejects_known_old_rows_before_delete(
    scoped_count: int,
    reaches_snapshot_mutation: bool,
) -> None:
    database = mock.MagicMock()
    run = {
        "run_key": "bounded-model-scope",
        "started_at": datetime.now(UTC).replace(tzinfo=None),
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "status": "running",
        "scope_brand": "TOYOTA",
        "scope_model": "TACOMA",
        "scope_vehicle_year_floor": 2006,
        "scheduled_job_run_id": 77,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "running",
        "scheduled_crawl_count": 1,
    }

    class SnapshotMutationReached(RuntimeError):
        pass

    def execute(sql: str, _params=()):
        cursor = mock.MagicMock()
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT cr.run_key"):
            cursor.fetchone.return_value = run
        elif normalized.startswith("SELECT id FROM crawl_state"):
            cursor.fetchall.return_value = []
        elif normalized == "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s":
            cursor.fetchone.return_value = {"row_count": 10_000}
        elif normalized.startswith("SELECT COUNT(*) AS row_count FROM parts AS part"):
            assert "CAST(LOWER(TRIM(brand.name)) AS BINARY) = CAST(%s AS BINARY)" in normalized
            assert "CAST(LOWER(TRIM(model.name)) AS BINARY) = CAST(%s AS BINARY)" in normalized
            assert "vehicle.production_to IS NULL OR" in normalized
            assert "CAST(LEFT(vehicle.production_to, 4) AS UNSIGNED) >= %s" in normalized
            cursor.fetchone.return_value = {"row_count": scoped_count}
        elif normalized.startswith("SELECT scope_brand, scope_model, scope_vehicle_year_floor"):
            cursor.fetchone.return_value = {
                "scope_brand": "toyota",
                "scope_model": "tacoma",
                "scope_vehicle_year_floor": 2006,
            }
        elif normalized == "DELETE FROM bounded_parts":
            raise SnapshotMutationReached
        else:
            raise AssertionError(f"unexpected scoped publish query: {normalized}")
        return cursor

    database._execute.side_effect = execute
    repository = CrawlRepository(database, "bounded-model-scope")
    repository._assert_verified_run_evidence = mock.MagicMock()  # type: ignore[method-assign]
    repository._assert_bounded_group_receipts = mock.MagicMock()  # type: ignore[method-assign]

    if reaches_snapshot_mutation:
        with pytest.raises(SnapshotMutationReached):
            repository.publish_bounded_parts(17, 10_000)
        repository._assert_verified_run_evidence.assert_called_once()
        repository._assert_bounded_group_receipts.assert_called_once_with(17, 10_000)
    else:
        with pytest.raises(RuntimeError, match="model scope mismatch"):
            repository.publish_bounded_parts(17, 10_000)
        repository._assert_verified_run_evidence.assert_not_called()
        repository._assert_bounded_group_receipts.assert_not_called()

    assert all(
        call.args[0] != "DELETE FROM bounded_parts"
        for call in database._execute.call_args_list[:-1]
    )


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
    assert "BINARY artifact.sanitizer_version <> BINARY %s" in query
    assert "BINARY artifact.verification_status = BINARY 'verified'" in query
    assert "BINARY artifact.verification_status = BINARY 'superseded'" in query
    assert "quota_part.seen_run_id = candidate.id" in query
    assert ">= candidate.target_parts" in query
    assert "failed_scope.run_key = candidate.run_key" in query
    assert "BINARY failed_scope.status = BINARY 'error'" in query
    assert "AS exhausted_with_failures" in query
    assert "ORDER BY cr.started_at DESC, cr.id DESC LIMIT 1) AS candidate" in query
    assert "receipt_group.fetched_run_key = candidate.run_key" in query
    assert "receipt_group.fetched_status" in query
    assert "REGEXP_LIKE(receipt_group.url" in query
    assert "AS bad_bounded_receipt" in query
    assert "bounded_group_receipts AS bounded_receipt" in query
    assert "membership_part.seen_run_id = candidate.id" in query
    assert "membership_receipt.group_id = membership_part.group_id" in query
    assert "receipt_part.seen_run_id = candidate.id" in query
    assert params == (SANITIZER_VERSION, 10_000, None, None, None, 77)

    database.reset_mock()
    database._execute.return_value.fetchone.return_value = None
    assert (
        CrawlRepository(database, "bounded-direct-resume-contract").resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=None,
        )
        is None
    )
    query, params = database._execute.call_args.args
    assert "ORDER BY started_at DESC, id DESC LIMIT 1) AS candidate" in query
    assert "BINARY artifact.verification_status = BINARY 'verified'" in query
    assert "BINARY artifact.verification_status = BINARY 'superseded'" in query
    assert "quota_part.seen_run_id = candidate.id" in query
    assert ">= candidate.target_parts" in query
    assert "failed_scope.run_key = candidate.run_key" in query
    assert "BINARY failed_scope.status = BINARY 'error'" in query
    assert "AS exhausted_with_failures" in query
    assert "receipt_group.fetched_run_key = candidate.run_key" in query
    assert "receipt_group.fetched_status" in query
    assert "REGEXP_LIKE(receipt_group.url" in query
    assert "AS bad_bounded_receipt" in query
    assert "bounded_group_receipts AS bounded_receipt" in query
    assert "membership_part.seen_run_id = candidate.id" in query
    assert "membership_receipt.group_id = membership_part.group_id" in query
    assert "receipt_part.seen_run_id = candidate.id" in query
    assert params == (SANITIZER_VERSION, 10_000, None, None, None)


def test_incompatible_bounded_resume_is_durably_rejected() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "id": 17,
        "run_key": "bounded-poisoned-receipt",
        "evidence_status": "collecting",
        "bad_evidence": 0,
        "bad_receipt": 1,
    }
    database._execute.return_value = cursor

    assert (
        CrawlRepository(database, "bounded-resume-contract").resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=None,
        )
        is None
    )

    update_query, update_params = database._execute.call_args_list[-1].args
    assert "status = 'error'" in update_query
    assert "finished_at = COALESCE(finished_at, UTC_TIMESTAMP())" in update_query
    assert "SET evidence_status = 'rejected'" in update_query
    assert update_params == (17,)


def test_exact_target_with_missing_bounded_receipt_is_durably_rejected() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "id": 18,
        "run_key": "bounded-missing-receipt",
        "evidence_status": "collecting",
        "bad_run_status": 0,
        "bad_evidence": 0,
        "bad_receipt": 0,
        "bad_bounded_receipt": 1,
        "exhausted_with_failures": 0,
    }
    database._execute.return_value = cursor

    assert (
        CrawlRepository(database, "bounded-resume-contract").resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=None,
        )
        is None
    )

    update_query, update_params = database._execute.call_args_list[-1].args
    assert "SET evidence_status = 'rejected'" in update_query
    assert update_params == (18,)


def test_exhausted_bounded_resume_with_failures_is_durably_rejected() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "id": 18,
        "run_key": "bounded-exhausted-with-error",
        "evidence_status": "collecting",
        "bad_run_status": 0,
        "bad_evidence": 0,
        "bad_receipt": 0,
        "exhausted_with_failures": 1,
    }
    database._execute.return_value = cursor

    assert (
        CrawlRepository(database, "bounded-resume-contract").resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=77,
        )
        is None
    )

    queries = [call.args[0] for call in database._execute.call_args_list]
    assert all("DELETE " not in query for query in queries)
    update_query, update_params = database._execute.call_args_list[-1].args
    assert "SET evidence_status = 'rejected'" in update_query
    assert update_params == (18,)


@pytest.mark.parametrize("scenario", ("under_target_with_error", "exact_target_without_error"))
def test_compatible_bounded_resume_remains_resumable(scenario: str) -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "id": 20,
        "run_key": f"bounded-{scenario}",
        "evidence_status": "collecting",
        "bad_run_status": 0,
        "bad_evidence": 0,
        "bad_receipt": 0,
        "exhausted_with_failures": 0,
    }
    database._execute.return_value = cursor

    assert (
        CrawlRepository(
            database,
            "bounded-resume-contract",
        ).resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=77,
        )
        == f"bounded-{scenario}"
    )
    assert database._execute.call_count == 1


def test_non_exact_rejected_resume_is_normalized_and_clears_stale_seals() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "id": 19,
        "run_key": "bounded-uppercase-rejected",
        "evidence_status": "REJECTED",
        "bad_run_status": 1,
        "bad_evidence": 0,
        "bad_receipt": 0,
    }
    database._execute.return_value = cursor

    assert (
        CrawlRepository(database, "bounded-resume-contract").resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=None,
        )
        is None
    )

    select_query = database._execute.call_args_list[0].args[0]
    assert "AS bad_run_status" in select_query
    update_query, update_params = database._execute.call_args_list[-1].args
    assert "SET evidence_status = 'rejected'" in update_query
    assert "evidence_manifest_sha256 = NULL" in update_query
    assert "evidence_dataset_sha256 = NULL" in update_query
    assert "evidence_verified_at = NULL" in update_query
    assert update_params == (19,)


@pytest.mark.parametrize(
    "raw_url",
    (
        "https://user:password@partsouq.com/en/catalog/genuine/unit?uid=10001",
        "https://example.test/en/catalog/genuine/unit?uid=10001",
        "https://partsouq.com/en/catalog/genuine/pick?uid=10001",
        "https://partsouq.com/en/catalog/genuine/unit",
    ),
)
def test_group_rejects_invalid_unit_url_before_db_or_http(monkeypatch, raw_url: str) -> None:
    _bounded_config(monkeypatch)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.vehicles = mock.MagicMock()
    instance.crawl = mock.MagicMock()
    instance.crawl.is_group_fetched.return_value = False
    instance._get = mock.MagicMock()
    group = _group()
    group["url"] = raw_url
    try:
        with pytest.raises(RuntimeError, match="invalid PartSouq catalog URL"):
            instance.crawl_group("TOYOTA", 7, group)
    finally:
        instance.close()

    instance.vehicles.upsert_category.assert_not_called()
    instance._get.assert_not_called()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_scoped_resume_requires_exact_scope_and_persists_replacement_owner() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        _set_desired_bounded_scope(database, "TOYOTA", "TACOMA", 2006)
        first_job_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        repository = CrawlRepository(database, "scoped-resume")
        run_id = repository.start_run(
            "scoped-resume",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=first_job_id,
            scope_brand="TOYOTA",
            scope_model="TACOMA",
            scope_vehicle_year_floor=2006,
        )
        repository.finish_run(run_id, "interrupted", {"parts": 0}, "retry fixture")
        # 模擬 029 上線前由 ai_ci schema 留下的大小寫；resolver 與
        # start_run 必須把它視為同一 scope，接手後再寫回 canonical 值。
        database._execute(
            "UPDATE crawl_runs SET scope_brand='TOYOTA',scope_model='TACOMA' WHERE id=%s",
            (run_id,),
        )
        database._execute(
            "UPDATE scheduled_job_runs SET status='failed',exit_code=1,"
            "finished_at=UTC_TIMESTAMP() WHERE id=%s",
            (first_job_id,),
        )
        next_job_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        database.commit()

        assert (
            repository.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=next_job_id,
                scope_brand="LEXUS",
                scope_model="TACOMA",
                scope_vehicle_year_floor=2006,
            )
            is None
        )
        assert (
            repository.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=next_job_id,
                scope_brand="TOYOTA",
                scope_model="CAMRY",
                scope_vehicle_year_floor=2006,
            )
            is None
        )
        assert (
            repository.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=next_job_id,
                scope_brand="TOYOTA",
                scope_model="TACOMA",
                scope_vehicle_year_floor=2007,
            )
            is None
        )
        assert repository.resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=next_job_id,
            scope_brand="Toyota",
            scope_model="Tacoma",
            scope_vehicle_year_floor=2006,
        ) == ("scoped-resume")

        assert (
            repository.start_run(
                "scoped-resume",
                dataset_kind="bounded",
                target_parts=10_000,
                scheduled_job_run_id=next_job_id,
                scope_brand="tOyOtA",
                scope_model="TaCoMa",
                scope_vehicle_year_floor=2006,
            )
            == run_id
        )
        database.commit()
        assert database._execute(
            "SELECT status,scheduled_job_run_id,scope_brand,scope_model,"
            "scope_vehicle_year_floor FROM crawl_runs WHERE id=%s",
            (run_id,),
        ).fetchone() == {
            "status": "running",
            "scheduled_job_run_id": next_job_id,
            "scope_brand": "toyota",
            "scope_model": "tacoma",
            "scope_vehicle_year_floor": 2006,
        }
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_incompatible_running_candidate_is_terminal_before_new_marker() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        _set_desired_bounded_scope(database, "TOYOTA", "TACOMA", 2006)
        previous_job_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        repository = CrawlRepository(database, "poisoned-running-candidate")
        old_run_id = repository.start_run(
            "poisoned-running-candidate",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=previous_job_id,
            scope_brand="TOYOTA",
            scope_model="TACOMA",
            scope_vehicle_year_floor=2006,
        )
        database._execute(
            "UPDATE crawl_runs SET evidence_status='rejected' WHERE id=%s",
            (old_run_id,),
        )
        database._execute(
            "UPDATE scheduled_job_runs SET status='failed',exit_code=1,"
            "finished_at=UTC_TIMESTAMP() WHERE id=%s",
            (previous_job_id,),
        )
        current_job_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        database.commit()

        assert (
            repository.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=current_job_id,
                scope_brand="TOYOTA",
                scope_model="TACOMA",
                scope_vehicle_year_floor=2006,
            )
            is None
        )
        database.commit()
        old_run = database._execute(
            "SELECT status,finished_at,error_msg,evidence_status FROM crawl_runs WHERE id=%s",
            (old_run_id,),
        ).fetchone()
        assert old_run is not None
        assert old_run["status"] == "error"
        assert old_run["finished_at"] is not None
        assert old_run["error_msg"] == (
            "bounded run evidence rejected; a new logical run is required"
        )
        assert old_run["evidence_status"] == "rejected"

        new_run_id = repository.start_run(
            "replacement-running-candidate",
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=current_job_id,
            scope_brand="TOYOTA",
            scope_model="TACOMA",
            scope_vehicle_year_floor=2006,
        )
        database.commit()
        assert new_run_id != old_run_id
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM crawl_runs WHERE status='running'"
        ).fetchone() == {"row_count": 1}
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_closed_under_target_starts_a_new_scoped_logical_run() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        _set_desired_bounded_scope(database, "TOYOTA", "TACOMA", 2006)
        first_job_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        repository = CrawlRepository(database, "scoped-under-target-1")
        first_run_id = repository.start_run(
            "scoped-under-target-1",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=first_job_id,
            scope_brand="Toyota",
            scope_model="Tacoma",
            scope_vehicle_year_floor=2006,
        )
        repository.reject_run_evidence(first_run_id)
        repository.finish_run(
            first_run_id,
            "error",
            {"parts": 8_000},
            "bounded run did not reach exact target: 8000/10000",
        )
        database._execute(
            "UPDATE scheduled_job_runs SET status='failed',exit_code=4,"
            "finished_at=UTC_TIMESTAMP() WHERE id=%s",
            (first_job_id,),
        )
        second_job_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        database.commit()

        assert (
            repository.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=second_job_id,
                scope_brand="TOYOTA",
                scope_model="TACOMA",
                scope_vehicle_year_floor=2006,
            )
            is None
        )
        second_run_id = repository.start_run(
            "scoped-under-target-2",
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=second_job_id,
            scope_brand="TOYOTA",
            scope_model="TACOMA",
            scope_vehicle_year_floor=2006,
        )
        database.commit()

        assert second_run_id != first_run_id
        assert database._execute(
            "SELECT run_key,status,scheduled_job_run_id,scope_brand,scope_model,"
            "scope_vehicle_year_floor FROM crawl_runs WHERE id=%s",
            (second_run_id,),
        ).fetchone() == {
            "run_key": "scoped-under-target-2",
            "status": "running",
            "scheduled_job_run_id": second_job_id,
            "scope_brand": "toyota",
            "scope_model": "tacoma",
            "scope_vehicle_year_floor": 2006,
        }
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_formal_discard_rejects_verified_evidence_and_forces_new_run(
    monkeypatch,
) -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        _set_desired_bounded_scope(database, "TOYOTA", "CAMRY", 2006)
        scheduled_job_run_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        repository = CrawlRepository(database, "scoped-discard-evidence")
        run_id = repository.start_run(
            "scoped-discard-evidence",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
            scope_brand="TOYOTA",
            scope_model="CAMRY",
            scope_vehicle_year_floor=2006,
        )
        brand_id = brands.upsert_brand("TOYOTA", None)
        model_id = brands.upsert_model(brand_id, "CAMRY", None, None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "CAMRY",
                "model_code": "AXVA70",
                "prod_period": "01.2018 - 12.2020",
                "production_from": "2018-01",
                "production_to": "2020-12",
                "engine": "",
                "grade": "",
                "vid": "SITE-VID-1",
                "ssd": "MODEL-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "PARTIAL ENGINE ASSEMBLY",
            "10001",
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&cid=1&uid=10001&vid=SITE-VID-1",
        )
        parts.upsert_parts(group_id, _parts(10_000), run_id)
        database.commit()
        _record_verified_live_evidence(
            database,
            repository,
            run_id=run_id,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        database._execute(
            "UPDATE parts SET code='' WHERE seen_run_id=%s AND part_number='P-09999'",
            (run_id,),
        )
        database.commit()

        _bounded_config(monkeypatch, target=10_000)
        monkeypatch.setitem(CRAWL, "bounded_run_key", "")
        monkeypatch.setitem(CRAWL, "bounded_brand", "TOYOTA")
        monkeypatch.setitem(CRAWL, "bounded_model", "CAMRY")
        monkeypatch.setitem(CRAWL, "vehicle_year_window", 20)
        monkeypatch.setitem(CRAWL, "scheduled_job_run_id", scheduled_job_run_id)
        monkeypatch.setattr("partsouq_catalog.crawler._vehicle_year_window_floor", lambda: 2006)
        http = mock.MagicMock()
        crawler = Crawler(http, database, workers=1)
        try:
            counts = crawler.run()
        finally:
            crawler.close()

        assert counts["parts"] == 9_999
        assert crawler.last_status == "bounded_under_target"
        http.ensure_fresh.assert_not_called()
        assert database._execute(
            "SELECT status,evidence_status,evidence_manifest_sha256,parts_ok "
            "FROM crawl_runs WHERE id=%s",
            (run_id,),
        ).fetchone() == {
            "status": "error",
            "evidence_status": "rejected",
            "evidence_manifest_sha256": None,
            "parts_ok": 9_999,
        }

        database._execute(
            "UPDATE scheduled_job_runs SET status='failed',exit_code=4,"
            "finished_at=UTC_TIMESTAMP() WHERE id=%s",
            (scheduled_job_run_id,),
        )
        next_job_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        database.commit()
        assert (
            repository.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=next_job_id,
                scope_brand="TOYOTA",
                scope_model="CAMRY",
                scope_vehicle_year_floor=2006,
            )
            is None
        )
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_scoped_membership_and_publish_allow_open_ended_only() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        _set_desired_bounded_scope(database, "TOYOTA", "TACOMA", 2006)
        scheduled_job_run_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        repository = CrawlRepository(database, "scoped-membership-mysql")
        run_id = repository.start_run(
            "scoped-membership-mysql",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
            scope_brand="TOYOTA",
            scope_model="TACOMA",
            scope_vehicle_year_floor=2006,
        )

        def group_for(
            brand_name: str,
            model_name: str,
            *,
            uid: str,
            production_to: str | None,
        ) -> int:
            brand_id = brands.upsert_brand(brand_name, None)
            model_id = brands.upsert_model(brand_id, model_name, None, None)
            vehicle_id = vehicles.upsert_vehicle(
                model_id,
                {
                    "name": model_name,
                    "model_code": f"CODE-{uid}",
                    "prod_period": "01.2000 -" if production_to is None else "01.2000 - 12.2005",
                    "production_from": "2000-01",
                    "production_to": production_to,
                    "vid": f"VID-{uid}",
                    "ssd": f"SSD-{uid}",
                },
            )
            category_id = vehicles.upsert_category(vehicle_id, "ENGINE", f"CID-{uid}")
            return vehicles.upsert_group(
                category_id,
                f"G-{uid}",
                "ENGINE PARTS",
                uid,
                f"https://partsouq.com/en/catalog/genuine/unit?uid={uid}",
            )

        open_group = group_for("TOYOTA", "TACOMA", uid="OPEN", production_to=None)
        old_group = group_for("TOYOTA", "TACOMA", uid="OLD", production_to="2005-12")
        wrong_brand_group = group_for("LEXUS", "TACOMA", uid="BRAND", production_to=None)
        wrong_model_group = group_for("TOYOTA", "CAMRY", uid="MODEL", production_to=None)
        parts.upsert_parts(open_group, _parts(9_997), run_id)
        parts.upsert_parts(old_group, _parts(1), run_id)
        parts.upsert_parts(wrong_brand_group, _parts(1), run_id)
        parts.upsert_parts(wrong_model_group, _parts(1), run_id)
        database.commit()

        with (
            mock.patch.object(repository, "_assert_verified_run_evidence"),
            pytest.raises(RuntimeError, match="model scope mismatch"),
        ):
            repository.publish_bounded_parts(run_id, 10_000)
        database.rollback()

        assert (
            repository.discard_invalid_bounded_membership(
                run_id,
                scope_brand="TOYOTA",
                scope_model="TACOMA",
                scope_vehicle_year_floor=2006,
            )
            == 3
        )
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id=%s",
            (run_id,),
        ).fetchone() == {"row_count": 9_997}
        parts.upsert_parts(open_group, _parts(10_000), run_id)
        database.commit()

        # 此案例只檢查 scope membership；full evidence 已由其他 E2E 覆蓋。
        # 建立 accepted unit bindings，讓 snapshot INSERT 可驗證其資料關聯。
        _record_verified_live_evidence(
            database,
            repository,
            run_id=run_id,
            scheduled_job_run_id=scheduled_job_run_id,
            page_types=frozenset({"unit"}),
            verify=False,
        )
        with mock.patch.object(repository, "_assert_verified_run_evidence"):
            assert repository.publish_bounded_parts(run_id, 10_000) == 10_000
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count,COUNT(DISTINCT brand) AS brands,"
            "COUNT(DISTINCT model) AS models,"
            "SUM(production_to IS NULL) AS open_ended FROM bounded_parts"
        ).fetchone() == {
            "row_count": 10_000,
            "brands": 1,
            "models": 1,
            "open_ended": 10_000,
        }
        assert database._execute(
            "SELECT MIN(brand) AS brand,MIN(model) AS model FROM bounded_parts"
        ).fetchone() == {"brand": "TOYOTA", "model": "TACOMA"}
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
@pytest.mark.parametrize(
    ("brand_name", "model_name"),
    (("TÓYOTA", "TACOMA"), ("TOYOTA", "TÁCOMA")),
)
def test_mysql_scoped_publish_and_membership_reject_accent_equivalents(
    brand_name: str,
    model_name: str,
) -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        _set_desired_bounded_scope(database, "TOYOTA", "TACOMA", 2006)
        scheduled_job_run_id = int(
            database._execute(
                "INSERT INTO scheduled_job_runs (job_name,trigger_mode,status,started_at) "
                "VALUES ('catalog','daemon','running',UTC_TIMESTAMP())"
            ).lastrowid
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        repository = CrawlRepository(database, "accent-scope")
        run_id = repository.start_run(
            "accent-scope",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
            scope_brand="TOYOTA",
            scope_model="TACOMA",
            scope_vehicle_year_floor=2006,
        )
        brand_id = brands.upsert_brand(brand_name, None)
        model_id = brands.upsert_model(brand_id, model_name, None, None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": model_name,
                "model_code": "ACCENT-SCOPE",
                "prod_period": "08.2015 -",
                "production_from": "2015-08",
                "production_to": None,
                "vid": "ACCENT-SCOPE",
                "ssd": "ACCENT-SCOPE",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE", "ACCENT")
        group_id = vehicles.upsert_group(
            category_id,
            "ACCENT",
            "ENGINE PARTS",
            "ACCENT",
            "https://partsouq.com/en/catalog/genuine/unit?uid=ACCENT",
        )
        parts.upsert_parts(group_id, _parts(10_000), run_id)
        database.commit()

        with pytest.raises(RuntimeError, match="model scope mismatch"):
            repository.publish_bounded_parts(run_id, 10_000)
        database.rollback()
        assert (
            repository.discard_invalid_bounded_membership(
                run_id,
                scope_brand="TOYOTA",
                scope_model="TACOMA",
                scope_vehicle_year_floor=2006,
            )
            == 10_000
        )
        assert repository.count_run_parts(run_id) == 0
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_bounded_publish_is_atomic_and_does_not_touch_full_snapshot() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    previous_desired_scope = None
    try:
        previous_desired_scope = database._execute(
            "SELECT scope_brand, scope_model, scope_vehicle_year_floor "
            "FROM catalog_desired_bounded_scope WHERE singleton_id = 1"
        ).fetchone()
        _clear_mysql_fixture(database)
        scope_brand = "TOYOTA"
        scope_model = "CAMRY"
        scope_vehicle_year_floor = 2018
        database._execute(
            "INSERT INTO catalog_desired_bounded_scope "
            "(singleton_id, scope_brand, scope_model, scope_vehicle_year_floor, updated_at) "
            "VALUES (1, %s, %s, %s, UTC_TIMESTAMP(6)) AS new "
            "ON DUPLICATE KEY UPDATE scope_brand = new.scope_brand, "
            "scope_model = new.scope_model, "
            "scope_vehicle_year_floor = new.scope_vehicle_year_floor, "
            "updated_at = new.updated_at",
            (scope_brand.casefold(), scope_model.casefold(), scope_vehicle_year_floor),
        )
        database.commit()
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
                "engine": "",
                "grade": "",
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
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&cid=1&uid=10001&vid=SITE-VID-1",
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
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_vehicle_year_floor,
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
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_vehicle_year_floor,
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
                scope_brand=scope_brand,
                scope_model=scope_model,
                scope_vehicle_year_floor=scope_vehicle_year_floor,
            )
            is None
        )
        assert manual.resumable_bounded_run_key(
            10_000,
            scheduled_job_run_id=None,
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_vehicle_year_floor,
        ) == ("bounded-manual-partial")

        first = CrawlRepository(database, "bounded-scheduled-resume")
        first_run_id = first.start_run(
            "bounded-scheduled-resume",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_vehicle_year_floor,
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
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_vehicle_year_floor,
        ) == ("bounded-scheduled-resume")

        retry_run_id = first.start_run(
            "bounded-scheduled-resume",
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=retry_scheduled_job_run_id,
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_vehicle_year_floor,
        )
        assert retry_run_id == first_run_id
        assert database._execute(
            "SELECT status, finished_at, error_msg FROM crawl_runs WHERE id = %s",
            (retry_run_id,),
        ).fetchone() == {"status": "running", "finished_at": None, "error_msg": None}
        assert first.count_run_parts(retry_run_id) == 8_000
        parts.upsert_parts(group_id, _parts(10_000)[8_000:], retry_run_id)
        assert first.count_run_parts(retry_run_id) == 10_000
        _record_verified_live_evidence(
            database,
            first,
            run_id=retry_run_id,
            scheduled_job_run_id=int(retry_scheduled_job_run_id),
            page_types=frozenset({"unit"}),
            unit_part_count=10_001,
            record_group_receipt=False,
        )
        with pytest.raises(RuntimeError, match="invalid group receipts"):
            first.publish_bounded_parts(retry_run_id, 10_000)
        _record_verified_live_evidence(
            database,
            first,
            run_id=retry_run_id,
            scheduled_job_run_id=int(retry_scheduled_job_run_id),
            page_types=frozenset({"unit"}),
            unit_part_count=10_001,
        )
        assert database._execute(
            "SELECT status, parsed_part_count, accepted_part_count "
            "FROM bounded_group_receipts WHERE crawl_run_id = %s",
            (retry_run_id,),
        ).fetchone() == {
            "status": "partial",
            "parsed_part_count": 10_001,
            "accepted_part_count": 10_000,
        }
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
            "source_url": (
                "https://partsouq.com/en/catalog/genuine/unit"
                "?c=TOYOTA&cid=1&uid=10001&vid=SITE-VID-1"
            ),
        }

        # crawler 已在自己的交易完成 snapshot、evidence seal 與 bounded_success，
        # 但 parent scheduler 尚未寫入完成結果（等同 child 後、parent 前 crash）。
        # 正式 view 必須先 fail closed；recovery 只在重新驗證後才補記 completion。
        assert database._execute(
            "SELECT status, finished_at, exit_code FROM scheduled_job_runs WHERE id = %s",
            (retry_scheduled_job_run_id,),
        ).fetchone() == {"status": "running", "finished_at": None, "exit_code": None}
        assert (
            first.audit_run_evidence(
                retry_run_id,
                allow_running_scheduler=True,
            )["verified"]
            is True
        )
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}
        assert scheduler._recover_interrupted_job_runs("catalog") is True
        recovered_scheduler = database._execute(
            "SELECT status, exit_code FROM scheduled_job_runs WHERE id = %s",
            (retry_scheduled_job_run_id,),
        ).fetchone()
        assert recovered_scheduler == {"status": "completed", "exit_code": 0}
        audit = first.audit_run_evidence(retry_run_id)
        assert audit["verified"] is True
        snapshot_binding = database._execute(
            "SELECT part_id, evidence_record_sha256 FROM bounded_parts "
            "WHERE crawl_run_id = %s ORDER BY part_id LIMIT 1",
            (retry_run_id,),
        ).fetchone()
        assert snapshot_binding is not None
        accepted_binding = database._execute(
            "SELECT record_sha256 FROM partsouq_artifact_records "
            "WHERE crawl_run_id = %s AND part_id = %s "
            "AND record_type = 'part' AND accepted = 1",
            (retry_run_id, snapshot_binding["part_id"]),
        ).fetchone()
        assert accepted_binding is not None
        assert snapshot_binding["evidence_record_sha256"] == accepted_binding["record_sha256"]

        # 後續 raw crawl 可改寫 parts 的 membership/內容；正式 snapshot 的
        # evidence audit 必須只讀 bounded_parts，不能因而失效或改驗新資料。
        database._execute(
            "UPDATE parts SET seen_run_id = NULL, code = 'RAW-CRAWL-REWRITE' WHERE id = %s",
            (snapshot_binding["part_id"],),
        )
        database.commit()
        assert first.audit_run_evidence(retry_run_id)["verified"] is True
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 10_000}

        # 已發布 snapshot 不允許直接改寫 evidence binding；資料與正式 view
        # 都必須保持原樣，下一版只能透過發布流程 DELETE + INSERT 更換。
        with pytest.raises(pymysql.MySQLError) as error:
            database._execute(
                "UPDATE bounded_parts SET evidence_record_sha256 = %s WHERE part_id = %s",
                (
                    hashlib.sha256(b"tampered bounded evidence binding").hexdigest(),
                    snapshot_binding["part_id"],
                ),
            )
        assert error.value.args[0] == 1644
        assert first.audit_run_evidence(retry_run_id)["verified"] is True
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 10_000}
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
            "source_url_sha256, raw_body_sha256, body_sha256, sanitizer_version, "
            "http_status, content_type, "
            "challenge_detected, fetched_at, elapsed_ms, attempt, parser_name, parser_version, "
            "parser_context_json, parser_context_sha256, malformed_row_count, "
            "skipped_record_count, parsed_record_count, parsed_records_sha256, "
            "accepted_record_count, accepted_records_sha256, verification_status, verified_at"
            ") SELECT %s, %s, capture_kind, page_type, public_source_url, source_url_sha256, "
            "%s, body_sha256, sanitizer_version, http_status, content_type, "
            "challenge_detected, fetched_at, "
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
            scope_brand=scope_brand,
            scope_model=scope_model,
            scope_vehicle_year_floor=scope_vehicle_year_floor,
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
        if previous_desired_scope is None:
            database._execute("DELETE FROM catalog_desired_bounded_scope WHERE singleton_id = 1")
        else:
            database._execute(
                "INSERT INTO catalog_desired_bounded_scope "
                "(singleton_id, scope_brand, scope_model, scope_vehicle_year_floor, updated_at) "
                "VALUES (1, %s, %s, %s, UTC_TIMESTAMP(6)) AS new "
                "ON DUPLICATE KEY UPDATE scope_brand = new.scope_brand, "
                "scope_model = new.scope_model, "
                "scope_vehicle_year_floor = new.scope_vehicle_year_floor, "
                "updated_at = new.updated_at",
                (
                    previous_desired_scope["scope_brand"],
                    previous_desired_scope["scope_model"],
                    previous_desired_scope["scope_vehicle_year_floor"],
                ),
            )
        database.commit()
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL full snapshot tests",
)
def test_mysql_full_candidate_archive_requires_verified_evidence() -> None:
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
                "engine": "",
                "grade": "",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "PARTIAL ENGINE ASSEMBLY",
            "10001",
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&cid=1&uid=10001&vid=SITE-VID-1",
        )

        unscheduled = CrawlRepository(database, "full-publish-unscheduled")
        unscheduled_run_id = unscheduled.start_run("full-publish-unscheduled", fresh=True)
        parts.upsert_parts(group_id, _parts(1), unscheduled_run_id)
        database.commit()
        with pytest.raises(RuntimeError, match="invalid scheduler provenance"):
            unscheduled.archive_full_candidate_parts(unscheduled_run_id)
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
            manual.archive_full_candidate_parts(manual_run_id)
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
            failed.archive_full_candidate_parts(failed_run_id)
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
            shared_first.archive_full_candidate_parts(shared_first_id)
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
        first.mark_group_fetched(
            group_id,
            first.run_key,
            status="done",
            row_count=1,
        )
        # 正式 full snapshot 必須先封存可重放的 live HTTP 證據。
        _record_verified_live_evidence(
            database,
            first,
            run_id=first_run_id,
            scheduled_job_run_id=scheduled_job_run_id,
            record_group_receipt=False,
            verify=False,
            unit_part_count=1,
        )
        # 證據必須先 commit：驗收的 body 副連線讀不到未提交寫入。
        database.commit()
        first.verify_run_evidence_full(first_run_id)
        with pytest.raises(RuntimeError, match="scheduler owner changed"):
            first.archive_full_candidate_parts(
                first_run_id,
                expected_scheduled_job_run_id=int(scheduled_job_run_id) + 1,
            )
        assert first.archive_full_candidate_parts(first_run_id) == 1
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
        # migration 039 切換閘：published 來自同一筆 full run（success、
        # 證據已封存、daemon completed exit=0、單一 linked crawl）時，
        # v_current_catalog_parts 改讀全量快照——published 的 1 筆在此
        # 全部條件成立後輸出。
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 1}
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
            sample.archive_full_candidate_parts(sample_run_id)
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
            invalid.archive_full_candidate_parts(invalid_run_id)
        database.rollback()

        invalid.mark_done("vehicle", "TOYOTA::CAMRY")
        invalid.seen("vehicle", "TOYOTA::CAMRY::pending")
        database.commit()
        with pytest.raises(RuntimeError, match="incomplete crawl state: count=1"):
            invalid.archive_full_candidate_parts(invalid_run_id)
        database.rollback()

        invalid.mark_done("vehicle", "TOYOTA::CAMRY::pending")
        invalid.mark_group_fetched(
            group_id,
            invalid.run_key,
            status="done",
            row_count=1,
        )
        database._execute(
            "UPDATE groups_t SET url = %s WHERE id = %s",
            ("https://example.com/en/catalog/genuine/unit?uid=10001", group_id),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="failed source/field quality gate"):
            invalid.archive_full_candidate_parts(invalid_run_id)
        database.rollback()

        database._execute(
            "UPDATE groups_t SET url = %s WHERE id = %s",
            (
                "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&cid=1&uid=10001&vid=SITE-VID-1",
                group_id,
            ),
        )
        database._execute(
            "UPDATE parts SET part_from = NULL, part_to = '2017-12' WHERE seen_run_id = %s",
            (invalid_run_id,),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="failed source/field quality gate"):
            invalid.archive_full_candidate_parts(invalid_run_id)
        database.rollback()

        database._execute(
            "UPDATE parts SET part_to = NULL, code = '' WHERE seen_run_id = %s",
            (invalid_run_id,),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="failed source/field quality gate"):
            invalid.archive_full_candidate_parts(invalid_run_id)
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
        failed_replacement.mark_group_fetched(
            group_id,
            failed_replacement.run_key,
            status="done",
            row_count=1,
        )
        _record_verified_live_evidence(
            database,
            failed_replacement,
            run_id=failed_replacement_run_id,
            scheduled_job_run_id=failed_replacement_job_id,
            record_group_receipt=False,
            verify=False,
            unit_part_count=1,
            unit_part_name="FAILED REPLACEMENT",
        )
        # 證據必須先 commit：驗收的 body 副連線讀不到未提交寫入。
        database.commit()
        failed_replacement.verify_run_evidence_full(failed_replacement_run_id)
        assert failed_replacement.archive_full_candidate_parts(failed_replacement_run_id) == 1
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
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}
        assert database._execute("SELECT COUNT(*) AS row_count FROM v_parts").fetchone() == {
            "row_count": 0
        }

        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 1, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (failed_replacement_job_id,),
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}

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
        resumed.mark_group_fetched(
            group_id,
            resumed.run_key,
            status="done",
            row_count=1,
        )
        _record_verified_live_evidence(
            database,
            resumed,
            run_id=resumed_run_id,
            scheduled_job_run_id=resumed_job_id,
            record_group_receipt=False,
            verify=False,
            unit_part_count=1,
            unit_part_name="RESUMED SNAPSHOT",
        )
        # 證據必須先 commit：驗收的 body 副連線讀不到未提交寫入。
        database.commit()
        resumed.verify_run_evidence_full(resumed_run_id)
        assert resumed.archive_full_candidate_parts(resumed_run_id) == 1
        resumed.finish_run(resumed_run_id, "success", {"parts": 999})
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 0}

        database._execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (resumed_job_id,),
        )
        database.commit()
        # migration 039 切換閘：resumed 的 full 快照（success、證據已封存、
        # daemon completed exit=0、單一 linked crawl）在此成為 current view。
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM v_current_catalog_parts"
        ).fetchone() == {"row_count": 1}
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL full snapshot tests",
)
def test_mysql_full_publish_rejects_group_receipt_inconsistency() -> None:
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

        def start_full_run(run_key: str) -> tuple[CrawlRepository, int]:
            scheduled_job_run_id = database._execute(
                "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
                "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
            ).lastrowid
            repository = CrawlRepository(database, run_key)
            run_id = repository.start_run(
                run_key,
                fresh=True,
                scheduled_job_run_id=scheduled_job_run_id,
            )
            return repository, run_id

        stale, stale_run_id = start_full_run("full-receipt-stale")
        parts.upsert_parts(group_id, _parts(1), stale_run_id)
        stale.mark_group_fetched(group_id, "different-run", status="done", row_count=1)
        database.commit()
        with pytest.raises(RuntimeError, match="incomplete group receipts: count=1"):
            stale.archive_full_candidate_parts(stale_run_id)

        null_status, null_status_run_id = start_full_run("full-receipt-null-status")
        parts.upsert_parts(group_id, _parts(1), null_status_run_id)
        database._execute(
            "UPDATE groups_t SET fetched_run_key = %s, fetched_status = NULL, "
            "fetched_row_count = 1 WHERE id = %s",
            (null_status.run_key, group_id),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="incomplete group receipts: count=1"):
            null_status.archive_full_candidate_parts(null_status_run_id)

        uppercase_status, uppercase_status_run_id = start_full_run("full-receipt-uppercase-status")
        parts.upsert_parts(group_id, _parts(1), uppercase_status_run_id)
        database._execute(
            "UPDATE groups_t SET fetched_run_key = %s, fetched_status = 'DONE', "
            "fetched_row_count = 1 WHERE id = %s",
            (uppercase_status.run_key, group_id),
        )
        database.commit()
        with pytest.raises(RuntimeError, match="incomplete group receipts: count=1"):
            uppercase_status.archive_full_candidate_parts(uppercase_status_run_id)

        valid_group_id = vehicles.upsert_group(
            category_id,
            "1102",
            "VALID ENGINE ASSEMBLY",
            "10002",
            "https://partsouq.com/en/catalog/genuine/unit?uid=10002",
        )
        missing_parts, missing_parts_run_id = start_full_run("full-receipt-missing-parts")
        parts.upsert_parts(valid_group_id, _parts(1), missing_parts_run_id)
        missing_parts.mark_group_fetched(
            valid_group_id,
            missing_parts.run_key,
            status="done",
            row_count=1,
        )
        missing_parts.mark_group_fetched(
            group_id,
            missing_parts.run_key,
            status="done",
            row_count=1,
        )
        database.commit()
        recoverable = missing_parts.list_null_groups(
            10,
            run_key=missing_parts.run_key,
            run_id=missing_parts_run_id,
            after_id=min(group_id, valid_group_id) - 1,
        )
        assert [int(row["id"]) for row in recoverable] == [group_id]
        with pytest.raises(RuntimeError, match="group receipt/part mismatches: count=1"):
            missing_parts.archive_full_candidate_parts(missing_parts_run_id)

        recover, recover_run_id = start_full_run("recover-20260829Tfixture")
        parts.upsert_parts(group_id, _parts(1), recover_run_id)
        recover.mark_group_fetched(group_id, recover.run_key, status="done", row_count=1)
        database.commit()
        with pytest.raises(RuntimeError, match="recover-only maintenance run"):
            recover.archive_full_candidate_parts(recover_run_id)
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


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_crawl_model_releases_evidence_lock_before_vehicle_worker(
    monkeypatch,
) -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    _bounded_config(monkeypatch, target=10_000)
    database = Database().connect()
    instance: Crawler | None = None
    try:
        _clear_mysql_fixture(database)
        run_id = database._execute(
            "INSERT INTO crawl_runs ("
            "run_key,started_at,status,dataset_kind,target_parts,evidence_status) "
            "VALUES ('bounded-lock-regression',UTC_TIMESTAMP(),'running',"
            "'bounded',10000,'collecting')"
        ).lastrowid
        assert run_id is not None
        database.commit()

        connection_ids: dict[str, int] = {}
        instance = Crawler(mock.MagicMock(), database, workers=1)
        instance.brands = mock.MagicMock()
        instance.brands.upsert_model.return_value = 31
        instance.crawl = CrawlRepository(database, "bounded-lock-regression")
        instance._fetch = mock.MagicMock(return_value=("<html>pick</html>", object()))
        vehicle = {
            "name": "CAMRY",
            "model_code": "AXVA70",
            "prod_period": "01.2018 - 12.2020",
            "production_from": "2018-01",
            "production_to": "2020-12",
            "vid": "SITE-VID-1",
            "ssd": "VEHICLE-SSD",
        }

        def capture_pick_lock(*_args, **_kwargs) -> None:
            row = database._execute("SELECT CONNECTION_ID() AS connection_id").fetchone()
            assert row is not None
            connection_ids["main"] = int(row["connection_id"])
            assert database._execute(
                "SELECT id FROM crawl_runs WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone() == {"id": run_id}

        def capture_vehicle_lock(*_args) -> None:
            database._execute("SET SESSION innodb_lock_wait_timeout=1")
            row = database._execute("SELECT CONNECTION_ID() AS connection_id").fetchone()
            assert row is not None
            connection_ids["worker"] = int(row["connection_id"])
            assert database._execute(
                "SELECT id FROM crawl_runs WHERE id=%s FOR UPDATE",
                (run_id,),
            ).fetchone() == {"id": run_id}

        instance._capture_http_evidence = mock.MagicMock(side_effect=capture_pick_lock)
        instance.crawl_vehicle = mock.MagicMock(side_effect=capture_vehicle_lock)
        with mock.patch(
            "partsouq_catalog.crawler.parse_vehicles",
            return_value=([vehicle], 0),
        ):
            result = instance.crawl_model(
                "TOYOTA",
                17,
                {"name": "CAMRY", "ssd": "MODEL-SSD", "url": "/pick"},
            )

        assert result == (0, True)
        assert connection_ids["main"] != connection_ids["worker"]
        assert database._execute(
            "SELECT status FROM crawl_state WHERE run_key='bounded-lock-regression' "
            "AND scope='vehicle' AND scope_key=%s",
            (instance._vehicle_key(31, vehicle),),
        ).fetchone() == {"status": "done"}
    finally:
        if instance is not None:
            instance.close()
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
@pytest.mark.parametrize(
    ("receipt_url", "receipt_status"),
    (
        ("/en/catalog/genuine/unit?uid=10001", "done"),
        ("HTTPS://PARTSOUQ.COM/en/catalog/genuine/unit?uid=10001", "done"),
        (
            "https://partsouq.com/en/catalog/genuine/unit?uid=10001&ssd=SECRET",
            "done",
        ),
        ("https://partsouq.com/en/catalog/genuine/unit?uid=10001&token=SECRET", "done"),
        ("https://partsouq.com/en/catalog/genuine/unit?", "done"),
        ("https://partsouq.com/en/catalog/genuine/unit?uid=10001", "DONE"),
        ("https://partsouq.com/en/catalog/genuine/unit?uid=10001", "done "),
        ("https://partsouq.com/en/catalog/genuine/unit?uid=99999", "done"),
    ),
)
def test_mysql_bounded_resume_durably_rejects_invalid_group_receipt(
    receipt_url: str,
    receipt_status: str,
) -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        assert database._execute(
            "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS columns_list "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'groups_t' AND INDEX_NAME = 'idx_group_fetched_run_key'"
        ).fetchone() == {"columns_list": "fetched_run_key"}
        older_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        older = CrawlRepository(database, "bounded-valid-older")
        older_run_id = older.start_run(
            "bounded-valid-older",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=older_job_id,
        )
        older.finish_run(older_run_id, "interrupted", {"parts": 0}, "older valid run")
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 125, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (older_job_id,),
        )
        first_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        crawl = CrawlRepository(database, "bounded-relative-receipt")
        run_id = crawl.start_run(
            "bounded-relative-receipt",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=first_job_id,
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
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
            receipt_url,
        )
        crawl.mark_group_fetched(group_id, "bounded-relative-receipt", row_count=1)
        database._execute(
            "UPDATE groups_t SET fetched_status = %s WHERE id = %s",
            (receipt_status, group_id),
        )
        crawl.finish_run(run_id, "interrupted", {"parts": 0}, "simulated old receipt")
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 125, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (first_job_id,),
        )
        retry_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        database.commit()

        assert (
            crawl.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=retry_job_id,
            )
            is None
        )

        database._execute(
            "UPDATE groups_t SET url = %s, fetched_status = 'done' WHERE id = %s",
            ("https://partsouq.com/en/catalog/genuine/unit?uid=10001", group_id),
        )
        database.commit()
        assert (
            crawl.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=retry_job_id,
            )
            is None
        )
        assert database._execute(
            "SELECT evidence_status FROM crawl_runs WHERE id = %s",
            (run_id,),
        ).fetchone() == {"evidence_status": "rejected"}
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
@pytest.mark.parametrize(
    ("membership_count", "has_error", "expected_resume"),
    (
        (10, True, False),
        (8, True, True),
        (10, False, False),
    ),
)
def test_mysql_bounded_resume_retires_exhausted_run_with_failures_or_missing_receipts(
    membership_count: int,
    has_error: bool,
    expected_resume: bool,
) -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        failed_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        run_key = f"bounded-exhausted-{membership_count}-{int(has_error)}"
        crawl = CrawlRepository(database, run_key)
        run_id = crawl.start_run(
            run_key,
            fresh=True,
            dataset_kind="bounded",
            target_parts=10,
            scheduled_job_run_id=failed_job_id,
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        brand_id = brands.upsert_brand("TOYOTA", None)
        model_id = brands.upsert_model(brand_id, "CAMRY", "MODEL-BOUNDED", None)
        vehicle_id = vehicles.upsert_vehicle(
            model_id,
            {
                "name": "CAMRY",
                "model_code": "AXVA70",
                "prod_period": "01.2018 - 12.2020",
                "production_from": "2018-01",
                "production_to": "2020-12",
                "vid": "SITE-VID-BOUNDED",
                "ssd": "VEHICLE-SSD",
            },
        )
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        completed_group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "COMPLETED ENGINE ASSEMBLY",
            "10001",
            "https://partsouq.com/en/catalog/genuine/unit?uid=10001",
        )
        partial_group_id = vehicles.upsert_group(
            category_id,
            "1102",
            "PARTIAL ENGINE ASSEMBLY",
            "10002",
            "https://partsouq.com/en/catalog/genuine/unit?uid=10002",
        )
        completed_count = membership_count - 2
        parts.upsert_parts(completed_group_id, _parts(completed_count), run_id)
        crawl.mark_group_fetched(
            completed_group_id,
            run_key,
            row_count=completed_count,
        )
        parts.upsert_parts(
            partial_group_id,
            _parts(2),
            run_id,
            complete_group=False,
        )
        if has_error:
            crawl.mark_error("vehicle", f"v5:{'a' * 64}", "simulated retryable error")

        body_sha256 = hashlib.sha256(run_key.encode()).hexdigest()
        database._execute(
            "INSERT INTO partsouq_response_bodies "
            "(body_sha256, compression, body_blob, original_bytes, stored_bytes, "
            "sanitizer_version) VALUES (%s, 'zlib', %s, 1, 1, %s)",
            (body_sha256, b"x", SANITIZER_VERSION),
        )
        database._execute(
            "INSERT INTO partsouq_http_artifacts ("
            "crawl_run_id, scheduled_job_run_id, capture_kind, page_type, "
            "public_source_url, source_url_sha256, raw_body_sha256, body_sha256, "
            "sanitizer_version, http_status, content_type, challenge_detected, "
            "fetched_at, elapsed_ms, attempt, parser_name, parser_version, "
            "parser_context_json, parser_context_sha256, malformed_row_count, "
            "skipped_record_count, parsed_record_count, parsed_records_sha256, "
            "accepted_record_count, accepted_records_sha256, verification_status, "
            "verified_at) VALUES ("
            "%s, %s, 'live_http', 'unit', %s, %s, %s, %s, %s, "
            "200, 'text/html', 0, UTC_TIMESTAMP(6), 1, 1, 'parse_parts', "
            "'partsouq-catalog-parser-v1', JSON_OBJECT(), %s, 0, 0, 1, %s, "
            "0, %s, 'superseded', UTC_TIMESTAMP(6))",
            (
                run_id,
                failed_job_id,
                "https://partsouq.com/en/catalog/genuine/unit?uid=10002",
                "1" * 64,
                "2" * 64,
                body_sha256,
                SANITIZER_VERSION,
                "3" * 64,
                "4" * 64,
                "5" * 64,
            ),
        )
        crawl.finish_run(
            run_id,
            "error" if has_error else "interrupted",
            {"parts": membership_count},
            "simulated bounded stop" if has_error else None,
        )
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 1, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (failed_job_id,),
        )
        retry_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        database.commit()

        resumed_key = crawl.resumable_bounded_run_key(
            10,
            scheduled_job_run_id=retry_job_id,
        )
        if expected_resume:
            assert resumed_key == run_key
            assert database._execute(
                "SELECT evidence_status FROM crawl_runs WHERE id = %s",
                (run_id,),
            ).fetchone() == {"evidence_status": "missing"}
            return

        assert resumed_key is None
        database.commit()
        assert database._execute(
            "SELECT evidence_status FROM crawl_runs WHERE id = %s",
            (run_id,),
        ).fetchone() == {"evidence_status": "rejected"}
        assert crawl.count_run_parts(run_id) == 10
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM partsouq_http_artifacts WHERE crawl_run_id = %s",
            (run_id,),
        ).fetchone() == {"row_count": 1}
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM partsouq_response_bodies WHERE body_sha256 = %s",
            (body_sha256,),
        ).fetchone() == {"row_count": 1}

        new_run_id = crawl.start_run(
            "bounded-clean-retry",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10,
            scheduled_job_run_id=retry_job_id,
        )
        assert new_run_id != run_id
        assert crawl.count_run_parts(new_run_id) == 0
        assert crawl.count_run_parts(run_id) == 10
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_bounded_resume_durably_rejects_pending_artifact_without_fallback() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        older_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        older = CrawlRepository(database, "bounded-artifact-older")
        older_run_id = older.start_run(
            "bounded-artifact-older",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=older_job_id,
        )
        older.finish_run(older_run_id, "interrupted", {"parts": 0}, "older valid run")
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 125, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (older_job_id,),
        )

        latest_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        latest = CrawlRepository(database, "bounded-artifact-latest")
        latest_run_id = latest.start_run(
            "bounded-artifact-latest",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=latest_job_id,
        )
        latest.finish_run(latest_run_id, "interrupted", {"parts": 0}, "pending evidence")
        database._execute(
            "UPDATE crawl_runs SET evidence_status = 'collecting', "
            "evidence_manifest_sha256 = %s, evidence_dataset_sha256 = %s, "
            "evidence_artifact_count = 1, evidence_record_count = 1, "
            "evidence_original_bytes = 1, evidence_stored_bytes = 1, "
            "evidence_verified_at = UTC_TIMESTAMP(6) WHERE id = %s",
            ("1" * 64, "2" * 64, latest_run_id),
        )
        body_sha256 = "3" * 64
        database._execute(
            "INSERT INTO partsouq_response_bodies "
            "(body_sha256, compression, body_blob, original_bytes, stored_bytes, "
            "sanitizer_version) VALUES (%s, 'zlib', %s, 1, 1, %s)",
            (body_sha256, b"x", SANITIZER_VERSION),
        )
        database._execute(
            "INSERT INTO partsouq_http_artifacts ("
            "crawl_run_id, scheduled_job_run_id, capture_kind, page_type, "
            "public_source_url, source_url_sha256, raw_body_sha256, body_sha256, "
            "sanitizer_version, http_status, content_type, challenge_detected, "
            "fetched_at, elapsed_ms, attempt, parser_name, parser_version, "
            "parser_context_json, parser_context_sha256, malformed_row_count, "
            "skipped_record_count, parsed_record_count, parsed_records_sha256, "
            "accepted_record_count, accepted_records_sha256, verification_status, "
            "verified_at) VALUES ("
            "%s, %s, 'live_http', 'genuine', "
            "'https://partsouq.com/en/catalog/genuine', %s, %s, %s, %s, "
            "200, 'text/html', 0, UTC_TIMESTAMP(6), 1, 1, 'parse_brands', "
            "'partsouq-catalog-parser-v1', JSON_OBJECT(), %s, 0, 0, 1, %s, "
            "0, %s, 'pending', NULL)",
            (
                latest_run_id,
                latest_job_id,
                "4" * 64,
                "5" * 64,
                body_sha256,
                SANITIZER_VERSION,
                "6" * 64,
                "7" * 64,
                "8" * 64,
            ),
        )
        database._execute(
            "UPDATE scheduled_job_runs SET status = 'failed', exit_code = 125, "
            "finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (latest_job_id,),
        )
        retry_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        database.commit()

        assert (
            latest.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=retry_job_id,
            )
            is None
        )
        database.commit()
        assert database._execute(
            "SELECT evidence_status, evidence_manifest_sha256, evidence_dataset_sha256, "
            "evidence_artifact_count, evidence_record_count, evidence_original_bytes, "
            "evidence_stored_bytes, evidence_verified_at FROM crawl_runs WHERE id = %s",
            (latest_run_id,),
        ).fetchone() == {
            "evidence_status": "rejected",
            "evidence_manifest_sha256": None,
            "evidence_dataset_sha256": None,
            "evidence_artifact_count": 0,
            "evidence_record_count": 0,
            "evidence_original_bytes": 0,
            "evidence_stored_bytes": 0,
            "evidence_verified_at": None,
        }

        database._execute(
            "UPDATE partsouq_http_artifacts SET verification_status = 'verified', "
            "verified_at = UTC_TIMESTAMP(6) WHERE crawl_run_id = %s",
            (latest_run_id,),
        )
        database.commit()
        assert (
            latest.resumable_bounded_run_key(
                10_000,
                scheduled_job_run_id=retry_job_id,
            )
            is None
        )
        assert database._execute(
            "SELECT evidence_status FROM crawl_runs WHERE id IN (%s, %s) ORDER BY id",
            (older_run_id, latest_run_id),
        ).fetchall() == [
            {"evidence_status": "missing"},
            {"evidence_status": "rejected"},
        ]
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_http_diagnostic_upserts_inline_sanitized_body_without_formal_evidence() -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    try:
        _clear_mysql_fixture(database)
        scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        crawl = CrawlRepository(database, "diagnostic-mysql-run")
        run_id = crawl.start_run(
            "diagnostic-mysql-run",
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
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
        public_url = "https://partsouq.com/en/catalog/genuine/unit?uid=10001"
        group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "PARTIAL ENGINE ASSEMBLY",
            "10001",
            public_url,
        )
        context = {
            "group_key": {
                "category": {
                    "vehicle": {
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
                    },
                    "cid": "1",
                    "category_name": "ENGINE/FUEL/TOOL",
                },
                "group_code": "1101",
                "uid": "10001",
            }
        }
        first_html = (
            "<html><input name='ssd' value='FIRST-SECRET'><p>Unexpected response one</p></html>"
        )
        first = sanitize_parser_html(first_html)
        first_id = crawl.record_http_diagnostic(
            run_id,
            scheduled_job_run_id,
            group_id,
            public_url=public_url,
            raw_body_sha256=hashlib.sha256(first_html.encode()).hexdigest(),
            status_code=200,
            content_type="text/html",
            fetched_at=datetime.now(UTC).replace(tzinfo=None),
            elapsed_ms=25,
            attempt=1,
            sanitized_body=first,
            parser_name="parse_parts",
            parser_version=PARSER_CONTRACT_VERSION,
            parser_context=context,
            reason="empty_parse",
        )
        second_html = (
            "<html><input name='ssd' value='SECOND-SECRET'><p>Unexpected response two</p></html>"
        )
        second = sanitize_parser_html(second_html)
        second_id = crawl.record_http_diagnostic(
            run_id,
            scheduled_job_run_id,
            group_id,
            public_url=public_url,
            raw_body_sha256=hashlib.sha256(second_html.encode()).hexdigest(),
            status_code=200,
            content_type="text/html",
            fetched_at=datetime.now(UTC).replace(tzinfo=None),
            elapsed_ms=30,
            attempt=2,
            sanitized_body=second,
            parser_name="parse_parts",
            parser_version=PARSER_CONTRACT_VERSION,
            parser_context=context,
            reason="empty_parse",
        )
        database.commit()

        assert second_id == first_id
        diagnostic = database._execute(
            "SELECT public_source_url, body_sha256, compression, body_blob, "
            "original_bytes, stored_bytes, http_status, attempt "
            "FROM partsouq_http_diagnostics WHERE id = %s",
            (first_id,),
        ).fetchone()
        assert diagnostic == {
            "public_source_url": public_url,
            "body_sha256": second.body_sha256,
            "compression": "zlib",
            "body_blob": second.compressed,
            "original_bytes": second.original_bytes,
            "stored_bytes": second.stored_bytes,
            "http_status": 200,
            "attempt": 2,
        }
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM partsouq_http_diagnostics"
        ).fetchone() == {"row_count": 1}
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM partsouq_http_artifacts"
        ).fetchone() == {"row_count": 0}
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM partsouq_response_bodies"
        ).fetchone() == {"row_count": 0}
        assert b"FIRST-SECRET" not in bytes(diagnostic["body_blob"])
        assert b"SECOND-SECRET" not in bytes(diagnostic["body_blob"])
        restored = restore_sanitized_body(
            str(diagnostic["compression"]),
            bytes(diagnostic["body_blob"]),
            expected_size=int(diagnostic["original_bytes"]),
        )
        assert b"FIRST-SECRET" not in restored
        assert b"SECOND-SECRET" not in restored

        # 新 scheduler 不得接管仍在執行中的同一 logical run。先由原 owner
        # 寫入終態，才模擬下一次 daemon 用 --fresh 清除舊 diagnostic。
        crawl.finish_run(run_id, "error", {"parts": 0}, "diagnostic retry fixture")
        database._execute(
            "UPDATE scheduled_job_runs SET status='failed', exit_code=1, "
            "finished_at=UTC_TIMESTAMP() WHERE id=%s",
            (scheduled_job_run_id,),
        )
        database.commit()
        replacement_job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        assert (
            crawl.start_run(
                "diagnostic-mysql-run",
                fresh=True,
                dataset_kind="bounded",
                target_parts=10_000,
                scheduled_job_run_id=replacement_job_id,
            )
            == run_id
        )
        database.commit()
        assert database._execute(
            "SELECT COUNT(*) AS row_count FROM partsouq_http_diagnostics WHERE crawl_run_id=%s",
            (run_id,),
        ).fetchone() == {"row_count": 0}
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL bounded tests",
)
def test_mysql_hard_404_terminal_writes_commit_or_rollback_together(monkeypatch) -> None:
    if not str(DB_CONFIG["database"]).endswith("_test"):
        raise ValueError("UNIFIED_TEST_MYSQL requires a database name ending in _test")

    database = Database().connect()
    instance: Crawler | None = None
    try:
        _clear_mysql_fixture(database)
        scheduled_job_run_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        run_key = "hard-404-transaction"
        crawl = CrawlRepository(database, run_key)
        run_id = crawl.start_run(
            run_key,
            fresh=True,
            dataset_kind="bounded",
            target_parts=10_000,
            scheduled_job_run_id=scheduled_job_run_id,
        )
        brands = BrandRepository(database)
        vehicles = VehicleRepository(database)
        parts = PartRepository(database)
        brand_id = brands.upsert_brand("TOYOTA", None)
        model_id = brands.upsert_model(brand_id, "CAMRY", "MODEL-SSD", None)
        vehicle_key = {
            "name": "CAMRY",
            "model_code": "AXVA70",
            "prod_period": "01.2018 - 12.2020",
            "production_from": "2018-01",
            "production_to": "2020-12",
            "engine": None,
            "trim_name": None,
            "vid": "SITE-VID-1",
            "ssd": "VEHICLE-SSD",
        }
        vehicle_id = vehicles.upsert_vehicle(model_id, vehicle_key)
        category_id = vehicles.upsert_category(vehicle_id, "ENGINE/FUEL/TOOL", "1")
        public_url = public_source_url(
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&vid=SITE-VID-1&cid=1&uid=10001"
        )
        group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "PARTIAL ENGINE ASSEMBLY",
            "10001",
            public_url,
        )
        part = _parts(1)[0]
        assert parts.upsert_parts(group_id, [part], run_id) == 1
        part_row = database._execute(
            "SELECT id FROM parts WHERE group_id=%s AND seen_run_id=%s",
            (group_id, run_id),
        ).fetchone()
        assert part_row is not None
        part_id = int(part_row["id"])
        parser_context = {
            "group_key": {
                "category": {
                    "vehicle": {
                        "brand": "TOYOTA",
                        "model": "CAMRY",
                        **{key: value for key, value in vehicle_key.items() if key != "ssd"},
                    },
                    "cid": "1",
                    "category_name": "ENGINE/FUEL/TOOL",
                },
                "group_code": "1101",
                "uid": "10001",
            }
        }
        html = _parts_html(1)
        sanitized = sanitize_parser_html(html)
        records, malformed_rows, skipped_rows = replay_catalog_records(
            sanitized.body,
            parser_name="parse_parts",
            parser_version=PARSER_CONTRACT_VERSION,
            context=parser_context,
        )
        assert len(records) == 1
        artifact_id = crawl.record_http_evidence(
            run_id,
            scheduled_job_run_id,
            page_type="unit",
            public_url=public_url,
            raw_body_sha256=hashlib.sha256(html.encode()).hexdigest(),
            status_code=200,
            content_type="text/html",
            fetched_at=datetime.now(UTC).replace(tzinfo=None),
            elapsed_ms=1,
            attempt=1,
            sanitized_body=sanitized,
            parser_name="parse_parts",
            parser_version=PARSER_CONTRACT_VERSION,
            parser_context=parser_context,
            parsed_records=records,
            replayed_records=records,
            accepted_records=[(part_id, records[0])],
            malformed_rows=malformed_rows,
            skipped_record_count=skipped_rows,
        )
        database._execute(
            "UPDATE crawl_runs SET evidence_status='verified', "
            "evidence_manifest_sha256=%s, evidence_dataset_sha256=%s, "
            "evidence_artifact_count=1, evidence_record_count=1, "
            "evidence_original_bytes=%s, evidence_stored_bytes=%s, "
            "evidence_verified_at=UTC_TIMESTAMP(6) WHERE id=%s",
            ("a" * 64, "b" * 64, sanitized.original_bytes, sanitized.stored_bytes, run_id),
        )
        database.commit()

        error_html = "<html><title>PartSouq - Error</title><p>Error 404</p></html>"
        error_body = sanitize_parser_html(error_html)
        original_run = database._execute(
            "SELECT evidence_status,evidence_manifest_sha256,evidence_dataset_sha256,"
            "evidence_artifact_count,evidence_record_count,evidence_original_bytes,"
            "evidence_stored_bytes,evidence_verified_at FROM crawl_runs WHERE id=%s",
            (run_id,),
        ).fetchone()
        original_group = database._execute(
            "SELECT fetched_run_key,fetched_status,fetched_row_count FROM groups_t WHERE id=%s",
            (group_id,),
        ).fetchone()
        _bounded_config(monkeypatch, target=10_000)
        instance = Crawler(mock.MagicMock(), database, workers=1)
        instance.run_id = run_id
        instance.scheduled_job_run_id = scheduled_job_run_id
        instance.crawl = crawl
        instance.counts["parts"] = 1
        group = {
            "category_name": "ENGINE/FUEL/TOOL",
            "cid": "1",
            "group_code": "1101",
            "group_name": "PARTIAL ENGINE ASSEMBLY",
            "uid": "10001",
            "url": public_url,
        }
        evidence_vehicle_key = parser_context["group_key"]["category"]["vehicle"]
        original_mark_group_fetched = crawl.mark_group_fetched

        def fail_after_receipt(*args, **kwargs) -> None:
            original_mark_group_fetched(*args, **kwargs)
            raise RuntimeError("injected receipt commit failure")

        monkeypatch.setattr(crawl, "mark_group_fetched", fail_after_receipt)
        instance._fetch = mock.MagicMock(
            side_effect=NotFoundError(
                "http 404",
                _catalog_response(error_html, status_code=404, url=public_url),
            )
        )
        with pytest.raises(RuntimeError, match="^injected receipt commit failure$"):
            instance.crawl_group(
                "TOYOTA",
                vehicle_id,
                group,
                evidence_vehicle_key=evidence_vehicle_key,
            )

        assert database._execute(
            "SELECT verification_status FROM partsouq_http_artifacts WHERE id=%s",
            (artifact_id,),
        ).fetchone() == {"verification_status": "verified"}
        assert database._execute(
            "SELECT seen_run_id FROM parts WHERE id=%s", (part_id,)
        ).fetchone() == {"seen_run_id": run_id}
        assert (
            database._execute(
                "SELECT fetched_run_key,fetched_status,fetched_row_count FROM groups_t WHERE id=%s",
                (group_id,),
            ).fetchone()
            == original_group
        )
        assert (
            database._execute(
                "SELECT evidence_status,evidence_manifest_sha256,evidence_dataset_sha256,"
                "evidence_artifact_count,evidence_record_count,evidence_original_bytes,"
                "evidence_stored_bytes,evidence_verified_at FROM crawl_runs WHERE id=%s",
                (run_id,),
            ).fetchone()
            == original_run
        )
        diagnostic = database._execute(
            "SELECT id,public_source_url,body_sha256,compression,body_blob,"
            "original_bytes,stored_bytes,http_status,attempt "
            "FROM partsouq_http_diagnostics "
            "WHERE crawl_run_id=%s AND group_id=%s AND reason='http_not_found'",
            (run_id, group_id),
        ).fetchone()
        assert diagnostic is not None
        diagnostic_id = int(diagnostic["id"])
        assert diagnostic == {
            "id": diagnostic_id,
            "public_source_url": public_url,
            "body_sha256": error_body.body_sha256,
            "compression": "zlib",
            "body_blob": error_body.compressed,
            "original_bytes": error_body.original_bytes,
            "stored_bytes": error_body.stored_bytes,
            "http_status": 404,
            "attempt": 1,
        }

        monkeypatch.setattr(crawl, "mark_group_fetched", original_mark_group_fetched)
        instance._fetch = mock.MagicMock(
            side_effect=NotFoundError(
                "http 404",
                _catalog_response(error_html, status_code=404, url=public_url),
            )
        )
        assert (
            instance.crawl_group(
                "TOYOTA",
                vehicle_id,
                group,
                evidence_vehicle_key=evidence_vehicle_key,
            )
            is False
        )

        assert database._execute(
            "SELECT verification_status FROM partsouq_http_artifacts WHERE id=%s",
            (artifact_id,),
        ).fetchone() == {"verification_status": "superseded"}
        assert database._execute(
            "SELECT seen_run_id FROM parts WHERE id=%s", (part_id,)
        ).fetchone() == {"seen_run_id": None}
        assert database._execute(
            "SELECT fetched_run_key,fetched_status,fetched_row_count FROM groups_t WHERE id=%s",
            (group_id,),
        ).fetchone() == {
            "fetched_run_key": run_key,
            "fetched_status": "not_found",
            "fetched_row_count": 0,
        }
        final_run = database._execute(
            "SELECT evidence_status,evidence_manifest_sha256,evidence_dataset_sha256,"
            "evidence_artifact_count,evidence_record_count,evidence_original_bytes,"
            "evidence_stored_bytes,evidence_verified_at FROM crawl_runs WHERE id=%s",
            (run_id,),
        ).fetchone()
        assert final_run == {
            "evidence_status": "collecting",
            "evidence_manifest_sha256": None,
            "evidence_dataset_sha256": None,
            "evidence_artifact_count": 0,
            "evidence_record_count": 0,
            "evidence_original_bytes": 0,
            "evidence_stored_bytes": 0,
            "evidence_verified_at": None,
        }
        assert database._execute(
            "SELECT id,COUNT(*) AS row_count FROM partsouq_http_diagnostics "
            "WHERE crawl_run_id=%s AND group_id=%s AND reason='http_not_found' GROUP BY id",
            (run_id, group_id),
        ).fetchone() == {"id": diagnostic_id, "row_count": 1}
    finally:
        if instance is not None:
            instance.close()
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()


def _clear_mysql_fixture(database: Database) -> None:
    # 036 之後已發布 bounded receipt 不可直接刪除；測試清理先讓測試 run
    # 離開 bounded_success，保留正式 schema 的不可變保護。
    database._execute("UPDATE crawl_runs SET status = 'error' WHERE status = 'bounded_success'")
    for table in (
        "bounded_parts",
        "bounded_group_receipts",
        "published_parts_previous",
        "published_parts",
        "partsouq_http_diagnostics",
        "partsouq_http_artifacts",
        "partsouq_response_bodies",
        "admin_vehicle_mappings",
        "catalog_desired_bounded_scope",
        "crawl_state",
        "crawl_runs",
        "scheduled_job_runs",
        "brands",
    ):
        database._execute(f"DELETE FROM {table}")
    database.commit()


@pytest.mark.skipif(
    os.getenv("UNIFIED_TEST_MYSQL") != "1",
    reason="set UNIFIED_TEST_MYSQL=1 to run shared MySQL full snapshot tests",
)
def test_mysql_mark_vehicle_groups_not_found_clears_membership_and_marks() -> None:
    """vehicle/category 頁被站方 302 回 /locate 時，既有零件組依 404 組
    語意收斂：membership 清除＋not_found 標記，closure 才有機會閉合。"""
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
        other_category_id = vehicles.upsert_category(vehicle_id, "BODY/INTERIOR", "2")
        group_id = vehicles.upsert_group(
            category_id,
            "1101",
            "PARTIAL ENGINE ASSEMBLY",
            "10001",
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&cid=1&uid=10001&vid=SITE-VID-1",
        )
        other_group_id = vehicles.upsert_group(
            other_category_id,
            "1102",
            "BODY PART",
            "10002",
            "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&cid=2&uid=10002&vid=SITE-VID-1",
        )
        job_id = database._execute(
            "INSERT INTO scheduled_job_runs (job_name, trigger_mode, status, started_at) "
            "VALUES ('catalog', 'daemon', 'running', UTC_TIMESTAMP())"
        ).lastrowid
        repository = CrawlRepository(database, "not-found-reconcile")
        run_id = repository.start_run(
            "not-found-reconcile",
            fresh=True,
            dataset_kind="full",
            scheduled_job_run_id=job_id,
        )
        parts.upsert_parts(group_id, _parts(1), run_id)
        parts.upsert_parts(other_group_id, _parts(1), run_id)
        database.commit()

        marked = repository.mark_vehicle_groups_not_found(
            vehicle_id,
            run_key=repository.run_key,
            run_id=run_id,
            cid="1",
        )
        database.commit()

        assert marked == 1
        assert database._execute(
            "SELECT fetched_status, fetched_row_count FROM groups_t WHERE id = %s",
            (group_id,),
        ).fetchone() == {"fetched_status": "not_found", "fetched_row_count": 0}
        # 其他分類不受影響
        assert database._execute(
            "SELECT fetched_status FROM groups_t WHERE id = %s",
            (other_group_id,),
        ).fetchone() == {"fetched_status": None}
        assert database._execute(
            "SELECT COUNT(*) AS n FROM parts WHERE group_id = %s AND seen_run_id = %s",
            (group_id, run_id),
        ).fetchone() == {"n": 0}
        assert database._execute(
            "SELECT COUNT(*) AS n FROM parts WHERE group_id = %s AND seen_run_id = %s",
            (other_group_id, run_id),
        ).fetchone() == {"n": 1}

        # 整車層級（不帶 cid）：全部收斂
        marked_all = repository.mark_vehicle_groups_not_found(
            vehicle_id,
            run_key=repository.run_key,
            run_id=run_id,
        )
        database.commit()
        assert marked_all == 1
        assert database._execute(
            "SELECT fetched_status FROM groups_t WHERE id = %s",
            (other_group_id,),
        ).fetchone() == {"fetched_status": "not_found"}
    finally:
        database.rollback()
        _clear_mysql_fixture(database)
        database.close()
