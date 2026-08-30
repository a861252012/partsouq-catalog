from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from partsouq_catalog.admission import release_catalog_writer_admission
from partsouq_catalog.migrations import (
    ACTIVE_VERSIONS,
    CATALOG_MANIFEST,
    STATION_ADMIN_ASSET,
    STATION_ADMIN_ASSET_HASHES,
    SUPERSEDED_VERSIONS,
    CatalogMigrationRunner,
    MigrationError,
    SchemaChange,
    _normalize_http_diagnostic_check_clause,
    _normalize_nhtsa_check_clause,
    _repairable_stale_nhtsa_runs,
    load_schema_changes,
    split_mysql_script,
    validate_ledger,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "catalog"
STATION_SCHEMA = PROJECT_ROOT / "db" / "station_admin.sql"


def test_migration_module_entrypoint_exposes_cli_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "partsouq_catalog.migrations", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Apply or verify catalog schema migrations" in completed.stdout


def test_admission_release_query_failure_closes_owner_connection() -> None:
    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        release_catalog_writer_admission(connection, "partsouq:test")

    connection.close.assert_called_once_with()


def test_repairable_stale_nhtsa_runs_rejects_child_started_after_parent_finished(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_table_exists(_connection: object, _table: str) -> bool:
        return True

    def fake_column_exists(_connection: object, table: str, column: str) -> bool:
        return table in {"nhtsa_sync_runs", "scheduled_job_runs"} and column in {
            "trigger_mode",
            "started_at",
            "finished_at",
            "exit_code",
            "run_key",
            "updated_at",
            "scheduled_job_run_id",
            "lease_slot",
            "lease_token",
            "heartbeat_at",
            "lease_expires_at",
            "parent_scheduled_job_run_id",
        }

    monkeypatch.setattr("partsouq_catalog.migrations._table_exists", fake_table_exists)
    monkeypatch.setattr("partsouq_catalog.migrations._column_exists", fake_column_exists)

    connection = mock.MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value

    def fake_fetchall() -> list[dict[str, object]]:
        return [{"id": 1, "run_key": "nhtsa-bulk-20260101T000000Z"}]

    cursor.fetchall.side_effect = fake_fetchall

    def capture(statement: str, params: object = None) -> None:
        if (
            "JOIN scheduled_job_runs AS jobs" in statement
            and "WHERE BINARY runs.status=BINARY 'running'" in statement
        ):
            captured["statement"] = statement
            captured["params"] = params
        return None

    cursor.execute.side_effect = capture

    with pytest.raises(KeyError):
        _repairable_stale_nhtsa_runs(connection, 900)
    assert "statement" in captured
    statement = str(captured["statement"])
    assert "jobs.started_at<=parent.finished_at" in statement or (
        "jobs.started_at <= parent.finished_at" in statement
    ), "failed parent recovery must reject children started after parent finished"


def test_migration_lock_timeout_cannot_wait_forever() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CatalogMigrationRunner(lock_timeout_seconds=-1)


@pytest.mark.parametrize("charset", ("ascii", "latin1", "utf8mb4"))
def test_nhtsa_check_normalizer_accepts_expected_literal_charsets(charset: str) -> None:
    clause = f"cast(_{charset}\\'writer\\' as char charset binary)"

    assert _normalize_nhtsa_check_clause(clause) == "binary 'writer'"


def test_nhtsa_check_normalizer_does_not_accept_unlisted_literal_charset() -> None:
    clause = "cast(_binary\\'writer\\' as char charset binary)"

    assert _normalize_nhtsa_check_clause(clause) == "binary _binary'writer'"


def test_http_diagnostic_check_normalizer_accepts_mysql_literal_charset_drift() -> None:
    latin1 = (
        "(json_valid(`parser_context_json`) and "
        "(not(regexp_like(lower(cast(`parser_context_json` as char charset latin1)),"
        "_latin1\\'ssd=\\'))))"
    )
    utf8mb4 = (
        "(json_valid(`parser_context_json`) and "
        "(not(regexp_like(lower(cast(`parser_context_json` as char charset utf8mb4)),"
        "_utf8mb4\\'ssd=\\'))))"
    )

    assert _normalize_http_diagnostic_check_clause(latin1) == (
        _normalize_http_diagnostic_check_clause(utf8mb4)
    )


def test_catalog_manifest_hashes_and_parser_cover_every_statement() -> None:
    changes = load_schema_changes(MIGRATIONS_DIR, STATION_SCHEMA)
    migrations = [change for change in changes if change.kind == "migration"]

    assert tuple(change.version for change in migrations if change.active) == ACTIVE_VERSIONS
    assert tuple(change.version for change in migrations if not change.active) == (
        SUPERSEDED_VERSIONS
    )
    assert [len(change.statements) for change in migrations] == [
        55,
        27,
        17,
        71,
        89,
        8,
        106,
        50,
        32,
        10,
        1,
        10,
        10,
        10,
        10,
        12,
        10,
        4,
        11,
        10,
        10,
        10,
        10,
        10,
        10,
        3,
        3,
        3,
        10,
        12,
        11,
        16,
        12,
        11,
        11,
    ]
    assert sum(len(change.statements) for change in migrations if change.active) == 675
    assert changes[-1].key == STATION_ADMIN_ASSET[0]
    assert changes[-1].statements


def test_docker_services_gate_schema_without_enabling_migration_or_scheduler(
    tmp_path: Path,
) -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (PROJECT_ROOT / "deploy" / "checked-entrypoint.sh").read_text(encoding="utf-8")
    shutil.copy(PROJECT_ROOT / "compose.yml", tmp_path / "compose.yml")
    (tmp_path / ".env").write_text(
        "PARTSOUQ_DB_PASSWORD=test-app-password\n"
        "PARTSOUQ_MYSQL_ROOT_PASSWORD=test-root-password\n"
        "PARTSOUQ_ADMIN_TOKEN=test-admin-token\n"
        "PARTSOUQ_STATION_ADMIN_SECRET_KEY=test-station-secret\n"
        "PARTSOUQ_STATION_ADMIN_USERNAME=test-station-user\n"
        "PARTSOUQ_STATION_ADMIN_PASSWORD=test-station-password\n",
        encoding="utf-8",
    )

    def compose_config(*args: str) -> str:
        return subprocess.run(
            ["docker", "compose", "-f", "compose.yml", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    assert "COPY migrations ./migrations" in dockerfile
    assert "partsouq-checked-entrypoint" in dockerfile
    assert "partsouq-catalog-migrate check" in entrypoint
    assert 'exec "$@"' in entrypoint

    assert set(compose_config("config", "--services").splitlines()) == {
        "mysql",
        "admin",
        "station-admin",
    }
    default_model = json.loads(compose_config("config", "--format", "json"))["services"]
    assert default_model["mysql"]["command"] == ["--log-bin-trust-function-creators=1"]
    assert default_model["admin"]["entrypoint"] == ["partsouq-checked-entrypoint"]
    assert default_model["station-admin"]["entrypoint"] == ["partsouq-checked-entrypoint"]
    migration_model = json.loads(
        compose_config("--profile", "migration", "config", "--format", "json")
    )["services"]
    assert not {"scheduler", "nhtsa-scheduler", "queue-scheduler"} & migration_model.keys()
    migration = migration_model["schema-migrate"]
    assert migration["profiles"] == ["migration"]
    assert migration.get("entrypoint") is None
    assert migration["command"] == ["partsouq-catalog-migrate", "apply"]
    assert migration["depends_on"] == {"mysql": {"condition": "service_healthy", "required": True}}

    scheduler_model = json.loads(
        compose_config("--profile", "scheduler", "config", "--format", "json")
    )["services"]
    assert "schema-migrate" not in scheduler_model
    for service in ("scheduler", "nhtsa-scheduler", "queue-scheduler"):
        assert scheduler_model[service]["entrypoint"] == ["partsouq-checked-entrypoint"]

    all_services = migration_model | scheduler_model
    secret_owners = {
        "MYSQL_ROOT_PASSWORD": {"mysql"},
        "PARTSOUQ_ADMIN_TOKEN": {"admin"},
        "PARTSOUQ_STATION_ADMIN_SECRET_KEY": {"station-admin"},
        "PARTSOUQ_STATION_ADMIN_PASSWORD": {"station-admin"},
        "PARTSOUQ_DB_PASSWORD": {
            "admin",
            "station-admin",
            "schema-migrate",
            "scheduler",
            "nhtsa-scheduler",
            "queue-scheduler",
        },
    }
    for secret, expected_owners in secret_owners.items():
        owners = {
            service
            for service, model in all_services.items()
            if secret in model.get("environment", {})
        }
        assert owners == expected_owners

    catalog_environment = scheduler_model["scheduler"]["environment"]
    assert catalog_environment["PSQ_SCHEDULER_STATE_DIR"] == "/app/logs"
    assert catalog_environment["PSQ_EVIDENCE_MAX_ARTIFACTS"] == "50000"
    assert catalog_environment["PSQ_CLOAK_STATE_DIR"] == "/app/data"


def test_mysql_splitter_handles_inline_statements_procedures_and_literals() -> None:
    script = """
    PREPARE stmt FROM 'SELECT \\'semi;colon\\''; EXECUTE stmt; DEALLOCATE PREPARE stmt;
    -- DELIMITER // is only a comment
    DELIMITER //
    CREATE PROCEDURE p()
    BEGIN
      SELECT 'literal // ;', "double //", `back//tick`;
      # comment //
      SELECT 2; /* block // ; */
    END//
    DELIMITER ;
    SELECT 3;
    """

    statements = split_mysql_script(script)

    assert len(statements) == 5
    assert statements[0].startswith("PREPARE stmt")
    assert statements[1] == "EXECUTE stmt"
    assert statements[2] == "DEALLOCATE PREPARE stmt"
    assert "CREATE PROCEDURE p()" in statements[3]
    assert statements[4] == "SELECT 3"
    assert split_mysql_script("/*!80000 SET @partsouq_test=1 */;") == (
        "/*!80000 SET @partsouq_test=1 */",
    )


@pytest.mark.parametrize(
    "script,error",
    [
        ("SELECT 'unfinished;", "unclosed quote"),
        ("SELECT 1 /* unfinished;", "unclosed block comment"),
        ("SELECT 1", "incomplete statement"),
    ],
)
def test_mysql_splitter_rejects_incomplete_input(script: str, error: str) -> None:
    with pytest.raises(MigrationError, match=error):
        split_mysql_script(script)


def test_manifest_mutation_and_unknown_file_fail_before_execution(tmp_path: Path) -> None:
    migration_copy = tmp_path / "catalog"
    shutil.copytree(MIGRATIONS_DIR, migration_copy)
    first = migration_copy / CATALOG_MANIFEST[0][1]
    first.write_bytes(first.read_bytes() + b"\n")

    with pytest.raises(MigrationError, match="migration:001 checksum mismatch"):
        load_schema_changes(migration_copy, STATION_SCHEMA)

    first.write_bytes((MIGRATIONS_DIR / first.name).read_bytes())
    (migration_copy / "999_unknown.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError, match="unknown=.*999_unknown.sql"):
        load_schema_changes(migration_copy, STATION_SCHEMA)


def test_ledger_requires_ordered_active_prefix_and_explicit_dirty_retry() -> None:
    changes = _metadata_changes()
    rows = [_ledger_row(changes, version, "applied") for version in (1, 2)]
    rows.append(_ledger_row(changes, 3, "failed"))

    with pytest.raises(MigrationError, match="retry that exact version"):
        validate_ledger(rows, changes, complete=False)
    assert (
        validate_ledger(rows, changes, complete=False, retry_version=3)["migration:003"]["state"]
        == "failed"
    )

    rows.append(_ledger_row(changes, 4, "applied"))
    with pytest.raises(MigrationError, match="after dirty migration"):
        validate_ledger(rows, changes, complete=False, retry_version=3)


@pytest.mark.parametrize("version", SUPERSEDED_VERSIONS)
@pytest.mark.parametrize("state", ("applying", "failed"))
def test_superseded_dirty_migration_requires_exact_retry_and_allows_convergence(
    version: int,
    state: str,
) -> None:
    changes = _metadata_changes()
    rows = [_ledger_row(changes, active_version, "applied") for active_version in ACTIVE_VERSIONS]
    rows.append(_ledger_row(changes, version, state))

    with pytest.raises(MigrationError, match="retry that exact version"):
        validate_ledger(rows, changes, complete=False)
    with pytest.raises(MigrationError, match="retry that exact version"):
        validate_ledger(rows, changes, complete=False, retry_version=15)

    records = validate_ledger(rows, changes, complete=False, retry_version=version)
    assert records[f"migration:{version:03d}"]["state"] == state


def test_ledger_rejects_unknown_checksum_drift_and_dirty_asset() -> None:
    changes = _metadata_changes()
    unknown = _ledger_row(changes, 1, "applied") | {"change_key": "migration:099"}
    with pytest.raises(MigrationError, match="unknown change"):
        validate_ledger([unknown], changes, complete=False)

    drifted = _ledger_row(changes, 1, "applied") | {"sha256": "0" * 64}
    with pytest.raises(MigrationError, match="checksum drift"):
        validate_ledger([drifted], changes, complete=False)

    asset = next(change for change in changes if change.kind == "asset")
    arbitrary_asset = _row(asset, "applied") | {"sha256": "0" * 64}
    with pytest.raises(MigrationError, match="checksum drift"):
        validate_ledger([arbitrary_asset], changes, complete=False, allow_asset_upgrade=True)

    known_old_asset = _row(asset, "failed") | {"sha256": STATION_ADMIN_ASSET_HASHES[-2]}
    validate_ledger(
        [known_old_asset],
        changes,
        complete=False,
        retry_station_asset=True,
        allow_asset_upgrade=True,
    )

    dirty_asset = _row(asset, "failed")
    with pytest.raises(MigrationError, match="retry it explicitly"):
        validate_ledger([dirty_asset], changes, complete=False)
    validate_ledger([dirty_asset], changes, complete=False, retry_station_asset=True)


def _metadata_changes() -> tuple[SchemaChange, ...]:
    return tuple(
        SchemaChange(
            key=f"migration:{version:03d}",
            kind="migration",
            version=version,
            filename=filename,
            sha256=sha256,
            statements=(),
            active=version not in SUPERSEDED_VERSIONS,
        )
        for version, filename, sha256 in CATALOG_MANIFEST
    ) + (
        SchemaChange(
            STATION_ADMIN_ASSET[0],
            "asset",
            None,
            STATION_ADMIN_ASSET[1],
            STATION_ADMIN_ASSET[2],
            (),
        ),
    )


def _ledger_row(changes: tuple[SchemaChange, ...], version: int, state: str) -> dict[str, object]:
    change = next(change for change in changes if change.version == version)
    return _row(change, state)


def _row(change: SchemaChange, state: str) -> dict[str, object]:
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "change_key": change.key,
        "kind": change.kind,
        "version": change.version,
        "filename": change.filename,
        "sha256": change.sha256,
        "state": state,
        "attempt_count": 1,
        "started_at": now,
        "finished_at": None if state == "applying" else now,
        "error_text": "failure" if state == "failed" else None,
    }
