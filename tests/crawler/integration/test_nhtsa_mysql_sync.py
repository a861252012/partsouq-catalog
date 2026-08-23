from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event

import pytest
from aiohttp import web

from partsouq_catalog.admission import AdmissionLockBusy
from partsouq_crawler.nhtsa.api import NhtsaApiParser, NhtsaApiPolicy, vin_source_key
from partsouq_crawler.nhtsa.api_client import NhtsaApiClient
from partsouq_crawler.nhtsa.api_service import (
    API_PARSER_VERSION,
    ApiSourceImport,
    NhtsaApiSyncService,
)
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import RECALL_FIELDS, ApiSource, BulkSource
from partsouq_crawler.nhtsa.models import (
    ApiDocument,
    ArtifactMember,
    DownloadedArtifact,
    NhtsaRunLease,
    ParsedRecord,
    RejectedRow,
)
from partsouq_crawler.nhtsa.repository import (
    BULK_PARSER_VERSION,
    NhtsaLeaseLostError,
    NhtsaMySQLRepository,
)
from partsouq_crawler.nhtsa.service import NhtsaBulkSyncService

from ..helpers import fake_site

pytestmark = pytest.mark.skipif(
    os.getenv("NHTSA_TEST_MYSQL") != "1",
    reason="set NHTSA_TEST_MYSQL=1 to run MySQL integration tests",
)

VIN = "ZZZTEST00X0000001"
OTHER_VIN = "ZZZTEST00X0000002"


class _LocalApiPolicy(NhtsaApiPolicy):
    def validate(self, _url: str) -> None:
        pass


class _LocalNhtsaApiClient(NhtsaApiClient):
    def __init__(self, config: NhtsaConfig) -> None:
        super().__init__(config, policy=_LocalApiPolicy())


def _api_body() -> bytes:
    return json.dumps(
        {
            "Count": 1,
            "Message": "Results returned successfully",
            "Results": [
                {
                    "Organization": "Test station",
                    "State": "IL",
                    "Zip": "60601",
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def _vin_payload(**overrides: str) -> dict[str, str]:
    payload = {
        "VIN": VIN,
        "Make": "TEST MAKE",
        "Model": "TEST MODEL",
        "ModelYear": "2020",
        "EngineConfiguration": "In-Line",
        "EngineModel": "TEST ENGINE",
        "DisplacementL": "2.0",
        "Trim": "TEST TRIM",
        "ErrorCode": "0",
        "ErrorText": "",
    }
    payload.update(overrides)
    return payload


def _patch_vin_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict[str, str],
) -> None:
    body = json.dumps(
        {
            "Count": 1,
            "Message": "Results returned successfully",
            "Results": [payload],
        }
    ).encode()
    raw_path = tmp_path / f"{hashlib.sha256(body).hexdigest()}.json"
    raw_path.write_bytes(body)

    class FakeNhtsaApiClient:
        def __init__(self, _config: NhtsaConfig) -> None:
            pass

        async def __aenter__(self) -> FakeNhtsaApiClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def fetch(
            self,
            _source: object,
            *,
            current_artifact: dict[str, object] | None,
        ) -> tuple[DownloadedArtifact, bytes]:
            del current_artifact
            return (
                DownloadedArtifact(
                    http_status=200,
                    response_headers={
                        "content-type": "application/json",
                        "content-length": str(len(body)),
                    },
                    path=raw_path,
                    sha256=hashlib.sha256(body).hexdigest(),
                    byte_count=len(body),
                ),
                body,
            )

    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.api_service.NhtsaApiClient",
        FakeNhtsaApiClient,
    )


def _row(record_id: str, campaign: str) -> list[str]:
    values = {field: "" for field in RECALL_FIELDS}
    values.update(
        {
            "RECORD_ID": record_id,
            "CAMPNO": campaign,
            "MAKETXT": "TOYOTA",
            "MODELTXT": "CAMRY",
            "YEARTXT": "2020",
            "COMPNAME": "FUEL SYSTEM",
            "DESC_DEFECT": "LOW-PRESSURE FUEL PUMP MAY FAIL.",
            "MFR_COMP_PTNO": "PUMP-001",
            "DO_NOT_DRIVE": "No",
            "PARK_OUTSIDE": "No",
        }
    )
    return [values[field] for field in RECALL_FIELDS]


def _zip(member: str, rows: list[list[str]]) -> bytes:
    output = io.BytesIO()
    body = "".join("\t".join(row) + "\n" for row in rows)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, body.encode("cp1252"))
    return output.getvalue()


def _config(tmp_path: Path) -> NhtsaConfig:
    config = NhtsaConfig.from_env(
        raw_dir=tmp_path / "raw",
        user_agent="nhtsa-test/1.0",
        request_timeout_seconds=10,
    )
    if not config.mysql_database.endswith("_test"):
        raise ValueError("NHTSA_TEST_MYSQL requires a database name ending in _test")
    return config


def _scheduled_job(repository: NhtsaMySQLRepository, job_name: str) -> int:
    with repository.transaction() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO scheduled_job_runs(job_name, trigger_mode, status, started_at)
            VALUES (%s, 'daemon', 'running', UTC_TIMESTAMP())
            """,
            (job_name,),
        )
        return int(cursor.lastrowid)


def _stage_empty_artifact(
    repository: NhtsaMySQLRepository,
    lease: NhtsaRunLease,
    tmp_path: Path,
    *,
    dataset_name: str,
    source_key: str,
    content_marker: str = "",
) -> int:
    body = json.dumps(
        {"source_key": source_key, "content_marker": content_marker},
        separators=(",", ":"),
    ).encode()
    sha256 = hashlib.sha256(body).hexdigest()
    raw_path = tmp_path / f"{sha256}.json"
    raw_path.write_bytes(body)
    artifact_id = repository.create_artifact(
        lease,
        dataset_name=dataset_name,
        source_key=source_key,
        source_url=f"https://example.test/{source_key}",
        download=DownloadedArtifact(
            http_status=200,
            response_headers={"content-type": "application/json"},
            path=raw_path,
            sha256=sha256,
            byte_count=len(body),
        ),
        parser_name="scope-test",
        parser_version="1",
    )
    repository.store_member(
        lease,
        artifact_id,
        ArtifactMember("response.json", len(body), len(body), None, (), sha256),
    )
    repository.reset_artifact_import(lease, artifact_id)
    repository.complete_artifact(
        lease,
        artifact_id,
        source_rows=0,
        new_versions=0,
        rejected_rows=0,
    )
    return artifact_id


def _stage_vin_artifact(
    repository: NhtsaMySQLRepository,
    lease: NhtsaRunLease,
    tmp_path: Path,
    payload: dict[str, str],
    *,
    source_key: str | None = None,
) -> int:
    artifact_source_key = source_key or vin_source_key(payload["VIN"])
    source = ApiSource(
        key=artifact_source_key,
        dataset_name="vpic_vin_decodes",
        url=(
            f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{payload['VIN']}?format=json"
        ),
    )
    body = json.dumps(
        {"Count": 1, "Message": "Results returned successfully", "Results": [payload]},
        separators=(",", ":"),
    ).encode()
    document = NhtsaApiParser().parse(body, source)
    sha256 = hashlib.sha256(body).hexdigest()
    raw_path = tmp_path / f"{sha256}.json"
    raw_path.write_bytes(body)
    artifact_id = repository.create_artifact(
        lease,
        dataset_name=source.dataset_name,
        source_key=source.key,
        source_url=source.url,
        download=DownloadedArtifact(
            http_status=200,
            response_headers={"content-type": "application/json"},
            path=raw_path,
            sha256=sha256,
            byte_count=len(body),
        ),
        parser_name="vin-boundary-test",
        parser_version="1",
    )
    repository.store_member(lease, artifact_id, document.member)
    repository.reset_artifact_import(lease, artifact_id)
    new_versions = repository.insert_records(lease, artifact_id, document.records)
    repository.complete_artifact(
        lease,
        artifact_id,
        source_rows=1,
        new_versions=new_versions,
        rejected_rows=0,
    )
    return artifact_id


def _stage_api_document(
    repository: NhtsaMySQLRepository,
    lease: NhtsaRunLease,
    tmp_path: Path,
    source: ApiSource,
    body: bytes,
) -> tuple[int, ApiDocument, int]:
    document = NhtsaApiParser().parse(body, source)
    sha256 = hashlib.sha256(body).hexdigest()
    raw_path = tmp_path / f"{sha256}-{source.key}.json"
    raw_path.write_bytes(body)
    artifact_id = repository.create_artifact(
        lease,
        dataset_name=source.dataset_name,
        source_key=source.key,
        source_url=source.url,
        download=DownloadedArtifact(
            http_status=200,
            response_headers={"content-type": "application/json"},
            path=raw_path,
            sha256=sha256,
            byte_count=len(body),
        ),
        parser_name="api-scope-test",
        parser_version="1",
    )
    repository.store_member(lease, artifact_id, document.member)
    repository.reset_artifact_import(lease, artifact_id)
    new_versions = repository.insert_records(lease, artifact_id, document.records)
    repository.complete_artifact(
        lease,
        artifact_id,
        source_rows=document.count,
        new_versions=new_versions,
        rejected_rows=0,
    )
    return artifact_id, document, new_versions


def _fail_completed_finalization(
    repository: NhtsaMySQLRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finish_run = repository._finish_run

    def fail_completed_run(*args: object, **kwargs: object) -> None:
        finish_run(*args, **kwargs)  # type: ignore[arg-type]
        if kwargs["status"] == "completed":
            raise RuntimeError("simulated run finalization failure")

    monkeypatch.setattr(repository, "_finish_run", fail_completed_run)


def test_bulk_sync_is_real_provenanced_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "partsouq_crawler.nhtsa.progress.HEARTBEAT_INTERVAL_SECONDS",
        0.001,
    )

    async def scenario() -> None:
        payloads = {
            "/pre.zip": _zip("pre.txt", [_row("1", "10V000001")]),
            "/post.zip": _zip("post.txt", [_row("2", "20V000002")]),
        }

        async def handler(request: web.Request) -> web.Response:
            if request.headers.get("If-None-Match") == '"fixture-v1"':
                return web.Response(status=304)
            return web.Response(
                body=payloads[request.path],
                headers={"ETag": '"fixture-v1"'},
                content_type="application/zip",
            )

        async with fake_site(handler) as base_url:
            sources = (
                BulkSource("test_pre", "recalls", f"{base_url}/pre.zip", "pre.txt"),
                BulkSource("test_post", "recalls", f"{base_url}/post.zip", "post.txt"),
            )
            repository = NhtsaMySQLRepository.create(_config(tmp_path))
            try:
                repository.clear_for_tests()
                service = NhtsaBulkSyncService(repository, _config(tmp_path))
                first = await service.run(
                    run_key="fixture-sync",
                    scope_name="recalls",
                    sources=sources,
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
                )
                assert first["status"] == "completed"
                assert first["source_rows"] == 2
                assert first["new_versions"] == 2
                assert first["rejected_rows"] == 0
                status = repository.status_report()
                assert status["current_record_counts"] == {"recalls": 2}

                with repository.connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT JSON_UNQUOTE(JSON_EXTRACT(payload_json, '$.MFR_COMP_PTNO')) AS part,
                               source_member, source_line, source_artifact_sha256
                        FROM nhtsa_current_records ORDER BY external_id
                        """
                    )
                    rows = cursor.fetchall()
                assert [row["part"] for row in rows] == ["PUMP-001", "PUMP-001"]
                assert all(len(str(row["source_artifact_sha256"])) == 64 for row in rows)
                with repository.connection.cursor() as cursor:
                    cursor.execute("SELECT DISTINCT published_run_id FROM nhtsa_current_artifacts")
                    assert {row["published_run_id"] for row in cursor} == {first["run_id"]}

                second = await service.run(
                    run_key="fixture-sync",
                    scope_name="recalls",
                    sources=sources,
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
                )
                assert second["status"] == "completed"
                assert second["artifacts_downloaded"] == 0
                assert second["artifacts_reused"] == 2
                assert repository.status_report()["current_record_counts"] == {"recalls": 2}
                with repository.connection.cursor() as cursor:
                    cursor.execute("SELECT DISTINCT published_run_id FROM nhtsa_current_artifacts")
                    assert {row["published_run_id"] for row in cursor} == {second["run_id"]}
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_reason", ("parser", "missing_path", "tampered"))
def test_bulk_invalid_current_forces_200_and_repairs_storage(
    tmp_path: Path,
    invalid_reason: str,
) -> None:
    async def scenario() -> None:
        body = _zip("recalls.txt", [_row("repair", "24V000010")])
        conditionals: list[tuple[str | None, str | None]] = []

        async def handler(request: web.Request) -> web.Response:
            conditionals.append(
                (
                    request.headers.get("If-None-Match"),
                    request.headers.get("If-Modified-Since"),
                )
            )
            if request.headers.get("If-None-Match") == '"repair-v1"':
                return web.Response(status=304)
            return web.Response(
                body=body,
                headers={"ETag": '"repair-v1"'},
                content_type="application/zip",
            )

        async with fake_site(handler) as base_url:
            source = BulkSource(
                "repair_recalls",
                "recalls",
                f"{base_url}/recalls.zip",
                "recalls.txt",
            )
            config = _config(tmp_path)
            repository = NhtsaMySQLRepository.create(config)
            try:
                repository.clear_for_tests()
                first = await NhtsaBulkSyncService(repository, config).run(
                    run_key="bulk-repair-first",
                    scope_name="recalls",
                    sources=(source,),
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
                )
                assert first["status"] == "completed"
                current = repository.current_artifact(source.dataset_name, source.key)
                assert current is not None
                original_id = int(str(current["id"]))
                original_path = Path(str(current["stored_path"]))

                if invalid_reason == "parser":
                    with repository.transaction() as connection, connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE nhtsa_source_artifacts SET parser_version = 'old' WHERE id = %s",
                            (original_id,),
                        )
                elif invalid_reason == "missing_path":
                    with repository.transaction() as connection, connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE nhtsa_source_artifacts SET stored_path = %s WHERE id = %s",
                            (str(tmp_path / "missing.zip"), original_id),
                        )
                else:
                    await asyncio.to_thread(original_path.write_bytes, b"x" * len(body))

                second = await NhtsaBulkSyncService(repository, config).run(
                    run_key="bulk-repair-second",
                    scope_name="recalls",
                    sources=(source,),
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
                )
                assert second["status"] == "completed"
                assert conditionals == [(None, None), (None, None)]

                refreshed = repository.current_artifact(source.dataset_name, source.key)
                assert refreshed is not None
                refreshed_path = Path(str(refreshed["stored_path"]))
                assert refreshed["parser_version"] == BULK_PARSER_VERSION
                assert await asyncio.to_thread(refreshed_path.read_bytes) == body
                refreshed_stat = await asyncio.to_thread(refreshed_path.stat)
                assert refreshed_stat.st_size == int(str(refreshed["byte_count"]))
                assert hashlib.sha256(body).hexdigest() == refreshed["sha256"]
                if invalid_reason == "parser":
                    assert int(str(refreshed["id"])) != original_id
                else:
                    assert int(str(refreshed["id"])) == original_id
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


