from __future__ import annotations

import errno
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest

from partsouq_catalog import scheduler, state_files


class FakeStopEvent:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        return self.stopped


@pytest.fixture(autouse=True)
def isolate_scheduler_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "acquire_catalog_writer_admission", lambda _connection: "test")
    monkeypatch.setattr(
        scheduler,
        "release_catalog_writer_admission",
        lambda _connection, _lock_name: None,
    )


def test_record_start_persists_trigger_mode(monkeypatch) -> None:
    cursor = mock.MagicMock(lastrowid=77)
    cursor.__enter__.return_value = cursor
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    monkeypatch.setattr(scheduler, "catalog_writer_admission", nullcontext)

    assert scheduler._record_start("catalog") == 77
    assert cursor.execute.call_args.args[1] == ("catalog", "manual")

    scheduler._JOB_CONTEXT.trigger_mode = "daemon"
    try:
        assert scheduler._record_start("catalog") == 77
        assert cursor.execute.call_args.args[1] == ("catalog", "daemon")
    finally:
        del scheduler._JOB_CONTEXT.trigger_mode


def test_schema_migration_defers_scheduler_before_child_spawn(monkeypatch) -> None:
    def migration_busy(_job: str) -> int:
        raise scheduler.AdmissionLockBusy("migration")

    monkeypatch.setattr(scheduler, "_record_start", migration_busy)
    popen = mock.MagicMock()
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)

    assert scheduler._run("catalog", ["python", "-m", "crawler"]) == 75
    popen.assert_not_called()


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


def test_record_finish_closes_matching_interrupted_catalog_run(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    scheduler._record_finish(79, -2, "KeyboardInterrupt\n")

    assert cursor.execute.call_count == 2
    scheduled_call, crawl_call = cursor.execute.call_args_list
    assert "UPDATE scheduled_job_runs" in scheduled_call.args[0]
    assert scheduled_call.args[1][-1] == 79
    assert "SET status = 'interrupted'" in crawl_call.args[0]
    assert "scheduled_job_run_id = %s" in crawl_call.args[0]
    assert crawl_call.args[1] == (79,)
    connection.begin.assert_called_once_with()
    connection.commit.assert_called_once_with()
    connection.rollback.assert_not_called()


def test_record_finish_does_not_interrupt_crawl_for_normal_failure(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    scheduler._record_finish(80, 1, "HTTP 403\n")

    assert cursor.execute.call_count == 1
    assert "UPDATE scheduled_job_runs" in cursor.execute.call_args.args[0]
    connection.commit.assert_called_once_with()


def test_record_finish_rolls_back_if_interrupted_crawl_update_fails(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.execute.side_effect = [None, scheduler.pymysql.OperationalError("update failed")]
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    with pytest.raises(scheduler.pymysql.OperationalError, match="update failed"):
        scheduler._record_finish(81, scheduler.INTERRUPTED_EXIT_CODE, "stopped\n")

    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()


def test_run_records_scheduler_id_in_child_environment(monkeypatch) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 42)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    monkeypatch.setenv("SCHEDULED_JOB_RUN_ID", "stale")
    monkeypatch.delenv("PSQ_BOUNDED_PARTS", raising=False)

    command = [
        sys.executable,
        "-c",
        "import os; print(os.environ['SCHEDULED_JOB_RUN_ID'])",
    ]
    assert scheduler._run("catalog", command) == 0

    assert finished == [(42, 0, "42\n")]


def test_run_streams_output_and_only_records_bounded_redacted_tail(monkeypatch, capsys) -> None:
    vin = "1M8GDM9AXKP042788"
    prefix = "first-line\n"

    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 43)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )

    script = (
        "import sys\n"
        "vin = sys.argv[2]\n"
        "sys.stdout.write('first-line\\n' + ''.join(f\"{vin} {'x' * 990}\\n\" "
        "for _ in range(100)))\n"
    )
    command = [sys.executable, "-c", script, "nhtsa-decode-vin", vin]
    assert scheduler._run("nhtsa-vin", command) == 0

    recorded = finished[0][2]
    streamed = capsys.readouterr().out
    assert len(recorded) == scheduler.MAX_OUTPUT_CHARS
    assert prefix not in recorded
    assert vin not in recorded
    assert vin not in streamed
    assert "1M8**********2788" in recorded


def test_run_masks_vin_split_across_pipe_reads(monkeypatch) -> None:
    vin = "1M8GDM9AXKP042788"
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 54)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    script = (
        "import sys, time\n"
        "vin = sys.argv[2]\n"
        "sys.stdout.write('before ' + vin[:8]); sys.stdout.flush(); time.sleep(0.05)\n"
        "sys.stdout.write(vin[8:] + ' after'); sys.stdout.flush()\n"
    )
    command = [sys.executable, "-c", script, "nhtsa-decode-vin", vin]

    assert scheduler._run("nhtsa-vin", command) == 0
    assert finished == [(54, 0, "before 1M8**********2788 after")]


def test_launchd_run_records_child_output_without_writing_unbounded_stdout(
    monkeypatch, capsys
) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setenv("LAUNCHD_JOB", "1")
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 49)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )

    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 0.2)
    command = [
        sys.executable,
        "-c",
        (
            "import os, time\n"
            "for index in range(5):\n"
            " if 'LAUNCHD_JOB' not in os.environ:\n"
            "  print(f'heartbeat-{index}', flush=True)\n"
            " time.sleep(0.05)\n"
        ),
    ]
    assert scheduler._run("catalog", command) == 0
    assert capsys.readouterr().out == ""
    assert finished[0][0:2] == (49, 0)
    assert "heartbeat-0" in finished[0][2]
    assert "heartbeat-4" in finished[0][2]


