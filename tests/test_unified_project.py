from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import Mock

import pytest

from partsouq_admin.app import (
    PartFitmentInput,
    VehicleMappingInput,
    VinVehicleMappingInput,
    list_vin_vehicle_candidates,
)
from partsouq_catalog import scheduler
from partsouq_catalog.http_client import ChallengeError, SessionManager
from partsouq_catalog.parsers import parse_parts
from partsouq_crawler.nhtsa.config import NhtsaConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_compose_requires_explicit_scheduler_profile_and_checks_admin_health() -> None:
    compose = (PROJECT_ROOT / "compose.yml").read_text(encoding="utf-8")
    scheduler_anchor = compose.split("services:", 1)[0]

    assert 'profiles: ["scheduler"]' in scheduler_anchor
    assert compose.count("<<: *scheduler-base") == 3
    assert "http://127.0.0.1:8000/api/health" in compose
    assert "http://127.0.0.1:8086/health" in compose
    assert "json.load(response).get('status') == 'ok'" in compose


def test_catalog_challenge_stops_without_cookie_refresh() -> None:
    manager = SessionManager(no_browser=True)
    response = Mock(status_code=403, text="Just a moment", headers={})
    manager.session.get = Mock(return_value=response)

    with pytest.raises(ChallengeError):
        manager.get("https://partsouq.example/catalog")

    assert manager.session.get.call_count == 1


def test_catalog_part_without_product_name_is_rejected() -> None:
    html = """
    <table><tr>
      <td><a href="/en/search/all?q=12345">12345</a></td>
      <td></td><td>100</td><td></td><td>01</td><td>01.2018 - 12.2019</td>
    </tr></table>
    """

    parts, malformed, skipped, _skipped_rows = parse_parts(html, diagnostics=True)

    assert malformed == 0
    assert skipped == 1
    assert parts == []


def test_nhtsa_uses_shared_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NHTSA_MYSQL_DATABASE", "must_be_ignored")
    monkeypatch.setenv("NHTSA_MYSQL_USER", "must_be_ignored")
    monkeypatch.setenv("PARTSOUQ_DB_NAME", "unified_database")
    monkeypatch.setenv("PARTSOUQ_DB_USER", "unified_user")

    config = NhtsaConfig.from_env()

    assert config.mysql_database == "unified_database"
    assert config.mysql_user == "unified_user"


def test_vin_mapping_rejects_full_vin() -> None:
    with pytest.raises(ValueError, match="完整 17 碼 VIN"):
        VehicleMappingInput(
            vin_prefix="ZZZTEST00X0000001",
            make_name="Toyota",
            model_name="Prius",
        )


def test_confirmed_mapping_accepts_full_vin_and_vehicle_id() -> None:
    mapping = VinVehicleMappingInput(
        vin="zzztest00x0000001",
        partsouq_vehicle_id=42,
    )

    assert mapping.vin == "ZZZTEST00X0000001"
    assert mapping.partsouq_vehicle_id == 42


def test_manual_fitment_rejects_reversed_years() -> None:
    with pytest.raises(ValueError, match="起始年份不得晚於結束年份"):
        PartFitmentInput(
            part_number="123-AB",
            make_name="Example",
            model_name="Model",
            model_year_from=2025,
            model_year_to=2020,
        )


def test_manual_fitment_rejects_empty_normalized_part_number() -> None:
    with pytest.raises(ValueError, match="正規化後不可為空"):
        PartFitmentInput(
            part_number="---",
            make_name="Example",
            model_name="Model",
        )


@pytest.mark.parametrize("source_reference", (None, "   "))
def test_name_override_requires_audit_reference(source_reference: str | None) -> None:
    with pytest.raises(ValueError, match="人工確認依據"):
        VinVehicleMappingInput(
            vin="ZZZTEST00X0000001",
            partsouq_vehicle_id=42,
            allow_name_override=True,
            source_reference=source_reference,
        )


def test_override_source_name_cannot_bypass_audit_flow() -> None:
    with pytest.raises(ValueError, match="只允許由人工確認流程設定"):
        VinVehicleMappingInput(
            vin="ZZZTEST00X0000001",
            partsouq_vehicle_id=42,
            source_name="manual-name-override",
        )


def test_vehicle_candidates_exclude_unmapped_legacy_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []
    monkeypatch.setattr(
        "partsouq_admin.app._fetch_all",
        lambda query, _params: queries.append(query) or [],
    )

    assert list_vin_vehicle_candidates("ZZZTEST00X0000001") == []
    assert "p.vehicle_id IS NOT NULL" in queries[0]
    assert "UPPER(p.engine)" in queries[0]
    assert "UPPER(p.trim_name)" in queries[0]
    assert "d.displacement_l IS NOT NULL" in queries[0]


def test_scheduler_runs_nhtsa_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job_name: 8)
    monkeypatch.setattr(scheduler, "_record_progress", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_record_finish", lambda *_args: None)

    def fake_run(job_name: str, command: list[str]) -> int:
        calls.append((job_name, command))
        return 0

    monkeypatch.setattr(scheduler, "_run", fake_run)

    assert scheduler.dispatch("nhtsa", "all") == 0
    assert [job_name for job_name, _ in calls] == ["nhtsa-bulk", "nhtsa-api"]
    assert calls[0][1][3:5] == ["nhtsa-sync-bulk", "--scope"]
    assert calls[1][1][3:5] == ["nhtsa-sync-api", "--scope"]


def test_scheduler_consumes_pending_admin_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    completed: list[tuple[int, int]] = []

    monkeypatch.setattr(scheduler, "LOG_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "_requeue_interrupted_requests", lambda: None)
    monkeypatch.setattr(scheduler, "_recover_interrupted_job_runs", lambda _job: None)
    monkeypatch.setattr(
        scheduler,
        "_pending_requests",
        lambda: [
            {
                "id": 7,
                "job_name": "nhtsa-vin",
                "requested_scope": "ZZZTEST00X0000001",
            }
        ],
    )
    monkeypatch.setattr(scheduler, "_claim_request", lambda _request_id: True)
    monkeypatch.setattr(
        scheduler,
        "_finish_request",
        lambda request_id, return_code: completed.append((request_id, return_code)),
    )
    monkeypatch.setattr(scheduler, "_run", lambda _job_name, _command: 0)

    assert scheduler.dispatch("pending", "all") == 0
    assert completed == [(7, 0)]


def test_scheduler_decodes_one_supplied_vin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        scheduler,
        "_run",
        lambda job_name, command: calls.append((job_name, command)) or 0,
    )

    assert scheduler.dispatch("nhtsa-vin", "ZZZTEST00X0000001") == 0
    assert calls[0][0] == "nhtsa-vin"
    assert calls[0][1][3:5] == ["nhtsa-decode-vin", "ZZZTEST00X0000001"]


def test_scheduler_redacts_vin_from_persisted_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[str] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job_name: 7)
    monkeypatch.setattr(
        scheduler,
        "_record_finish",
        lambda _run_id, _return_code, output, *_success: saved.append(output),
    )
    process = Mock(returncode=0, stdout=io.StringIO('{"vin":"ZZZTEST00X0000001"}'))
    process.wait.return_value = 0
    process.poll.return_value = 0
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *_args, **_kwargs: process)

    assert (
        scheduler._run(
            "nhtsa-vin",
            ["python", "-m", "partsouq_crawler", "nhtsa-decode-vin", " zzztest00x0000001 "],
        )
        == 0
    )
    assert saved == ['{"vin":"ZZZ**********0001"}']
