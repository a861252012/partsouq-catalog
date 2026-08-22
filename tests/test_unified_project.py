from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import sys
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
    assert "PARTSOUQ_ADMIN_BIND_HOST: 0.0.0.0" in compose
    assert "http://127.0.0.1:8086/health" in compose
    assert "json.load(response).get('status') == 'ok'" in compose
    station_admin = compose.split("  station-admin:", 1)[1].split("\n  scheduler:", 1)[0]
    assert 'PARTSOUQ_STATION_ADMIN_REQUIRE_AUTH: "1"' in station_admin
    assert '"0.0.0.0:8086"' in station_admin


def test_macos_catalog_scheduler_uses_host_browser_and_formal_bounded_mode() -> None:
    launcher_path = PROJECT_ROOT / "deploy/run-macos-catalog-scheduler.zsh"
    launcher = launcher_path.read_text(encoding="utf-8")

    assert launcher_path.stat().st_mode & 0o111
    assert 'export PSQ_CLOAK_LAUNCHER=""' in launcher
    assert "export PSQ_LIMIT_PARTS=0" in launcher
    assert "export PSQ_BOUNDED_PARTS=10000" in launcher
    assert "PSQ_BOUNDED_PARTS:-" not in launcher
    assert 'export PSQ_CLOAK_STATE_DIR="$HOST_STATE_DIR/cloak"' in launcher
    assert 'export PSQ_SCHEDULER_STATE_DIR="$HOST_STATE_DIR/scheduler"' in launcher
    assert '[[ "${PARTSOUQ_DB_NAME:-}" != "partsouq_catalog" ]]' in launcher
    assert launcher.count('/usr/bin/env -i "${RUNTIME_ENV[@]}"') == 2
    runtime_environment = launcher.split("RUNTIME_ENV=(", 1)[1].split("\n)", 1)[0]
    for secret_name in (
        "PARTSOUQ_MYSQL_ROOT_PASSWORD",
        "PARTSOUQ_ADMIN_TOKEN",
        "PARTSOUQ_STATION_ADMIN_SECRET_KEY",
        "PARTSOUQ_STATION_ADMIN_USERNAME",
        "PARTSOUQ_STATION_ADMIN_PASSWORD",
    ):
        assert secret_name not in runtime_environment
    assert '"$PROJECT_ROOT/.venv/bin/partsouq-catalog-migrate" check' in launcher
    assert "--job catalog" in launcher
    assert "--daemon" in launcher

    template = (PROJECT_ROOT / "deploy/com.partsouq.catalog-scheduler.plist.template").read_text(
        encoding="utf-8"
    )
    config = plistlib.loads(template.replace("__PROJECT_ROOT__", str(PROJECT_ROOT)).encode())
    assert config["Label"] == "com.partsouq.catalog-scheduler"
    assert config["LimitLoadToSessionType"] == "Aqua"
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is True
    assert config["ProgramArguments"] == [str(launcher_path)]
    assert config["EnvironmentVariables"]["PARTSOUQ_LAUNCHD_JOB"] == "1"
    assert config["EnvironmentVariables"]["LAUNCHD_JOB"] == "1"


def test_macos_catalog_scheduler_rejects_non_production_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "deploy", project / "deploy")
    (project / ".env").write_text(
        "PARTSOUQ_DB_NAME=partsouq_catalog_test\n",
        encoding="utf-8",
    )
    environment = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHD_JOB": "1",
        "LAUNCHD_JOB": "1",
    }

    result = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "requires PARTSOUQ_DB_NAME=partsouq_catalog" in result.stderr


