from __future__ import annotations

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
