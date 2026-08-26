from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import time
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
MACOS_LAUNCH_AGENT_REASON = "requires macOS zsh, plutil, and LaunchAgent filesystem semantics"
MACOS_LAUNCH_AGENT_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason=MACOS_LAUNCH_AGENT_REASON,
)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _runtime_manifest_lines(release: Path) -> list[str]:
    paths = [
        release / "scheduler.env",
        release / "source.sha256",
        release / "cloak-browser.sha256",
        release / ".install-complete",
        release / ".trusted-free-runtime-v1",
    ]
    for directory in (release / "app/.venv", release / "cloak-venv"):
        paths.extend(path for path in directory.rglob("*") if path.is_file() or path.is_symlink())
    lines = []
    for path in paths:
        relative = path.relative_to(release)
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            assert resolved.is_file()
            lines.append(
                f"L\t{hashlib.sha256(resolved.read_bytes()).hexdigest()}"
                f"\t{relative}\t{os.readlink(path)}"
            )
        else:
            lines.append(f"F\t{hashlib.sha256(path.read_bytes()).hexdigest()}\t{relative}")
    return sorted(lines)


def _make_staged_runtime(
    tmp_path: Path,
    *,
    database: str = "partsouq_catalog",
) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    release = home / "Library/Application Support/partsouq-catalog/releases" / "test-release"
    project = release / "app"
    deploy = project / "deploy"
    deploy.mkdir(parents=True)
    shutil.copy(
        PROJECT_ROOT / "deploy/run-macos-catalog-scheduler.zsh",
        deploy / "run-macos-catalog-scheduler.zsh",
    )
    (release / "scheduler.env").write_text(
        "PARTSOUQ_DB_HOST=127.0.0.1\n"
        "PARTSOUQ_DB_PORT=3308\n"
        f"PARTSOUQ_DB_NAME={database}\n"
        "PARTSOUQ_DB_USER=partsouq\n"
        "PARTSOUQ_DB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )
    execution_log = tmp_path / "runtime-execution.log"
    child_script = (
        "#!/bin/zsh\n"
        "set -eu\n"
        '[[ "$PARTSOUQ_DB_NAME" = "partsouq_catalog" ]]\n'
        '[[ "$PSQ_BOUNDED_PARTS" = "10000" ]]\n'
        '[[ "$PSQ_LIMIT_PARTS" = "0" ]]\n'
        '[[ "$CLOAKBROWSER_AUTO_UPDATE" = "false" ]]\n'
        '[[ -z "${PARTSOUQ_ADMIN_TOKEN:-}" ]]\n'
        '[[ -z "${CLOAKBROWSER_LICENSE_KEY:-}" ]]\n'
        f"print -r -- ${{0:t}} >> {str(execution_log)!r}\n"
        'print -r -- "${0:t}" >> "$PSQ_RUNTIME_LOG_DIR/crawl.log"\n'
        'if [[ "${0:t}" = "partsouq-scheduler" ]]; then\n'
        '  print -r -- $$ > "$PARTSOUQ_SCHEDULER_READY_MARKER"\n'
        "fi\n"
    )
    for name in ("partsouq-catalog-migrate", "partsouq-scheduler"):
        _write_executable(project / ".venv/bin" / name, child_script)
    project_package = project / ".venv/lib/python-test/site-packages/partsouq_catalog/runtime.py"
    project_package.parent.mkdir(parents=True)
    project_package.write_text("STAGED = True\n", encoding="utf-8")
    external_python = home / "Library/Application Support/partsouq-catalog/runtime-python"
    _write_executable(external_python, "#!/bin/zsh\nexit 0\n")
    (project / ".venv/bin/python").symlink_to(external_python)
    cloak_python = release / "cloak-venv/bin/python"
    _write_executable(
        cloak_python,
        "#!/bin/zsh\n"
        "set -eu\n"
        '[[ -z "${PARTSOUQ_DB_PASSWORD:-}" ]]\n'
        '[[ -z "${CLOAKBROWSER_LICENSE_KEY:-}" ]]\n',
    )
    cloak_package = release / "cloak-venv/lib/python-test/site-packages/cloakbrowser/__init__.py"
    cloak_package.parent.mkdir(parents=True)
    cloak_package.write_text("__version__ = 'test'\n", encoding="utf-8")
    browser_cache = home / "Library/Application Support/partsouq-catalog/cloak/free-browser-cache"
    browser = browser_cache / "chromium-test/fake-chromium"
    _write_executable(browser, "#!/bin/zsh\nexit 0\n")

    source_lines = []
    for path in sorted(project.rglob("*")):
        if not path.is_file() or ".venv" in path.parts:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        source_lines.append(f"{digest}  ./{path.relative_to(project)}")
    (release / "source.sha256").write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    (release / "cloak-browser.sha256").write_text(
        f"{hashlib.sha256(browser.read_bytes()).hexdigest()}  {browser}\n",
        encoding="utf-8",
    )
    (release / ".trusted-free-runtime-v1").write_text("test-commit\n", encoding="utf-8")
    (release / ".install-complete").write_text("test-commit\n", encoding="utf-8")
    for path in [release, *release.rglob("*")]:
        if path.is_symlink():
            continue
        path.chmod(0o700 if path.is_dir() or os.access(path, os.X_OK) else 0o600)
    (release / "runtime.sha256").write_text(
        "\n".join(_runtime_manifest_lines(release)) + "\n",
        encoding="utf-8",
    )
    (release / "runtime.sha256").chmod(0o600)
    return home, project, execution_log


def _make_installable_project(tmp_path: Path) -> Path:
    project = tmp_path / "source project"
    shutil.copytree(PROJECT_ROOT / "deploy", project / "deploy")
    for filename in ("README.md", "pyproject.toml", "uv.lock"):
        shutil.copy(PROJECT_ROOT / filename, project / filename)
    for directory in ("src", "db", "migrations"):
        runtime_file = project / directory / ".runtime-test"
        runtime_file.parent.mkdir()
        runtime_file.write_text("runtime fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", project], check=True)
    subprocess.run(["git", "-C", project, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            project,
            "-c",
            "user.name=Runtime Test",
            "-c",
            "user.email=runtime-test@example.invalid",
            "commit",
            "-qm",
            "runtime fixture",
        ],
        check=True,
    )
    (project / ".env").write_text(
        "PARTSOUQ_DB_HOST=127.0.0.1\n"
        "PARTSOUQ_DB_PORT=3308\n"
        "PARTSOUQ_DB_NAME=partsouq_catalog\n"
        "PARTSOUQ_DB_USER=partsouq\n"
        "PARTSOUQ_DB_PASSWORD='db secret with spaces'\n"
        "PARTSOUQ_MYSQL_ROOT_PASSWORD=root-secret\n"
        "PARTSOUQ_ADMIN_TOKEN=admin-secret\n"
        "PARTSOUQ_STATION_ADMIN_SECRET_KEY=station-secret\n"
        "PSQ_WORKERS=4\n",
        encoding="utf-8",
    )
    return project


