from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from partsouq_catalog import scheduler
from partsouq_crawler import cli
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import BulkSource
from partsouq_crawler.nhtsa.models import ArtifactMember, ParsedRecord
from partsouq_crawler.nhtsa.progress import scheduler_heartbeat
from partsouq_crawler.nhtsa.service import NhtsaBulkSyncService

VIN = "ZZZTEST00X0000001"


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


def test_scheduler_accepts_nhtsa_heartbeat_without_polluting_stdout(
    monkeypatch,
    capsys,
) -> None:
    finished: list[tuple[int, int, str]] = []
    monkeypatch.setenv("LAUNCHD_JOB", "1")
    monkeypatch.setattr(scheduler, "CHILD_STALL_TIMEOUT_SECONDS", 0.15)
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 91)
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
    monkeypatch.setattr(scheduler, "_record_start", lambda _job: 92)
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
    service.writer.insert = MagicMock(side_effect=lambda _artifact_id, rows: (len(rows), 0))

    result = service._import_artifact(
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
