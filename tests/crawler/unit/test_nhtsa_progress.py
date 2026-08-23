from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from partsouq_catalog import scheduler
from partsouq_crawler import cli
from partsouq_crawler.nhtsa import progress
from partsouq_crawler.nhtsa.api_service import (
    API_PARSER_NAME,
    API_PARSER_VERSION,
    ApiSourceImport,
    NhtsaApiSyncService,
)
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import ApiSource, BulkSource
from partsouq_crawler.nhtsa.models import (
    ApiDocument,
    ArtifactMember,
    DownloadedArtifact,
    NhtsaRunLease,
    ParsedRecord,
    read_verified_stored_artifact,
    verified_stored_artifact_path,
)
from partsouq_crawler.nhtsa.progress import lease_heartbeat, scheduler_heartbeat
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository
from partsouq_crawler.nhtsa.service import NhtsaBulkSyncService

VIN = "ZZZTEST00X0000001"


def test_stored_artifact_integrity_requires_current_parser_size_and_sha256(
    tmp_path: Path,
) -> None:
    body = b'{"Count":0,"Results":[]}'
    path = tmp_path / "artifact.json"
    path.write_bytes(body)
    artifact: dict[str, object] = {
        "status": "imported",
        "verified_at": object(),
        "rejected_rows": 0,
        "parser_name": API_PARSER_NAME,
        "parser_version": API_PARSER_VERSION,
        "stored_path": str(path),
        "byte_count": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }

    assert (
        verified_stored_artifact_path(
            artifact,
            parser_name=API_PARSER_NAME,
            parser_version=API_PARSER_VERSION,
        )
        == path
    )
    assert (
        read_verified_stored_artifact(
            artifact,
            parser_name=API_PARSER_NAME,
            parser_version=API_PARSER_VERSION,
        )
        == body
    )

    for override in (
        {"parser_version": "old"},
        {"stored_path": str(tmp_path / "missing.json")},
        {"byte_count": len(body) + 1},
        {"sha256": "0" * 64},
    ):
        invalid = {**artifact, **override}
        assert (
            verified_stored_artifact_path(
                invalid,
                parser_name=API_PARSER_NAME,
                parser_version=API_PARSER_VERSION,
            )
            is None
        )

    path.write_bytes(b"x" * len(body))
    assert (
        read_verified_stored_artifact(
            artifact,
            parser_name=API_PARSER_NAME,
            parser_version=API_PARSER_VERSION,
        )
        is None
    )


def test_api_304_rejects_a_different_artifact_id(tmp_path: Path) -> None:
    body = b'{"Count":0,"Message":"ok","Results":[]}'
    path = tmp_path / "artifact.json"
    path.write_bytes(body)
    current: dict[str, object] = {
        "id": 11,
        "status": "imported",
        "verified_at": object(),
        "rejected_rows": 0,
        "parser_name": API_PARSER_NAME,
        "parser_version": API_PARSER_VERSION,
        "stored_path": str(path),
        "byte_count": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    repository = MagicMock()
    repository.current_artifact.return_value = current
    client = MagicMock()
    client.fetch = AsyncMock(
        return_value=(
            DownloadedArtifact(304, {}, None, None, 0, reused_artifact_id=12),
            None,
        )
    )
    service = NhtsaApiSyncService(
        repository,
        NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
    )

    with pytest.raises(ValueError, match="failed 304 revalidation"):
        asyncio.run(
            service._sync_source(
                client,
                ApiSource("cssi_test", "cssi_stations", "https://example.test/cssi"),
                NhtsaRunLease(7, "a" * 64, 9),
            )
        )


def test_time_driven_heartbeat_survives_sync_work_and_stops(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.progress.HEARTBEAT_INTERVAL_SECONDS",
        0.02,
    )
    before = {thread.name for thread in threading.enumerate()}

    with scheduler_heartbeat("bulk-test"):
        time.sleep(0.07)
    print('{"status":"completed"}')

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "completed"}
    assert "nhtsa bulk-test: still working" in captured.err
    assert {thread.name for thread in threading.enumerate()} == before