def test_bulk_304_revalidates_raw_before_publication(tmp_path: Path) -> None:
    async def scenario() -> None:
        body = _zip("recalls.txt", [_row("race", "24V000011")])
        current_path: Path | None = None

        async def handler(request: web.Request) -> web.Response:
            if request.headers.get("If-None-Match") == '"race-v1"':
                assert current_path is not None
                await asyncio.to_thread(current_path.write_bytes, b"x" * len(body))
                return web.Response(status=304)
            return web.Response(
                body=body,
                headers={"ETag": '"race-v1"'},
                content_type="application/zip",
            )

        async with fake_site(handler) as base_url:
            source = BulkSource(
                "race_recalls",
                "recalls",
                f"{base_url}/recalls.zip",
                "recalls.txt",
            )
            config = _config(tmp_path)
            repository = NhtsaMySQLRepository.create(config)
            try:
                repository.clear_for_tests()
                first = await NhtsaBulkSyncService(repository, config).run(
                    run_key="bulk-race-first",
                    scope_name="recalls",
                    sources=(source,),
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
                )
                assert first["status"] == "completed"
                current = repository.current_artifact(source.dataset_name, source.key)
                assert current is not None
                current_path = Path(str(current["stored_path"]))

                second = await NhtsaBulkSyncService(repository, config).run(
                    run_key="bulk-race-second",
                    scope_name="recalls",
                    sources=(source,),
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
                )
                assert second["status"] == "failed"
                assert "failed 304 revalidation" in str(second["error"])
                with repository.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT published_run_id FROM nhtsa_current_artifacts "
                        "WHERE dataset_name = %s AND source_key = %s",
                        (source.dataset_name, source.key),
                    )
                    assert cursor.fetchone()["published_run_id"] == first["run_id"]
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_reason", ("parser", "missing_path", "tampered"))
def test_api_invalid_current_forces_200_and_repairs_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_reason: str,
) -> None:
    async def scenario() -> None:
        body = _api_body()
        conditionals: list[tuple[str | None, str | None]] = []

        async def handler(request: web.Request) -> web.Response:
            conditionals.append(
                (
                    request.headers.get("If-None-Match"),
                    request.headers.get("If-Modified-Since"),
                )
            )
            if request.headers.get("If-None-Match") == '"api-repair-v1"':
                return web.Response(status=304)
            return web.Response(
                body=body,
                headers={"ETag": '"api-repair-v1"'},
                content_type="application/json",
            )

        async with fake_site(handler) as base_url:
            source = ApiSource(
                "cssi_state_test",
                "cssi_stations",
                f"{base_url}/CSSIStation/state/IL?format=json",
            )
            monkeypatch.setattr(
                "partsouq_crawler.nhtsa.api_service.NhtsaApiClient",
                _LocalNhtsaApiClient,
            )
            monkeypatch.setattr(
                "partsouq_crawler.nhtsa.api_service.CSSI_SOURCES",
                (source,),
            )
            config = replace(_config(tmp_path), api_delay_seconds=0)
            repository = NhtsaMySQLRepository.create(config)
            try:
                repository.clear_for_tests()
                first = await NhtsaApiSyncService(repository, config).run(
                    run_key="api-repair-first",
                    scope_name="cssi",
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
                )
                assert first["status"] == "completed"
                current = repository.current_artifact(source.dataset_name, source.key)
                assert current is not None
                original_id = int(str(current["id"]))
                original_path = Path(str(current["stored_path"]))

                if invalid_reason == "parser":
                    with repository.transaction() as connection, connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE nhtsa_source_artifacts SET parser_version = 'old' WHERE id = %s",
                            (original_id,),
                        )
                elif invalid_reason == "missing_path":
                    with repository.transaction() as connection, connection.cursor() as cursor:
                        cursor.execute(
                            "UPDATE nhtsa_source_artifacts SET stored_path = %s WHERE id = %s",
                            (str(tmp_path / "missing.json"), original_id),
                        )
                else:
                    await asyncio.to_thread(original_path.write_bytes, b"x" * len(body))

                second = await NhtsaApiSyncService(repository, config).run(
                    run_key="api-repair-second",
                    scope_name="cssi",
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
                )
                assert second["status"] == "completed"
                assert conditionals == [(None, None), (None, None)]

                refreshed = repository.current_artifact(source.dataset_name, source.key)
                assert refreshed is not None
                refreshed_path = Path(str(refreshed["stored_path"]))
                assert refreshed["parser_version"] == API_PARSER_VERSION
                assert await asyncio.to_thread(refreshed_path.read_bytes) == body
                refreshed_stat = await asyncio.to_thread(refreshed_path.stat)
                assert refreshed_stat.st_size == int(str(refreshed["byte_count"]))
                assert hashlib.sha256(body).hexdigest() == refreshed["sha256"]
                if invalid_reason == "parser":
                    assert int(str(refreshed["id"])) != original_id
                else:
                    assert int(str(refreshed["id"])) == original_id
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