def test_macos_catalog_scheduler_passes_only_crawler_runtime_environment(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "deploy", project / "deploy")
    (project / ".venv/bin").mkdir(parents=True)
    environment_log = tmp_path / "runtime-environment.log"
    cloak_python = tmp_path / "cloak-python"
    cloak_python.write_text(
        "#!/bin/zsh\n"
        "set -eu\n"
        '[[ -z "${PARTSOUQ_DB_PASSWORD:-}" ]]\n'
        '[[ -z "${PARTSOUQ_MYSQL_ROOT_PASSWORD:-}" ]]\n'
        '[[ -z "${PARTSOUQ_ADMIN_TOKEN:-}" ]]\n'
        '[[ -z "${PARTSOUQ_STATION_ADMIN_SECRET_KEY:-}" ]]\n'
        '[[ -z "${PARTSOUQ_STATION_ADMIN_USERNAME:-}" ]]\n'
        '[[ -z "${PARTSOUQ_STATION_ADMIN_PASSWORD:-}" ]]\n'
        f'print -r -- "cloak-clean" >> {str(environment_log)!r}\n',
        encoding="utf-8",
    )
    cloak_python.chmod(0o700)
    child_script = (
        "#!/bin/zsh\n"
        "set -eu\n"
        '[[ "$PARTSOUQ_DB_PASSWORD" = "db-secret" ]]\n'
        '[[ "$PARTSOUQ_DB_NAME" = "partsouq_catalog" ]]\n'
        '[[ "$PSQ_BOUNDED_PARTS" = "10000" ]]\n'
        '[[ "$PSQ_LIMIT_PARTS" = "0" ]]\n'
        '[[ -z "${PARTSOUQ_MYSQL_ROOT_PASSWORD:-}" ]]\n'
        '[[ -z "${PARTSOUQ_ADMIN_TOKEN:-}" ]]\n'
        '[[ -z "${PARTSOUQ_STATION_ADMIN_SECRET_KEY:-}" ]]\n'
        '[[ -z "${PARTSOUQ_STATION_ADMIN_USERNAME:-}" ]]\n'
        '[[ -z "${PARTSOUQ_STATION_ADMIN_PASSWORD:-}" ]]\n'
        f'print -r -- "clean" >> {str(environment_log)!r}\n'
    )
    for name in ("partsouq-catalog-migrate", "partsouq-scheduler"):
        executable = project / ".venv/bin" / name
        executable.write_text(child_script, encoding="utf-8")
        executable.chmod(0o700)
    (project / ".env").write_text(
        "PARTSOUQ_DB_HOST=127.0.0.1\n"
        "PARTSOUQ_DB_PORT=3308\n"
        "PARTSOUQ_DB_NAME=partsouq_catalog\n"
        "PARTSOUQ_DB_USER=partsouq\n"
        "export PARTSOUQ_DB_PASSWORD=db-secret\n"
        "export PARTSOUQ_MYSQL_ROOT_PASSWORD=root-secret\n"
        "export PARTSOUQ_ADMIN_TOKEN=admin-secret\n"
        "export PARTSOUQ_STATION_ADMIN_SECRET_KEY=station-secret\n"
        "export PARTSOUQ_STATION_ADMIN_USERNAME=station-user\n"
        "export PARTSOUQ_STATION_ADMIN_PASSWORD=station-password\n"
        f"PSQ_CLOAK_PYTHON={cloak_python}\n",
        encoding="utf-8",
    )
    environment = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHD_JOB": "1",
        "LAUNCHD_JOB": "1",
    }

    result = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert environment_log.read_text(encoding="utf-8").splitlines() == [
        "cloak-clean",
        "clean",
        "clean",
    ]