def test_run_terminates_silent_process_after_stall_timeout(monkeypatch) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(scheduler, "CHILD_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 44)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )

    started = time.monotonic()
    result = scheduler._run(
        "catalog",
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )

    assert result == scheduler.CHILD_STALL_EXIT_CODE
    assert time.monotonic() - started < 3
    assert finished[0][0:2] == (44, scheduler.CHILD_STALL_EXIT_CODE)
    assert "no output" in finished[0][2]


def test_run_does_not_stall_while_child_keeps_reporting_progress(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 45)
    monkeypatch.setattr(scheduler, "_record_finish", lambda *_args, **_kwargs: None)

    script = "import time\nfor i in range(5):\n print(i, flush=True)\n time.sleep(0.05)\n"

    assert scheduler._run("catalog", [sys.executable, "-c", script]) == 0


def test_run_does_not_stall_during_cookie_backoff_heartbeats(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 48)
    monkeypatch.setattr(scheduler, "_record_finish", lambda *_args, **_kwargs: None)
    script = (
        "print('child-started', flush=True)\n"
        "import logging, sys, time\n"
        "from partsouq_catalog import http_client\n"
        "logging.basicConfig(stream=sys.stdout, level=logging.WARNING, force=True)\n"
        "real_sleep = time.sleep\n"
        "http_client.BACKOFF_HEARTBEAT_SECONDS = 1.0\n"
        "http_client.session_backoff_remaining = lambda: 10.0\n"
        "http_client.time.sleep = lambda _seconds: real_sleep(0.25)\n"
        "manager = object.__new__(http_client.SessionManager)\n"
        "manager._sleep_with_backoff(0)\n"
    )

    assert scheduler._run("catalog", [sys.executable, "-c", script]) == 0


def test_run_counts_flushed_bytes_without_newlines_as_progress(monkeypatch) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 0.25)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 51)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    script = (
        "import sys, time\n"
        "for _ in range(10):\n"
        " sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.04)\n"
    )

    assert scheduler._run("catalog", [sys.executable, "-c", script]) == 0
    assert finished == [(51, 0, "x" * 10)]


def test_run_kills_descendant_that_keeps_stdout_open_after_wrapper_exits(monkeypatch) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(scheduler, "CHILD_PIPE_DRAIN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(scheduler, "CHILD_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 46)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('parent-exit', flush=True)\n"
    )

    started = time.monotonic()
    result = scheduler._run("catalog", [sys.executable, "-c", script])

    assert result == scheduler.CHILD_STALL_EXIT_CODE
    assert time.monotonic() - started < 3
    assert finished[0][0:2] == (46, scheduler.CHILD_STALL_EXIT_CODE)
    assert "stdout remained open" in finished[0][2]


def test_run_allows_kill_timer_to_finish_owned_process_group(monkeypatch) -> None:
    real_os = scheduler.os
    read_fd, write_fd = real_os.pipe()
    stdout = real_os.fdopen(read_fd, "rb", buffering=0)
    signals: list[int] = []

    class Process:
        pid = 987_654
        returncode = 0

        def __init__(self) -> None:
            self.stdout = stdout

        def poll(self) -> int:
            return self.returncode

        def wait(self, timeout=None) -> int:
            return self.returncode

    class ProcessGroupOS:
        def __getattr__(self, name: str):
            return getattr(real_os, name)

        @staticmethod
        def killpg(_pid: int, child_signal: int) -> None:
            signals.append(child_signal)
            if child_signal == scheduler.signal.SIGKILL:
                real_os.close(write_fd)

    monkeypatch.setattr(scheduler, "os", ProcessGroupOS())
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(scheduler, "CHILD_PIPE_DRAIN_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(scheduler, "CHILD_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 55)
    monkeypatch.setattr(scheduler, "_record_finish", lambda *_args, **_kwargs: None)

    try:
        assert scheduler._run("catalog", ["fake-command"]) == scheduler.CHILD_STALL_EXIT_CODE
    finally:
        try:
            real_os.close(write_fd)
        except OSError:
            pass

    assert signals == [scheduler.signal.SIGINT, 0, scheduler.signal.SIGKILL]
    assert stdout.closed


