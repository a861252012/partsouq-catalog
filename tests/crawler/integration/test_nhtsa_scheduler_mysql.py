from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pymysql
import pytest

from partsouq_catalog import scheduler

pytestmark = pytest.mark.skipif(
    os.getenv("NHTSA_TEST_MYSQL") != "1",
    reason="set NHTSA_TEST_MYSQL=1 to run MySQL integration tests",
)


@dataclass
class SchedulerRows:
    connection: pymysql.connections.Connection[pymysql.cursors.DictCursor]
    scheduled_ids: list[int] = field(default_factory=list)
    domain_ids: list[int] = field(default_factory=list)
    artifact_ids: list[int] = field(default_factory=list)

    def scheduled(
        self,
        job_name: str,
        *,
        status: str = "running",
        output_text: str | None = None,
        parent_scheduled_job_run_id: int | None = None,
        # 生產上 NHTSA child 只會由 daemon 或 queue 觸發；手動觸發一律由
        # 負向測試以 trigger_mode="manual" 明確傳入。
        trigger_mode: str = "daemon",
    ) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(parent_scheduled_job_run_id, job_name, trigger_mode, status, started_at, "
                "finished_at, exit_code, output_text) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), "
                "CASE WHEN %s = 'running' THEN NULL ELSE UTC_TIMESTAMP() END, "
                "CASE WHEN %s = 'running' THEN NULL ELSE 0 END, %s)",
                (
                    parent_scheduled_job_run_id,
                    job_name,
                    trigger_mode,
                    status,
                    status,
                    status,
                    output_text,
                ),
            )
            run_id = int(cursor.lastrowid)
        self.scheduled_ids.append(run_id)
        return run_id

    def domain(
        self,
        scheduled_job_run_id: int,
        *,
        run_key: str,
        status: str,
    ) -> int:
        if status == "running":
            lease_slot = "writer"
            lease_token = secrets.token_hex(32)
        else:
            lease_slot = None
            lease_token = None
        with self.connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO nhtsa_sync_runs "
                "(scheduled_job_run_id, run_key, scope_name, status, source_keys_json, "
                "lease_slot, lease_token, started_at, updated_at, heartbeat_at, "
                "lease_expires_at, ended_at) VALUES "
                "(%s, %s, 'scheduler-test', %s, JSON_ARRAY('scheduler-test'), %s, %s, "
                "UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), "
                "CASE WHEN %s = 'running' THEN UTC_TIMESTAMP(6) ELSE NULL END, "
                "CASE WHEN %s = 'running' THEN UTC_TIMESTAMP(6) + INTERVAL 10 MINUTE "
                "ELSE NULL END, "
                "CASE WHEN %s = 'running' THEN NULL ELSE UTC_TIMESTAMP(6) END)",
                (
                    scheduled_job_run_id,
                    run_key,
                    status,
                    lease_slot,
                    lease_token,
                    status,
                    status,
                    status,
                ),
            )
            run_id = int(cursor.lastrowid)
        self.domain_ids.append(run_id)
        return run_id

    def fetch_scheduled(self, run_id: int) -> dict[str, object]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, finished_at, exit_code, output_text "
                "FROM scheduled_job_runs WHERE id = %s",
                (run_id,),
            )
            row = cursor.fetchone()
        assert row is not None
        return row

    def fetch_domain(self, run_id: int) -> dict[str, object]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, ended_at, lease_slot, lease_token, lease_expires_at, "
                "error_message FROM nhtsa_sync_runs WHERE id = %s",
                (run_id,),
            )
            row = cursor.fetchone()
        assert row is not None
        return row