@pytest.mark.parametrize("body_fails", (False, True))
def test_scheduler_heartbeat_emit_failure_preserves_body_error(
    monkeypatch,
    body_fails: bool,
) -> None:
    monkeypatch.setattr(progress, "HEARTBEAT_INTERVAL_SECONDS", 0.001)

    def fail_emit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("emit failed")

    monkeypatch.setattr(progress, "print", fail_emit, raising=False)
    expected_error = ValueError if body_fails else RuntimeError
    expected_message = "body failed" if body_fails else "emit failed"

    with pytest.raises(expected_error, match=expected_message), scheduler_heartbeat("test"):
        time.sleep(0.02)
        if body_fails:
            raise ValueError("body failed")


def test_scheduler_heartbeat_shutdown_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(progress, "HEARTBEAT_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(progress, "HEARTBEAT_JOIN_TIMEOUT_SECONDS", 0.01)
    started = threading.Event()
    release = threading.Event()

    def block_emit(*_args: object, **_kwargs: object) -> None:
        started.set()
        release.wait(1)

    monkeypatch.setattr(progress, "print", block_emit, raising=False)
    started_at = time.monotonic()
    with (
        pytest.raises(RuntimeError, match="heartbeat did not stop before timeout"),
        scheduler_heartbeat("blocked"),
    ):
        assert started.wait(1)
    elapsed = time.monotonic() - started_at
    release.set()

    assert elapsed < 0.5


def test_database_heartbeat_uses_owned_connection_and_stops(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.progress.HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    heartbeat_repository = MagicMock()
    create_calls: list[int] = []
    monkeypatch.setattr(
        NhtsaMySQLRepository,
        "create",
        lambda _config, *, timeout_seconds: (
            create_calls.append(timeout_seconds) or heartbeat_repository
        ),
    )
    lease = NhtsaRunLease(7, "a" * 64, 9)
    before = {thread.name for thread in threading.enumerate()}

    with lease_heartbeat(
        NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
        lease,
    ) as check:
        time.sleep(0.035)
        check()

    assert heartbeat_repository.heartbeat.call_count >= 2
    heartbeat_repository.heartbeat.assert_called_with(lease)
    heartbeat_repository.close.assert_called_once_with()
    assert create_calls == [15]
    assert {thread.name for thread in threading.enumerate()} == before


def test_database_heartbeat_shutdown_is_bounded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.progress.HEARTBEAT_INTERVAL_SECONDS",
        0.001,
    )
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.progress.HEARTBEAT_JOIN_TIMEOUT_SECONDS",
        0.01,
    )
    started = threading.Event()
    release = threading.Event()
    heartbeat_repository = MagicMock()

    def block_heartbeat(_lease: NhtsaRunLease) -> None:
        started.set()
        release.wait(1)

    heartbeat_repository.heartbeat.side_effect = block_heartbeat
    monkeypatch.setattr(
        NhtsaMySQLRepository,
        "create",
        lambda _config, *, timeout_seconds: heartbeat_repository,
    )
    lease = NhtsaRunLease(7, "a" * 64, 9)

    started_at = time.monotonic()
    with (
        pytest.raises(RuntimeError, match="did not stop before timeout"),
        lease_heartbeat(
            NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
            lease,
        ),
    ):
        assert started.wait(1)
    elapsed = time.monotonic() - started_at
    release.set()

    assert elapsed < 0.5


