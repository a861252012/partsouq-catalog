from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_direct_mypy_command_targets_source_tree() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'mypy_path = "src"' in pyproject


def test_docker_build_uses_locked_dependencies() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (PROJECT_ROOT / "compose.yml").read_text(encoding="utf-8")
    cloak_requirements = (PROJECT_ROOT / "deploy" / "requirements-cloakbrowser.txt").read_text(
        encoding="utf-8"
    )

    assert dockerfile.startswith("FROM python:3.12-slim@sha256:")
    assert "COPY deploy/requirements-cloakbrowser.txt" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "PSQ_CLOAK_PYTHON=/usr/local/bin/python" in dockerfile
    assert "uv sync --locked --no-dev --no-install-project" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    assert "pip install --no-cache-dir ." not in dockerfile
    assert "cloakbrowser==0.4.0" in cloak_requirements
    assert "--hash=sha256:" in cloak_requirements
    assert "image: mysql:8.4.11@sha256:" in compose


def test_ci_has_python_312_quality_unit_mysql_and_browser_gates() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    triggers = workflow.split("permissions:", maxsplit=1)[0]

    quality_job, remaining_jobs = workflow.split("\n  unit:\n", maxsplit=1)
    unit_job, e2e_job = remaining_jobs.split("\n  mysql-browser-e2e:\n", maxsplit=1)

    assert 'python-version: "3.12"' in quality_job
    assert "workflow_dispatch:" in triggers
    assert "push:" not in triggers
    assert "pull_request:" not in triggers
    assert "upload-artifact" not in workflow
    assert "uv run --locked mypy" in quality_job
    assert "--allow-message" in unit_job
    assert "--expected-count 170" in unit_job
    assert "-W error" in unit_job
    assert "mysql:8.4.11@sha256:" in e2e_job
    assert 'MYSQL_ROOT_HOST: "%"' in e2e_job
    assert 'UNIFIED_TEST_MYSQL: "1"' in e2e_job
    assert 'STATION_ADMIN_E2E: "1"' in e2e_job
    assert "-W error" in e2e_job
    assert "playwright install --with-deps chromium" in e2e_job
    assert "--expected-count 56" in e2e_job
    assert (
        "uv run --locked python scripts/ci_assert_pytest_skips.py "
        "artifacts/mysql-browser-e2e.xml" in e2e_job
    )
    assert "outputs/station_admin_e2e" not in e2e_job


def test_skip_gate_allows_only_documented_messages(tmp_path: Path) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuites><testsuite><testcase><skipped message="set UNIFIED_TEST_MYSQL=1 '
        'to run gate" /></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "ci_assert_pytest_skips.py"),
            str(report),
            "--allow-message",
            "set UNIFIED_TEST_MYSQL=1 to run gate",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_skip_gate_rejects_unexpected_skip(tmp_path: Path) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuites><testsuite><testcase><skipped message="missing browser" />'
        "</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "ci_assert_pytest_skips.py"),
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "missing browser" in result.stderr


def test_skip_gate_rejects_unlisted_message_with_allowed_prefix(tmp_path: Path) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuites><testsuite><testcase><skipped message="set UNIFIED_TEST_MYSQL=1 '
        'because Chromium is missing" /></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "ci_assert_pytest_skips.py"),
            str(report),
            "--allow-message",
            "set UNIFIED_TEST_MYSQL=1 to run gate",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "because Chromium is missing" in result.stderr


def test_skip_gate_rejects_documented_skip_count_drift(tmp_path: Path) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        '<testsuites><testsuite><testcase><skipped message="documented" />'
        "</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "ci_assert_pytest_skips.py"),
            str(report),
            "--allow-message",
            "documented",
            "--expected-count",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Expected 2 pytest skip(s), found 1" in result.stderr