@pytest.fixture
def scheduler_rows() -> Iterator[SchedulerRows]:
    connection = scheduler._connect()
    rows = SchedulerRows(connection)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS database_name")
            database = cursor.fetchone()
            assert database is not None
            if not str(database["database_name"]).endswith("_test"):
                raise ValueError("NHTSA_TEST_MYSQL requires a database name ending in _test")
            cursor.execute(
                "SELECT COUNT(*) AS count FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND "
                "((TABLE_NAME = 'scheduled_job_runs' "
                "AND COLUMN_NAME = 'parent_scheduled_job_run_id') OR "
                "(TABLE_NAME = 'nhtsa_sync_runs' "
                "AND COLUMN_NAME IN ('scheduled_job_run_id', 'lease_slot', "
                "'lease_token', 'heartbeat_at', 'lease_expires_at')))"
            )
            schema = cursor.fetchone()
        assert schema is not None
        assert int(schema["count"]) == 6, "apply catalog migration 024 to the _test DB"
        yield rows
    finally:
        with connection.cursor() as cursor:
            if rows.artifact_ids:
                placeholders = ",".join("%s" for _artifact_id in rows.artifact_ids)
                cursor.execute(
                    f"DELETE FROM nhtsa_current_artifacts WHERE artifact_id IN ({placeholders})",
                    tuple(rows.artifact_ids),
                )
                cursor.execute(
                    f"DELETE FROM nhtsa_source_artifacts WHERE id IN ({placeholders})",
                    tuple(rows.artifact_ids),
                )
            if rows.domain_ids:
                placeholders = ",".join("%s" for _run_id in rows.domain_ids)
                cursor.execute(
                    f"DELETE FROM nhtsa_sync_runs WHERE id IN ({placeholders})",
                    tuple(rows.domain_ids),
                )
            if rows.scheduled_ids:
                for scheduled_id in reversed(rows.scheduled_ids):
                    cursor.execute(
                        "DELETE FROM scheduled_job_runs WHERE id = %s",
                        (scheduled_id,),
                    )
        connection.close()


def test_successful_child_without_exact_completed_domain_is_failed(
    scheduler_rows: SchedulerRows,
) -> None:
    child_id = scheduler_rows.scheduled("nhtsa-api")

    return_code = scheduler._record_finish(child_id, 0, "api exited 0")

    assert return_code == 1
    child = scheduler_rows.fetch_scheduled(child_id)
    assert child["status"] == "failed"
    assert child["exit_code"] == 1
    assert "without an exact atomic completed tuple" in str(child["output_text"])


def test_successful_child_without_atomic_completion_interrupts_linked_domain(
    scheduler_rows: SchedulerRows,
) -> None:
    child_id = scheduler_rows.scheduled("nhtsa-api")
    domain_id = scheduler_rows.domain(
        child_id,
        run_key="exit-zero-without-publish",
        status="running",
    )

    return_code = scheduler._record_finish(child_id, 0, "api exited 0")

    assert return_code == 1
    child = scheduler_rows.fetch_scheduled(child_id)
    assert child["status"] == "failed"
    assert child["exit_code"] == 1
    domain = scheduler_rows.fetch_domain(domain_id)
    assert domain["status"] == "interrupted"
    assert domain["ended_at"] is not None
    assert domain["lease_slot"] is None
    assert domain["lease_token"] is None
    assert domain["lease_expires_at"] is None
    assert domain["error_message"] == "linked scheduler child failed"


@pytest.mark.parametrize(
    "system_trigger_mode",
    ["daemon", "queue"],
)
def test_successful_child_with_exact_completed_domain_is_completed(
    scheduler_rows: SchedulerRows,
    system_trigger_mode: str,
) -> None:
    """daemon 與 queue（後台請求）都是合法 NHTSA 觸發來源；兩者的
    exact lineage 完成都必須被接受。"""
    child_id = scheduler_rows.scheduled(
        "nhtsa-bulk",
        status="completed",
        trigger_mode=system_trigger_mode,
    )
    scheduler_rows.domain(child_id, run_key="lineage-bulk", status="completed")

    return_code = scheduler._record_finish(child_id, 0, "bulk exited 0")

    assert return_code == 0
    child = scheduler_rows.fetch_scheduled(child_id)
    assert child["status"] == "completed"
    assert child["exit_code"] == 0
    assert child["finished_at"] is not None