def test_database_heartbeat_close_failure_does_not_mask_body_error(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    heartbeat_repository = MagicMock()
    heartbeat_repository.close.side_effect = RuntimeError("close failed")
    monkeypatch.setattr(
        NhtsaMySQLRepository,
        "create",
        lambda _config, *, timeout_seconds: heartbeat_repository,
    )

    with (
        pytest.raises(ValueError, match="body failed"),
        lease_heartbeat(
            NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
            NhtsaRunLease(7, "a" * 64, 9),
        ),
    ):
        raise ValueError("body failed")

    assert "heartbeat cleanup failed: close failed" in capsys.readouterr().err


def test_transaction_rolls_back_when_commit_fails() -> None:
    connection = MagicMock()
    connection.commit.side_effect = RuntimeError("commit failed")
    repository = NhtsaMySQLRepository(connection)

    with pytest.raises(RuntimeError, match="commit failed"), repository.transaction():
        pass

    connection.rollback.assert_called_once_with()


def test_bulk_stops_database_heartbeat_before_publication(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []

    @contextmanager
    def tracked_heartbeat(
        _config: NhtsaConfig,
        _lease: NhtsaRunLease,
    ) -> Iterator[object]:
        events.append("started")
        try:
            yield lambda: None
        finally:
            events.append("stopped")

    class FakeClient:
        def __init__(self, _config: NhtsaConfig) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

    repository = MagicMock()
    repository.start_run.return_value = NhtsaRunLease(7, "a" * 64, 9)

    def publish(*_args: object, **_kwargs: object) -> None:
        assert events == ["started", "stopped"]

    repository.complete_run_and_publish_artifacts.side_effect = publish
    monkeypatch.setattr("partsouq_crawler.nhtsa.service.lease_heartbeat", tracked_heartbeat)
    monkeypatch.setattr("partsouq_crawler.nhtsa.service.NhtsaBulkClient", FakeClient)

    report = asyncio.run(
        NhtsaBulkSyncService(
            repository,
            NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
        ).run(
            run_key="heartbeat-barrier",
            scope_name="test",
            sources=(),
            scheduled_job_run_id=9,
        )
    )

    assert report["status"] == "completed"
    assert events == ["started", "stopped"]


def test_api_stops_database_heartbeat_before_publication(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []

    @contextmanager
    def tracked_heartbeat(
        _config: NhtsaConfig,
        _lease: NhtsaRunLease,
    ) -> Iterator[object]:
        events.append("started")
        try:
            yield lambda: None
        finally:
            events.append("stopped")

    class FakeClient:
        def __init__(self, _config: NhtsaConfig) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

    source = ApiSource("cssi_test", "cssi_stations", "https://example.test/cssi")
    document = ApiDocument(
        member=ArtifactMember("response.json", 1, 1, None, (), "a" * 64),
        records=(),
        rejections=(),
        count=0,
        message="ok",
    )
    repository = MagicMock()
    repository.start_run.return_value = NhtsaRunLease(7, "a" * 64, 9)

    def publish(*_args: object, **_kwargs: object) -> None:
        assert events == ["started", "stopped"]

    repository.complete_run_and_publish_artifacts.side_effect = publish
    service = NhtsaApiSyncService(
        repository,
        NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
    )
    service._sync_source = AsyncMock(return_value=ApiSourceImport(1, document, True, 0))
    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.CSSI_SOURCES", (source,))
    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.lease_heartbeat", tracked_heartbeat)
    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.NhtsaApiClient", FakeClient)

    report = asyncio.run(
        service.run(run_key="api-heartbeat-barrier", scope_name="cssi", scheduled_job_run_id=9)
    )

    assert report["status"] == "completed"
    assert events == ["started", "stopped"]


def test_vin_stops_database_heartbeat_before_publication(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []

    @contextmanager
    def tracked_heartbeat(
        _config: NhtsaConfig,
        _lease: NhtsaRunLease,
    ) -> Iterator[object]:
        events.append("started")
        try:
            yield lambda: None
        finally:
            events.append("stopped")

    class FakeClient:
        def __init__(self, _config: NhtsaConfig) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

    payload = {
        "VIN": VIN,
        "Make": "TEST",
        "Model": "MODEL",
        "ModelYear": "2020",
        "EngineConfiguration": "In-Line",
        "DisplacementL": "2.0",
        "Trim": "TEST",
        "ErrorCode": "0",
    }
    record = ParsedRecord(
        dataset_name="vpic_vin_decodes",
        natural_key_sha256="b" * 64,
        record_sha256="c" * 64,
        natural_key_text=VIN,
        external_id=VIN,
        make_name="TEST",
        model_name="MODEL",
        model_year=2020,
        campaign_number=None,
        component_name=None,
        summary_text=None,
        payload_json=json.dumps(payload),
        member_name="response.json",
        source_line=1,
    )
    document = ApiDocument(
        member=ArtifactMember("response.json", 1, 1, None, (), "a" * 64),
        records=(record,),
        rejections=(),
        count=1,
        message="ok",
    )
    repository = MagicMock()
    repository.start_run.return_value = NhtsaRunLease(7, "a" * 64, 9)

    def publish(*_args: object, **_kwargs: object) -> dict[str, object]:
        assert events == ["started", "stopped"]
        return {}

    repository.complete_run_and_publish_vin_decode.side_effect = publish
    service = NhtsaApiSyncService(
        repository,
        NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
    )
    service._sync_source = AsyncMock(return_value=ApiSourceImport(1, document, True, 0))
    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.lease_heartbeat", tracked_heartbeat)
    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.NhtsaApiClient", FakeClient)

    report = asyncio.run(
        service.decode_vin(run_key="vin-heartbeat-barrier", vin=VIN, scheduled_job_run_id=9)
    )

    assert report["status"] == "completed"
    assert events == ["started", "stopped"]


@pytest.mark.parametrize("fail_on_enter", (False, True))
def test_bulk_heartbeat_failure_prevents_publication_and_enters_terminal_cleanup(
    monkeypatch,
    tmp_path: Path,
    fail_on_enter: bool,
) -> None:
    @contextmanager
    def failing_heartbeat(
        _config: NhtsaConfig,
        _lease: NhtsaRunLease,
    ) -> Iterator[object]:
        if fail_on_enter:
            raise RuntimeError("heartbeat enter failed")
        yield lambda: None
        raise RuntimeError("heartbeat close failed")

    class FakeClient:
        def __init__(self, _config: NhtsaConfig) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

    repository = MagicMock()
    repository.start_run.return_value = NhtsaRunLease(7, "a" * 64, 9)
    monkeypatch.setattr("partsouq_crawler.nhtsa.service.lease_heartbeat", failing_heartbeat)
    monkeypatch.setattr("partsouq_crawler.nhtsa.service.NhtsaBulkClient", FakeClient)

    report = asyncio.run(
        NhtsaBulkSyncService(
            repository,
            NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
        ).run(
            run_key="heartbeat-failure",
            scope_name="test",
            sources=(),
            scheduled_job_run_id=9,
        )
    )

    assert report["status"] == "failed"
    assert report["error"] == (
        "heartbeat enter failed" if fail_on_enter else "heartbeat close failed"
    )
    repository.complete_run_and_publish_artifacts.assert_not_called()
    repository.finish_run.assert_called_once()


def test_bulk_quarantine_failure_preserves_parser_error_and_finishes_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    body = b"test"
    raw_path = tmp_path / "bulk.zip"
    raw_path.write_bytes(body)

    class FakeClient:
        def __init__(self, _config: NhtsaConfig) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def download(
            self,
            _source: BulkSource,
            *,
            current_artifact: dict[str, object] | None,
        ) -> DownloadedArtifact:
            del current_artifact
            return DownloadedArtifact(200, {}, raw_path, "a" * 64, len(body))

    repository = MagicMock()
    repository.start_run.return_value = NhtsaRunLease(7, "a" * 64, 9)
    repository.current_artifact.return_value = None
    repository.artifact_by_content.return_value = None
    repository.create_artifact.return_value = 11
    repository.quarantine_artifact.side_effect = RuntimeError("quarantine failed")
    parser = MagicMock()
    parser.inspect.side_effect = ValueError("bulk parser failed")
    monkeypatch.setattr("partsouq_crawler.nhtsa.service.NhtsaBulkClient", FakeClient)
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.service.lease_heartbeat",
        lambda *_args: nullcontext(lambda: None),
    )

    report = asyncio.run(
        NhtsaBulkSyncService(
            repository,
            NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
            parser=parser,
        ).run(
            run_key="bulk-parser-failure",
            scope_name="test",
            sources=(
                BulkSource(
                    "recalls",
                    "recalls",
                    "https://example.test/recalls.zip",
                    "recalls.txt",
                ),
            ),
            scheduled_job_run_id=9,
        )
    )

    assert report["status"] == "failed"
    assert report["error"] == "bulk parser failed"
    repository.quarantine_artifact.assert_called_once()
    repository.finish_run.assert_called_once()


def test_api_quarantine_failure_preserves_parser_error_and_finishes_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    body = b'{"Count":0,"Results":[]}'
    raw_path = tmp_path / "api.json"
    raw_path.write_bytes(body)

    class FakeClient:
        def __init__(self, _config: NhtsaConfig) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def fetch(
            self,
            _source: ApiSource,
            *,
            current_artifact: dict[str, object] | None,
        ) -> tuple[DownloadedArtifact, bytes]:
            del current_artifact
            return DownloadedArtifact(200, {}, raw_path, "a" * 64, len(body)), body

    source = ApiSource("cssi_test", "cssi_stations", "https://example.test/cssi")
    repository = MagicMock()
    repository.start_run.return_value = NhtsaRunLease(7, "a" * 64, 9)
    repository.current_artifact.return_value = None
    repository.artifact_by_content.return_value = None
    repository.create_artifact.return_value = 11
    repository.quarantine_artifact.side_effect = RuntimeError("quarantine failed")
    parser = MagicMock()
    parser.parse.side_effect = ValueError("API parser failed")
    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.CSSI_SOURCES", (source,))
    monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.NhtsaApiClient", FakeClient)
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.api_service.lease_heartbeat",
        lambda *_args: nullcontext(lambda: None),
    )

    report = asyncio.run(
        NhtsaApiSyncService(
            repository,
            NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
            parser=parser,
        ).run(run_key="api-parser-failure", scope_name="cssi", scheduled_job_run_id=9)
    )

    assert report["status"] == "failed"
    assert report["error"] == "API parser failed"
    repository.quarantine_artifact.assert_called_once()
    repository.finish_run.assert_called_once()


def test_vin_quarantine_failure_preserves_validation_error_and_finishes_run(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = ApiDocument(
        member=ArtifactMember("response.json", 1, 1, None, (), "a" * 64),
        records=(),
        rejections=(),
        count=0,
        message="empty",
    )
    repository = MagicMock()
    repository.start_run.return_value = NhtsaRunLease(7, "a" * 64, 9)
    repository.quarantine_artifact.side_effect = RuntimeError("quarantine failed")
    service = NhtsaApiSyncService(
        repository,
        NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
    )
    service._sync_source = AsyncMock(return_value=ApiSourceImport(11, document, True, 0))
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.api_service.lease_heartbeat",
        lambda *_args: nullcontext(lambda: None),
    )

    report = asyncio.run(
        service.decode_vin(
            run_key="vin-validation-failure",
            vin=VIN,
            scheduled_job_run_id=9,
        )
    )

    assert report["status"] == "failed"
    assert report["error"].startswith("NHTSA VIN decode must return exactly one valid result")
    repository.quarantine_artifact.assert_called_once()
    repository.finish_run.assert_called_once()


def test_scheduler_accepts_nhtsa_heartbeat_without_polluting_stdout(
    monkeypatch,
    capsys,
) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setenv("LAUNCHD_JOB", "1")
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job, _parent=None: 91)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda run_id, code, output, *_success: finished.append((run_id, code, output)),
    )
    script = (
        "import time\n"
        "from partsouq_crawler.nhtsa import progress\n"
        "with progress.scheduler_heartbeat('bulk-test'):\n"
        " time.sleep(0.22)\n"
        'print(\'{"status":"completed"}\')\n'
    )

    assert scheduler._run("nhtsa-bulk", [sys.executable, "-c", script]) == 0
    assert capsys.readouterr().out == ""
    assert finished[0][0:2] == (91, 0)
    assert "nhtsa bulk-test: still working" in finished[0][2]
    assert finished[0][2].splitlines()[-1] == '{"status":"completed"}'


