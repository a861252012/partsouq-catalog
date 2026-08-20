from __future__ import annotations

import errno
import io
import sys
from unittest import mock

import pytest

from partsouq_catalog import scheduler


class FakeStopEvent:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return self.stopped


def test_record_start_persists_trigger_mode(monkeypatch) -> None:
    cursor = mock.MagicMock(lastrowid=77)
    cursor.__enter__.return_value = cursor
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    assert scheduler._record_start("catalog") == 77
    assert cursor.execute.call_args.args[1] == ("catalog", "manual")

    scheduler._JOB_CONTEXT.trigger_mode = "daemon"
    try:
        assert scheduler._record_start("catalog") == 77
        assert cursor.execute.call_args.args[1] == ("catalog", "daemon")
    finally:
        del scheduler._JOB_CONTEXT.trigger_mode


def test_api_progress_persists_actual_stage_completion_time(monkeypatch) -> None:
    cursor = mock.MagicMock(rowcount=1)
    cursor.__enter__.return_value = cursor
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    scheduler._record_progress(78, scheduler.NHTSA_API_COMPLETED)

    statement, params = cursor.execute.call_args.args
    assert "finished_at = CASE WHEN %s THEN UTC_TIMESTAMP()" in statement
    assert params == (
        f"{scheduler.NHTSA_API_COMPLETED}\n",
        scheduler.MAX_OUTPUT_CHARS,
        True,
        78,
    )


def test_run_records_scheduler_id_in_child_environment(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Process:
        returncode = 0
        stdout = io.StringIO("completed\n")

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return 0

    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Process()

    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 42)
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    monkeypatch.setenv("SCHEDULED_JOB_RUN_ID", "stale")
    monkeypatch.delenv("PSQ_BOUNDED_PARTS", raising=False)

    assert scheduler._run("catalog", ["python", "-m", "crawler"]) == 0

    environment = captured["kwargs"]["env"]
    assert environment["SCHEDULED_JOB_RUN_ID"] == "42"
    assert finished == [(42, 0, "completed\n")]


def test_run_streams_output_and_only_records_bounded_redacted_tail(monkeypatch, capsys) -> None:
    vin = "1M8GDM9AXKP042788"
    prefix = "first-line\n"
    large_output = [prefix, *[f"{vin} {'x' * 990}\n" for _ in range(100)]]

    class Process:
        returncode = 0
        stdout = io.StringIO("".join(large_output))

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 43)
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )

    command = ["python", "-m", "partsouq_crawler", "nhtsa-decode-vin", vin]
    assert scheduler._run("nhtsa-vin", command) == 0

    recorded = finished[0][2]
    streamed = capsys.readouterr().out
    assert len(recorded) == scheduler.MAX_OUTPUT_CHARS
    assert prefix not in recorded
    assert vin not in recorded
    assert vin not in streamed
    assert "1M8**********2788" in recorded


def test_execution_lock_rejects_a_second_copy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    first_lock = scheduler._try_lock("scheduler-job", "nhtsa")
    assert first_lock is not None
    called = False

    def dispatch(_job: str, _scope: str) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(scheduler, "dispatch", dispatch)
    monkeypatch.setattr(scheduler, "_recover_interrupted_job_runs", lambda _job: None)
    try:
        assert scheduler.dispatch_locked("nhtsa-api", "all") == scheduler.LOCK_BUSY_EXIT_CODE
        assert called is False
    finally:
        first_lock.close()

    assert scheduler.dispatch_locked("nhtsa-api", "all") == 0
    assert called is True


def test_pending_request_stays_pending_when_same_job_is_running(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "_requeue_interrupted_requests", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_pending_requests",
        lambda: [{"id": 7, "job_name": "nhtsa-vin", "requested_scope": "VIN"}],
    )
    monkeypatch.setattr(scheduler, "_claim_request", lambda _request_id: True)
    monkeypatch.setattr(
        scheduler,
        "dispatch_locked",
        lambda _job, _scope: scheduler.LOCK_BUSY_EXIT_CODE,
    )
    deferred: list[int] = []
    monkeypatch.setattr(scheduler, "_defer_request", deferred.append)
    finish_request = mock.MagicMock()
    monkeypatch.setattr(scheduler, "_finish_request", finish_request)

    assert scheduler.dispatch("pending", "all") == 0
    assert deferred == [7]
    finish_request.assert_not_called()