@pytest.mark.parametrize("observed_return_code", [9, -9])
def test_observed_process_failure_cannot_reverse_exact_atomic_completion(
    scheduler_rows: SchedulerRows,
    observed_return_code: int,
) -> None:
    child_id = scheduler_rows.scheduled("nhtsa-api", status="completed")
    domain_id = scheduler_rows.domain(
        child_id,
        run_key="atomic-api",
        status="completed",
    )

    return_code = scheduler._record_finish(
        child_id,
        observed_return_code,
        "child process failed",
    )

    assert return_code == 0
    child = scheduler_rows.fetch_scheduled(child_id)
    assert child["status"] == "completed"
    assert child["exit_code"] == 0
    assert f"process exit {observed_return_code} was observed" in str(child["output_text"])
    assert "completed state preserved" in str(child["output_text"])
    domain = scheduler_rows.fetch_domain(domain_id)
    assert domain["status"] == "completed"
    assert domain["error_message"] is None


def test_completed_child_without_finished_at_is_not_an_atomic_completion(
    scheduler_rows: SchedulerRows,
) -> None:
    child_id = scheduler_rows.scheduled("nhtsa-api")
    domain_id = scheduler_rows.domain(
        child_id,
        run_key="incomplete-api",
        status="completed",
    )
    with scheduler_rows.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE scheduled_job_runs SET status = 'completed', exit_code = 0 WHERE id = %s",
            (child_id,),
        )

    return_code = scheduler._record_finish(child_id, 9, "child process failed")

    assert return_code == 9
    child = scheduler_rows.fetch_scheduled(child_id)
    assert child["status"] == "failed"
    assert child["exit_code"] == 9
    domain = scheduler_rows.fetch_domain(domain_id)
    assert domain["status"] == "completed"
    assert domain["error_message"] is None


def test_record_finish_waits_for_domain_before_child_without_deadlock(
    scheduler_rows: SchedulerRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_id = scheduler_rows.scheduled("nhtsa-api")
    domain_id = scheduler_rows.domain(
        child_id,
        run_key="barrier-api",
        status="running",
    )
    domain_lock_attempted = threading.Event()
    results: list[int] = []
    errors: list[BaseException] = []
    real_execute = pymysql.cursors.DictCursor.execute

    def observe_domain_lock(
        cursor: pymysql.cursors.DictCursor,
        query: str,
        args: Any = None,
    ) -> int:
        if (
            threading.current_thread().name == "scheduler-record-finish"
            and "FROM nhtsa_sync_runs WHERE scheduled_job_run_id" in query
            and query.endswith("FOR UPDATE")
        ):
            domain_lock_attempted.set()
        return real_execute(cursor, query, args)

    monkeypatch.setattr(pymysql.cursors.DictCursor, "execute", observe_domain_lock)

    def record_finish() -> None:
        try:
            results.append(scheduler._record_finish(child_id, -9, "child was signalled"))
        except BaseException as error:
            errors.append(error)

    finalizer = scheduler._connect()
    worker = threading.Thread(target=record_finish, name="scheduler-record-finish")
    worker_started = False
    try:
        finalizer.begin()
        with finalizer.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM nhtsa_sync_runs WHERE id = %s FOR UPDATE",
                (domain_id,),
            )
            assert cursor.fetchone() is not None
        worker.start()
        worker_started = True
        assert domain_lock_attempted.wait(timeout=3)
        with finalizer.cursor() as cursor:
            cursor.execute(
                "UPDATE nhtsa_sync_runs SET status = 'completed', "
                "updated_at = UTC_TIMESTAMP(6), ended_at = UTC_TIMESTAMP(6), "
                "lease_slot = NULL, lease_token = NULL, lease_expires_at = NULL "
                "WHERE id = %s",
                (domain_id,),
            )
            cursor.execute(
                "UPDATE scheduled_job_runs SET status = 'completed', "
                "finished_at = UTC_TIMESTAMP(), exit_code = 0 WHERE id = %s",
                (child_id,),
            )
        finalizer.commit()
    except BaseException:
        finalizer.rollback()
        raise
    finally:
        if worker_started:
            worker.join(timeout=5)
        finalizer.close()

    assert worker_started
    assert not worker.is_alive()
    assert errors == []
    assert results == [0]
    child = scheduler_rows.fetch_scheduled(child_id)
    assert child["status"] == "completed"
    assert child["exit_code"] == 0
    assert "process exit -9 was observed" in str(child["output_text"])
    domain = scheduler_rows.fetch_domain(domain_id)
    assert domain["status"] == "completed"
    assert domain["lease_slot"] is None


