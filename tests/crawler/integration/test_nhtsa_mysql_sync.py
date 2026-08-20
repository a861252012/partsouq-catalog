from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web

from partsouq_crawler.nhtsa.api_service import NhtsaApiSyncService
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.datasets import RECALL_FIELDS, BulkSource
from partsouq_crawler.nhtsa.models import DownloadedArtifact, ParsedRecord
from partsouq_crawler.nhtsa.repository import NhtsaMySQLRepository
from partsouq_crawler.nhtsa.service import NhtsaBulkSyncService

from ..helpers import fake_site

pytestmark = pytest.mark.skipif(
    os.getenv("NHTSA_TEST_MYSQL") != "1",
    reason="set NHTSA_TEST_MYSQL=1 to run MySQL integration tests",
)

VIN = "ZZZTEST00X0000001"
OTHER_VIN = "ZZZTEST00X0000002"


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
    return NhtsaConfig.from_env(
        raw_dir=tmp_path / "raw",
        user_agent="nhtsa-test/1.0",
        request_timeout_seconds=10,
    )


def test_bulk_sync_is_real_provenanced_and_idempotent(tmp_path: Path) -> None:
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

                second = await service.run(
                    run_key="fixture-sync",
                    scope_name="recalls",
                    sources=sources,
                )
                assert second["status"] == "completed"
                assert second["artifacts_downloaded"] == 0
                assert second["artifacts_reused"] == 2
                assert repository.status_report()["current_record_counts"] == {"recalls": 2}
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
        artifact_id = repository.create_artifact(
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

        updated_payload = json.dumps(
            {"Organization": "Duplicate station", "LastUpdatedDate": "2025-01-01"}
        )
        assert (
            repository.insert_records(
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
            artifact_id,
            source_rows=3,
            new_versions=2,
            rejected_rows=0,
        )
        repository.publish_artifacts([("cssi_stations", "cssi_state_test", artifact_id)])

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


def test_failure_after_vin_publish_keeps_current_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_vin_client(monkeypatch, tmp_path, _vin_payload())
    repository = NhtsaMySQLRepository.create(_config(tmp_path))
    try:
        repository.clear_for_tests()
        finish_run = repository.finish_run

        def fail_completed_run(
            run_id: int,
            *,
            status: str,
            downloaded: int,
            reused: int,
            source_rows: int,
            new_versions: int,
            rejected_rows: int,
            error_message: str | None = None,
        ) -> None:
            if status == "completed":
                raise RuntimeError("simulated run finalization failure")
            finish_run(
                run_id,
                status=status,
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
                error_message=error_message,
            )

        monkeypatch.setattr(repository, "finish_run", fail_completed_run)
        report = asyncio.run(
            NhtsaApiSyncService(repository, _config(tmp_path)).decode_vin(
                run_key="valid-vin-fixture",
                vin=VIN,
            )
        )
        assert report["status"] == "failed"
        assert report["error"] == "simulated run finalization failure"
        source_key = f"vpic_vin_sha256_{hashlib.sha256(VIN.encode()).hexdigest()}"
        preserved = repository.current_artifact("vpic_vin_decodes", source_key)
        assert preserved is not None
        assert preserved["status"] == "imported"
        assert preserved["error_message"] is None
        assert repository.status_report()["vin_decodes"] == 1
    finally:
        repository.clear_for_tests()
        repository.close()