def test_scheduler_still_terminates_after_nhtsa_heartbeat_stops(monkeypatch) -> None:
    monkeypatch.setenv("LAUNCHD_JOB", "1")
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(scheduler, "CHILD_TERMINATE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job, _parent=None: 92)
    monkeypatch.setattr(scheduler, "_record_finish", lambda *_args, **_kwargs: None)
    script = (
        "import time\n"
        "from partsouq_crawler.nhtsa.progress import scheduler_heartbeat\n"
        "with scheduler_heartbeat('bulk'):\n"
        " time.sleep(0.1)\n"
        "time.sleep(30)\n"
    )

    assert scheduler._run("nhtsa-bulk", [sys.executable, "-c", script]) == 124


@pytest.mark.parametrize(
    ("arguments", "label"),
    (
        (["nhtsa-sync-bulk", "--scope", "recalls"], "bulk"),
        (["nhtsa-sync-api", "--scope", "vpic"], "api"),
        (["nhtsa-decode-vin", VIN], "vin"),
    ),
)
def test_all_nhtsa_cli_jobs_emit_heartbeats_and_clean_up(
    monkeypatch,
    capsys,
    arguments: list[str],
    label: str,
) -> None:
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.progress.HEARTBEAT_INTERVAL_SECONDS",
        0.02,
    )
    monkeypatch.setenv("SCHEDULED_JOB_RUN_ID", "91")
    repository = MagicMock()
    monkeypatch.setattr(cli.NhtsaMySQLRepository, "create", lambda _config: repository)

    class FakeBulkService:
        def __init__(self, *_args: object) -> None:
            pass

        async def run(self, **_kwargs: object) -> dict[str, str]:
            await asyncio.sleep(0.06)
            return {"status": "completed"}

    class FakeApiService:
        def __init__(self, *_args: object) -> None:
            pass

        async def run(self, **_kwargs: object) -> dict[str, str]:
            await asyncio.sleep(0.06)
            return {"status": "completed"}

        async def decode_vin(self, **_kwargs: object) -> dict[str, str]:
            await asyncio.sleep(0.06)
            return {"status": "completed"}

    monkeypatch.setattr(cli, "NhtsaBulkSyncService", FakeBulkService)
    monkeypatch.setattr(cli, "NhtsaApiSyncService", FakeApiService)
    before = {thread.name for thread in threading.enumerate()}

    assert asyncio.run(cli._dispatch_nhtsa(cli.build_parser().parse_args(arguments))) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "completed"}
    assert f"nhtsa {label}: still working" in captured.err
    assert {thread.name for thread in threading.enumerate()} == before
    repository.close.assert_called_once_with()