def test_api_304_revalidates_raw_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        body = _api_body()
        current_path: Path | None = None
        tamper_on_304 = False

        async def handler(request: web.Request) -> web.Response:
            if request.headers.get("If-None-Match") == '"api-race-v1"':
                assert current_path is not None
                if tamper_on_304:
                    await asyncio.to_thread(current_path.write_bytes, b"x" * len(body))
                return web.Response(status=304)
            return web.Response(
                body=body,
                headers={"ETag": '"api-race-v1"'},
                content_type="application/json",
            )

        async with fake_site(handler) as base_url:
            source = ApiSource(
                "cssi_state_test",
                "cssi_stations",
                f"{base_url}/CSSIStation/state/IL?format=json",
            )
            monkeypatch.setattr(
                "partsouq_crawler.nhtsa.api_service.NhtsaApiClient",
                _LocalNhtsaApiClient,
            )
            monkeypatch.setattr(
                "partsouq_crawler.nhtsa.api_service.CSSI_SOURCES",
                (source,),
            )
            config = replace(_config(tmp_path), api_delay_seconds=0)
            repository = NhtsaMySQLRepository.create(config)
            try:
                repository.clear_for_tests()
                first = await NhtsaApiSyncService(repository, config).run(
                    run_key="api-race-first",
                    scope_name="cssi",
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
                )
                assert first["status"] == "completed"
                current = repository.current_artifact(source.dataset_name, source.key)
                assert current is not None
                current_path = Path(str(current["stored_path"]))

                second = await NhtsaApiSyncService(repository, config).run(
                    run_key="api-race-second",
                    scope_name="cssi",
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
                )
                assert second["status"] == "completed"
                assert second["artifacts_reused"] == 1

                tamper_on_304 = True
                third = await NhtsaApiSyncService(repository, config).run(
                    run_key="api-race-third",
                    scope_name="cssi",
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
                )
                assert third["status"] == "failed"
                assert "failed 304 revalidation" in str(third["error"])
                with repository.connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT published_run_id FROM nhtsa_current_artifacts "
                        "WHERE dataset_name = %s AND source_key = %s",
                        (source.dataset_name, source.key),
                    )
                    assert cursor.fetchone()["published_run_id"] == second["run_id"]
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