def test_child_failure_interrupts_only_exact_linked_domain(
    scheduler_rows: SchedulerRows,
) -> None:
    failed_child_id = scheduler_rows.scheduled("nhtsa-api")
    unrelated_child_id = scheduler_rows.scheduled("nhtsa-api")
    unrelated_domain_id = scheduler_rows.domain(
        unrelated_child_id,
        run_key="2026-08-api-near-match",
        status="running",
    )

    return_code = scheduler._record_finish(failed_child_id, 17, "api failed")

    assert return_code == 17
    assert scheduler_rows.fetch_scheduled(failed_child_id)["status"] == "failed"
    unrelated_domain = scheduler_rows.fetch_domain(unrelated_domain_id)
    assert unrelated_domain["status"] == "running"
    assert unrelated_domain["lease_slot"] == "writer"
    assert unrelated_domain["error_message"] is None

    with scheduler_rows.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE nhtsa_sync_runs SET status = 'interrupted', "
            "updated_at = UTC_TIMESTAMP(6), ended_at = UTC_TIMESTAMP(6), "
            "lease_slot = NULL, lease_token = NULL, lease_expires_at = NULL "
            "WHERE id = %s",
            (unrelated_domain_id,),
        )

    linked_child_id = scheduler_rows.scheduled("nhtsa-vin")
    linked_domain_id = scheduler_rows.domain(
        linked_child_id,
        run_key="2026-08-api",
        status="running",
    )

    return_code = scheduler._record_finish(linked_child_id, 18, "vin failed")

    assert return_code == 18
    linked_domain = scheduler_rows.fetch_domain(linked_domain_id)
    assert linked_domain["status"] == "interrupted"
    assert linked_domain["ended_at"] is not None
    assert linked_domain["lease_slot"] is None
    assert linked_domain["lease_token"] is None
    assert linked_domain["lease_expires_at"] is None
    assert linked_domain["error_message"] == "linked scheduler child failed"


def test_marker_and_similar_run_key_without_foreign_key_do_not_authorize_success(
    scheduler_rows: SchedulerRows,
) -> None:
    child_id = scheduler_rows.scheduled(
        "nhtsa-api",
        output_text=f"{scheduler.NHTSA_API_COMPLETED}\n",
    )
    other_child_id = scheduler_rows.scheduled("nhtsa-api", status="completed")
    other_domain_id = scheduler_rows.domain(
        other_child_id,
        run_key="2026-08-api",
        status="completed",
    )

    return_code = scheduler._record_finish(child_id, 0, "2026-08-api completed")

    assert return_code == 1
    assert scheduler_rows.fetch_scheduled(child_id)["status"] == "failed"
    assert scheduler_rows.fetch_domain(other_domain_id)["status"] == "completed"