def test_run_does_not_wait_for_escaped_descendant_holding_stdout(tmp_path, monkeypatch) -> None:
    pid_path = tmp_path / "escaped-child.pid"
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(scheduler, "CHILD_PIPE_DRAIN_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(scheduler, "CHILD_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 52)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    script = (
        "import pathlib, subprocess, sys\n"
        "child = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
    )

    started = time.monotonic()
    try:
        result = scheduler._run("catalog", [sys.executable, "-c", script, str(pid_path)])
    finally:
        if pid_path.exists():
            escaped_pid = int(pid_path.read_text())
            try:
                scheduler.os.killpg(escaped_pid, scheduler.signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert result == scheduler.CHILD_STALL_EXIT_CODE
    assert time.monotonic() - started < 2
    assert finished[0][0:2] == (52, scheduler.CHILD_STALL_EXIT_CODE)
    assert "stdout remained open" in finished[0][2]


def test_run_kills_child_that_closes_stdout_but_remains_alive(monkeypatch) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(scheduler, "CHILD_PIPE_DRAIN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(scheduler, "CHILD_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 50)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    script = "import os, time\nos.close(1)\nos.close(2)\ntime.sleep(30)\n"

    started = time.monotonic()
    result = scheduler._run("catalog", [sys.executable, "-c", script])

    assert result == scheduler.CHILD_STALL_EXIT_CODE
    assert time.monotonic() - started < 3
    assert finished[0][0:2] == (50, scheduler.CHILD_STALL_EXIT_CODE)
    assert "closed stdout but remained alive" in finished[0][2]


def test_run_observes_shutdown_while_child_stdout_is_blocked(monkeypatch) -> None:
    stop_event = threading.Event()
    finished: list[tuple[int, int, str]] = []
    termination_timer = mock.Mock()
    terminated_children: list[object] = []

    def terminate_child(child) -> mock.Mock:
        terminated_children.append(child)
        scheduler._signal_child_group(child, scheduler.signal.SIGINT)
        return termination_timer

    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(scheduler, "CHILD_TERMINATE_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(scheduler, "_SHUTDOWN_EVENT", stop_event)
    monkeypatch.setattr(scheduler, "_terminate_child", terminate_child)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 47)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    timer = threading.Timer(0.1, stop_event.set)
    timer.start()
    try:
        result = scheduler._run(
            "catalog",
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
    finally:
        timer.cancel()

    assert result == scheduler.INTERRUPTED_EXIT_CODE
    assert finished[0][0:2] == (47, scheduler.INTERRUPTED_EXIT_CODE)
    assert len(terminated_children) == 1
    termination_timer.cancel.assert_called_once_with()
    termination_timer.join.assert_called_once_with()


def test_signal_handler_only_sets_shutdown_event(monkeypatch) -> None:
    stop_event = threading.Event()
    registered_handlers: list[object] = []
    terminate_child = mock.MagicMock()
    monkeypatch.setattr(scheduler, "_SHUTDOWN_EVENT", None)
    monkeypatch.setattr(scheduler, "_terminate_child", terminate_child)
    monkeypatch.setattr(
        scheduler.signal,
        "signal",
        lambda _signal, handler: registered_handlers.append(handler),
    )

    scheduler._install_signal_handlers(stop_event)
    for handler in registered_handlers:
        handler(scheduler.signal.SIGTERM, None)

    assert stop_event.is_set()
    terminate_child.assert_not_called()


def test_stdout_read_error_terminates_child_and_closes_pipe(monkeypatch) -> None:
    real_os = scheduler.os
    processes: list[scheduler.subprocess.Popen[bytes]] = []
    timers: list[threading.Timer] = []
    real_popen = scheduler.subprocess.Popen
    real_terminate_child = scheduler._terminate_child

    class FailingReadOS:
        def __getattr__(self, name: str):
            return getattr(real_os, name)

        @staticmethod
        def read(_fd: int, _size: int) -> bytes:
            raise OSError(errno.EIO, "read failed")

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def terminate_child(process):
        timer = real_terminate_child(process)
        timers.append(timer)
        return timer

    finished: list[tuple[int, int, str]] = []
    monkeypatch.setattr(scheduler, "os", FailingReadOS())
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)
    monkeypatch.setattr(scheduler, "_terminate_child", terminate_child)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 53)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    script = "import sys, time; print('ready', flush=True); time.sleep(30)"

    assert scheduler._run("catalog", [sys.executable, "-c", script]) == 127
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout is not None and processes[0].stdout.closed
    assert len(timers) == 1 and not timers[0].is_alive()
    assert "stdout read failed" in finished[0][2]


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


@pytest.mark.parametrize("prefix", ("scheduler-job", "scheduler-daemon"))
def test_scheduler_lock_rejects_symlinked_leaf_without_touching_target(
    prefix, tmp_path, monkeypatch
) -> None:
    external = tmp_path / "external-lock-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / f"{prefix}-catalog.lock").symlink_to(external)
    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(state_dir))

    with pytest.raises(OSError, match="refusing symlinked state file"):
        scheduler._try_lock(prefix, "catalog")

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_scheduler_job_is_not_dispatched_through_symlinked_lock(tmp_path, monkeypatch) -> None:
    external = tmp_path / "external-job-lock-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "scheduler-job-catalog.lock").symlink_to(external)
    dispatch = mock.Mock()
    recover = mock.Mock()
    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(state_dir))
    monkeypatch.setattr(scheduler, "dispatch", dispatch)
    monkeypatch.setattr(scheduler, "_recover_interrupted_job_runs", recover)

    with pytest.raises(OSError, match="refusing symlinked state file"):
        scheduler.dispatch_locked("catalog", "all")

    dispatch.assert_not_called()
    recover.assert_not_called()
    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_scheduler_daemon_does_not_run_through_symlinked_lock(tmp_path, monkeypatch) -> None:
    external = tmp_path / "external-daemon-lock-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "scheduler-daemon-catalog.lock").symlink_to(external)
    stop_event = FakeStopEvent()

    def mark_work_started(_job, _scope):
        stop_event.stopped = True
        return 0

    dispatch = mock.Mock(side_effect=mark_work_started)
    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(state_dir))
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)
    monkeypatch.setattr(scheduler, "_seconds_until_next_run", lambda _job, _interval: 0.0)

    with pytest.raises(OSError, match="refusing symlinked state file"):
        scheduler.run_daemon(
            "catalog",
            "all",
            60,
            10,
            100,
            stop_event=stop_event,
        )

    dispatch.assert_not_called()
    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_scheduler_lock_rejects_symlinked_state_ancestor_without_writes(
    tmp_path, monkeypatch
) -> None:
    external = tmp_path / "external-state"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    alias = tmp_path / "state-alias"
    alias.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("PSQ_SCHEDULER_STATE_DIR", str(alias / "scheduler"))

    with pytest.raises(OSError, match="refusing symlinked private state path"):
        scheduler._try_lock("scheduler-job", "catalog")

    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


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