def test_api_vpic_scope_expands_models_for_every_make(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    makes_body = json.dumps(
        {
            "Count": 3,
            "Message": "Results returned successfully",
            "Results": [
                {"Make_ID": 955, "Make_Name": "TESLA"},
                {"Make_ID": 460, "Make_Name": "BMW"},
                {"Make_ID": 240, "Make_Name": "TOYOTA"},
            ],
        },
        separators=(",", ":"),
    ).encode()
    empty_body = json.dumps(
        {"Count": 0, "Message": "Results returned successfully", "Results": []},
        separators=(",", ":"),
    ).encode()

    async def scenario() -> None:
        calls: list[ApiSource] = []

        class NoFetchNhtsaApiClient:
            def __init__(self, _config: NhtsaConfig) -> None:
                pass

            async def __aenter__(self) -> NoFetchNhtsaApiClient:
                return self

            async def __aexit__(self, *_args: object) -> None:
                pass

            async def fetch(
                self,
                _source: object,
                *,
                current_artifact: dict[str, object] | None,
            ) -> tuple[DownloadedArtifact, bytes]:
                del current_artifact
                raise AssertionError("dynamic expansion must go through _sync_source only")

        async def fake_sync_source(
            service: NhtsaApiSyncService,
            client: NoFetchNhtsaApiClient,
            source: ApiSource,
            lease: NhtsaRunLease,
        ) -> ApiSourceImport:
            del service, client
            calls.append(source)
            body = makes_body if source.dataset_name == "vpic_makes" else empty_body
            artifact_id, document, new_versions = _stage_api_document(
                repository,
                lease,
                tmp_path,
                source,
                body,
            )
            return ApiSourceImport(artifact_id, document, True, new_versions)

        monkeypatch.setattr(NhtsaApiSyncService, "_sync_source", fake_sync_source)
        monkeypatch.setattr(
            "partsouq_crawler.nhtsa.api_service.NhtsaApiClient",
            NoFetchNhtsaApiClient,
        )
        config = replace(_config(tmp_path), api_delay_seconds=0)
        repository = NhtsaMySQLRepository.create(config)
        try:
            repository.clear_for_tests()
            report = await NhtsaApiSyncService(repository, config).run(
                run_key="vpic-models-expansion",
                scope_name="vpic",
                scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            )

            assert report["status"] == "completed"
            assert [source.key for source in calls] == [
                "vpic_all_makes",
                "vpic_variables",
                "vpic_models_for_make_955",
                "vpic_models_for_make_460",
                "vpic_models_for_make_240",
                "vpic_manufacturers_page_001",
            ]
            assert [
                (source.key, source.url, dict(source.context))
                for source in calls
                if source.dataset_name == "vpic_models"
            ] == [
                (
                    "vpic_models_for_make_955",
                    "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/955?format=json",
                    {"Make_ID": "955", "Make_Name": "TESLA"},
                ),
                (
                    "vpic_models_for_make_460",
                    "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/460?format=json",
                    {"Make_ID": "460", "Make_Name": "BMW"},
                ),
                (
                    "vpic_models_for_make_240",
                    "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeId/240?format=json",
                    {"Make_ID": "240", "Make_Name": "TOYOTA"},
                ),
            ]
            assert report["published_sources"] == 5
            assert report["source_rows"] == 3
            with repository.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_key FROM nhtsa_current_artifacts
                    WHERE dataset_name = 'vpic_models' ORDER BY source_key
                    """
                )
                assert [row["source_key"] for row in cursor] == [
                    "vpic_models_for_make_240",
                    "vpic_models_for_make_460",
                    "vpic_models_for_make_955",
                ]
        finally:
            repository.clear_for_tests()
            repository.close()

    asyncio.run(scenario())


def test_api_request_budget_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _api_body()
    sha256 = hashlib.sha256(body).hexdigest()
    raw_path = tmp_path / f"{sha256}.json"
    raw_path.write_bytes(body)

    async def scenario() -> None:
        sources = tuple(
            ApiSource(
                f"cssi_state_{state}",
                "cssi_stations",
                f"https://api.nhtsa.gov/CSSIStation/state/{state}?format=json",
            )
            for state in ("IL", "NV", "CO")
        )

        class FetchNhtsaApiClient:
            def __init__(self, _config: NhtsaConfig) -> None:
                pass

            async def __aenter__(self) -> FetchNhtsaApiClient:
                return self

            async def __aexit__(self, *_args: object) -> None:
                pass

            async def fetch(
                self,
                _source: object,
                *,
                current_artifact: dict[str, object] | None,
            ) -> tuple[DownloadedArtifact, bytes]:
                del current_artifact
                return (
                    DownloadedArtifact(
                        http_status=200,
                        response_headers={
                            "content-type": "application/json",
                            "content-length": str(len(body)),
                        },
                        path=raw_path,
                        sha256=sha256,
                        byte_count=len(body),
                    ),
                    body,
                )

        monkeypatch.setattr(
            "partsouq_crawler.nhtsa.api_service.NhtsaApiClient",
            FetchNhtsaApiClient,
        )
        monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.CSSI_SOURCES", sources)
        monkeypatch.setattr("partsouq_crawler.nhtsa.api_service.API_REQUEST_BUDGET", 2)
        config = replace(_config(tmp_path), api_delay_seconds=0)
        repository = NhtsaMySQLRepository.create(config)
        try:
            repository.clear_for_tests()
            report = await NhtsaApiSyncService(repository, config).run(
                run_key="budget-fail-closed",
                scope_name="cssi",
                scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            )

            assert report["status"] == "failed"
            assert "request budget exceeded" in str(report["error"])
            assert report["api_requests"] == 3
            with repository.connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
                assert cursor.fetchone()["row_count"] == 0
                cursor.execute(
                    "SELECT status FROM nhtsa_sync_runs WHERE id = %s",
                    (report["run_id"],),
                )
                assert cursor.fetchone()["status"] == "failed"
        finally:
            repository.clear_for_tests()
            repository.close()

    asyncio.run(scenario())


def test_bulk_finalization_failure_rolls_back_current_domain_and_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def handler(_request: web.Request) -> web.Response:
            return web.Response(
                body=_zip("recalls.txt", [_row("atomic", "24V000001")]),
                content_type="application/zip",
            )

        async with fake_site(handler) as base_url:
            repository = NhtsaMySQLRepository.create(_config(tmp_path))
            try:
                repository.clear_for_tests()
                scheduled_job_run_id = _scheduled_job(repository, "nhtsa-bulk")
                _fail_completed_finalization(repository, monkeypatch)
                report = await NhtsaBulkSyncService(repository, _config(tmp_path)).run(
                    run_key="bulk-atomic-rollback",
                    scope_name="recalls",
                    sources=(
                        BulkSource(
                            "atomic_recalls",
                            "recalls",
                            f"{base_url}/recalls.zip",
                            "recalls.txt",
                        ),
                    ),
                    scheduled_job_run_id=scheduled_job_run_id,
                )

                assert report["status"] == "failed"
                assert report["error"] == "simulated run finalization failure"
                with repository.connection.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
                    assert cursor.fetchone()["row_count"] == 0
                    cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_records")
                    assert cursor.fetchone()["row_count"] == 0
                    cursor.execute(
                        "SELECT status FROM nhtsa_sync_runs WHERE id = %s",
                        (report["run_id"],),
                    )
                    assert cursor.fetchone()["status"] == "failed"
                    cursor.execute(
                        "SELECT status, exit_code FROM scheduled_job_runs WHERE id = %s",
                        (scheduled_job_run_id,),
                    )
                    scheduled_job = cursor.fetchone()
                    assert scheduled_job == {"status": "failed", "exit_code": 1}
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


def test_rejected_source_is_quarantined_and_not_published(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(_request: web.Request) -> web.Response:
            return web.Response(body=_zip("bad.txt", [_row("3", "30V000003")[:-1]]))

        async with fake_site(handler) as base_url:
            source = BulkSource("test_bad", "recalls", f"{base_url}/bad.zip", "bad.txt")
            repository = NhtsaMySQLRepository.create(_config(tmp_path))
            try:
                repository.clear_for_tests()
                report = await NhtsaBulkSyncService(repository, _config(tmp_path)).run(
                    run_key="bad-fixture",
                    scope_name="recalls",
                    sources=(source,),
                    scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
                )
                assert report["status"] == "failed"
                assert report["rejected_rows"] == 1
                status = repository.status_report()
                assert status["current_record_counts"] == {}
                assert status["artifact_status_counts"] == {"quarantined": 1}
                assert status["rejected_rows"] == 1
            finally:
                repository.clear_for_tests()
                repository.close()

    asyncio.run(scenario())


def test_duplicate_and_updated_source_rows_keep_every_lineage_entry(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        payload_json = json.dumps({"Organization": "Duplicate station"})
        record_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        key_hash = hashlib.sha256(b"duplicate-station").hexdigest()
        first = ParsedRecord(
            dataset_name="cssi_stations",
            natural_key_sha256=key_hash,
            record_sha256=record_hash,
            natural_key_text="duplicate-station",
            external_id=None,
            make_name=None,
            model_name=None,
            model_year=None,
            campaign_number=None,
            component_name=None,
            summary_text="Duplicate station",
            payload_json=payload_json,
            member_name="response.json",
            source_line=72,
        )
        raw_path = tmp_path / "duplicate.json"
        raw_path.write_text(payload_json)
        lease = repository.start_run(
            "duplicate-fixture",
            "api-cssi",
            ("cssi",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        artifact_id = repository.create_artifact(
            lease,
            dataset_name="cssi_stations",
            source_key="cssi_state_test",
            source_url="https://api.nhtsa.gov/CSSIStation/state/IL?format=json",
            download=DownloadedArtifact(
                http_status=200,
                response_headers={"content-type": "application/json"},
                path=raw_path,
                sha256=record_hash,
                byte_count=raw_path.stat().st_size,
            ),
            parser_name="test",
            parser_version="1",
        )
        repository.store_member(
            lease,
            artifact_id,
            ArtifactMember(
                "response.json",
                raw_path.stat().st_size,
                raw_path.stat().st_size,
                None,
                ("Organization",),
                record_hash,
            ),
        )

        updated_payload = json.dumps(
            {"Organization": "Duplicate station", "LastUpdatedDate": "2025-01-01"}
        )
        assert (
            repository.insert_records(
                lease,
                artifact_id,
                [
                    first,
                    replace(first, source_line=73),
                    replace(
                        first,
                        record_sha256=hashlib.sha256(updated_payload.encode()).hexdigest(),
                        payload_json=updated_payload,
                        source_line=74,
                    ),
                ],
            )
            == 2
        )
        repository.complete_artifact(
            lease,
            artifact_id,
            source_rows=3,
            new_versions=2,
            rejected_rows=0,
        )
        repository.complete_run_and_publish_artifacts(
            lease,
            [("cssi_stations", "cssi_state_test", artifact_id)],
            replace_datasets=("cssi_stations",),
            downloaded=1,
            reused=0,
            source_rows=3,
            new_versions=2,
            rejected_rows=0,
        )

        with repository.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_line FROM nhtsa_artifact_records
                WHERE artifact_id = %s ORDER BY source_line
                """,
                (artifact_id,),
            )
            assert [row["source_line"] for row in cursor] == [72, 73, 74]
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_records")
            assert cursor.fetchone()["row_count"] == 3
    finally:
        repository.clear_for_tests()
        repository.close()