def test_parent_recovery_rejects_completed_child_without_finished_at(
    scheduler_rows: SchedulerRows,
) -> None:
    parent_id = scheduler_rows.scheduled("nhtsa", status="failed", trigger_mode="daemon")
    bulk_child_id = scheduler_rows.scheduled(
        "nhtsa-bulk",
        status="completed",
        parent_scheduled_job_run_id=parent_id,
        trigger_mode="daemon",
    )
    api_child_id = scheduler_rows.scheduled(
        "nhtsa-api",
        status="completed",
        parent_scheduled_job_run_id=parent_id,
        trigger_mode="daemon",
    )
    bulk_domain_id = scheduler_rows.domain(
        bulk_child_id,
        run_key=f"recovery-bulk-{secrets.token_hex(6)}",
        status="completed",
    )
    api_domain_id = scheduler_rows.domain(
        api_child_id,
        run_key=f"recovery-api-{secrets.token_hex(6)}",
        status="completed",
    )

    with scheduler_rows.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE scheduled_job_runs SET finished_at = NULL WHERE id = %s",
            (bulk_child_id,),
        )
        cursor.execute(
            "UPDATE scheduled_job_runs SET exit_code = %s WHERE id = %s",
            (scheduler.INTERRUPTED_EXIT_CODE, parent_id),
        )
        for dataset_name, domain_id in (
            (f"recovery_bulk_{secrets.token_hex(6)}", bulk_domain_id),
            (f"recovery_api_{secrets.token_hex(6)}", api_domain_id),
        ):
            source_key = secrets.token_hex(8)
            cursor.execute(
                "INSERT INTO nhtsa_source_artifacts "
                "(dataset_name, source_key, source_url, http_status, "
                "response_headers_json, sha256, stored_path, byte_count, parser_name, "
                "parser_version, status, downloaded_at, imported_at) "
                "VALUES (%s, %s, 'https://example.invalid/nhtsa-recovery', 200, "
                "JSON_OBJECT(), %s, '/tmp/nhtsa-recovery', 1, 'test', '1', "
                "'imported', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
                (dataset_name, source_key, secrets.token_hex(32)),
            )
            artifact_id = int(cursor.lastrowid)
            scheduler_rows.artifact_ids.append(artifact_id)
            cursor.execute(
                "INSERT INTO nhtsa_current_artifacts "
                "(dataset_name, source_key, artifact_id, published_run_id, published_at) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(6))",
                (dataset_name, source_key, artifact_id, domain_id),
            )

    assert scheduler._recover_interrupted_job_runs("nhtsa") is False
    parent = scheduler_rows.fetch_scheduled(parent_id)
    assert parent["status"] == "failed"
    assert parent["finished_at"] is not None
    assert parent["exit_code"] == scheduler.INTERRUPTED_EXIT_CODE

    with scheduler_rows.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE scheduled_job_runs SET finished_at = UTC_TIMESTAMP() WHERE id = %s",
            (bulk_child_id,),
        )

    assert scheduler._recover_interrupted_job_runs("nhtsa") is True
    parent = scheduler_rows.fetch_scheduled(parent_id)
    assert parent["status"] == "completed"
    assert parent["finished_at"] is not None
    assert parent["exit_code"] == 0


def test_parent_recovery_ignores_manual_completed_lineage(
    scheduler_rows: SchedulerRows,
) -> None:
    parent_id = scheduler_rows.scheduled("nhtsa", status="failed", trigger_mode="manual")
    bulk_child_id = scheduler_rows.scheduled(
        "nhtsa-bulk",
        status="completed",
        parent_scheduled_job_run_id=parent_id,
        trigger_mode="manual",
    )
    api_child_id = scheduler_rows.scheduled(
        "nhtsa-api",
        status="completed",
        parent_scheduled_job_run_id=parent_id,
        trigger_mode="manual",
    )
    bulk_domain_id = scheduler_rows.domain(
        bulk_child_id,
        run_key=f"manual-bulk-{secrets.token_hex(6)}",
        status="completed",
    )
    api_domain_id = scheduler_rows.domain(
        api_child_id,
        run_key=f"manual-api-{secrets.token_hex(6)}",
        status="completed",
    )
    with scheduler_rows.connection.cursor() as cursor:
        # 與 daemon 對照組（test_parent_recovery_rejects_completed_child_...
        # without_finished_at）相同：先模擬已中斷的 parent，證明差異只來自
        # trigger_mode='manual'——daemon 會被自動對帳，manual 一律不碰。
        cursor.execute(
            "UPDATE scheduled_job_runs SET exit_code = %s WHERE id = %s",
            (scheduler.INTERRUPTED_EXIT_CODE, parent_id),
        )
        for dataset_name, domain_id in (
            (f"manual_bulk_{secrets.token_hex(6)}", bulk_domain_id),
            (f"manual_api_{secrets.token_hex(6)}", api_domain_id),
        ):
            source_key = secrets.token_hex(8)
            cursor.execute(
                "INSERT INTO nhtsa_source_artifacts "
                "(dataset_name, source_key, source_url, http_status, "
                "response_headers_json, sha256, stored_path, byte_count, parser_name, "
                "parser_version, status, downloaded_at, imported_at) "
                "VALUES (%s, %s, 'https://example.invalid/nhtsa-manual', 200, "
                "JSON_OBJECT(), %s, '/tmp/nhtsa-manual', 1, 'test', '1', "
                "'imported', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))",
                (dataset_name, source_key, secrets.token_hex(32)),
            )
            artifact_id = int(cursor.lastrowid)
            scheduler_rows.artifact_ids.append(artifact_id)
            cursor.execute(
                "INSERT INTO nhtsa_current_artifacts "
                "(dataset_name, source_key, artifact_id, published_run_id, published_at) "
                "VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(6))",
                (dataset_name, source_key, artifact_id, domain_id),
            )

    assert scheduler._recover_interrupted_job_runs("nhtsa") is False
    parent = scheduler_rows.fetch_scheduled(parent_id)
    assert parent["status"] == "failed"
    assert parent["exit_code"] == scheduler.INTERRUPTED_EXIT_CODE