def _make_fake_uv(
    tmp_path: Path,
    *,
    fail_sync: bool = False,
    scheduler_ready: bool = True,
    download_pro_artifact: bool = False,
) -> tuple[Path, Path, Path]:
    uv = tmp_path / "uv"
    uv_log = tmp_path / "uv.log"
    execution_log = tmp_path / "installed-runtime.log"
    browser_seed = tmp_path / "fake-cloak-browser-seed"
    _write_executable(browser_seed, "#!/bin/zsh\nexit 0\n")
    pro_artifact_command = (
        '  print -r -- paid > "$CLOAKBROWSER_CACHE_DIR/.license_cache"\n'
        if download_pro_artifact
        else ""
    )
    cloak_python_template = tmp_path / "fake-cloak-python"
    _write_executable(
        cloak_python_template,
        "#!/bin/zsh\n"
        "set -eu\n"
        '[[ -z "${CLOAKBROWSER_LICENSE_KEY:-}" ]]\n'
        '[[ -z "${CLOAKBROWSER_TOKEN:-}" ]]\n'
        '[[ -z "${CLOAKBROWSER_DOWNLOAD_URL:-}" ]]\n'
        'if [[ "$*" == *ensure_binary* ]]; then\n'
        f'  print -r -- "ensure-cache=$CLOAKBROWSER_CACHE_DIR" >> {str(uv_log)!r}\n'
        '  BINARY="$CLOAKBROWSER_CACHE_DIR/chromium-test/fake-chromium"\n'
        '  /bin/mkdir -p "${BINARY:h}"\n'
        '  if [[ ! -e "$BINARY" ]]; then\n'
        f'    /bin/cp {str(browser_seed)!r} "$BINARY"\n'
        '    /bin/chmod 700 "$BINARY"\n'
        "  fi\n"
        f"{pro_artifact_command}"
        '  print -r -- "$BINARY"\n'
        "fi\n",
    )
    sync_result = "exit 9" if fail_sync else ":"
    scheduler_behavior = (
        'print -r -- $$ > "$PARTSOUQ_SCHEDULER_READY_MARKER"; /bin/sleep 10'
        if scheduler_ready
        else "exit 42"
    )
    _write_executable(
        uv,
        "#!/bin/zsh\n"
        "set -eu\n"
        f'print -r -- "$*" >> {str(uv_log)!r}\n'
        'case "$1" in\n'
        "  sync)\n"
        f"    {sync_result}\n"
        "    shift\n"
        "    PROJECT=\n"
        "    while (( $# )); do\n"
        '      if [[ "$1" = "--project" ]]; then PROJECT=$2; shift 2; else shift; fi\n'
        "    done\n"
        '    /bin/mkdir -p "$PROJECT/.venv/bin"\n'
        '    /bin/mkdir -p "$PROJECT/.venv/lib/python-test/site-packages/partsouq_catalog"\n'
        '    print -r -- "STAGED = True" > '
        '"$PROJECT/.venv/lib/python-test/site-packages/partsouq_catalog/runtime.py"\n'
        '    /bin/mkdir -p "$PROJECT/.venv/lib/python-test/site-packages/generated/__pycache__"\n'
        "    print -r -- generated > "
        '"$PROJECT/.venv/lib/python-test/site-packages/generated/__pycache__/build.pyc"\n'
        "    /bin/ln -sf "
        f"{sys._base_executable!r} "
        '"$PROJECT/.venv/bin/python"\n'
        "    for NAME in partsouq-catalog-migrate partsouq-scheduler; do\n"
        '      TARGET="$PROJECT/.venv/bin/$NAME"\n'
        "      print -r -- '#!/bin/zsh' > \"$TARGET\"\n"
        "      print -r -- 'set -eu' >> \"$TARGET\"\n"
        '      print -r -- \'[[ "$PARTSOUQ_DB_NAME" = "partsouq_catalog" ]]\' >> "$TARGET"\n'
        '      print -r -- \'[[ "$CLOAKBROWSER_AUTO_UPDATE" = "false" ]]\' >> "$TARGET"\n'
        f"      print -r -- 'print -r -- ${{0:t}} >> {str(execution_log)!r}' >> \"$TARGET\"\n"
        '      if [[ "$NAME" = "partsouq-scheduler" ]]; then\n'
        f'        print -r -- {scheduler_behavior!r} >> "$TARGET"\n'
        "      fi\n"
        '      /bin/chmod 700 "$TARGET"\n'
        "    done\n"
        "    ;;\n"
        "  venv)\n"
        "    TARGET=${@[-1]}\n"
        '    /bin/mkdir -p "$TARGET/bin" '
        '"$TARGET/lib/python-test/site-packages/cloakbrowser"\n'
        '    print -r -- "__version__ = test" > '
        '"$TARGET/lib/python-test/site-packages/cloakbrowser/__init__.py"\n'
        '    /bin/mkdir -p "$TARGET/lib/python-test/site-packages/generated/__pycache__"\n'
        "    print -r -- generated > "
        '"$TARGET/lib/python-test/site-packages/generated/__pycache__/build.pyc"\n'
        '    PYTHON="$TARGET/bin/python"\n'
        f'    /bin/cp {str(cloak_python_template)!r} "$PYTHON"\n'
        '    /bin/chmod 700 "$PYTHON"\n'
        "    ;;\n"
        "  pip) : ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
    )
    return uv, uv_log, execution_log


def _make_fake_launchctl(tmp_path: Path) -> tuple[Path, Path, Path]:
    launchctl = tmp_path / "launchctl"
    launchctl_log = tmp_path / "launchctl.log"
    launchctl_state = tmp_path / "launchctl.loaded"
    launchctl_program = tmp_path / "launchctl.loaded.program"
    reported_state = tmp_path / "launchctl.reported.state"
    reported_program = tmp_path / "launchctl.reported.program"
    reported_pid = tmp_path / "launchctl.reported.pid"
    _write_executable(
        launchctl,
        "#!/bin/zsh\n"
        "set -eu\n"
        "unsetopt BG_NICE\n"
        f'print -r -- "$*" >> {str(launchctl_log)!r}\n'
        'case "$1" in\n'
        "  print)\n"
        f"    [[ -f {str(launchctl_state)!r} ]] || exit 1\n"
        f"    PID=$(/bin/cat {str(launchctl_state)!r})\n"
        f"    PROGRAM=$(/bin/cat {str(launchctl_program)!r})\n"
        "    STATE=running\n"
        f"    [[ ! -f {str(reported_state)!r} ]] || "
        f"STATE=$(/bin/cat {str(reported_state)!r})\n"
        f"    [[ ! -f {str(reported_program)!r} ]] || "
        f"PROGRAM=$(/bin/cat {str(reported_program)!r})\n"
        f"    [[ ! -f {str(reported_pid)!r} ]] || PID=$(/bin/cat {str(reported_pid)!r})\n"
        '    print -r -- "state = $STATE"\n'
        '    print -r -- "program = $PROGRAM"\n'
        '    print -r -- "pid = $PID"\n'
        "    ;;\n"
        "  bootstrap)\n"
        "    RUNNER=$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' \"$3\")\n"
        '    PARTSOUQ_LAUNCHD_JOB=1 LAUNCHD_JOB=1 "$RUNNER" '
        ">/dev/null 2>&1 </dev/null &\n"
        "    PID=$!\n"
        f'    print -r -- "$PID" > {str(launchctl_state)!r}\n'
        f'    print -r -- "$RUNNER" > {str(launchctl_program)!r}\n'
        "    ;;\n"
        "  bootout)\n"
        f"    if [[ -f {str(launchctl_state)!r} ]]; then\n"
        f"      PID=$(/bin/cat {str(launchctl_state)!r})\n"
        '      /bin/kill "$PID" 2>/dev/null || true\n'
        "    fi\n"
        f"    /bin/rm -f {str(launchctl_state)!r}\n"
        f"    /bin/rm -f {str(launchctl_program)!r}\n"
        "    ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
    )
    return launchctl, launchctl_log, launchctl_state


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