def test_pending_rejects_catalog_without_blocking_vin_worker(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "_requeue_interrupted_requests", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_pending_requests",
        lambda: [{"id": 8, "job_name": "catalog", "requested_scope": "all"}],
    )
    monkeypatch.setattr(scheduler, "_claim_request", lambda _request_id: True)
    dispatch_locked = mock.MagicMock()
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch_locked)
    finish_request = mock.MagicMock()
    monkeypatch.setattr(scheduler, "_finish_request", finish_request)

    assert scheduler.dispatch("pending", "all") == 0
    dispatch_locked.assert_not_called()
    finish_request.assert_called_once_with(
        8,
        2,
        "catalog is handled by the dedicated catalog daemon",
    )


def test_pending_jobs_record_queue_trigger_mode(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "_requeue_interrupted_requests", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_pending_requests",
        lambda: [{"id": 9, "job_name": "nhtsa-vin", "requested_scope": "VIN"}],
    )
    monkeypatch.setattr(scheduler, "_claim_request", lambda _request_id: True)
    modes: list[str] = []

    def dispatch_locked(_job: str, _scope: str) -> int:
        modes.append(scheduler._JOB_CONTEXT.trigger_mode)
        return 0

    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch_locked)
    monkeypatch.setattr(scheduler, "_finish_request", lambda *_args: None)

    assert scheduler.dispatch("pending", "all") == 0
    assert modes == ["queue"]
    assert not hasattr(scheduler._JOB_CONTEXT, "trigger_mode")


def test_nhtsa_daemon_uses_last_completed_api_run(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {
        "job_name": "nhtsa",
        "status": "completed",
        "age_seconds": 40,
    }
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler.pymysql, "connect", lambda **_kwargs: connection)

    assert scheduler._seconds_until_next_run("nhtsa", 100) == 60
    assert cursor.execute.call_args.args[1] == ("nhtsa",)
    assert "trigger_mode = 'daemon'" in cursor.execute.call_args.args[0]
    connection.close.assert_called_once_with()


def test_failed_latest_run_is_due_immediately(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {
        "job_name": "nhtsa",
        "status": "failed",
        "age_seconds": 10,
    }
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler.pymysql, "connect", lambda **_kwargs: connection)

    assert scheduler._seconds_until_next_run("nhtsa", 100) == 0


def test_catalog_is_not_delayed_without_exact_bounded_success(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {
        "job_name": "catalog",
        "status": "completed",
        "age_seconds": 10,
        "dataset_kind": "bounded",
        "crawl_status": "bounded_success",
        "target_parts": 10_000,
        "parts_ok": 10_000,
        "snapshot_rows": 9_999,
    }
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler.pymysql, "connect", lambda **_kwargs: connection)
    monkeypatch.setenv("PSQ_BOUNDED_PARTS", "10000")

    assert scheduler._seconds_until_next_run("catalog", 100) == 0

    cursor.fetchone.return_value["snapshot_rows"] = 10_000
    assert scheduler._seconds_until_next_run("catalog", 100) == 90
    assert "jobs.trigger_mode = 'daemon'" in cursor.execute.call_args.args[0]