def test_stale_nhtsa_child_recovery_ignores_manual_rows(
    scheduler_rows: SchedulerRows,
) -> None:
    bulk_child_id = scheduler_rows.scheduled(
        "nhtsa-bulk",
        status="running",
        trigger_mode="manual",
    )
    scheduler_rows.domain(
        bulk_child_id,
        run_key=f"manual-stale-{secrets.token_hex(6)}",
        status="running",
    )
    with scheduler_rows.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE scheduled_job_runs SET started_at = "
            "UTC_TIMESTAMP() - INTERVAL %s SECOND WHERE id = %s",
            (scheduler.RECOVERY_MIN_AGE_SECONDS + 60, bulk_child_id),
        )
        # 合法 running 狀態必須帶完整 writer lease（chk_nhtsa_sync_status_lease）；
        # 這裡把 lease_expires_at 調到過期，模擬 queue/daemon 中斷後的 stale child。
        cursor.execute(
            "UPDATE nhtsa_sync_runs SET lease_slot = 'writer', "
            "lease_token = %s, heartbeat_at = UTC_TIMESTAMP(6) - INTERVAL 30 SECOND, "
            "lease_expires_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND "
            "WHERE scheduled_job_run_id = %s",
            (secrets.token_hex(32), bulk_child_id),
        )

    assert scheduler._recover_interrupted_job_runs("nhtsa") is False
    child = scheduler_rows.fetch_scheduled(bulk_child_id)
    assert child["status"] == "running"


def test_record_finish_rolls_back_domain_update_when_scheduler_update_fails(
    scheduler_rows: SchedulerRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_id = scheduler_rows.scheduled("nhtsa-api")
    domain_id = scheduler_rows.domain(
        child_id,
        run_key="rollback-api",
        status="running",
    )

    real_execute = pymysql.cursors.DictCursor.execute

    def fail_scheduler_update(
        cursor: pymysql.cursors.DictCursor,
        query: str,
        args: Any = None,
    ) -> int:
        if query.startswith("UPDATE scheduled_job_runs"):
            raise pymysql.MySQLError("forced scheduler finish failure")
        return real_execute(cursor, query, args)

    monkeypatch.setattr(pymysql.cursors.DictCursor, "execute", fail_scheduler_update)
    with pytest.raises(pymysql.MySQLError, match="forced scheduler finish failure"):
        scheduler._record_finish(child_id, 19, "api failed")

    child = scheduler_rows.fetch_scheduled(child_id)
    assert child["status"] == "running"
    assert child["finished_at"] is None
    assert child["exit_code"] is None
    domain = scheduler_rows.fetch_domain(domain_id)
    assert domain["status"] == "running"
    assert domain["ended_at"] is None
    assert domain["lease_slot"] == "writer"
    assert domain["lease_token"] is not None
    assert domain["lease_expires_at"] is not None
    assert domain["error_message"] is None
