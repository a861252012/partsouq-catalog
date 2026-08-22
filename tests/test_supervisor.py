from __future__ import annotations

import errno
import fcntl
import json
import logging
from pathlib import Path
from unittest import mock

import pytest

from partsouq_catalog import supervisor


def _supervisor_with_database(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[supervisor.Supervisor, mock.MagicMock]:
    database = mock.MagicMock()
    database.connect.return_value = database
    monkeypatch.setattr(supervisor, "Database", mock.MagicMock(return_value=database))
    instance = supervisor.Supervisor(workers=1)
    monkeypatch.setattr(instance, "_load_restart_state", mock.MagicMock())
    monkeypatch.setattr(instance, "_write_summary", mock.MagicMock())
    return instance, database


def test_startup_admission_busy_defers_without_starting_child(monkeypatch) -> None:
    instance, database = _supervisor_with_database(monkeypatch)
    monkeypatch.setattr(
        instance,
        "_cleanup_stale_runs",
        mock.MagicMock(side_effect=supervisor.AdmissionLockBusy("migration")),
    )
    start = mock.MagicMock()
    monkeypatch.setattr(instance, "start", start)

    assert instance.run() == 75
    start.assert_not_called()
    database.close.assert_called_once_with()
    instance._write_summary.assert_called_once_with("deferred-schema-migration")


def test_startup_cleanup_failure_refuses_to_start_child(monkeypatch) -> None:
    instance, database = _supervisor_with_database(monkeypatch)
    monkeypatch.setattr(
        instance,
        "_cleanup_stale_runs",
        mock.MagicMock(side_effect=RuntimeError("release failed")),
    )
    start = mock.MagicMock()
    monkeypatch.setattr(instance, "start", start)

    assert instance.run() == 1
    start.assert_not_called()
    database.close.assert_called_once_with()
    instance._write_summary.assert_called_once_with("startup-error")


def test_cleanup_release_failure_discards_connection_and_propagates(monkeypatch) -> None:
    instance = supervisor.Supervisor(workers=1)
    database = mock.MagicMock()
    connection = mock.MagicMock()
    database._thread_conn.return_value = connection
    instance.db = database
    monkeypatch.setattr(supervisor, "acquire_catalog_writer_admission", lambda _conn: "lock")
    monkeypatch.setattr(
        supervisor,
        "release_catalog_writer_admission",
        mock.MagicMock(side_effect=RuntimeError("release failed")),
    )

    with pytest.raises(RuntimeError, match="release failed"):
        instance._cleanup_stale_runs()

    database.commit.assert_called_once_with()
    database._discard_thread_conn.assert_called_once_with()


def test_child_admission_defer_does_not_count_as_restart(monkeypatch) -> None:
    instance = supervisor.Supervisor(workers=1)
    process = mock.MagicMock()
    process.poll.return_value = 75
    instance.proc = process
    monkeypatch.setattr(instance, "_crawl_done", lambda: False)
    restart = mock.MagicMock()
    monkeypatch.setattr(instance, "restart", restart)

    instance._tick_inner()

    assert instance.proc is None
    restart.assert_not_called()
    assert instance.restarts == []


def test_main_rejects_symlinked_runtime_log_ancestor_before_any_write(
    monkeypatch, tmp_path
) -> None:
    external = tmp_path / "external-logs"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    alias = tmp_path / "logs-alias"
    alias.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(supervisor, "LOG_DIR", alias / "runtime")
    monkeypatch.setattr(supervisor.sys, "argv", ["partsouq-catalog-supervisor"])

    with pytest.raises(OSError, match="refusing symlinked private state path"):
        supervisor.main()

    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


def test_main_rejects_symlinked_supervisor_log_without_touching_target(
    monkeypatch, tmp_path
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    external = tmp_path / "external-log"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    (log_dir / "supervisor.log").symlink_to(external)
    monkeypatch.setattr(supervisor, "LOG_DIR", log_dir)
    monkeypatch.setattr(supervisor.sys, "argv", ["partsouq-catalog-supervisor"])

    with pytest.raises(OSError, match="refusing symlinked state file"):
        supervisor.main()

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_main_rejects_symlinked_supervisor_lock_without_touching_target(
    monkeypatch, tmp_path
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    external = tmp_path / "external-lock"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    (log_dir / "supervisor.lock").symlink_to(external)
    monkeypatch.setattr(supervisor, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        supervisor, "PrivateRotatingFileHandler", mock.Mock(return_value=mock.Mock())
    )
    monkeypatch.setattr(supervisor.logging, "basicConfig", mock.Mock())
    monkeypatch.setattr(supervisor.sys, "argv", ["partsouq-catalog-supervisor"])
    monkeypatch.setenv("LAUNCHD_JOB", "1")

    with pytest.raises(OSError, match="refusing symlinked state file"):
        supervisor.main()

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_summary_rejects_symlinked_leaf_without_touching_target(monkeypatch, tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    external = tmp_path / "external-summary"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    (log_dir / "summary.json").symlink_to(external)
    monkeypatch.setattr(supervisor, "LOG_DIR", log_dir)
    instance = supervisor.Supervisor(workers=1)

    instance._write_summary("failed")

    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_restart_state_uses_unpredictable_private_temp_without_touching_old_symlink(
    monkeypatch, tmp_path
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    external = tmp_path / "external-restart-state"
    external.write_text("preserve\n", encoding="utf-8")
    external.chmod(0o640)
    predictable_temporary = log_dir / ".supervisor_state.json.tmp"
    predictable_temporary.symlink_to(external)
    monkeypatch.setattr(supervisor, "LOG_DIR", log_dir)
    instance = supervisor.Supervisor(workers=1)
    instance._restart_state_loaded = True

    instance._persist_restart_state()

    state_path = log_dir / "supervisor_state.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 1
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert predictable_temporary.is_symlink()
    assert external.read_text(encoding="utf-8") == "preserve\n"
    assert external.stat().st_mode & 0o777 == 0o640


def test_main_releases_lock_and_closes_handler_when_supervisor_returns(
    monkeypatch, tmp_path
) -> None:
    handler = mock.MagicMock(spec=logging.Handler)
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "PrivateRotatingFileHandler", mock.Mock(return_value=handler))
    monkeypatch.setattr(supervisor.logging, "basicConfig", mock.Mock())
    monkeypatch.setattr(supervisor, "Supervisor", mock.Mock(return_value=mock.Mock(run=lambda: 0)))
    monkeypatch.setattr(supervisor.sys, "argv", ["partsouq-catalog-supervisor"])
    monkeypatch.setenv("LAUNCHD_JOB", "1")

    assert supervisor.main() == 0
    handler.close.assert_called_once_with()
    with (tmp_path / "supervisor.lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_main_propagates_non_contention_lock_error_and_closes_handler(
    monkeypatch, tmp_path
) -> None:
    handler = mock.MagicMock(spec=logging.Handler)
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "PrivateRotatingFileHandler", mock.Mock(return_value=handler))
    monkeypatch.setattr(supervisor.logging, "basicConfig", mock.Mock())
    monkeypatch.setattr(supervisor.sys, "argv", ["partsouq-catalog-supervisor"])
    monkeypatch.setenv("LAUNCHD_JOB", "1")
    monkeypatch.setattr(fcntl, "flock", mock.Mock(side_effect=OSError(errno.EBADF, "bad fd")))

    with pytest.raises(OSError) as error:
        supervisor.main()

    assert error.value.errno == errno.EBADF
    handler.close.assert_called_once_with()


def test_main_closes_handler_when_argument_parsing_exits(monkeypatch, tmp_path) -> None:
    handler = mock.MagicMock(spec=logging.Handler)
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "PrivateRotatingFileHandler", mock.Mock(return_value=handler))
    monkeypatch.setattr(supervisor.logging, "basicConfig", mock.Mock())
    monkeypatch.setattr(supervisor.sys, "argv", ["partsouq-catalog-supervisor", "--unknown"])
    monkeypatch.setenv("LAUNCHD_JOB", "1")

    with pytest.raises(SystemExit):
        supervisor.main()

    handler.close.assert_called_once_with()