def test_nhtsa_cli_heartbeat_stops_when_service_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.progress.HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setenv("SCHEDULED_JOB_RUN_ID", "92")
    repository = MagicMock()
    monkeypatch.setattr(cli.NhtsaMySQLRepository, "create", lambda _config: repository)

    class FailingService:
        def __init__(self, *_args: object) -> None:
            pass

        async def run(self, **_kwargs: object) -> dict[str, str]:
            await asyncio.sleep(0.03)
            raise RuntimeError("expected failure")

    monkeypatch.setattr(cli, "NhtsaBulkSyncService", FailingService)
    before = {thread.name for thread in threading.enumerate()}

    with pytest.raises(RuntimeError, match="expected failure"):
        asyncio.run(
            cli._dispatch_nhtsa(
                cli.build_parser().parse_args(["nhtsa-sync-bulk", "--scope", "recalls"])
            )
        )

    assert {thread.name for thread in threading.enumerate()} == before
    repository.close.assert_called_once_with()


def test_bulk_import_reports_progress_for_scheduler_watchdog(
    monkeypatch,
    capsys,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("partsouq_crawler.nhtsa.service.BATCH_SIZE", 2)
    repository = MagicMock()
    parser = MagicMock()
    service = NhtsaBulkSyncService(
        repository,
        NhtsaConfig.from_env(raw_dir=tmp_path, user_agent="test/1.0"),
        parser=parser,
    )
    member = ArtifactMember(
        name="recalls.txt",
        uncompressed_bytes=1,
        compressed_bytes=1,
        crc32=1,
        field_names=("RECORD_ID",),
        schema_sha256="a" * 64,
    )
    parser.iter_records.return_value = iter(
        ParsedRecord(
            dataset_name="recalls",
            natural_key_sha256=f"{index:064x}",
            record_sha256=f"{index + 10:064x}",
            natural_key_text=str(index),
            external_id=str(index),
            make_name="TEST",
            model_name="MODEL",
            model_year=2020,
            campaign_number=None,
            component_name=None,
            summary_text=None,
            payload_json="{}",
            member_name="recalls.txt",
            source_line=index,
        )
        for index in range(1, 6)
    )
    service.writer.insert = MagicMock(side_effect=lambda _lease, _artifact_id, rows: (len(rows), 0))

    result = service._import_artifact(
        NhtsaRunLease(7, "a" * 64, 9),
        1,
        tmp_path / "unused.zip",
        BulkSource("recalls", "recalls", "https://example.test/recalls.zip", "recalls.txt"),
        member,
    )

    assert result == (5, 5, 0)
    assert capsys.readouterr().err.splitlines() == [
        "nhtsa bulk recalls: processed 2 rows",
        "nhtsa bulk recalls: processed 4 rows",
        "nhtsa bulk recalls: processed 5 rows",
    ]