@pytest.mark.parametrize("error", [RuntimeError("manifest"), ValueError("invalid body")])
def test_catalog_evidence_audit_rejects_persisted_validation_errors(
    monkeypatch, capsys, error: Exception
) -> None:
    from partsouq_catalog import db as db_module
    from partsouq_catalog import repositories as repositories_module

    database = mock.MagicMock()
    database_factory = mock.MagicMock()
    database_factory.return_value.connect.return_value = database
    repository = mock.MagicMock()
    repository.audit_run_evidence.side_effect = error
    monkeypatch.setattr(db_module, "Database", database_factory)
    monkeypatch.setattr(
        repositories_module,
        "CrawlRepository",
        mock.MagicMock(return_value=repository),
    )

    assert scheduler._audit_catalog_evidence(17, 10_000) is False

    assert capsys.readouterr().err == (
        f"crawl run 17 的 HTTP evidence 驗證失敗：{type(error).__name__}: {error}\n"
    )
    database.rollback.assert_called_once_with()
    database.close.assert_called_once_with()


def test_catalog_evidence_audit_does_not_hide_database_failure(monkeypatch) -> None:
    from partsouq_catalog import db as db_module
    from partsouq_catalog import repositories as repositories_module

    database = mock.MagicMock()
    database_factory = mock.MagicMock()
    database_factory.return_value.connect.return_value = database
    repository = mock.MagicMock()
    repository.audit_run_evidence.side_effect = scheduler.pymysql.OperationalError("db down")
    monkeypatch.setattr(db_module, "Database", database_factory)
    monkeypatch.setattr(
        repositories_module,
        "CrawlRepository",
        mock.MagicMock(return_value=repository),
    )

    with pytest.raises(scheduler.pymysql.OperationalError, match="db down"):
        scheduler._audit_catalog_evidence(17, 10_000)

    database.rollback.assert_called_once_with()
    database.close.assert_called_once_with()


def test_catalog_is_not_delayed_without_exact_bounded_success(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {
        "job_name": "catalog",
        "status": "completed",
        "age_seconds": 10,
        "crawl_run_id": 17,
        "dataset_kind": "bounded",
        "crawl_status": "bounded_success",
        "target_parts": 10_000,
        "parts_ok": 10_000,
        "snapshot_rows": 9_999,
        "evidence_status": "verified",
        "evidence_manifest_sha256": "a" * 64,
        "evidence_dataset_sha256": "b" * 64,
        "evidence_artifact_count": 250,
        "evidence_record_count": 10_000,
        "evidence_verified_at": object(),
    }
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler.pymysql, "connect", lambda **_kwargs: connection)
    monkeypatch.setenv("PSQ_BOUNDED_PARTS", "10000")
    evidence_audit = mock.MagicMock(return_value=True)
    monkeypatch.setattr(scheduler, "_audit_catalog_evidence", evidence_audit)

    assert scheduler._seconds_until_next_run("catalog", 100) == 0

    cursor.fetchone.return_value["snapshot_rows"] = 10_000
    assert scheduler._seconds_until_next_run("catalog", 100) == 90
    evidence_audit.assert_called_once_with(17, 10_000)

    evidence_audit.return_value = False
    assert scheduler._seconds_until_next_run("catalog", 100) == 0
    assert "jobs.trigger_mode = 'daemon'" in cursor.execute.call_args.args[0]


def test_catalog_sample_never_satisfies_daemon_cadence(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {
        "job_name": "catalog",
        "status": "completed",
        "age_seconds": 10,
        "dataset_kind": "sample",
        "crawl_status": "sample",
        "target_parts": 60,
        "parts_ok": 60,
        "snapshot_rows": 0,
    }
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler.pymysql, "connect", lambda **_kwargs: connection)
    monkeypatch.delenv("PSQ_BOUNDED_PARTS", raising=False)

    assert scheduler._seconds_until_next_run("catalog", 100) == 0