@pytest.mark.parametrize(
    ("published_dataset", "published_source_key", "expected_error"),
    (
        ("wrong_dataset", "cssi_state_test", "outside the lease dataset scope"),
        (
            "cssi_stations",
            "wrong_source_key",
            "all NHTSA artifacts must be imported without rejections",
        ),
    ),
)
def test_bulk_publish_rejects_artifact_identity_mismatch(
    tmp_path: Path,
    published_dataset: str,
    published_source_key: str,
    expected_error: str,
) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        scheduled_job_run_id = _scheduled_job(repository, "nhtsa-api")
        lease = repository.start_run(
            "artifact-identity-mismatch",
            "api-cssi",
            ("cssi",),
            scheduled_job_run_id=scheduled_job_run_id,
            expected_job_name="nhtsa-api",
        )
        body = b'{"Organization":"identity test"}'
        raw_path = tmp_path / "identity.json"
        raw_path.write_bytes(body)
        artifact_id = repository.create_artifact(
            lease,
            dataset_name="cssi_stations",
            source_key="cssi_state_test",
            source_url="https://api.nhtsa.gov/CSSIStation/state/IL?format=json",
            download=DownloadedArtifact(
                http_status=200,
                response_headers={"content-type": "application/json"},
                path=raw_path,
                sha256=hashlib.sha256(body).hexdigest(),
                byte_count=len(body),
            ),
            parser_name="test",
            parser_version="1",
        )
        repository.store_member(
            lease,
            artifact_id,
            ArtifactMember(
                "response.json",
                len(body),
                len(body),
                None,
                ("Organization",),
                hashlib.sha256(body).hexdigest(),
            ),
        )
        repository.complete_artifact(
            lease,
            artifact_id,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )

        with pytest.raises(
            ValueError,
            match=expected_error,
        ):
            repository.complete_run_and_publish_artifacts(
                lease,
                [(published_dataset, published_source_key, artifact_id)],
                replace_datasets=("cssi_stations",),
                downloaded=1,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, lease_slot FROM nhtsa_sync_runs WHERE id = %s",
                (lease.id,),
            )
            domain = cursor.fetchone()
            assert domain["status"] == "running"
            assert domain["lease_slot"] == "writer"
            cursor.execute(
                "SELECT status, finished_at FROM scheduled_job_runs WHERE id = %s",
                (scheduled_job_run_id,),
            )
            child = cursor.fetchone()
            assert child["status"] == "running"
            assert child["finished_at"] is None
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0
    finally:
        repository.clear_for_tests()
        repository.close()