def test_macos_catalog_scheduler_stages_a_tcc_safe_locked_runtime() -> None:
    launcher = (PROJECT_ROOT / "deploy/run-macos-catalog-scheduler.zsh").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "deploy/install-macos-catalog-scheduler.zsh").read_text(
        encoding="utf-8"
    )
    template = (PROJECT_ROOT / "deploy/com.partsouq.catalog-scheduler.plist.template").read_text(
        encoding="utf-8"
    )

    assert 'RUNTIME_CONFIG="$RELEASE_DIR/scheduler.env"' in launcher
    assert 'source "$PROJECT_ROOT/.env"' not in launcher
    assert 'export PSQ_CLOAK_PYTHON="$RELEASE_DIR/cloak-venv/bin/python"' in launcher
    assert 'export CLOAKBROWSER_CACHE_DIR="$HOST_STATE_DIR/cloak/free-browser-cache"' in launcher
    assert 'export PSQ_RUNTIME_LOG_DIR="$HOST_STATE_DIR/logs/runtime"' in launcher
    assert "export CLOAKBROWSER_AUTO_UPDATE=false" in launcher
    assert "export PSQ_LIMIT_PARTS=0" in launcher
    assert "export PSQ_BOUNDED_PARTS=10000" in launcher
    assert '[[ "${PARTSOUQ_DB_NAME:-}" != "partsouq_catalog" ]]' in launcher
    assert "READY_MARKER=" in launcher
    assert '"PARTSOUQ_APPLY_MIGRATIONS_ON_START=1"' in launcher
    assert "--recover-only" not in launcher
    assert 'partsouq-catalog-migrate" apply' not in launcher
    assert launcher.index('cmp -s "$RUNTIME_CHECK"') < launcher.index('source "$RUNTIME_CONFIG"')
    assert launcher.index('source "$RUNTIME_CONFIG"') < launcher.index(
        'export CLOAKBROWSER_BINARY_PATH="$EXPECTED_CLOAK_BINARY"'
    )
    assert 'CURRENT_GROUP_IDS=" $(/usr/bin/id -G) "' in launcher
    assert "8#$TARGET_MODE & 8#2" in launcher
    assert "GROUP_WRITABLE_BY_CURRENT_USER" in launcher
    assert "runtime symlink target has unsafe owner or writable mode" in launcher

    assert "status --porcelain --untracked-files=no" in installer
    assert "archive --format=tar" in installer
    assert "--locked" in installer
    assert "--no-editable" in installer
    assert "--require-hashes" in installer
    assert "CLOAKBROWSER_LICENSE_KEY" in installer
    assert "CLOAKBROWSER_DOWNLOAD_URL" in installer
    assert ".install-complete" in installer
    assert "LaunchAgent child readiness failed" in installer
    assert 'CURRENT_GROUP_IDS=" $(/usr/bin/id -G) "' in installer
    assert "8#$TARGET_MODE & 8#2" in installer
    assert "GROUP_WRITABLE_BY_CURRENT_USER" in installer
    assert "runtime symlink target has unsafe owner or writable mode" in installer
    symlink_preflight = installer.split("reject_existing_symlink_ancestors \\\n", 1)[1].split(
        "/bin/mkdir -p", 1
    )[0]
    for private_leaf in (
        '"$AGENT_PATH"',
        '"$STDOUT_PATH"',
        '"$STDERR_PATH"',
        '"$RELOAD_MARKER"',
    ):
        assert private_leaf in symlink_preflight
    assert "AGENT_PATH.rollback.$$" not in installer
    assert "ACTIVE_AGENT_PATH.$$" not in installer
    assert 'mktemp "$LAUNCH_AGENTS_DIR/.$LABEL.rollback.XXXXXX"' in installer
    assert installer.count('mktemp "$HOST_STATE_DIR/.active-launch-agent.XXXXXX"') == 2
    assert '/bin/mv "$STAGED_RELEASE"' not in installer

    config = plistlib.loads(
        template.replace("__RUNTIME_PROJECT_ROOT__", "/runtime/release/app").encode()
    )
    assert config["ProgramArguments"] == [
        "/runtime/release/app/deploy/run-macos-catalog-scheduler.zsh"
    ]
    assert config["WorkingDirectory"] == "/runtime/release/app"
    assert config["LimitLoadToSessionType"] == "Aqua"


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_scheduler_rejects_non_production_database(tmp_path: Path) -> None:
    home, project, _execution_log = _make_staged_runtime(
        tmp_path,
        database="partsouq_catalog_test",
    )
    environment = {
        "HOME": str(home),
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


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_staged_runner_survives_source_removal_and_filters_environment(
    tmp_path: Path,
) -> None:
    home, project, execution_log = _make_staged_runtime(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHD_JOB": "1",
        "LAUNCHD_JOB": "1",
        "PARTSOUQ_ADMIN_TOKEN": "must-not-reach-child",
    }

    result = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    restart = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert restart.returncode == 0, restart.stderr
    expected_executions = ["partsouq-scheduler"]
    assert execution_log.read_text(encoding="utf-8").splitlines() == expected_executions * 2
    assert not (project / "logs").exists()
    runtime_log = home / "Library/Application Support/partsouq-catalog/logs/runtime/crawl.log"
    assert runtime_log.read_text(encoding="utf-8").splitlines() == expected_executions * 2
    ready_marker = (
        home
        / "Library/Application Support/partsouq-catalog/scheduler"
        / "launch-ready-test-release"
    )
    assert ready_marker.read_text(encoding="utf-8").strip().isdigit()

    python_target = (project / ".venv/bin/python").resolve(strict=True)
    python_target.chmod(0o777)
    execution_before_unsafe_mode = execution_log.read_bytes()
    runtime_log_before_unsafe_mode = runtime_log.read_bytes()
    unsafe_target_restart = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert unsafe_target_restart.returncode == 2
    assert "runtime symlink target has unsafe owner or writable mode" in (
        unsafe_target_restart.stderr
    )
    assert execution_log.read_bytes() == execution_before_unsafe_mode
    assert runtime_log.read_bytes() == runtime_log_before_unsafe_mode
    python_target.chmod(0o700)

    python_target.chmod(0o720)
    execution_before_unsafe_group = execution_log.read_bytes()
    runtime_log_before_unsafe_group = runtime_log.read_bytes()
    unsafe_group_restart = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert unsafe_group_restart.returncode == 2
    assert "runtime symlink target has unsafe owner or writable mode" in (
        unsafe_group_restart.stderr
    )
    assert execution_log.read_bytes() == execution_before_unsafe_group
    assert runtime_log.read_bytes() == runtime_log_before_unsafe_group
    python_target.chmod(0o700)

    app_support = home / "Library/Application Support"
    external_app_support = tmp_path / "external-app-support"
    app_support.rename(external_app_support)
    app_support.symlink_to(external_app_support, target_is_directory=True)
    external_tree_before = {
        path.relative_to(external_app_support) for path in external_app_support.rglob("*")
    }
    execution_before = execution_log.read_bytes()
    runtime_log_before = runtime_log.read_bytes()

    blocked_restart = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert blocked_restart.returncode == 2
    assert "refusing symlink in private runtime state path" in blocked_restart.stderr
    assert execution_log.read_bytes() == execution_before
    assert runtime_log.read_bytes() == runtime_log_before
    assert {
        path.relative_to(external_app_support) for path in external_app_support.rglob("*")
    } == external_tree_before


@MACOS_LAUNCH_AGENT_ONLY
@pytest.mark.parametrize(
    "mutation",
    [
        "new_pth",
        "legacy_pyc",
        "valid_pycache",
        "python_symlink",
        "external_python",
        "dangling_python",
        "directory_python",
        "project_package",
        "cloak_package",
    ],
)
def test_macos_staged_runner_rejects_runtime_dependency_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    home, project, execution_log = _make_staged_runtime(tmp_path)
    release = project.parent
    if mutation == "new_pth":
        target = project / ".venv/lib/python-test/site-packages/injected.pth"
        target.write_text("/tmp/injected\n", encoding="utf-8")
        target.chmod(0o600)
    elif mutation == "legacy_pyc":
        target = project / ".venv/lib/python-test/site-packages/sitecustomize.pyc"
        target.write_bytes(b"legacy bytecode")
        target.chmod(0o600)
    elif mutation == "valid_pycache":
        target = (
            project
            / ".venv/lib/python-test/site-packages/injected/__pycache__/module.cpython-314.pyc"
        )
        target.parent.mkdir(parents=True, mode=0o700)
        target.parent.parent.chmod(0o700)
        target.write_bytes(b"valid-looking bytecode")
        target.chmod(0o600)
    elif mutation == "python_symlink":
        python_link = project / ".venv/bin/python"
        python_link.unlink()
        python_link.write_text("replaced symlink\n", encoding="utf-8")
        python_link.chmod(0o600)
    elif mutation == "external_python":
        python_target = (project / ".venv/bin/python").resolve(strict=True)
        python_target.write_text("#!/bin/zsh\nexit 9\n", encoding="utf-8")
        python_target.chmod(0o700)
    elif mutation == "dangling_python":
        python_link = project / ".venv/bin/python"
        python_link.unlink()
        python_link.symlink_to(tmp_path / "missing-python")
    elif mutation == "directory_python":
        python_link = project / ".venv/bin/python"
        python_link.unlink()
        python_link.symlink_to(tmp_path)
    elif mutation == "project_package":
        (project / ".venv/lib/python-test/site-packages/partsouq_catalog/runtime.py").write_text(
            "TAMPERED = True\n", encoding="utf-8"
        )
    else:
        (release / "cloak-venv/lib/python-test/site-packages/cloakbrowser/__init__.py").write_text(
            "TAMPERED = True\n", encoding="utf-8"
        )

    result = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_LAUNCHD_JOB": "1",
            "LAUNCHD_JOB": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert (
        "staged runtime integrity check failed" in result.stderr
        or "runtime symlink target is missing or not a regular file" in result.stderr
    )
    assert not execution_log.exists()


@MACOS_LAUNCH_AGENT_ONLY
@pytest.mark.parametrize("mutation", ["scheduler_config", "package", "directory"])
def test_macos_staged_runner_rejects_non_private_runtime_modes(
    tmp_path: Path,
    mutation: str,
) -> None:
    home, project, execution_log = _make_staged_runtime(tmp_path)
    release = project.parent
    if mutation == "scheduler_config":
        (release / "scheduler.env").chmod(0o644)
    elif mutation == "package":
        (project / ".venv/lib/python-test/site-packages/partsouq_catalog/runtime.py").chmod(0o660)
    else:
        (project / ".venv/lib/python-test/site-packages").chmod(0o770)

    result = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_LAUNCHD_JOB": "1",
            "LAUNCHD_JOB": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "ownership or permissions are not owner-only" in result.stderr or (
        "manifest/config must be owner-only" in result.stderr
    )
    assert not execution_log.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_staged_runner_validates_scheduler_config_before_sourcing(tmp_path: Path) -> None:
    home, project, execution_log = _make_staged_runtime(tmp_path)
    injected_marker = tmp_path / "scheduler-env-was-sourced"
    with (project.parent / "scheduler.env").open("a", encoding="utf-8") as config_file:
        config_file.write(f"/usr/bin/touch {injected_marker!s}\n")

    result = subprocess.run(
        [project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_LAUNCHD_JOB": "1",
            "LAUNCHD_JOB": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert not injected_marker.exists()
    assert not execution_log.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_install_is_tcc_safe_repeatable_and_source_independent(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, uv_log, execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"

    launch_agents = home / "Library/LaunchAgents"
    launch_agents.mkdir(parents=True)
    external_agent = tmp_path / "external-launch-agent.plist"
    external_agent.write_text("external plist must remain unchanged\n", encoding="utf-8")
    external_agent.chmod(0o644)
    agent_path = launch_agents / "com.partsouq.catalog-scheduler.plist"
    agent_path.symlink_to(external_agent)
    blocked_agent = subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked_agent.returncode == 2
    assert "refusing symlink in private runtime state path" in blocked_agent.stderr
    assert external_agent.read_text(encoding="utf-8") == "external plist must remain unchanged\n"
    assert stat.S_IMODE(external_agent.stat().st_mode) == 0o644
    assert not uv_log.exists()
    assert not execution_log.exists()
    assert not launchctl_log.exists()
    agent_path.unlink()

    state_dir = home / "Library/Application Support/partsouq-catalog"
    state_dir.mkdir(parents=True)
    external_marker = tmp_path / "external-reload-marker"
    external_marker.write_text("preserve\n", encoding="utf-8")
    external_marker.chmod(0o640)
    reload_marker = state_dir / "launch-agent-needs-reload"
    reload_marker.symlink_to(external_marker)
    blocked = subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 2
    assert "refusing symlink in private runtime state path" in blocked.stderr
    assert external_marker.read_text(encoding="utf-8") == "preserve\n"
    assert stat.S_IMODE(external_marker.stat().st_mode) == 0o640
    assert not uv_log.exists()
    assert not execution_log.exists()
    assert not launchctl_log.exists()
    reload_marker.unlink()

    installer_pid = tmp_path / "installer-active-temp.pid"
    installer_gate = tmp_path / "installer-active-temp.go"
    installer_wrapper = tmp_path / "run-installer-with-known-pid.zsh"
    _write_executable(
        installer_wrapper,
        "#!/bin/zsh\n"
        "set -eu\n"
        f"print -r -- $$ > {str(installer_pid)!r}\n"
        f"while [[ ! -e {str(installer_gate)!r} ]]; do /bin/sleep 0.01; done\n"
        'exec "$@"\n',
    )
    process = subprocess.Popen(
        [installer_wrapper, installer, "--no-start"],
        cwd=project,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not installer_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert installer_pid.exists()
    external_active = tmp_path / "external-active-agent"
    external_active.write_text("active target must remain unchanged\n", encoding="utf-8")
    external_active.chmod(0o640)
    legacy_active_temp = (
        state_dir / f"active-launch-agent.plist.{installer_pid.read_text().strip()}"
    )
    legacy_active_temp.symlink_to(external_active)
    installer_gate.touch()
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, f"{stdout}\n{stderr}"
    assert external_active.read_text(encoding="utf-8") == "active target must remain unchanged\n"
    assert stat.S_IMODE(external_active.stat().st_mode) == 0o640
    assert legacy_active_temp.is_symlink()
    legacy_active_temp.unlink()
    assert launchctl_log.read_text(encoding="utf-8").splitlines() == [
        f"print gui/{os.getuid()}/com.partsouq.catalog-scheduler"
    ]

    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    config = plistlib.loads(agent.read_bytes())
    runtime_project = Path(config["WorkingDirectory"])
    release = runtime_project.parent
    releases = home / "Library/Application Support/partsouq-catalog/releases"
    assert release.parent == releases
    assert config["ProgramArguments"] == [
        str(runtime_project / "deploy/run-macos-catalog-scheduler.zsh")
    ]
    assert str(project) not in agent.read_text(encoding="utf-8")
    assert "/Documents/" not in agent.read_text(encoding="utf-8")
    assert "/Desktop/" not in agent.read_text(encoding="utf-8")
    assert not (runtime_project / ".git").exists()
    assert not (runtime_project / ".env").exists()
    assert not (runtime_project / "tests").exists()
    assert not (runtime_project / ".github").exists()
    assert (release / ".install-complete").exists()

    scheduler_config = (release / "scheduler.env").read_text(encoding="utf-8")
    assert "PARTSOUQ_DB_NAME=partsouq_catalog" in scheduler_config
    assert "PARTSOUQ_DB_PASSWORD=db\\ secret\\ with\\ spaces" in scheduler_config
    for secret_name in (
        "PARTSOUQ_MYSQL_ROOT_PASSWORD",
        "PARTSOUQ_ADMIN_TOKEN",
        "PARTSOUQ_STATION_ADMIN_SECRET_KEY",
        "CLOAKBROWSER_LICENSE_KEY",
        "GITHUB_TOKEN",
    ):
        assert secret_name not in scheduler_config
    assert stat.S_IMODE(agent.stat().st_mode) == 0o600
    assert stat.S_IMODE((release / "scheduler.env").stat().st_mode) == 0o600
    assert stat.S_IMODE(release.stat().st_mode) == 0o700
    for private_file in (
        ".install-complete",
        ".trusted-free-runtime-v1",
        "source.sha256",
        "runtime.sha256",
        "cloak-browser.sha256",
    ):
        assert stat.S_IMODE((release / private_file).stat().st_mode) == 0o600
    for path in (release, *release.rglob("*")):
        if path.is_symlink():
            continue
        assert path.stat().st_uid == os.getuid()
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert not list((release / "app/.venv").rglob("*.py[co]"))
    assert not list((release / "cloak-venv").rglob("*.py[co]"))

    uv_calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert any(
        "sync --locked --no-dev --no-editable" in call and f"--project {runtime_project}" in call
        for call in uv_calls
    )
    assert any("pip install" in call and "--require-hashes" in call for call in uv_calls)
    assert all(".install." not in call for call in uv_calls if "sync " in call or "venv " in call)

    subprocess.run(
        [installer],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [installer],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("bootstrap ") for call in calls) == 1
    assert not any(call.startswith("bootout ") for call in calls)
    assert launchctl_state.exists()
    assert len(list(releases.iterdir())) == 1

    moved_source = tmp_path / "source-moved-away"
    project.rename(moved_source)
    runner_environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHD_JOB": "1",
        "LAUNCHD_JOB": "1",
    }
    result = subprocess.run(
        [runtime_project / "deploy/run-macos-catalog-scheduler.zsh"],
        cwd=tmp_path,
        env=runner_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    expected_executions = ["partsouq-scheduler"]
    assert execution_log.read_text(encoding="utf-8").splitlines() == expected_executions * 2


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_fresh_install_replaces_untrusted_free_cache_without_network(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, _launchctl_state = _make_fake_launchctl(tmp_path)
    cache = home / "Library/Application Support/partsouq-catalog/cloak/free-browser-cache"
    untrusted_browser = cache / "chromium-test/fake-chromium"
    _write_executable(untrusted_browser, "#!/bin/zsh\nexit 91\n")
    (cache / "latest_version").write_text("chromium-test\n", encoding="utf-8")
    (cache / "latest_version_darwin-arm64").write_text("chromium-test\n", encoding="utf-8")
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"

    subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert untrusted_browser.read_text(encoding="utf-8") == "#!/bin/zsh\nexit 0\n"
    quarantine = home / "Library/Application Support/partsouq-catalog/quarantine"
    assert list(quarantine.glob("cloak-free-replaced-*/chromium-test/fake-chromium"))
    assert list(quarantine.glob("cloak-free-replaced-*/latest_version"))
    assert list(quarantine.glob("cloak-free-replaced-*/latest_version_darwin-arm64"))
    ensure_calls = [
        line
        for line in uv_log.read_text(encoding="utf-8").splitlines()
        if line.startswith("ensure-")
    ]
    assert len(ensure_calls) == 1
    assert "/.install." in ensure_calls[0]

    subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    ensure_calls_after_reuse = [
        line
        for line in uv_log.read_text(encoding="utf-8").splitlines()
        if line.startswith("ensure-")
    ]
    assert ensure_calls_after_reuse == ensure_calls
    releases = home / "Library/Application Support/partsouq-catalog/releases"
    assert len(list(releases.iterdir())) == 1


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_rejects_pro_artifact_created_by_fresh_downloader(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, uv_log, _execution_log = _make_fake_uv(tmp_path, download_pro_artifact=True)

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "Pro artifacts" in result.stderr
    assert any(
        line.startswith("ensure-cache=") for line in uv_log.read_text(encoding="utf-8").splitlines()
    )
    assert not (home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist").exists()
    releases = home / "Library/Application Support/partsouq-catalog/releases"
    assert list(releases.iterdir()) == []


@MACOS_LAUNCH_AGENT_ONLY
@pytest.mark.parametrize(
    ("field", "value"),
    [("state", "waiting"), ("program", "/wrong/runtime/runner"), ("pid", "current")],
)
def test_macos_catalog_readiness_requires_live_launchctl_identity_each_round(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    reported = launchctl_state.with_name(f"launchctl.reported.{field}")
    reported.write_text(f"{os.getpid() if value == 'current' else value}\n", encoding="utf-8")

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
            "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "3",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "child readiness failed" in result.stderr
    assert not launchctl_state.exists()
    assert (
        sum(
            line.startswith("print ")
            for line in launchctl_log.read_text(encoding="utf-8").splitlines()
        )
        >= 4
    )


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_readiness_timeout_restores_loaded_plist(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"

    subprocess.run(
        [installer],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    original = agent.read_bytes()
    assert launchctl_state.exists()

    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("PSQ_WORKERS=5\n")
    _make_fake_uv(tmp_path, scheduler_ready=False)
    failing_environment = {**environment, "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "3"}
    installer_pid = tmp_path / "installer-rollback.pid"
    installer_gate = tmp_path / "installer-rollback.go"
    installer_wrapper = tmp_path / "run-rollback-with-known-pid.zsh"
    _write_executable(
        installer_wrapper,
        "#!/bin/zsh\n"
        "set -eu\n"
        f"print -r -- $$ > {str(installer_pid)!r}\n"
        f"while [[ ! -e {str(installer_gate)!r} ]]; do /bin/sleep 0.01; done\n"
        'exec "$@"\n',
    )
    process = subprocess.Popen(
        [installer_wrapper, installer],
        cwd=project,
        env=failing_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not installer_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert installer_pid.exists()
    external_rollback = tmp_path / "external-rollback-agent"
    external_rollback.write_text("rollback target must remain unchanged\n", encoding="utf-8")
    external_rollback.chmod(0o640)
    legacy_rollback_temp = agent.with_name(
        f"{agent.name}.rollback.{installer_pid.read_text().strip()}"
    )
    legacy_rollback_temp.symlink_to(external_rollback)
    installer_gate.touch()
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 1, stdout
    assert "child readiness failed" in stderr
    assert external_rollback.read_text(encoding="utf-8") == (
        "rollback target must remain unchanged\n"
    )
    assert stat.S_IMODE(external_rollback.stat().st_mode) == 0o640
    assert legacy_rollback_temp.is_symlink()
    legacy_rollback_temp.unlink()
    assert agent.read_bytes() == original
    assert launchctl_state.exists()
    releases = home / "Library/Application Support/partsouq-catalog/releases"
    assert len(list(releases.iterdir())) == 2
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("bootout ") for call in calls) >= 2
    assert sum(call.startswith("bootstrap ") for call in calls) >= 3


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_rollback_prefers_the_plist_actually_loaded_after_login(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    release_a = agent.read_bytes()

    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("PSQ_WORKERS=5\n")
    subprocess.run(
        [installer, "--no-start"], cwd=project, env=environment, check=True, capture_output=True
    )
    release_b = agent.read_bytes()
    assert release_b != release_a

    service = f"gui/{os.getuid()}/com.partsouq.catalog-scheduler"
    domain = f"gui/{os.getuid()}"
    subprocess.run([launchctl, "bootout", service], check=True)
    subprocess.run([launchctl, "bootstrap", domain, agent], check=True)
    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("PSQ_WORKERS=6\n")
    _make_fake_uv(tmp_path, scheduler_ready=False)

    result = subprocess.run(
        [installer],
        cwd=project,
        env={**environment, "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "3"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert agent.read_bytes() == release_b
    assert agent.read_bytes() != release_a
    loaded_program = Path(f"{launchctl_state}.program").read_text(encoding="utf-8").strip()
    assert loaded_program == plistlib.loads(release_b)["ProgramArguments"][0]


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_restarts_unhealthy_unchanged_launch_agent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    runtime = Path(plistlib.loads(agent.read_bytes())["WorkingDirectory"]).parent
    ready_marker = (
        home
        / "Library/Application Support/partsouq-catalog/scheduler"
        / f"launch-ready-{runtime.name}"
    )

    ready_marker.write_text("999999\n", encoding="utf-8")
    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)
    live_pid = int(launchctl_state.read_text(encoding="utf-8"))
    os.kill(live_pid, 15)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(live_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)

    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)

    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("bootstrap ") for call in calls) == 3
    assert sum(call.startswith("bootout ") for call in calls) == 2
    assert int(launchctl_state.read_text(encoding="utf-8")) != live_pid


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_refuses_loaded_agent_without_program_identity(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    original = agent.read_bytes()
    Path(f"{launchctl_state}.program").write_text("\n", encoding="utf-8")

    result = subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "did not report its program" in result.stderr
    assert agent.read_bytes() == original
    assert launchctl_state.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_refuses_unknown_loaded_program_before_bootout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    original = agent.read_bytes()
    Path(f"{launchctl_state}.program").write_text("/unknown/runtime/runner\n", encoding="utf-8")
    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("PSQ_WORKERS=5\n")
    before = launchctl_log.read_text(encoding="utf-8").splitlines()

    result = subprocess.run(
        [installer],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "does not match a recoverable plist" in result.stderr
    assert agent.read_bytes() == original
    after = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("bootout ") for call in after[len(before) :])
    assert launchctl_state.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_install_failure_keeps_existing_plist(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path, fail_sync=True)
    launchctl, _launchctl_log, _launchctl_state = _make_fake_launchctl(tmp_path)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    agent.parent.mkdir(parents=True)
    original = b"existing-plist-must-survive"
    agent.write_bytes(original)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
    }

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 9
    assert agent.read_bytes() == original
    releases = home / "Library/Application Support/partsouq-catalog/releases"
    assert list(releases.iterdir()) == []


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_archive_is_pinned_to_resolved_commit_during_head_race(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    initial_commit = subprocess.check_output(
        ["git", "-C", project, "rev-parse", "HEAD"], text=True
    ).strip()
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, _launchctl_state = _make_fake_launchctl(tmp_path)
    git_wrapper = tmp_path / "git-head-race"
    race_marker = "HEAD_RACE_MUST_NOT_ENTER_RESOLVED_RELEASE"
    _write_executable(
        git_wrapper,
        "#!/bin/zsh\n"
        "set -eu\n"
        'if [[ "$*" == *" archive "* ]]; then\n'
        f"  print -r -- {race_marker!r} >> {str(project / 'README.md')!r}\n"
        f"  /usr/bin/git -C {str(project)!r} add README.md\n"
        f"  /usr/bin/git -C {str(project)!r} -c user.name=RaceTest "
        "-c user.email=race@example.invalid commit -qm race\n"
        "fi\n"
        'exec /usr/bin/git "$@"\n',
    )

    subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_GIT_BIN": str(git_wrapper),
            "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert subprocess.check_output(
        ["git", "-C", project, "rev-parse", "HEAD"], text=True
    ).strip() != (initial_commit)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    runtime_project = Path(plistlib.loads(agent.read_bytes())["WorkingDirectory"])
    assert runtime_project.parent.name.startswith(f"{initial_commit}-")
    assert race_marker not in (runtime_project / "README.md").read_text(encoding="utf-8")


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_env_cannot_override_installer_control_variables(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write(
            "NO_START=0\n"
            "LAUNCHCTL_BIN=/must/not/run\n"
            "PROJECT_ROOT=/must/not/use\n"
            "COMMIT_SHA=must-not-use\n"
            "WORK_DIR=/must/not/use\n"
            "CONFIG_ENV=(must_not_replace_allowlist)\n"
        )
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, _launchctl_state = _make_fake_launchctl(tmp_path)

    subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert launchctl_log.read_text(encoding="utf-8").splitlines() == [
        f"print gui/{os.getuid()}/com.partsouq.catalog-scheduler"
    ]
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    assert agent.exists()
    scheduler_config = Path(plistlib.loads(agent.read_bytes())["WorkingDirectory"]).parent / (
        "scheduler.env"
    )
    assert "PARTSOUQ_DB_NAME=partsouq_catalog" in scheduler_config.read_text(encoding="utf-8")
    assert "must_not_replace_allowlist" not in scheduler_config.read_text(encoding="utf-8")


@MACOS_LAUNCH_AGENT_ONLY
@pytest.mark.parametrize(
    "mutation",
    [
        "new_pth",
        "legacy_pyc",
        "valid_pycache",
        "python_symlink",
        "scheduler_mode",
        "missing_cloak_venv",
    ],
)
def test_macos_catalog_installer_does_not_reuse_mutated_or_incomplete_release(
    tmp_path: Path,
    mutation: str,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, _launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run(
        [installer, "--no-start"], cwd=project, env=environment, check=True, capture_output=True
    )
    releases = home / "Library/Application Support/partsouq-catalog/releases"
    original_release = next(releases.iterdir())
    if mutation == "new_pth":
        target = original_release / "app/.venv/lib/python-test/site-packages/injected.pth"
        target.write_text("/tmp/injected\n", encoding="utf-8")
        target.chmod(0o600)
    elif mutation == "legacy_pyc":
        target = original_release / "app/.venv/lib/python-test/site-packages/sitecustomize.pyc"
        target.write_bytes(b"legacy bytecode")
        target.chmod(0o600)
    elif mutation == "valid_pycache":
        target = (
            original_release
            / "app/.venv/lib/python-test/site-packages/injected/__pycache__/module.cpython-314.pyc"
        )
        target.parent.mkdir(parents=True, mode=0o700)
        target.parent.parent.chmod(0o700)
        target.write_bytes(b"valid-looking bytecode")
        target.chmod(0o600)
    elif mutation == "python_symlink":
        python_link = original_release / "app/.venv/bin/python"
        python_link.unlink()
        python_link.write_text("replaced symlink\n", encoding="utf-8")
        python_link.chmod(0o600)
    elif mutation == "scheduler_mode":
        (original_release / "scheduler.env").chmod(0o644)
    else:
        shutil.rmtree(original_release / "cloak-venv")

    subprocess.run(
        [installer, "--no-start"], cwd=project, env=environment, check=True, capture_output=True
    )

    all_releases = list(releases.iterdir())
    assert len(all_releases) == 2
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    active_runtime = Path(plistlib.loads(agent.read_bytes())["WorkingDirectory"]).parent
    assert active_runtime != original_release


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_installer_quarantines_and_repairs_corrupt_free_browser(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, _launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run(
        [installer, "--no-start"], cwd=project, env=environment, check=True, capture_output=True
    )
    releases = home / "Library/Application Support/partsouq-catalog/releases"
    release = next(releases.iterdir())
    browser_manifest = (release / "cloak-browser.sha256").read_text(encoding="utf-8")
    expected_sha, browser_name = browser_manifest.strip().split("  ", 1)
    browser = Path(browser_name)
    browser.write_text("corrupt browser\n", encoding="utf-8")
    unrelated = browser.parent.parent / "unrelated-cache"
    unrelated.write_text("keep me\n", encoding="utf-8")

    result = subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "quarantined corrupt free CloakBrowser cache" in result.stderr
    assert hashlib.sha256(browser.read_bytes()).hexdigest() == expected_sha
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"
    quarantined = list(
        (home / "Library/Application Support/partsouq-catalog/quarantine").glob(
            "cloak-free-corrupt-*/chromium-test/fake-chromium"
        )
    )
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "corrupt browser\n"
    assert len(list(releases.iterdir())) == 1


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_invalid_ready_timeout_does_not_touch_loaded_agent(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run(
        [installer],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    original_agent = agent.read_bytes()
    original_launchctl_log = launchctl_log.read_bytes()
    assert launchctl_state.exists()

    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("PSQ_WORKERS=5\n")
    result = subprocess.run(
        [installer],
        cwd=project,
        env={**environment, "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "invalid"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "must be an integer of at least 3" in result.stderr
    assert agent.read_bytes() == original_agent
    assert launchctl_log.read_bytes() == original_launchctl_log
    assert launchctl_state.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_no_start_failure_rolls_back_to_loaded_release(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    original = agent.read_bytes()

    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("PSQ_WORKERS=5\n")
    _make_fake_uv(tmp_path, scheduler_ready=False)
    subprocess.run(
        [installer, "--no-start"],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
    )
    assert agent.read_bytes() != original

    result = subprocess.run(
        [installer],
        cwd=project,
        env={**environment, "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "3"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert agent.read_bytes() == original
    assert launchctl_state.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_immediate_scheduler_exit_is_not_ready(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path, scheduler_ready=False)
    launchctl, _launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "3",
    }

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "child readiness failed" in result.stderr
    assert not (home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist").exists()
    assert not launchctl_state.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_signal_during_switch_restores_loaded_release(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, launchctl_log, launchctl_state = _make_fake_launchctl(tmp_path)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
        "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "15",
    }
    installer = project / "deploy/install-macos-catalog-scheduler.zsh"
    subprocess.run([installer], cwd=project, env=environment, check=True, capture_output=True)
    agent = home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist"
    original = agent.read_bytes()

    with (project / ".env").open("a", encoding="utf-8") as environment_file:
        environment_file.write("PSQ_WORKERS=5\n")
    _make_fake_uv(tmp_path, scheduler_ready=False)
    process = subprocess.Popen(
        [installer],
        cwd=project,
        env={**environment, "PARTSOUQ_LAUNCH_READY_TIMEOUT_SECONDS": "30"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        calls = launchctl_log.read_text(encoding="utf-8").splitlines()
        if sum(call.startswith("bootstrap ") for call in calls) >= 2:
            break
        time.sleep(0.05)
    else:
        process.kill()
        process.wait()
        pytest.fail("new LaunchAgent was not bootstrapped before the signal deadline")

    process.terminate()
    process.communicate(timeout=10)

    assert process.returncode != 0
    assert agent.read_bytes() == original
    assert launchctl_state.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_install_lock_rejects_parallel_installer(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, _uv_log, _execution_log = _make_fake_uv(tmp_path)
    launchctl, _launchctl_log, _launchctl_state = _make_fake_launchctl(tmp_path)
    state_dir = home / "Library/Application Support/partsouq-catalog"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "install.lock"
    subprocess.run(["/usr/bin/shlock", "-f", lock, "-p", str(os.getpid())], check=True)
    environment = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "PARTSOUQ_LAUNCHCTL_BIN": str(launchctl),
        "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
        "PARTSOUQ_UV_BIN": str(uv),
    }

    try:
        result = subprocess.run(
            [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
            cwd=project,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        lock.unlink()

    assert result.returncode == 75
    assert "already running" in result.stderr
    assert not (home / "Library/LaunchAgents/com.partsouq.catalog-scheduler.plist").exists()


@MACOS_LAUNCH_AGENT_ONLY
@pytest.mark.parametrize(
    "relative_path",
    [
        "home",
        "library",
        "application-support",
        "state",
        "cloak",
        "releases",
        "free-cache",
    ],
)
def test_macos_catalog_installer_rejects_symlinked_private_state_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, uv_log, _execution_log = _make_fake_uv(tmp_path)
    state = home / "Library/Application Support/partsouq-catalog"
    symlink_paths = {
        "home": home,
        "library": home / "Library",
        "application-support": home / "Library/Application Support",
        "state": state,
        "cloak": state / "cloak",
        "releases": state / "releases",
        "free-cache": state / "cloak/free-browser-cache",
    }
    symlink = symlink_paths[relative_path]
    symlink.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / f"external-{relative_path}"
    external.mkdir(mode=0o755)
    marker = external / "must-not-change"
    marker.write_text("preserve\n", encoding="utf-8")
    symlink.symlink_to(external, target_is_directory=True)

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "refusing symlink in private runtime state path" in result.stderr
    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert marker.read_text(encoding="utf-8") == "preserve\n"
    assert not uv_log.exists()


@MACOS_LAUNCH_AGENT_ONLY
def test_macos_catalog_rejects_paid_license_in_free_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, uv_log, _execution_log = _make_fake_uv(tmp_path)
    cache = home / "Library/Application Support/partsouq-catalog/cloak/free-browser-cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "license.key").write_text("must-not-be-used", encoding="utf-8")

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "free cache containing license.key" in result.stderr
    assert not uv_log.exists()


@MACOS_LAUNCH_AGENT_ONLY
@pytest.mark.parametrize(
    ("artifact", "is_directory"),
    [
        (".license_cache", False),
        (".last_pro_version_check", False),
        (".last_pro_update_check", False),
        ("latest_pro_version_test", False),
        ("chromium-pro-test", True),
    ],
)
def test_macos_catalog_rejects_other_pro_artifacts_in_free_cache(
    tmp_path: Path,
    artifact: str,
    is_directory: bool,
) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, uv_log, _execution_log = _make_fake_uv(tmp_path)
    cache = home / "Library/Application Support/partsouq-catalog/cloak/free-browser-cache"
    target = cache / artifact
    if is_directory:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("must-not-be-used\n", encoding="utf-8")

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "Pro artifacts" in result.stderr
    assert not uv_log.exists()


@MACOS_LAUNCH_AGENT_ONLY
@pytest.mark.parametrize("name", ["SSL_CERT_DIR", "SSL_CERT_FILE"])
def test_macos_catalog_rejects_custom_tls_paths(tmp_path: Path, name: str) -> None:
    home = tmp_path / "home"
    project = _make_installable_project(tmp_path)
    uv, uv_log, _execution_log = _make_fake_uv(tmp_path)

    result = subprocess.run(
        [project / "deploy/install-macos-catalog-scheduler.zsh", "--no-start"],
        cwd=project,
        env={
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PARTSOUQ_RUNTIME_PYTHON": sys._base_executable,
            "PARTSOUQ_UV_BIN": str(uv),
            name: str(home / "Desktop/custom-certificates"),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "custom TLS setting" in result.stderr
    assert not uv_log.exists()


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
    monkeypatch.setattr(scheduler, "_record_start", lambda _job_name, _parent=None: 8)
    monkeypatch.setattr(scheduler, "_record_progress", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_record_finish", lambda *_args: None)

    def fake_run(
        job_name: str,
        command: list[str],
        *,
        parent_scheduled_job_run_id: int | None = None,
    ) -> int:
        assert parent_scheduled_job_run_id == 8
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
    monkeypatch.setattr(scheduler, "_latest_nhtsa_vin_child_output", lambda: None)
    monkeypatch.setattr(
        scheduler,
        "_finish_request",
        lambda request_id, return_code, _note=None: completed.append((request_id, return_code)),
    )
    monkeypatch.setattr(scheduler, "_run", lambda _job_name, _command, **_kwargs: 0)

    assert scheduler.dispatch("pending", "all") == 0
    assert completed == [(7, 0)]


def test_scheduler_decodes_one_supplied_vin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        scheduler,
        "_run",
        lambda job_name, command, **_kwargs: calls.append((job_name, command)) or 0,
    )

    assert scheduler.dispatch("nhtsa-vin", "ZZZTEST00X0000001") == 0
    assert calls[0][0] == "nhtsa-vin"
    assert calls[0][1][3:5] == ["nhtsa-decode-vin", "ZZZTEST00X0000001"]


def test_scheduler_redacts_vin_from_persisted_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[str] = []
    monkeypatch.setattr(scheduler, "_record_start", lambda _job_name, _parent=None: 7)
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