def test_catalog_crash_after_publish_is_reconciled_without_recrawl(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = [
        {
            "scheduled_job_run_id": 88,
            "crawl_run_id": 17,
            "target_parts": 10_000,
        }
    ]

    def execute(statement: str, _params=None) -> None:
        cursor.rowcount = 1 if statement.startswith("UPDATE scheduled_job_runs AS jobs") else 0

    cursor.execute.side_effect = execute
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    monkeypatch.setenv("PSQ_BOUNDED_PARTS", "10000")
    evidence_audit = mock.MagicMock(return_value=True)
    monkeypatch.setattr(scheduler, "_audit_catalog_evidence", evidence_audit)

    assert scheduler._recover_interrupted_job_runs("catalog") is True

    candidate_call = cursor.execute.call_args_list[0]
    candidate_query = candidate_call.args[0]
    assert "jobs.trigger_mode = 'daemon'" in candidate_query
    assert "jobs.status = 'failed'" in candidate_query
    assert "jobs.exit_code <> 0" in candidate_query
    assert "runs.status = 'bounded_success'" in candidate_query
    assert "runs.target_parts = 10000" in candidate_query
    assert "runs.parts_ok = runs.target_parts" in candidate_query
    assert "snapshots.snapshot_rows = runs.target_parts" in candidate_query
    assert "runs.evidence_status = 'verified'" in candidate_query
    assert "runs.evidence_record_count = runs.target_parts" in candidate_query
    evidence_audit.assert_called_once_with(
        17,
        10_000,
        allow_running_scheduler=True,
        allow_failed_scheduler=True,
    )

    reconciliation_call = next(
        call
        for call in cursor.execute.call_args_list
        if call.args[0].startswith("UPDATE scheduled_job_runs AS jobs")
    )
    assert "jobs.finished_at = runs.finished_at" in reconciliation_call.args[0]
    assert "jobs.status = 'failed'" in reconciliation_call.args[0]
    assert "jobs.exit_code <> 0" in reconciliation_call.args[0]
    assert "runs.evidence_status = 'verified'" in reconciliation_call.args[0]
    assert reconciliation_call.args[1][0].endswith("reconciled automatically\n")
    assert reconciliation_call.args[1][2:] == (88,)
    connection.begin.assert_called_once_with()
    connection.commit.assert_called_once_with()
    connection.rollback.assert_not_called()


def test_catalog_recovery_does_not_reconcile_failed_evidence_audit(monkeypatch) -> None:
    cursor = mock.MagicMock(rowcount=0)
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = [
        {
            "scheduled_job_run_id": 88,
            "crawl_run_id": 17,
            "target_parts": 10_000,
        }
    ]
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    evidence_audit = mock.MagicMock(return_value=False)
    monkeypatch.setattr(scheduler, "_audit_catalog_evidence", evidence_audit)

    assert scheduler._recover_interrupted_job_runs("catalog") is False

    evidence_audit.assert_called_once_with(
        17,
        10_000,
        allow_running_scheduler=True,
        allow_failed_scheduler=True,
    )
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert not any(
        statement.startswith("UPDATE scheduled_job_runs AS jobs") for statement in statements
    )
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


def test_recent_remote_catalog_daemon_defers_child(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_recover_interrupted_job_runs",
        mock.MagicMock(side_effect=scheduler.ActiveDaemonRun("remote catalog")),
    )
    dispatch = mock.MagicMock()
    monkeypatch.setattr(scheduler, "dispatch", dispatch)

    assert scheduler.dispatch_locked("catalog", "all") == scheduler.LOCK_BUSY_EXIT_CODE

    dispatch.assert_not_called()


def test_catalog_reconciliation_rolls_back_if_stale_cleanup_fails(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None
    calls = 0

    def execute(statement: str, _params=None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            cursor.fetchall.return_value = [
                {
                    "scheduled_job_run_id": 88,
                    "crawl_run_id": 17,
                    "target_parts": 10_000,
                }
            ]
            assert statement.startswith("SELECT jobs.id AS scheduled_job_run_id")
            return
        if calls == 2:
            cursor.rowcount = 1
            assert statement.startswith("UPDATE scheduled_job_runs AS jobs")
            return
        if calls == 3:
            assert statement.startswith("SELECT id FROM scheduled_job_runs")
            return
        raise scheduler.pymysql.OperationalError("cleanup failed")

    cursor.execute.side_effect = execute
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    monkeypatch.setenv("PSQ_BOUNDED_PARTS", "10000")
    monkeypatch.setattr(scheduler, "_audit_catalog_evidence", lambda *_args, **_kwargs: True)

    with pytest.raises(scheduler.pymysql.OperationalError, match="cleanup failed"):
        scheduler._recover_interrupted_job_runs("catalog")

    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()


def test_catalog_recovery_marks_stale_running_rows_interrupted(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None
    cursor.rowcount = 0
    cursor.execute.side_effect = lambda *_args, **_kwargs: None
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)
    monkeypatch.delenv("PSQ_BOUNDED_PARTS", raising=False)

    assert scheduler._recover_interrupted_job_runs("catalog") is False

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert len(statements) == 4
    reconcile_call, active_call, stale_jobs_call, stale_runs_call = cursor.execute.call_args_list
    # 第一筆是 bounded 對帳（env 獨立），rowcount=0 → 無需略過 dispatch
    assert "dataset_kind = 'bounded'" in reconcile_call.args[0]
    assert "started_at >= UTC_TIMESTAMP()" in active_call.args[0]
    assert active_call.args[1] == ("catalog", scheduler.RECOVERY_MIN_AGE_SECONDS)
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


def test_catalog_recovery_rejects_recent_remote_running_row(monkeypatch) -> None:
    cursor = mock.MagicMock(rowcount=0)
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = {"id": 99}
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    with pytest.raises(scheduler.ActiveDaemonRun, match="recent catalog daemon"):
        scheduler._recover_interrupted_job_runs("catalog")

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert len(statements) == 2
    assert statements[1].startswith("SELECT id FROM scheduled_job_runs")
    assert not any(statement.startswith("UPDATE") for statement in statements)
    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()


def test_nhtsa_recovery_closes_scheduler_owned_domain_run(monkeypatch) -> None:
    cursor = mock.MagicMock(rowcount=0)
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None
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
    assert "updated_at < UTC_TIMESTAMP(6)" in domain_recovery
    connection.commit.assert_called_once_with()


def test_nhtsa_recovery_rejects_recent_remote_running_row(monkeypatch) -> None:
    cursor = mock.MagicMock(rowcount=0)
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = {"id": 99}
    connection = mock.MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setattr(scheduler, "_connect", lambda: connection)

    with pytest.raises(scheduler.ActiveDaemonRun, match="recent nhtsa daemon"):
        scheduler._recover_interrupted_job_runs("nhtsa")

    assert len(cursor.execute.call_args_list) == 1
    active_query, params = cursor.execute.call_args.args
    assert "job_name LIKE 'nhtsa%%'" in active_query
    assert params == (scheduler.RECOVERY_MIN_AGE_SECONDS,)
    connection.rollback.assert_called_once_with()
    connection.commit.assert_not_called()


def test_nhtsa_api_commit_is_reconciled_without_repeating_stages(monkeypatch) -> None:
    cursor = mock.MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.return_value = None

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
    results = iter((1, 1, 0))
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


def test_daemon_writes_ready_marker_only_after_lock_is_acquired(tmp_path, monkeypatch) -> None:
    stop_event = FakeStopEvent()
    stop_event.stopped = True
    daemon_lock = mock.MagicMock()
    marker = tmp_path / "scheduler.ready"
    monkeypatch.setenv("PARTSOUQ_SCHEDULER_READY_MARKER", str(marker))
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )

    assert scheduler.run_daemon("catalog", "all", 60, 10, 100, stop_event=stop_event) == 0
    assert marker.read_text(encoding="utf-8").strip() == str(scheduler.os.getpid())
    assert marker.stat().st_mode & 0o777 == 0o600
    daemon_lock.close.assert_called_once_with()


def test_daemon_ready_marker_ignores_predictable_symlinked_temp_without_touching_target(
    tmp_path, monkeypatch
) -> None:
    marker = tmp_path / "launch-ready-release"
    external = tmp_path / "external-ready-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    predictable_temporary = marker.with_name(f"{marker.name}.{scheduler.os.getpid()}")
    predictable_temporary.symlink_to(external)
    monkeypatch.setenv("PARTSOUQ_SCHEDULER_READY_MARKER", str(marker))

    scheduler._write_daemon_ready_marker()

    assert marker.read_text(encoding="utf-8") == f"{scheduler.os.getpid()}\n"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert predictable_temporary.is_symlink()
    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_daemon_ready_marker_replaces_symlinked_leaf_without_touching_target(
    tmp_path, monkeypatch
) -> None:
    marker = tmp_path / "launch-ready-release"
    external = tmp_path / "external-ready-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    marker.symlink_to(external)
    monkeypatch.setenv("PARTSOUQ_SCHEDULER_READY_MARKER", str(marker))

    scheduler._write_daemon_ready_marker()

    assert not marker.is_symlink()
    assert marker.read_text(encoding="utf-8") == f"{scheduler.os.getpid()}\n"
    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_daemon_ready_marker_rejects_symlinked_ancestor_without_writes(
    tmp_path, monkeypatch
) -> None:
    external = tmp_path / "external-ready-state"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    alias = tmp_path / "ready-alias"
    alias.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv(
        "PARTSOUQ_SCHEDULER_READY_MARKER",
        str(alias / "scheduler/launch-ready-release"),
    )

    with pytest.raises(OSError, match="refusing symlinked private state path"):
        scheduler._write_daemon_ready_marker()

    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


def test_daemon_does_not_write_ready_marker_without_lock(tmp_path, monkeypatch) -> None:
    stop_event = FakeStopEvent()
    marker = tmp_path / "scheduler.ready"
    monkeypatch.setenv("PARTSOUQ_SCHEDULER_READY_MARKER", str(marker))
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: None,
    )

    assert scheduler.run_daemon("catalog", "all", 60, 10, 100, stop_event=stop_event) == 0
    assert not marker.exists()


def test_daemon_stops_retrying_after_max_consecutive_failures(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    schedule_checks = 0

    def due_now(_job: str, _interval: int) -> float:
        nonlocal schedule_checks
        schedule_checks += 1
        return 0.0

    monkeypatch.setattr(scheduler, "_seconds_until_next_run", due_now)
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
    assert schedule_checks == scheduler.MAX_CONSECUTIVE_FAILURES + 1


def test_daemon_non_site_failures_do_not_trip_failure_cap(monkeypatch, capsys) -> None:
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
        if calls > 8:
            stop_event.stopped = True
        return scheduler.LOCK_BUSY_EXIT_CODE if calls <= 8 else 0

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
    # 8 次鎖衝突（非站台失敗）都不能觸發 MAX_CONSECUTIVE_FAILURES：
    # 沒有 interval 靜默、沒有「停止重試」訊息。
    assert calls == 9
    assert 60.0 not in stop_event.waits
    assert "排程連續失敗" not in capsys.readouterr().err


def test_catalog_sample_exit_waits_without_immediate_retry(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        lambda _job, _stop_event: daemon_lock,
    )
    schedule_checks = 0

    def due_now(_job: str, _interval: int) -> float:
        nonlocal schedule_checks
        schedule_checks += 1
        if schedule_checks == 2:
            stop_event.stopped = True
        return 0.0

    monkeypatch.setattr(scheduler, "_seconds_until_next_run", due_now)
    dispatch = mock.MagicMock(return_value=3)
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert scheduler.run_daemon("catalog", "all", 60, 10, 100, stop_event=stop_event) == 0
    assert stop_event.waits == [0.0, 60.0]
    dispatch.assert_called_once_with("catalog", "all")


def test_catalog_sample_exit_is_not_recorded_as_scheduler_success(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def run(job_name: str, command: list[str], success_codes=(0,)) -> int:
        captured.update(job=job_name, command=command, success_codes=success_codes)
        return 3

    monkeypatch.setattr(scheduler, "_run", run)
    monkeypatch.setenv("PSQ_WORKERS", "1")

    assert scheduler.dispatch("catalog", "all") == 3
    assert captured["job"] == "catalog"
    assert captured["success_codes"] == (0,)


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
    )
    termination_timer = None
    try:
        termination_timer = scheduler._terminate_child(child)
        child.wait(timeout=2)
        assert child.returncode is not None
    finally:
        if termination_timer is not None:
            termination_timer.cancel()
            termination_timer.join()
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


def test_lock_file_is_closed_when_permission_hardening_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    opened_descriptor = None
    real_open = state_files.os.open

    def tracked_open(*args, **kwargs):
        nonlocal opened_descriptor
        opened_descriptor = real_open(*args, **kwargs)
        return opened_descriptor

    monkeypatch.setattr(state_files.os, "open", tracked_open)
    monkeypatch.setattr(
        state_files.os,
        "fchmod",
        mock.Mock(side_effect=OSError(errno.EACCES, "permission denied")),
    )

    with pytest.raises(OSError, match="permission denied"):
        scheduler._try_lock("scheduler-job", "catalog")

    assert opened_descriptor is not None
    with pytest.raises(OSError) as error:
        state_files.os.fstat(opened_descriptor)
    assert error.value.errno == errno.EBADF


def test_private_state_open_rejects_fifo_before_open(tmp_path, monkeypatch) -> None:
    fifo = tmp_path / "state.fifo"
    state_files.os.mkfifo(fifo)
    opened_paths: list[object] = []
    real_open = state_files.os.open

    def tracked_open(path, *args, **kwargs):
        opened_paths.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(state_files.os, "open", tracked_open)

    with pytest.raises(OSError) as error:
        state_files.open_private_state_file(fifo, state_files.os.O_RDONLY)

    assert error.value.errno == errno.EINVAL
    assert opened_paths == [fifo.parent]


def test_private_state_open_rejects_hardlink_without_mutating_target(tmp_path) -> None:
    target = tmp_path / "target"
    target.write_text("preserve\n", encoding="utf-8")
    target.chmod(0o640)
    linked = tmp_path / "state"
    state_files.os.link(target, linked)

    with pytest.raises(OSError) as error:
        state_files.open_private_state_file(
            linked,
            state_files.os.O_WRONLY | state_files.os.O_TRUNC,
        )

    assert error.value.errno == errno.EMLINK
    assert target.read_text(encoding="utf-8") == "preserve\n"
    assert target.stat().st_mode & 0o777 == 0o640


def test_private_state_open_validates_before_truncate_and_uses_nonblocking(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / "state"
    state_path.write_text("old\n", encoding="utf-8")
    opened_flags = 0
    real_open = state_files.os.open

    def tracked_open(path, flags, mode=0o777, **kwargs):
        nonlocal opened_flags
        opened_flags = flags
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(state_files.os, "open", tracked_open)

    descriptor = state_files.open_private_state_file(
        state_path,
        state_files.os.O_WRONLY | state_files.os.O_TRUNC,
    )
    state_files.os.close(descriptor)

    assert opened_flags & state_files.os.O_NONBLOCK
    assert not opened_flags & state_files.os.O_TRUNC
    assert state_path.read_bytes() == b""


def test_private_state_open_retries_transient_create_enoent_on_same_parent(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / "state"
    real_open = state_files.os.open
    parent_calls = 0
    leaf_dir_fds: list[int] = []

    def transient_open(path, flags, mode=0o777, **kwargs):
        nonlocal parent_calls
        if path == state_path.parent:
            parent_calls += 1
        elif path == state_path.name:
            leaf_dir_fds.append(kwargs["dir_fd"])
            if len(leaf_dir_fds) == 1:
                raise FileNotFoundError(errno.ENOENT, "transient openat failure", path)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(state_files.os, "open", transient_open)

    descriptor = state_files.open_private_state_file(
        state_path,
        state_files.os.O_WRONLY | state_files.os.O_CREAT,
    )
    state_files.os.close(descriptor)

    assert parent_calls == 1
    assert len(leaf_dir_fds) == 2
    assert leaf_dir_fds[0] == leaf_dir_fds[1]


def test_private_state_open_propagates_persistent_create_enoent_after_one_retry(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / "state"
    real_open = state_files.os.open
    leaf_calls = 0

    def missing_leaf_open(path, flags, mode=0o777, **kwargs):
        nonlocal leaf_calls
        if path == state_path.name:
            leaf_calls += 1
            raise FileNotFoundError(errno.ENOENT, "persistent openat failure", path)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(state_files.os, "open", missing_leaf_open)

    with pytest.raises(FileNotFoundError, match="persistent openat failure"):
        state_files.open_private_state_file(
            state_path,
            state_files.os.O_WRONLY | state_files.os.O_CREAT,
        )

    assert leaf_calls == 2


def test_private_state_open_does_not_retry_enoent_without_create(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "state"
    real_open = state_files.os.open
    leaf_calls = 0

    def missing_leaf_open(path, flags, mode=0o777, **kwargs):
        nonlocal leaf_calls
        if path == state_path.name:
            leaf_calls += 1
            raise FileNotFoundError(errno.ENOENT, "missing state file", path)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(state_files.os, "open", missing_leaf_open)

    with pytest.raises(FileNotFoundError, match="missing state file"):
        state_files.open_private_state_file(state_path, state_files.os.O_RDONLY)

    assert leaf_calls == 1


def test_private_state_open_rejects_parent_swap_to_symlink_without_external_write(
    tmp_path, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    moved_state_dir = tmp_path / "moved-state"
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    real_open = state_files.os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, **kwargs):
        nonlocal swapped
        if not swapped and Path(path) == state_dir:
            swapped = True
            state_dir.rename(moved_state_dir)
            state_dir.symlink_to(external, target_is_directory=True)
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(state_files.os, "open", swapping_open)

    with pytest.raises(OSError):
        state_files.open_private_state_file(
            state_dir / "state-file",
            state_files.os.O_WRONLY | state_files.os.O_CREAT | state_files.os.O_TRUNC,
        )

    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


def test_private_rotating_handler_replaces_symlinked_backup_without_touching_target(
    tmp_path,
) -> None:
    log_path = tmp_path / "crawl.log"
    external = tmp_path / "external-backup-target"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    backup = tmp_path / "crawl.log.1"
    backup.symlink_to(external)
    handler = state_files.PrivateRotatingFileHandler(log_path, maxBytes=1, backupCount=1)
    try:
        assert handler.stream is not None
        handler.stream.write("entry\n")
        handler.stream.flush()
        handler.doRollover()
    finally:
        handler.close()

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640
    assert log_path.is_file() and not log_path.is_symlink()
    assert backup.is_file() and not backup.is_symlink()
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600


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


def test_catalog_auto_migration_runs_under_daemon_and_job_locks_before_ready(
    monkeypatch,
) -> None:
    stop_event = FakeStopEvent()
    stop_event.stopped = True
    daemon_lock = mock.MagicMock()
    job_lock = mock.MagicMock()
    runner = mock.MagicMock()
    ready = mock.MagicMock()
    recover = mock.MagicMock(return_value=False)
    monkeypatch.setenv("PARTSOUQ_APPLY_MIGRATIONS_ON_START", "1")
    monkeypatch.setattr(
        scheduler,
        "_wait_for_daemon_lock",
        mock.MagicMock(return_value=daemon_lock),
    )
    try_lock = mock.MagicMock(return_value=job_lock)
    monkeypatch.setattr(scheduler, "_try_lock", try_lock)
    monkeypatch.setattr(scheduler, "CatalogMigrationRunner", mock.MagicMock(return_value=runner))
    monkeypatch.setattr(scheduler, "_recover_interrupted_job_runs", recover)
    monkeypatch.setattr(scheduler, "_write_daemon_ready_marker", ready)

    assert scheduler.run_daemon("catalog", "all", 60, 10, 100, stop_event=stop_event) == 0
    try_lock.assert_called_once_with("scheduler-job", "catalog")
    runner.apply.assert_called_once_with(
        recover_stale_catalog_daemon_seconds=scheduler.RECOVERY_MIN_AGE_SECONDS,
    )
    runner.check.assert_called_once_with()
    recover.assert_called_once_with("catalog")
    ready.assert_called_once_with()
    job_lock.close.assert_called_once_with()
    daemon_lock.close.assert_called_once_with()


def test_catalog_auto_migration_failure_stops_before_ready_or_dispatch(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    job_lock = mock.MagicMock()
    runner = mock.MagicMock()
    runner.apply.side_effect = scheduler.MigrationError("ledger checksum drift")
    ready = mock.MagicMock()
    dispatch = mock.MagicMock()
    monkeypatch.setenv("PARTSOUQ_APPLY_MIGRATIONS_ON_START", "1")
    monkeypatch.setattr(scheduler, "_wait_for_daemon_lock", lambda _job, _stop: daemon_lock)
    monkeypatch.setattr(scheduler, "_try_lock", lambda _prefix, _job: job_lock)
    monkeypatch.setattr(scheduler, "CatalogMigrationRunner", mock.MagicMock(return_value=runner))
    monkeypatch.setattr(scheduler, "_write_daemon_ready_marker", ready)
    monkeypatch.setattr(scheduler, "dispatch_locked", dispatch)

    assert (
        scheduler.run_daemon("catalog", "all", 60, 10, 100, stop_event=stop_event)
        == scheduler.SCHEDULER_DB_ERROR_EXIT_CODE
    )
    runner.check.assert_not_called()
    ready.assert_not_called()
    dispatch.assert_not_called()
    job_lock.close.assert_called_once_with()
    daemon_lock.close.assert_called_once_with()


def test_catalog_auto_migration_refuses_when_job_lock_is_owned(monkeypatch) -> None:
    stop_event = FakeStopEvent()
    daemon_lock = mock.MagicMock()
    runner = mock.MagicMock()
    monkeypatch.setenv("PARTSOUQ_APPLY_MIGRATIONS_ON_START", "1")
    monkeypatch.setattr(scheduler, "_wait_for_daemon_lock", lambda _job, _stop: daemon_lock)
    monkeypatch.setattr(scheduler, "_try_lock", mock.MagicMock(return_value=None))
    monkeypatch.setattr(scheduler, "CatalogMigrationRunner", runner)

    assert (
        scheduler.run_daemon("catalog", "all", 60, 10, 100, stop_event=stop_event)
        == scheduler.LOCK_BUSY_EXIT_CODE
    )
    runner.assert_not_called()
    daemon_lock.close.assert_called_once_with()