def test_catalog_crash_after_publish_is_reconciled_without_recrawl(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor

    def execute(statement: str, _params=None) -> None:
        cursor.rowcount = 1 if "JOIN crawl_runs AS runs" in statement else 0

    cursor.execute.side_effect = execute
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    monkeypatch.setenv("PSQ_BOUNDED_PARTS", "10000")

    assert scheduler._recover_interrupted_job_runs("catalog") is True

    reconciliation_call = next(
        call for call in cursor.execute.call_args_list if "JOIN crawl_runs" in call.args[0]
    )
    reconciliation = reconciliation_call.args[0]
    assert "jobs.trigger_mode = 'daemon'" in reconciliation
    assert "runs.status = 'bounded_success'" in reconciliation
    assert "jobs.finished_at = runs.finished_at" in reconciliation
    assert "runs.finished_at IS NOT NULL" in reconciliation
    # 對帳不依賴排程器自身的 PSQ_BOUNDED_PARTS：以 DB 內的
    # target/parts_ok/snapshot 一致性為準。
    assert "runs.target_parts > 0" in reconciliation
    assert "runs.parts_ok = runs.target_parts" in reconciliation
    assert "snapshots.snapshot_rows = runs.target_parts" in reconciliation
    assert reconciliation_call.args[1][0].endswith("reconciled automatically\n")
    connection.begin.assert_called_once_with()
    connection.commit.assert_called_once_with()
    connection.rollback.assert_not_called()


def test_reconciled_catalog_daemon_skips_child(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "_recover_interrupted_job_runs", lambda _job: True)
    dispatch = mock.MagicMock()
    monkeypatch.setattr(scheduler, "dispatch", dispatch)
    scheduler._JOB_CONTEXT.trigger_mode = "daemon"
    try:
        assert scheduler.dispatch_locked("catalog", "all") == 0
    finally:
        del scheduler._JOB_CONTEXT.trigger_mode
    dispatch.assert_not_called()


def test_catalog_reconciliation_rolls_back_if_stale_cleanup_fails(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    calls = 0

    def execute(statement: str, _params=None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            cursor.rowcount = 1
            assert "JOIN crawl_runs AS runs" in statement
            return
        raise scheduler.pymysql.OperationalError("cleanup failed")

    cursor.execute.side_effect = execute
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    monkeypatch.setenv("PSQ_BOUNDED_PARTS", "10000")

    with pytest.raises(scheduler.pymysql.OperationalError, match="cleanup failed"):
        scheduler._recover_interrupted_job_runs("catalog")

    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()


def test_catalog_recovery_marks_stale_running_rows_interrupted(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.rowcount = 0
    cursor.execute.side_effect = lambda *_args, **_kwargs: None
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    monkeypatch.delenv("PSQ_BOUNDED_PARTS", raising=False)

    assert scheduler._recover_interrupted_job_runs("catalog") is False

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert len(statements) == 3
    reconcile_call, stale_jobs_call, stale_runs_call = cursor.execute.call_args_list
    # 第一筆是 bounded 對帳（env 獨立），rowcount=0 → 無需略過 dispatch
    assert "dataset_kind = 'bounded'" in reconcile_call.args[0]
    stale_jobs = stale_jobs_call.args[0]
    assert "SET status = 'failed'" in stale_jobs
    assert "job_name = %s" in stale_jobs
    # 年齡閘：啟動未滿 RECOVERY_MIN_AGE_SECONDS 的 run 不得被自動翻 failed
    assert "started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND" in stale_jobs
    assert stale_jobs_call.args[1][0] == scheduler.INTERRUPTED_EXIT_CODE
    assert stale_jobs_call.args[1][-1] == scheduler.RECOVERY_MIN_AGE_SECONDS
    stale_runs = stale_runs_call.args[0]
    assert "SET runs.status = 'interrupted'" in stale_runs
    assert "JOIN scheduled_job_runs AS jobs" in stale_runs
    assert "jobs.status = 'failed'" in stale_runs
    assert "jobs.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND" in stale_runs
    connection.commit.assert_called_once_with()


def test_nhtsa_recovery_closes_scheduler_owned_domain_run(monkeypatch) -> None:
    cursor = mock.MagicMock(rowcount=0)
    cursor.__enter__.return_value = cursor
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    assert scheduler._recover_interrupted_job_runs("nhtsa") is False

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("UPDATE nhtsa_sync_runs" in statement for statement in statements)
    domain_recovery = next(
        statement for statement in statements if "UPDATE nhtsa_sync_runs" in statement
    )
    assert "status = 'interrupted'" in domain_recovery
    assert "run_key REGEXP" in domain_recovery
    connection.commit.assert_called_once_with()


def test_nhtsa_api_commit_is_reconciled_without_repeating_stages(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor

    def execute(statement: str, _params=None) -> None:
        cursor.rowcount = 1 if "output_text LIKE" in statement else 0

    cursor.execute.side_effect = execute
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    assert scheduler._recover_interrupted_job_runs("nhtsa") is True
    reconciliation = next(
        call for call in cursor.execute.call_args_list if "output_text LIKE" in call.args[0]
    )
    assert reconciliation.args[1][-1] == f"%{scheduler.NHTSA_API_COMPLETED}%"
    assert "finished_at = COALESCE(finished_at, started_at)" in reconciliation.args[0]
    connection.commit.assert_called_once_with()


def test_daemon_retries_with_bounded_exponential_backoff(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    monkeypatch.setattr(scheduler, "_seconds_until_next_run", lambda _job, _interval: 0.0)
    results = iter((3, 1, 0))
    trigger_modes: list[str] = []

    def dispatch(_job: str, _scope: str) -> int:
        trigger_modes.append(scheduler._JOB_CONTEXT.trigger_mode)
        result = next(results)
        if result == 0:
            stop_event.stopped = True
        return result

    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon(
            "catalog",
            "all",
            1000,
            10,
            15,
            stop_event=stop_event,
        )
        == 0
    )
    assert stop_event.waits == [0.0, 10.0, 15.0]
    assert trigger_modes == ["daemon", "daemon", "daemon"]
    assert not hasattr(scheduler._JOB_CONTEXT, "trigger_mode")
    daemon_lock.close.assert_called_once_with()


def test_daemon_stops_retrying_after_max_consecutive_failures(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    monkeypatch.setattr(scheduler, "_seconds_until_next_run", lambda _job, _interval: 0.0)
    calls = 0

    def dispatch(_job: str, _scope: str) -> int:
        nonlocal calls
        calls += 1
        if calls > scheduler.MAX_CONSECUTIVE_FAILURES:
            stop_event.stopped = True
        return 1

    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon(
            "catalog",
            "all",
            60,
            10,
            1000,
            stop_event=stop_event,
        )
        == 0
    )
    # 前 MAX_CONSECUTIVE_FAILURES 次走指數重試（10→20→40→80→160）；
    # 達標後直接等整個 interval（60s），不再以退避每輪重發。
    assert calls == scheduler.MAX_CONSECUTIVE_FAILURES + 1
    assert stop_event.waits[-1] == 60.0


def test_successful_daemon_waits_full_interval_before_next_cycle(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    due_times = iter((0.0, 100.0, 0.0))
    monkeypatch.setattr(
        scheduler,
        "_seconds_until_next_run",
        lambda _job, _interval: next(due_times),
    )
    calls = 0

    def dispatch(_job: str, _scope: str) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            stop_event.stopped = True
        return 0

    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon(
            "catalog",
            "all",
            100,
            10,
            100,
            stop_event=stop_event,
        )
        == 0
    )
    assert calls == 2
    assert stop_event.waits == [0.0, 0.0, 100.0]


def test_pending_signal_exit_is_requeued_and_not_reported_success(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "_requeue_interrupted_requests", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_pending_requests",
        lambda: [{"id": 9, "job_name": "nhtsa-vin", "requested_scope": "VIN"}],
    )
    monkeypatch.setattr(scheduler, "_claim_request", lambda _request_id: True)
    monkeypatch.setattr(scheduler, "dispatch_locked", lambda _job, _scope: -15)
    deferred: list[int] = []
    monkeypatch.setattr(scheduler, "_defer_request", deferred.append)
    finish_request = mock.MagicMock()
    monkeypatch.setattr(scheduler, "_finish_request", finish_request)

    assert scheduler.dispatch("pending", "all") == 1
    assert deferred == [9]
    finish_request.assert_not_called()


def test_pending_invalid_vin_failure_is_terminal(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "_requeue_interrupted_requests", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_pending_requests",
        lambda: [{"id": 10, "job_name": "nhtsa-vin", "requested_scope": "VIN"}],
    )
    monkeypatch.setattr(scheduler, "_claim_request", lambda _request_id: True)
    monkeypatch.setattr(scheduler, "dispatch_locked", lambda _job, _scope: 1)
    defer_request = mock.MagicMock()
    monkeypatch.setattr(scheduler, "_defer_request", defer_request)
    finish_request = mock.MagicMock()
    monkeypatch.setattr(scheduler, "_finish_request", finish_request)

    assert scheduler.dispatch("pending", "all") == 1
    defer_request.assert_not_called()
    finish_request.assert_called_once_with(10, 1)


def test_interrupted_queue_rows_are_recovered_before_read(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        scheduler,
        "_requeue_interrupted_requests",
        lambda: events.append("recovered"),
    )
    monkeypatch.setattr(
        scheduler,
        "_pending_requests",
        lambda: events.append("read") or [],
    )

    assert scheduler.dispatch("pending", "all") == 0
    assert events == ["recovered", "read"]


def test_database_failure_uses_backoff_instead_of_tight_loop(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )

    checks = 0

    def database_unavailable(_job: str, _interval: int) -> float:
        nonlocal checks
        checks += 1
        if checks == 2:
            stop_event.stopped = True
        raise scheduler.pymysql.OperationalError("database unavailable")

    monkeypatch.setattr(scheduler, "_seconds_until_next_run", database_unavailable)
    dispatch = mock.MagicMock()
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon(
            "catalog",
            "all",
            1000,
            60,
            3600,
            stop_event=stop_event,
        )
        == 0
    )
    assert stop_event.waits == [0.0, 60.0, 120.0]
    dispatch.assert_not_called()


def test_completion_check_failure_requeries_without_duplicate_work(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    checks = 0

    def next_run(_job: str, _interval: int) -> float:
        nonlocal checks
        checks += 1
        if checks == 1:
            return 0.0
        if checks == 2:
            raise scheduler.pymysql.OperationalError("temporary read failure")
        stop_event.stopped = True
        return 100.0

    monkeypatch.setattr(scheduler, "_seconds_until_next_run", next_run)
    dispatch = mock.MagicMock(return_value=0)
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon(
            "catalog",
            "all",
            100,
            10,
            100,
            stop_event=stop_event,
        )
        == 0
    )
    assert stop_event.waits == [0.0, 0.0, 10.0, 100.0]
    dispatch.assert_called_once_with("catalog", "all")


def test_lock_busy_rechecks_cadence_before_starting_work(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    checks = 0

    def next_run(_job: str, _interval: int) -> float:
        nonlocal checks
        checks += 1
        if checks == 1:
            return 0.0
        stop_event.stopped = True
        return 100.0

    monkeypatch.setattr(scheduler, "_seconds_until_next_run", next_run)
    dispatch = mock.MagicMock(return_value=scheduler.LOCK_BUSY_EXIT_CODE)
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon(
            "catalog",
            "all",
            100,
            10,
            100,
            stop_event=stop_event,
        )
        == 0
    )
    assert stop_event.waits == [0.0, 10.0, 100.0]
    dispatch.assert_called_once_with("catalog", "all")


def test_ambiguous_finish_failure_rechecks_cadence_before_retrying_work(
    monkeypatch,
) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    checks = 0

    def next_run(_job: str, _interval: int) -> float:
        nonlocal checks
        checks += 1
        if checks == 1:
            return 0.0
        stop_event.stopped = True
        return 100.0

    monkeypatch.setattr(scheduler, "_seconds_until_next_run", next_run)
    dispatch = mock.MagicMock(return_value=1)
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon(
            "catalog",
            "all",
            100,
            10,
            100,
            stop_event=stop_event,
        )
        == 0
    )
    assert stop_event.waits == [0.0, 10.0, 100.0]
    dispatch.assert_called_once_with("catalog", "all")


def test_nhtsa_retry_stage_only_uses_latest_failed_daemon_parent(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {
        "status": "failed",
        "output_text": f"{scheduler.NHTSA_BULK_COMPLETED}\n",
    }
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_dict_connect", lambda: connection)
    scheduler._JOB_CONTEXT.trigger_mode = "daemon"
    try:
        assert scheduler._nhtsa_bulk_completed_for_retry() is True
        cursor.fetchone.return_value["status"] = "completed"
        assert scheduler._nhtsa_bulk_completed_for_retry() is False
    finally:
        del scheduler._JOB_CONTEXT.trigger_mode
    query = cursor.execute.call_args.args[0]
    assert "started_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY" in query
    assert "ORDER BY started_at DESC" in query


def test_nhtsa_retry_does_not_resume_an_expired_bulk_stage(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_dict_connect", lambda: connection)
    scheduler._JOB_CONTEXT.trigger_mode = "daemon"
    try:
        assert scheduler._nhtsa_bulk_completed_for_retry() is False
    finally:
        del scheduler._JOB_CONTEXT.trigger_mode
    assert "INTERVAL 1 DAY" in cursor.execute.call_args.args[0]


def test_nhtsa_retry_resumes_api_without_repeating_bulk(monkeypatch) -> None:
    retry_stage = iter((False, True))
    monkeypatch.setattr(
        scheduler,
        "_nhtsa_bulk_completed_for_retry",
        lambda: next(retry_stage),
    )
    run_ids = iter((81, 82))
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: next(run_ids))
    progress: list[tuple[int, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_record_progress",
        lambda run_id, marker: progress.append((run_id, marker)),
    )
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    calls: list[str] = []
    results = iter((0, 1, 0))

    def dispatch(job: str, _scope: str) -> int:
        calls.append(job)
        return next(results)

    monkeypatch.setattr(scheduler, "dispatch", dispatch)

    assert scheduler._run_nhtsa("all") == 1
    assert scheduler._run_nhtsa("all") == 0

    assert calls == ["nhtsa-bulk", "nhtsa-api", "nhtsa-api"]
    assert progress == [
        (81, scheduler.NHTSA_BULK_COMPLETED),
        (82, scheduler.NHTSA_BULK_COMPLETED),
        (82, scheduler.NHTSA_API_COMPLETED),
    ]
    assert scheduler.NHTSA_BULK_COMPLETED in finished[0][2]
    assert scheduler.NHTSA_API_COMPLETED not in finished[0][2]
    assert scheduler.NHTSA_API_COMPLETED in finished[1][2]


def test_nhtsa_composite_rejects_incompatible_scope(monkeypatch) -> None:
    record_start = mock.MagicMock()
    monkeypatch.setattr(scheduler, "_record_start", record_start)

    assert scheduler._run_nhtsa("recalls") == 2
    record_start.assert_not_called()


def test_nhtsa_api_success_survives_progress_write_failure(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "_nhtsa_bulk_completed_for_retry", lambda: False)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 83)

    def record_progress(_run_id: int, marker: str) -> None:
        if marker == scheduler.NHTSA_API_COMPLETED:
            raise scheduler.pymysql.OperationalError("temporary progress failure")

    monkeypatch.setattr(scheduler, "_record_progress", record_progress)
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    monkeypatch.setattr(scheduler, "dispatch", lambda _job, _scope: 0)

    assert scheduler._run_nhtsa("all") == 0
    assert finished[0][0:2] == (83, 0)
    assert scheduler.NHTSA_API_COMPLETED in finished[0][2]


def test_shutdown_before_spawn_records_interrupted_without_starting_child(monkeypatch) -> None:
    stop_event = scheduler.threading.Event()
    stop_event.set()
    monkeypatch.setattr(scheduler, "_SHUTDOWN_EVENT", stop_event)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 61)
    popen = mock.MagicMock()
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )

    assert scheduler._run("catalog", ["python", "-m", "crawler"]) == 125
    popen.assert_not_called()
    assert finished[0][:2] == (61, scheduler.INTERRUPTED_EXIT_CODE)


def test_child_receives_sigint_without_inheriting_blocked_mask() -> None:
    child = scheduler.subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        text=True,
    )
    try:
        scheduler._terminate_child(child)
        child.wait(timeout=2)
        assert child.returncode is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_non_contention_lock_error_is_not_hidden(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)

    def fail_lock(*_args) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(scheduler.fcntl, "flock", fail_lock)

    with pytest.raises(OSError, match="I/O error"):
        scheduler._try_lock("scheduler-job", "catalog")


@pytest.mark.parametrize(
    "option,value",
    (
        ("--interval-seconds", "0"),
        ("--interval-seconds", "-1"),
        ("--retry-base-seconds", "0"),
        ("--retry-max-seconds", "-1"),
    ),
)
def test_daemon_rejects_non_positive_cli_seconds(option, value, monkeypatch) -> None:
    monkeypatch.setattr(
        scheduler.sys,
        "argv",
        ["partsouq-scheduler", "--job", "catalog", "--daemon", option, value],
    )

    assert scheduler.main() == 2