def test_macos_catalog_launch_agent_install_disable_is_repeatable_and_preserves_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project & scheduler"
    shutil.copytree(PROJECT_ROOT / "deploy", project / "deploy")
    fake_launchctl = tmp_path / "launchctl"
    launchctl_log = tmp_path / "launchctl.log"
    launchctl_state = tmp_path / "launchctl.loaded"
    fake_launchctl.write_text(
        "#!/bin/zsh\n"
        "set -eu\n"
        'print -r -- "$*" >> "$PARTSOUQ_TEST_LAUNCHCTL_LOG"\n'
        'case "$1" in\n'
        '  print) [[ -f "$PARTSOUQ_TEST_LAUNCHCTL_STATE" ]] ;;\n'
        '  bootstrap) : > "$PARTSOUQ_TEST_LAUNCHCTL_STATE" ;;\n'
        '  bootout) /bin/rm -f "$PARTSOUQ_TEST_LAUNCHCTL_STATE" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_launchctl.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PARTSOUQ_LAUNCHCTL_BIN": str(fake_launchctl),
            "PARTSOUQ_TEST_LAUNCHCTL_LOG": str(launchctl_log),
            "PARTSOUQ_TEST_LAUNCHCTL_STATE": str(launchctl_state),
        }
    )
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    disable = project / "deploy/disable-macos-catalog-scheduler.zsh"

    subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not launchctl_log.exists()

    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    first_render = agent.read_bytes()
    assert b"__PROJECT_ROOT__" not in first_render
    assert b"__STDOUT_PATH__" not in first_render
    assert b"__STDERR_PATH__" not in first_render
    config = plistlib.loads(first_render)
    host_state = home / "Library/Application Support/partsouq-catalog"
    log_dir = host_state / "logs"
    reload_marker = host_state / "launch-agent-needs-reload"
    assert reload_marker.exists()
    assert config["ProgramArguments"] == [str(project / "deploy/run-macos-catalog-scheduler.zsh")]
    assert config["WorkingDirectory"] == str(project)
    assert config["StandardOutPath"] == str(log_dir / "catalog-scheduler.stdout.log")
    assert config["StandardErrorPath"] == str(log_dir / "catalog-scheduler.stderr.log")
    assert stat.S_IMODE(agent.stat().st_mode) == 0o600
    for private_dir in (host_state, host_state / "cloak", host_state / "scheduler", log_dir):
        assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700

    for _ in range(2):
        subprocess.run(
            [installer],
            cwd=project,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    assert agent.read_bytes() == first_render
    launchctl_calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("bootstrap ") for call in launchctl_calls) == 1
    assert not any(call.startswith("bootout ") for call in launchctl_calls)
    assert not reload_marker.exists()

    cookie = host_state / "cloak/cookies.json"
    scheduler_log = log_dir / "catalog-scheduler.stdout.log"
    cookie.write_text("retained-cookie", encoding="utf-8")
    scheduler_log.write_text("retained-log", encoding="utf-8")
    subprocess.run(
        [disable],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert agent.exists()
    assert cookie.read_text(encoding="utf-8") == "retained-cookie"
    assert scheduler_log.read_text(encoding="utf-8") == "retained-log"
    assert not launchctl_state.exists()
    assert launchctl_log.read_text(encoding="utf-8").splitlines()[-1].startswith("bootout ")


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


def test_catalog_ignores_legacy_database_environment() -> None:
    environment = os.environ.copy()
    for name in (
        "PARTSOUQ_DB_HOST",
        "PARTSOUQ_DB_PORT",
        "PARTSOUQ_DB_USER",
        "PARTSOUQ_DB_PASSWORD",
        "PARTSOUQ_DB_NAME",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PSQ_DB_HOST": "legacy-host",
            "PSQ_DB_PORT": "9999",
            "PSQ_DB_USER": "legacy-user",
            "PSQ_DB_PASS": "legacy-password",
            "PSQ_DB_NAME": "legacy-database",
        }
    )
    command = (
        "from partsouq_catalog.config import DB_CONFIG; "
        "assert DB_CONFIG == {'host': '127.0.0.1', 'port': 3308, "
        "'user': 'partsouq', 'password': 'partsouq-local', "
        "'database': 'partsouq_catalog'}"
    )

    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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
    assert (
        scheduler._run(
            "nhtsa-vin",
            [
                sys.executable,
                "-c",
                "import sys; print('{\"vin\":\"' + sys.argv[2].strip() + '\"}', end='')",
                "nhtsa-decode-vin",
                " zzztest00x0000001 ",
            ],
        )
        == 0
    )
    assert saved == ['{"vin":"ZZZ**********0001"}']