@pytest.mark.parametrize("attempt", ("artifact", "replacement"))
def test_api_cssi_publish_cannot_cross_into_recalls_scope(
    tmp_path: Path,
    attempt: str,
) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            f"api-cssi-cross-scope-{attempt}",
            "api-cssi",
            ("cssi",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        artifacts: list[tuple[str, str, int]] = []
        replacements: tuple[str, ...] = ("cssi_stations",)
        expected_error = "outside the lease dataset scope"
        if attempt == "artifact":
            artifact_id = _stage_empty_artifact(
                repository,
                lease,
                tmp_path,
                dataset_name="recalls",
                source_key="recalls-outside-cssi",
            )
            artifacts.append(("recalls", "recalls-outside-cssi", artifact_id))
        else:
            replacements = ("recalls",)
            expected_error = "must exactly match the lease scope"

        with pytest.raises(ValueError, match=expected_error):
            repository.complete_run_and_publish_artifacts(
                lease,
                artifacts,
                replace_datasets=replacements,
                downloaded=1,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0
            cursor.execute("SELECT status FROM nhtsa_sync_runs WHERE id = %s", (lease.id,))
            assert cursor.fetchone()["status"] == "running"
    finally:
        repository.clear_for_tests()
        repository.close()


def test_bulk_publish_rejects_source_key_outside_lease(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "bulk-source-key-boundary",
            "recalls",
            ("leased-recalls",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
            expected_job_name="nhtsa-bulk",
        )
        artifact_id = _stage_empty_artifact(
            repository,
            lease,
            tmp_path,
            dataset_name="recalls",
            source_key="foreign-recalls",
        )

        with pytest.raises(ValueError, match="do not match the lease source keys"):
            repository.complete_run_and_publish_artifacts(
                lease,
                [("recalls", "foreign-recalls", artifact_id)],
                downloaded=1,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
            )

        assert repository.current_artifact("recalls", "foreign-recalls") is None
    finally:
        repository.clear_for_tests()
        repository.close()


def test_api_all_empty_snapshot_clears_only_its_own_datasets(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    api_datasets = (
        "vpic_makes",
        "vpic_models",
        "vpic_manufacturers",
        "vpic_variables",
        "vpic_variable_values",
        "cssi_stations",
    )
    try:
        repository.clear_for_tests()
        bulk_lease = repository.start_run(
            "unrelated-bulk-pointer",
            "recalls",
            ("unrelated-recalls",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-bulk"),
            expected_job_name="nhtsa-bulk",
        )
        unrelated_id = _stage_empty_artifact(
            repository,
            bulk_lease,
            tmp_path,
            dataset_name="recalls",
            source_key="unrelated-recalls",
        )
        repository.complete_run_and_publish_artifacts(
            bulk_lease,
            [("recalls", "unrelated-recalls", unrelated_id)],
            downloaded=1,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )

        first_api_lease = repository.start_run(
            "api-all-first-snapshot",
            "api-all",
            ("vpic", "cssi"),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        vpic_id = _stage_empty_artifact(
            repository,
            first_api_lease,
            tmp_path,
            dataset_name="vpic_makes",
            source_key="vpic-first",
        )
        cssi_id = _stage_empty_artifact(
            repository,
            first_api_lease,
            tmp_path,
            dataset_name="cssi_stations",
            source_key="cssi-first",
        )
        repository.complete_run_and_publish_artifacts(
            first_api_lease,
            [
                ("vpic_makes", "vpic-first", vpic_id),
                ("cssi_stations", "cssi-first", cssi_id),
            ],
            replace_datasets=api_datasets,
            downloaded=2,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )

        empty_api_lease = repository.start_run(
            "api-all-empty-snapshot",
            "api-all",
            ("vpic", "cssi"),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        repository.complete_run_and_publish_artifacts(
            empty_api_lease,
            [],
            replace_datasets=api_datasets,
            downloaded=0,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )

        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT dataset_name, source_key, artifact_id "
                "FROM nhtsa_current_artifacts ORDER BY dataset_name, source_key"
            )
            assert cursor.fetchall() == [
                {
                    "dataset_name": "recalls",
                    "source_key": "unrelated-recalls",
                    "artifact_id": unrelated_id,
                }
            ]
    finally:
        repository.clear_for_tests()
        repository.close()


def test_api_partial_replacement_scope_rolls_back_and_keeps_previous_pointer(
    tmp_path: Path,
) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    vpic_datasets = (
        "vpic_makes",
        "vpic_models",
        "vpic_manufacturers",
        "vpic_variables",
        "vpic_variable_values",
    )
    try:
        repository.clear_for_tests()
        first_lease = repository.start_run(
            "vpic-full-replacement",
            "api-vpic",
            ("vpic",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        first_artifact_id = _stage_empty_artifact(
            repository,
            first_lease,
            tmp_path,
            dataset_name="vpic_makes",
            source_key="vpic-makes-old",
            content_marker="old",
        )
        repository.complete_run_and_publish_artifacts(
            first_lease,
            [("vpic_makes", "vpic-makes-old", first_artifact_id)],
            replace_datasets=vpic_datasets,
            downloaded=1,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )

        partial_lease = repository.start_run(
            "vpic-partial-replacement",
            "api-vpic",
            ("vpic",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        replacement_id = _stage_empty_artifact(
            repository,
            partial_lease,
            tmp_path,
            dataset_name="vpic_makes",
            source_key="vpic-makes-new",
            content_marker="new",
        )
        with pytest.raises(ValueError, match="must exactly match the lease scope"):
            repository.complete_run_and_publish_artifacts(
                partial_lease,
                [("vpic_makes", "vpic-makes-new", replacement_id)],
                replace_datasets=("vpic_makes",),
                downloaded=1,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
            )

        current = repository.current_artifact("vpic_makes", "vpic-makes-old")
        assert current is not None
        assert int(str(current["id"])) == first_artifact_id
        assert repository.current_artifact("vpic_makes", "vpic-makes-new") is None
        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM nhtsa_sync_runs WHERE id = %s",
                (partial_lease.id,),
            )
            assert cursor.fetchone()["status"] == "running"
    finally:
        repository.clear_for_tests()
        repository.close()


@pytest.mark.parametrize("invalid_state", ("verified_at", "imported_at", "counter"))
def test_publish_rejects_incomplete_artifact_or_counter_drift(
    tmp_path: Path,
    invalid_state: str,
) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            f"publish-integrity-{invalid_state}",
            "api-cssi",
            ("cssi",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        artifact_id = _stage_empty_artifact(
            repository,
            lease,
            tmp_path,
            dataset_name="cssi_stations",
            source_key=f"cssi-integrity-{invalid_state}",
        )
        source_rows = 1 if invalid_state == "counter" else 0
        if invalid_state != "counter":
            with repository.transaction() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE nhtsa_source_artifacts SET {invalid_state} = NULL WHERE id = %s",
                    (artifact_id,),
                )

        with pytest.raises(
            ValueError,
            match=(
                "run counters do not match"
                if invalid_state == "counter"
                else "must be imported without rejections"
            ),
        ):
            repository.complete_run_and_publish_artifacts(
                lease,
                [("cssi_stations", f"cssi-integrity-{invalid_state}", artifact_id)],
                replace_datasets=("cssi_stations",),
                downloaded=1,
                reused=0,
                source_rows=source_rows,
                new_versions=0,
                rejected_rows=0,
            )

        assert (
            repository.current_artifact("cssi_stations", f"cssi-integrity-{invalid_state}") is None
        )
    finally:
        repository.clear_for_tests()
        repository.close()


def test_current_artifact_read_sees_publication_from_another_connection(tmp_path: Path) -> None:
    reader = NhtsaMySQLRepository.create(_config(tmp_path))
    writer = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        writer.clear_for_tests()
        first_lease = writer.start_run(
            "read-snapshot-first",
            "api-cssi",
            ("cssi",),
            scheduled_job_run_id=_scheduled_job(writer, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        first_artifact_id = _stage_empty_artifact(
            writer,
            first_lease,
            tmp_path,
            dataset_name="cssi_stations",
            source_key="cssi-read-snapshot",
            content_marker="first",
        )
        writer.complete_run_and_publish_artifacts(
            first_lease,
            [("cssi_stations", "cssi-read-snapshot", first_artifact_id)],
            replace_datasets=("cssi_stations",),
            downloaded=1,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )
        first_read = reader.current_artifact("cssi_stations", "cssi-read-snapshot")
        assert first_read is not None
        assert int(str(first_read["id"])) == first_artifact_id

        second_lease = writer.start_run(
            "read-snapshot-second",
            "api-cssi",
            ("cssi",),
            scheduled_job_run_id=_scheduled_job(writer, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        second_artifact_id = _stage_empty_artifact(
            writer,
            second_lease,
            tmp_path,
            dataset_name="cssi_stations",
            source_key="cssi-read-snapshot",
            content_marker="second",
        )
        writer.complete_run_and_publish_artifacts(
            second_lease,
            [("cssi_stations", "cssi-read-snapshot", second_artifact_id)],
            replace_datasets=("cssi_stations",),
            downloaded=1,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )

        refreshed = reader.current_artifact("cssi_stations", "cssi-read-snapshot")
        assert refreshed is not None
        assert int(str(refreshed["id"])) == second_artifact_id
        assert second_artifact_id != first_artifact_id
    finally:
        reader.close()
        writer.clear_for_tests()
        writer.close()


@pytest.mark.parametrize("same_scheduler_child", (False, True))
def test_two_connections_concurrently_claim_exactly_one_writer(
    tmp_path: Path,
    same_scheduler_child: bool,
) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        first_job_id = _scheduled_job(repository, "nhtsa-bulk")
        jobs = (
            (first_job_id, "nhtsa-bulk", "first-writer"),
            (
                first_job_id if same_scheduler_child else _scheduled_job(repository, "nhtsa-api"),
                "nhtsa-bulk" if same_scheduler_child else "nhtsa-api",
                "second-writer",
            ),
        )
        barrier = Barrier(2)

        def claim(
            scheduled_job_run_id: int,
            job_name: str,
            run_key: str,
        ) -> NhtsaRunLease | Exception:
            # scope 必須與 child job 對應（api- 前綴 → nhtsa-api，其餘 →
            # nhtsa-bulk）；writer 槽是表級唯一，跨 scope 仍會互斥。
            scope = "api-concurrent" if job_name == "nhtsa-api" else "bulk-concurrent"
            contender = NhtsaMySQLRepository.create(_config(tmp_path))
            try:
                barrier.wait()
                for attempt in range(10):
                    try:
                        return contender.start_run(
                            run_key,
                            scope,
                            (run_key,),
                            scheduled_job_run_id=scheduled_job_run_id,
                            expected_job_name=job_name,
                        )
                    except AdmissionLockBusy:
                        if attempt == 9:
                            raise
                        time.sleep(0.01)
                raise AssertionError("unreachable")
            except Exception as error:
                return error
            finally:
                contender.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda values: claim(*values), jobs))
        leases = [result for result in results if isinstance(result, NhtsaRunLease)]
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(leases) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], NhtsaLeaseLostError)
        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM nhtsa_sync_runs WHERE status='running'"
            )
            assert cursor.fetchone()["row_count"] == 1
            cursor.execute(
                "SELECT COUNT(*) AS row_count FROM nhtsa_sync_runs WHERE lease_slot='writer'"
            )
            assert cursor.fetchone()["row_count"] == 1
        repository.finish_run(
            leases[0],
            status="interrupted",
            downloaded=0,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
            error_message="test cleanup",
        )
    finally:
        repository.clear_for_tests()
        repository.close()


def test_expired_takeover_revokes_old_token_and_preserves_new_owner(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        old_job_id = _scheduled_job(repository, "nhtsa-bulk")
        old_lease = repository.start_run(
            "expired-owner",
            "recalls",
            ("old",),
            scheduled_job_run_id=old_job_id,
            expected_job_name="nhtsa-bulk",
        )
        with repository.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhtsa_sync_runs
                SET heartbeat_at = TIMESTAMPADD(SECOND, -2, UTC_TIMESTAMP(6)),
                    lease_expires_at = TIMESTAMPADD(SECOND, -1, UTC_TIMESTAMP(6))
                WHERE id = %s
                """,
                (old_lease.id,),
            )
        new_job_id = _scheduled_job(repository, "nhtsa-api")
        new_lease = repository.start_run(
            "takeover-owner",
            "api-vpic",
            ("new",),
            scheduled_job_run_id=new_job_id,
            expected_job_name="nhtsa-api",
        )

        with pytest.raises(NhtsaLeaseLostError, match="lease was lost"):
            repository.heartbeat(old_lease)
        with pytest.raises(NhtsaLeaseLostError, match="lease was lost"):
            repository.finish_run(
                old_lease,
                status="failed",
                downloaded=0,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
                error_message="stale owner",
            )
        with pytest.raises(NhtsaLeaseLostError, match="lease was lost"):
            repository.complete_run_and_publish_artifacts(
                old_lease,
                [("recalls", "old", 0)],
                downloaded=0,
                reused=1,
                source_rows=1,
                new_versions=0,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, lease_slot, lease_token, lease_expires_at, error_message
                FROM nhtsa_sync_runs WHERE id = %s
                """,
                (old_lease.id,),
            )
            old_run = cursor.fetchone()
            assert old_run["status"] == "interrupted"
            assert old_run["lease_slot"] is None
            assert old_run["lease_token"] is None
            assert old_run["lease_expires_at"] is None
            assert old_run["error_message"] == "expired NHTSA lease recovered"
            cursor.execute(
                "SELECT status, lease_token FROM nhtsa_sync_runs WHERE id = %s",
                (new_lease.id,),
            )
            assert cursor.fetchone() == {"status": "running", "lease_token": new_lease.token}
            cursor.execute(
                "SELECT status, exit_code FROM scheduled_job_runs WHERE id = %s",
                (old_job_id,),
            )
            assert cursor.fetchone() == {"status": "failed", "exit_code": 125}
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0

        repository.finish_run(
            new_lease,
            status="interrupted",
            downloaded=0,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
            error_message="test cleanup",
        )
    finally:
        repository.clear_for_tests()
        repository.close()


def test_expired_owner_cannot_mutate_same_content_artifact(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        old_lease = repository.start_run(
            "stale-artifact-owner",
            "api-cssi",
            ("cssi_state_test",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        body = b'{"Organization":"stale owner"}'
        raw_path = tmp_path / "stale-owner.json"
        raw_path.write_bytes(body)
        download = DownloadedArtifact(
            http_status=200,
            response_headers={"content-type": "application/json"},
            path=raw_path,
            sha256=hashlib.sha256(body).hexdigest(),
            byte_count=len(body),
        )
        artifact_id = repository.create_artifact(
            old_lease,
            dataset_name="cssi_stations",
            source_key="cssi_state_test",
            source_url="https://api.nhtsa.gov/CSSIStation/state/IL?format=json",
            download=download,
            parser_name="test",
            parser_version="1",
        )
        with repository.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhtsa_sync_runs
                SET heartbeat_at = TIMESTAMPADD(SECOND, -2, UTC_TIMESTAMP(6)),
                    lease_expires_at = TIMESTAMPADD(SECOND, -1, UTC_TIMESTAMP(6))
                WHERE id = %s
                """,
                (old_lease.id,),
            )
        new_lease = repository.start_run(
            "new-artifact-owner",
            "api-cssi",
            ("cssi_state_test",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        assert (
            repository.create_artifact(
                new_lease,
                dataset_name="cssi_stations",
                source_key="cssi_state_test",
                source_url="https://api.nhtsa.gov/CSSIStation/state/IL?format=json",
                download=download,
                parser_name="test",
                parser_version="1",
            )
            == artifact_id
        )
        member = ArtifactMember(
            name="response.json",
            uncompressed_bytes=len(body),
            compressed_bytes=len(body),
            crc32=None,
            field_names=("Organization",),
            schema_sha256="a" * 64,
        )
        record = ParsedRecord(
            dataset_name="cssi_stations",
            natural_key_sha256="b" * 64,
            record_sha256="c" * 64,
            natural_key_text="stale-owner",
            external_id=None,
            make_name=None,
            model_name=None,
            model_year=None,
            campaign_number=None,
            component_name=None,
            summary_text="stale owner",
            payload_json=body.decode(),
            member_name="response.json",
            source_line=1,
        )
        rejection = RejectedRow(
            member_name="response.json",
            source_line=1,
            raw_sha256="d" * 64,
            error_type="StaleOwner",
            error_message="must not persist",
            raw_text=body.decode(),
        )
        stale_mutations = (
            lambda: repository.create_artifact(
                old_lease,
                dataset_name="cssi_stations",
                source_key="cssi_state_test",
                source_url="https://api.nhtsa.gov/CSSIStation/state/IL?format=json",
                download=download,
                parser_name="test",
                parser_version="1",
            ),
            lambda: repository.store_member(old_lease, artifact_id, member),
            lambda: repository.reset_artifact_import(old_lease, artifact_id),
            lambda: repository.insert_records(old_lease, artifact_id, (record,)),
            lambda: repository.insert_rejections(old_lease, artifact_id, (rejection,)),
            lambda: repository.complete_artifact(
                old_lease,
                artifact_id,
                source_rows=1,
                new_versions=1,
                rejected_rows=0,
            ),
            lambda: repository.quarantine_artifact(
                old_lease,
                artifact_id,
                "stale owner must not persist",
            ),
        )
        for mutate in stale_mutations:
            with pytest.raises(NhtsaLeaseLostError, match="lease was lost"):
                mutate()

        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, error_message FROM nhtsa_source_artifacts WHERE id = %s",
                (artifact_id,),
            )
            assert cursor.fetchone() == {"status": "downloaded", "error_message": None}
            for table in (
                "nhtsa_artifact_members",
                "nhtsa_artifact_records",
                "nhtsa_rejected_rows",
                "nhtsa_record_versions",
            ):
                cursor.execute(f"SELECT COUNT(*) AS row_count FROM {table}")
                assert cursor.fetchone()["row_count"] == 0

        repository.finish_run(
            new_lease,
            status="interrupted",
            downloaded=0,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
            error_message="test cleanup",
        )
    finally:
        repository.clear_for_tests()
        repository.close()


@pytest.mark.parametrize("operation", ("heartbeat", "finish"))
def test_lease_expiry_is_checked_after_waiting_for_run_lock(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    blocker = NhtsaMySQLRepository.create(_config(tmp_path))
    contender = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "lock-wait-owner",
            "api-vpic",
            ("vpic",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        with repository.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhtsa_sync_runs
                SET lease_expires_at = TIMESTAMPADD(MICROSECOND, 250000, UTC_TIMESTAMP(6))
                WHERE id = %s
                """,
                (lease.id,),
            )

        blocker.connection.begin()
        blocker_cursor = blocker.connection.cursor()
        blocker_cursor.execute(
            "SELECT id FROM nhtsa_sync_runs WHERE id = %s FOR UPDATE", (lease.id,)
        )
        started = Event()

        def finish_after_lock() -> Exception | None:
            started.set()
            try:
                if operation == "heartbeat":
                    contender.heartbeat(lease)
                else:
                    contender.finish_run(
                        lease,
                        status="interrupted",
                        downloaded=0,
                        reused=0,
                        source_rows=0,
                        new_versions=0,
                        rejected_rows=0,
                    )
            except Exception as error:
                return error
            return None

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(finish_after_lock)
            assert started.wait(1)
            time.sleep(0.35)
            blocker.connection.commit()
            error = future.result(timeout=2)
        blocker_cursor.close()

        assert isinstance(error, NhtsaLeaseLostError)
        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM nhtsa_sync_runs WHERE id = %s",
                (lease.id,),
            )
            assert cursor.fetchone()["status"] == "running"
            cursor.execute(
                "SELECT status FROM scheduled_job_runs WHERE id = %s",
                (lease.scheduled_job_run_id,),
            )
            assert cursor.fetchone()["status"] == "running"

        takeover = repository.start_run(
            "lock-wait-takeover",
            "api-vpic",
            ("vpic",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        repository.finish_run(
            takeover,
            status="interrupted",
            downloaded=0,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
            error_message="test cleanup",
        )
    finally:
        blocker.connection.rollback()
        contender.close()
        blocker.close()
        repository.clear_for_tests()
        repository.close()


@pytest.mark.parametrize("fail_finalization", (False, True))
def test_finalization_lock_survives_expiry_and_still_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_finalization: bool,
) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "slow-finalization",
            "api-cssi",
            ("cssi",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        body = b'{"Organization":"slow finalization"}'
        raw_path = tmp_path / "slow-finalization.json"
        raw_path.write_bytes(body)
        artifact_id = repository.create_artifact(
            lease,
            dataset_name="cssi_stations",
            source_key="cssi_state_test",
            source_url="https://api.nhtsa.gov/CSSIStation/state/IL?format=json",
            download=DownloadedArtifact(
                http_status=200,
                response_headers={"content-type": "application/json"},
                path=raw_path,
                sha256=hashlib.sha256(body).hexdigest(),
                byte_count=len(body),
            ),
            parser_name="test",
            parser_version="1",
        )
        repository.store_member(
            lease,
            artifact_id,
            ArtifactMember(
                "response.json",
                len(body),
                len(body),
                None,
                ("Organization",),
                hashlib.sha256(body).hexdigest(),
            ),
        )
        repository.complete_artifact(
            lease,
            artifact_id,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
        )
        with repository.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhtsa_sync_runs
                SET lease_expires_at = TIMESTAMPADD(MICROSECOND, 250000, UTC_TIMESTAMP(6))
                WHERE id = %s
                """,
                (lease.id,),
            )

        finish_run = repository._finish_run

        def delayed_finish(*args: object, **kwargs: object) -> None:
            time.sleep(0.35)
            finish_run(*args, **kwargs)  # type: ignore[arg-type]
            if fail_finalization:
                raise RuntimeError("simulated slow finalization failure")

        monkeypatch.setattr(repository, "_finish_run", delayed_finish)
        if fail_finalization:
            with pytest.raises(RuntimeError, match="slow finalization failure"):
                repository.complete_run_and_publish_artifacts(
                    lease,
                    [("cssi_stations", "cssi_state_test", artifact_id)],
                    replace_datasets=("cssi_stations",),
                    downloaded=1,
                    reused=0,
                    source_rows=0,
                    new_versions=0,
                    rejected_rows=0,
                )
        else:
            repository.complete_run_and_publish_artifacts(
                lease,
                [("cssi_stations", "cssi_state_test", artifact_id)],
                replace_datasets=("cssi_stations",),
                downloaded=1,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, ended_at, lease_slot, lease_token, lease_expires_at "
                "FROM nhtsa_sync_runs WHERE id = %s",
                (lease.id,),
            )
            domain_run = cursor.fetchone()
            cursor.execute(
                "SELECT status, finished_at, exit_code FROM scheduled_job_runs WHERE id = %s",
                (lease.scheduled_job_run_id,),
            )
            scheduled_run = cursor.fetchone()
            if fail_finalization:
                assert domain_run["status"] == "running"
                assert domain_run["ended_at"] is None
                assert domain_run["lease_slot"] == "writer"
                assert domain_run["lease_token"] == lease.token
                assert domain_run["lease_expires_at"] is not None
                assert scheduled_run == {
                    "status": "running",
                    "finished_at": None,
                    "exit_code": None,
                }
            else:
                assert domain_run["status"] == "completed"
                assert domain_run["ended_at"] is not None
                assert domain_run["lease_slot"] is None
                assert domain_run["lease_token"] is None
                assert domain_run["lease_expires_at"] is None
                assert scheduled_run["status"] == "completed"
                assert scheduled_run["finished_at"] is not None
                assert scheduled_run["exit_code"] == 0
            cursor.execute(
                """
                SELECT artifact_id, published_run_id FROM nhtsa_current_artifacts
                WHERE dataset_name = 'cssi_stations' AND source_key = 'cssi_state_test'
                """
            )
            current = cursor.fetchone()
            if fail_finalization:
                assert current is None
            else:
                assert current == {"artifact_id": artifact_id, "published_run_id": lease.id}

        if fail_finalization:
            monkeypatch.setattr(repository, "_finish_run", finish_run)
            takeover = repository.start_run(
                "slow-finalization-takeover",
                "api-cssi",
                ("cssi_state_test",),
                scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
                expected_job_name="nhtsa-api",
            )
            repository.finish_run(
                takeover,
                status="interrupted",
                downloaded=0,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
                error_message="test cleanup",
            )
    finally:
        repository.clear_for_tests()
        repository.close()


def test_wrong_lease_token_cannot_heartbeat_or_finish(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "token-owner",
            "api-vpic",
            ("vpic",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )
        forged = NhtsaRunLease(lease.id, "f" * 64, lease.scheduled_job_run_id)

        with pytest.raises(NhtsaLeaseLostError, match="lease was lost"):
            repository.heartbeat(forged)
        with pytest.raises(NhtsaLeaseLostError, match="lease was lost"):
            repository.finish_run(
                forged,
                status="failed",
                downloaded=0,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
                error_message="forged",
            )

        repository.finish_run(
            lease,
            status="interrupted",
            downloaded=0,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
            error_message="test cleanup",
        )
    finally:
        repository.clear_for_tests()
        repository.close()


def test_public_finish_cannot_bypass_atomic_publication(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "completed-bypass",
            "api-vpic",
            ("vpic",),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-api"),
            expected_job_name="nhtsa-api",
        )

        with pytest.raises(ValueError, match="unsupported NHTSA terminal status: completed"):
            repository.finish_run(
                lease,
                status="completed",
                downloaded=0,
                reused=0,
                source_rows=0,
                new_versions=0,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM nhtsa_sync_runs WHERE id = %s",
                (lease.id,),
            )
            assert cursor.fetchone()["status"] == "running"
            cursor.execute(
                "SELECT status FROM scheduled_job_runs WHERE id = %s",
                (lease.scheduled_job_run_id,),
            )
            assert cursor.fetchone()["status"] == "running"
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0

        repository.finish_run(
            lease,
            status="interrupted",
            downloaded=0,
            reused=0,
            source_rows=0,
            new_versions=0,
            rejected_rows=0,
            error_message="test cleanup",
        )
    finally:
        repository.clear_for_tests()
        repository.close()


def test_vin_publish_rejects_artifact_for_a_different_vin(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "vin-a-artifact-vin-b-payload",
            "api-vin",
            (vin_source_key(VIN),),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-vin"),
            expected_job_name="nhtsa-vin",
        )
        artifact_id = _stage_vin_artifact(repository, lease, tmp_path, _vin_payload())

        with pytest.raises(ValueError, match="lease scope does not match"):
            repository.complete_run_and_publish_vin_decode(
                lease,
                artifact_id,
                OTHER_VIN,
                _vin_payload(VIN=OTHER_VIN),
                downloaded=1,
                reused=0,
                source_rows=1,
                new_versions=1,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_vin_decodes")
            assert cursor.fetchone()["row_count"] == 0
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0
            cursor.execute("SELECT status FROM nhtsa_sync_runs WHERE id = %s", (lease.id,))
            assert cursor.fetchone()["status"] == "running"
    finally:
        repository.clear_for_tests()
        repository.close()


def test_vin_publish_rejects_payload_changed_after_artifact_import(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "vin-payload-binding",
            "api-vin",
            (vin_source_key(VIN),),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-vin"),
            expected_job_name="nhtsa-vin",
        )
        artifact_id = _stage_vin_artifact(repository, lease, tmp_path, _vin_payload())

        with pytest.raises(ValueError, match="artifact record does not match"):
            repository.complete_run_and_publish_vin_decode(
                lease,
                artifact_id,
                VIN,
                _vin_payload(Make="ALTERED MAKE"),
                downloaded=1,
                reused=0,
                source_rows=1,
                new_versions=1,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_vin_decodes")
            assert cursor.fetchone()["row_count"] == 0
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0
    finally:
        repository.clear_for_tests()
        repository.close()


def test_vin_publish_rejects_nonformal_source_key(tmp_path: Path) -> None:
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    legacy_source_key = f"vpic_vin_{VIN}"
    try:
        repository.clear_for_tests()
        lease = repository.start_run(
            "vin-nonformal-source-key",
            "api-vin",
            (legacy_source_key,),
            scheduled_job_run_id=_scheduled_job(repository, "nhtsa-vin"),
            expected_job_name="nhtsa-vin",
        )
        artifact_id = _stage_vin_artifact(
            repository,
            lease,
            tmp_path,
            _vin_payload(),
            source_key=legacy_source_key,
        )

        with pytest.raises(ValueError, match="lease scope does not match"):
            repository.complete_run_and_publish_vin_decode(
                lease,
                artifact_id,
                VIN,
                _vin_payload(),
                downloaded=1,
                reused=0,
                source_rows=1,
                new_versions=1,
                rejected_rows=0,
            )

        with repository.connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_vin_decodes")
            assert cursor.fetchone()["row_count"] == 0
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0
    finally:
        repository.clear_for_tests()
        repository.close()


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    (
        ({"ErrorCode": "1", "ErrorText": "Invalid VIN"}, "ErrorCode=1"),
        ({"VIN": OTHER_VIN}, "does not match the requested VIN"),
        ({"Make": ""}, "missing required fields: Make"),
        ({"EngineConfiguration": ""}, "missing required fields: EngineConfiguration"),
        ({"DisplacementL": ""}, "missing required fields: DisplacementL"),
        ({"Trim": ""}, "missing required fields: Trim"),
    ),
)
def test_invalid_vin_decode_quarantines_unpublished_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    expected_error: str,
) -> None:
    _patch_vin_client(monkeypatch, tmp_path, _vin_payload(**overrides))
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        report = asyncio.run(
            NhtsaApiSyncService(repository, _config(tmp_path)).decode_vin(
                run_key="invalid-vin-fixture",
                vin=VIN,
                scheduled_job_run_id=_scheduled_job(repository, "nhtsa-vin"),
            )
        )

        assert report["status"] == "failed"
        assert expected_error in str(report["error"])
        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, error_message FROM nhtsa_source_artifacts ORDER BY id DESC LIMIT 1"
            )
            artifact = cursor.fetchone()
            assert artifact is not None
            assert artifact["status"] == "quarantined"
            assert expected_error in str(artifact["error_message"])
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_current_artifacts")
            assert cursor.fetchone()["row_count"] == 0
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_vin_decodes")
            assert cursor.fetchone()["row_count"] == 0
            cursor.execute(
                "SELECT status FROM nhtsa_sync_runs WHERE id = %s",
                (report["run_id"],),
            )
            assert cursor.fetchone()["status"] == "failed"
    finally:
        repository.clear_for_tests()
        repository.close()


def test_vin_decode_allows_optional_engine_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_vin_client(monkeypatch, tmp_path, _vin_payload(EngineModel=""))
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        report = asyncio.run(
            NhtsaApiSyncService(repository, _config(tmp_path)).decode_vin(
                run_key="vin-without-engine-model",
                vin=VIN,
                scheduled_job_run_id=_scheduled_job(repository, "nhtsa-vin"),
            )
        )

        assert report["status"] == "completed"
        assert report["vehicle"]["engine_model"] is None
        with repository.connection.cursor() as cursor:
            cursor.execute("SELECT engine_model FROM nhtsa_vin_decodes WHERE vin = %s", (VIN,))
            assert cursor.fetchone()["engine_model"] is None
    finally:
        repository.clear_for_tests()
        repository.close()


def test_failure_during_vin_finalization_rolls_back_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_vin_client(monkeypatch, tmp_path, _vin_payload())
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        scheduled_job_run_id = _scheduled_job(repository, "nhtsa-vin")
        _fail_completed_finalization(repository, monkeypatch)
        report = asyncio.run(
            NhtsaApiSyncService(repository, _config(tmp_path)).decode_vin(
                run_key="valid-vin-fixture",
                vin=VIN,
                scheduled_job_run_id=scheduled_job_run_id,
            )
        )
        assert report["status"] == "failed"
        assert report["error"] == "simulated run finalization failure"
        source_key = f"vpic_vin_sha256_{hashlib.sha256(VIN.encode()).hexdigest()}"
        assert repository.current_artifact("vpic_vin_decodes", source_key) is None
        assert repository.status_report()["vin_decodes"] == 0
        with repository.connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM nhtsa_sync_runs WHERE id = %s",
                (report["run_id"],),
            )
            assert cursor.fetchone()["status"] == "failed"
            cursor.execute(
                "SELECT status, exit_code FROM scheduled_job_runs WHERE id = %s",
                (scheduled_job_run_id,),
            )
            assert cursor.fetchone() == {"status": "failed", "exit_code": 1}
    finally:
        repository.clear_for_tests()
        repository.close()
