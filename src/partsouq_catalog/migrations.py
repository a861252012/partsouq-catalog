"""Fail-closed catalog schema migration runner."""

from __future__ import annotations

import argparse
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import pymysql
from pymysql.constants import CLIENT
from pymysql.cursors import DictCursor

from .config import BASE_DIR, DB_CONFIG

type MigrationConnection = pymysql.connections.Connection[DictCursor]
type ConnectionFactory = Callable[[], MigrationConnection]
type ChangeKind = Literal["migration", "asset"]

LEDGER_TABLE = "catalog_schema_ledger"
SUPERSEDED_VERSIONS = (13, 14)

# Published migrations are immutable. Hashes cover the exact file bytes.
# fmt: off
CATALOG_MANIFEST = (
    (1, "001_fix_nullable_unique_keys.sql", "f5c30852f641285a7b285e40cd994f7ca72b081e1e265fb4e0cd583a4a9f1a71"),
    (2, "002_monthly_run_isolation.sql", "adf466d18a916a999069b20562f52241b3f5bae786068f72b8feb9187cd75f3d"),
    (3, "003_group_receipt_columns.sql", "e930f1efc388202a0be01ccb431927bb4e5387b5e85ad6ce7e3c962f5ba8faee"),
    (4, "004_current_snapshot_and_vehicle_identity.sql", "d50c52c361f9624884c1d9730f49e59229d26ddc6fb4e1a87bf82d925b2ea2df"),
    (5, "005_vehicle_identity_v5_and_category_cid.sql", "a9a3fe9c7d6b6970ee540bf0584b20fa9c2fc4128b5b938d1a26a5da1b01ce72"),
    (6, "006_group_high_water.sql", "a6c173b4eee777163599c7f3c91878ce40027b8dfefb12498fab1e85be2ea5c8"),
    (7, "007_unified_vin_mapping.sql", "678a8ee1c17abe408d02c152ff74d0810d978394d480f34100e47e31786749d0"),
    (8, "008_admin_source_ids.sql", "14d26e1553d90205f356856a38261cd3b8acbe917eb7271ca486872d6c707be6"),
    (9, "009_bounded_production_dataset.sql", "6aa35e72a6b5d4dc3bce1c0dad58266406bc6918965cb69bae1ddef222625bc3"),
    (10, "010_group_uid_identity.sql", "043601d84cd39b1cc75650940012adbc5535ac6360c59e6c54247e80c5e69cb3"),
    (11, "011_part_quarantine.sql", "6640ad784cc61350d6c72d840dbc98a12086d9fd0475e02e5c6124a2e81a3fee"),
    (12, "012_part_quarantine_resolution.sql", "2b1d6da3ed387dbc215a7032667291c8c75db05ae56ac0f46c12575a42b21280"),
    (13, "013_part_quarantine_run_key_updated_index.sql", "a131023995543a4408f415b9d518e6455c65a5abff27c8479ff4c3cfe0ee9d10"),
    (14, "014_part_quarantine_run_key_resolved_updated_index.sql", "1bf0828d5a5cf0ecf9510e57cc14b8daad583b3f372795ba7bb8b870c3ad41a5"),
    (15, "015_quarantine_index_contract_cleanup.sql", "f6018b0d488462cb9c9ec229975b8a22c49208786164c4cc21d289027c5c80af"),
    (16, "016_published_snapshot_provenance.sql", "1d48e38a7af8e95984fb41e4ae66207c1c836da5f0a48825b96feb4a5e8302d3"),
    (17, "017_partsouq_http_evidence.sql", "21d4fede707821d89b61bc103447be2128884e0581d98fab93956b21c196a19f"),
    (18, "018_superseded_routine_cleanup.sql", "73a61c858931652db346dfc2c6a276407af77c6db3b6720e63617a3e037defb1"),
    (19, "019_verified_bounded_catalog_view.sql", "a65d8375fc2b9d9f257558b980ddd7292c928e0c03f2ad09b99aab565c0a0e65"),
    (20, "020_artifact_sanitizer_version.sql", "03fae175dfb027353874bdb796ed7dd751eafbb0a10e6558a7412cff038c4877"),
    (21, "021_exact_artifact_sanitizer_contract.sql", "46a974719bde384e9853660181b658cadbdbe4559cb4bae4e4597a8bf3332873"),
    (22, "022_group_receipt_run_key_index.sql", "41303ad2d06bbe682655bb276893893aa4f2a3d5f817bc34275b406920cd47fd"),
)
# fmt: on
ACTIVE_VERSIONS = tuple(
    version for version, _, _ in CATALOG_MANIFEST if version not in SUPERSEDED_VERSIONS
)
STATION_ADMIN_ASSET_HASHES = (
    "5e566d9b931be3d658a78dbf2636fad53500dcbb33641671dd85b12ad7c4fd02",
    "4745c64212cb9cd3d0c7a7bd2579dd443d6183fc906558efa05c857d67a754c1",
    "a42990b80a047f4ae94e211050c0bfd9bdd5fc7078aaf0b2b7d6effdcc2fdda7",
)
STATION_ADMIN_ASSET = (
    "asset:station-admin",
    "station_admin.sql",
    STATION_ADMIN_ASSET_HASHES[-1],
)

_DELIMITER_DIRECTIVE = re.compile(r"\s*DELIMITER\s+(\S+)\s*", re.IGNORECASE)
_CREATE_PROCEDURE = re.compile(
    r"\bCREATE\s+PROCEDURE\s+`?([A-Za-z0-9_]+)`?",
    re.IGNORECASE,
)
_DROP_PROCEDURE = re.compile(
    r"\bDROP\s+PROCEDURE(?:\s+IF\s+EXISTS)?\s+`?([A-Za-z0-9_]+)`?",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    """The migration manifest, ledger, or execution is unsafe."""


@dataclass(frozen=True, slots=True)
class SchemaChange:
    key: str
    kind: ChangeKind
    version: int | None
    filename: str
    sha256: str
    statements: tuple[str, ...]
    active: bool = True


def split_mysql_script(script: str) -> tuple[str, ...]:
    """Split mysql-client SQL without enabling CLIENT.MULTI_STATEMENTS."""
    statements: list[str] = []
    buffer: list[str] = []
    delimiter = ";"
    quote: str | None = None
    line_comment = False
    block_comment = False
    has_sql = False
    index = 0
    line_start = 0

    while index < len(script):
        char = script[index]
        following = script[index + 1] if index + 1 < len(script) else ""

        if index == line_start and quote is None and not block_comment:
            line_end = script.find("\n", index)
            if line_end == -1:
                line_end = len(script)
            line = script[index:line_end].rstrip("\r")
            directive = _DELIMITER_DIRECTIVE.fullmatch(line)
            if directive is not None:
                if has_sql:
                    raise MigrationError("DELIMITER directive appears inside a statement")
                delimiter = directive.group(1)
                buffer.clear()
                index = line_end + (line_end < len(script))
                line_start = index
                line_comment = False
                continue

        if line_comment:
            buffer.append(char)
            index += 1
            if char == "\n":
                line_comment = False
                line_start = index
            continue
        if block_comment:
            buffer.append(char)
            if char == "*" and following == "/":
                buffer.append(following)
                index += 2
                block_comment = False
            else:
                index += 1
            if char == "\n":
                line_start = index
            continue
        if quote is not None:
            buffer.append(char)
            if char == "\\" and following:
                buffer.append(following)
                index += 2
                continue
            if char == quote and following == quote:
                buffer.append(following)
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            if char == "\n":
                line_start = index
            continue

        if char == "-" and following == "-" and _starts_dash_comment(script, index):
            buffer.extend((char, following))
            index += 2
            line_comment = True
            continue
        if char == "#":
            buffer.append(char)
            index += 1
            line_comment = True
            continue
        if char == "/" and following == "*":
            buffer.extend((char, following))
            if index + 2 < len(script) and script[index + 2] == "!":
                has_sql = True
            index += 2
            block_comment = True
            continue
        if char in "'\"`":
            quote = char
            buffer.append(char)
            has_sql = True
            index += 1
            continue
        if script.startswith(delimiter, index):
            if has_sql:
                statements.append("".join(buffer).strip())
            buffer.clear()
            has_sql = False
            index += len(delimiter)
            continue

        buffer.append(char)
        has_sql = has_sql or not char.isspace()
        index += 1
        if char == "\n":
            line_start = index

    if quote is not None:
        raise MigrationError("SQL script ended with an unclosed quote")
    if block_comment:
        raise MigrationError("SQL script ended with an unclosed block comment")
    if has_sql:
        raise MigrationError("SQL script ended with an incomplete statement")
    return tuple(statements)


def _starts_dash_comment(script: str, index: int) -> bool:
    following = index + 2
    return following >= len(script) or script[following].isspace()


def load_schema_changes(
    migrations_dir: Path,
    station_schema_path: Path,
) -> tuple[SchemaChange, ...]:
    """Hash and parse every immutable input before opening MySQL."""
    versions = tuple(version for version, _, _ in CATALOG_MANIFEST)
    filenames = tuple(filename for _, filename, _ in CATALOG_MANIFEST)
    if versions != tuple(range(1, len(CATALOG_MANIFEST) + 1)) or len(set(filenames)) != len(
        filenames
    ):
        raise MigrationError("catalog migration manifest must be consecutive and unique")
    if not set(SUPERSEDED_VERSIONS).issubset(versions):
        raise MigrationError("catalog migration manifest is missing superseded history")

    expected_files = set(filenames)
    actual_files = {path.name for path in migrations_dir.glob("*.sql")}
    if actual_files != expected_files:
        raise MigrationError(
            "catalog migration files differ from manifest: "
            f"missing={sorted(expected_files - actual_files)}, "
            f"unknown={sorted(actual_files - expected_files)}"
        )

    changes = [
        _load_change(
            path=migrations_dir / filename,
            key=f"migration:{version:03d}",
            kind="migration",
            version=version,
            expected_sha256=sha256,
            active=version not in SUPERSEDED_VERSIONS,
        )
        for version, filename, sha256 in CATALOG_MANIFEST
    ]
    asset_key, asset_filename, asset_sha256 = STATION_ADMIN_ASSET
    if station_schema_path.name != asset_filename:
        raise MigrationError("station-admin schema asset filename mismatch")
    changes.append(
        _load_change(
            path=station_schema_path,
            key=asset_key,
            kind="asset",
            version=None,
            expected_sha256=asset_sha256,
        )
    )
    return tuple(changes)


def _load_change(
    *,
    path: Path,
    key: str,
    kind: ChangeKind,
    version: int | None,
    expected_sha256: str,
    active: bool = True,
) -> SchemaChange:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise MigrationError(f"{key} checksum mismatch")
    try:
        script = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationError(f"{key} is not UTF-8") from exc
    return SchemaChange(
        key, kind, version, path.name, expected_sha256, split_mysql_script(script), active
    )


def validate_ledger(
    rows: Sequence[Mapping[str, object]],
    changes: Sequence[SchemaChange],
    *,
    complete: bool,
    retry_version: int | None = None,
    retry_station_asset: bool = False,
    allow_asset_upgrade: bool = False,
) -> dict[str, Mapping[str, object]]:
    expected = {change.key: change for change in changes}
    records: dict[str, Mapping[str, object]] = {}
    dirty_superseded: list[int] = []
    for row in rows:
        key = row.get("change_key")
        if not isinstance(key, str) or key not in expected:
            raise MigrationError(f"ledger contains unknown change {key!r}")
        if key in records:
            raise MigrationError(f"ledger contains duplicate change {key}")
        change = expected[key]
        if (
            row.get("kind") != change.kind
            or row.get("version") != change.version
            or row.get("filename") != change.filename
        ):
            raise MigrationError(f"ledger metadata drift for {key}")
        state = row.get("state")
        if state not in ("applying", "applied", "failed"):
            raise MigrationError(f"ledger state is invalid for {key}")
        attempt_count = row.get("attempt_count")
        if not isinstance(attempt_count, int) or attempt_count < 1:
            raise MigrationError(f"ledger attempt count is invalid for {key}")
        finished_at = row.get("finished_at")
        if (state == "applying") != (finished_at is None):
            raise MigrationError(f"ledger timestamps are invalid for {key}")
        checksum_matches = row.get("sha256") == change.sha256
        asset_upgrade = (
            change.kind == "asset"
            and allow_asset_upgrade
            and row.get("sha256") in STATION_ADMIN_ASSET_HASHES
        )
        if not checksum_matches and not asset_upgrade:
            raise MigrationError(f"ledger checksum drift for {key}")
        if not change.active and state != "applied":
            if change.version is None:
                raise MigrationError(f"superseded {key} has no migration version")
            dirty_superseded.append(change.version)
        records[key] = row

    gap = False
    dirty_version: int | None = None
    for version in ACTIVE_VERSIONS:
        key = f"migration:{version:03d}"
        active_record = records.get(key)
        if active_record is None:
            gap = True
            continue
        if gap:
            raise MigrationError(f"ledger skips an active migration before {version:03d}")
        if dirty_version is not None:
            raise MigrationError(
                f"ledger contains migration {version:03d} after dirty migration {dirty_version:03d}"
            )
        if active_record["state"] != "applied":
            dirty_version = version

    if dirty_version is not None and dirty_superseded:
        raise MigrationError("ledger contains more than one dirty migration")
    if len(dirty_superseded) > 1:
        raise MigrationError("ledger contains more than one dirty superseded migration")
    dirty_superseded_version = dirty_superseded[0] if dirty_superseded else None
    if dirty_version is not None and retry_version != dirty_version:
        raise MigrationError(
            f"migration {dirty_version:03d} is dirty; retry that exact version explicitly"
        )
    if dirty_superseded_version is not None and retry_version != dirty_superseded_version:
        raise MigrationError(
            f"superseded migration {dirty_superseded_version:03d} is dirty; "
            "retry that exact version explicitly"
        )
    if dirty_version is None and dirty_superseded_version is None and retry_version is not None:
        raise MigrationError(f"migration {retry_version:03d} is not dirty")

    asset = records.get(STATION_ADMIN_ASSET[0])
    if asset is None:
        if retry_station_asset:
            raise MigrationError("station-admin schema asset is not dirty")
    else:
        asset_current = asset["sha256"] == STATION_ADMIN_ASSET[2]
        asset_dirty = asset["state"] != "applied"
        if asset_dirty and not retry_station_asset:
            raise MigrationError("station-admin schema asset is dirty; retry it explicitly")
        if not asset_dirty and retry_station_asset:
            raise MigrationError("station-admin schema asset is not dirty")
        if complete and (asset_dirty or not asset_current):
            raise MigrationError("station-admin schema asset is incomplete")

    if complete:
        missing = [
            version
            for version in ACTIVE_VERSIONS
            if records.get(f"migration:{version:03d}", {}).get("state") != "applied"
        ]
        if missing:
            raise MigrationError(
                "active catalog migrations are incomplete: "
                + ", ".join(f"{version:03d}" for version in missing)
            )
        if asset is None:
            raise MigrationError("station-admin schema asset is incomplete")
    return records


class CatalogMigrationRunner:
    def __init__(
        self,
        *,
        migrations_dir: Path | None = None,
        station_schema_path: Path | None = None,
        connection_factory: ConnectionFactory | None = None,
        lock_timeout_seconds: int = 0,
    ) -> None:
        if lock_timeout_seconds < 0:
            raise ValueError("migration lock timeout must be non-negative")
        self._migrations_dir = migrations_dir or BASE_DIR / "migrations" / "catalog"
        self._station_schema_path = station_schema_path or BASE_DIR / "db" / "station_admin.sql"
        self._connection_factory = connection_factory or _connect_from_config
        self._lock_timeout_seconds = lock_timeout_seconds

    def check(self) -> None:
        changes = load_schema_changes(self._migrations_dir, self._station_schema_path)
        connection = self._connection_factory()
        try:
            _assert_connection(connection)
            _selected_database(connection)
            if not _table_exists(connection, LEDGER_TABLE):
                raise MigrationError("catalog schema ledger does not exist")
            validate_ledger(_load_ledger(connection), changes, complete=True)
            _assert_no_owned_routines(connection, changes)
        finally:
            connection.close()

    def apply(
        self,
        *,
        retry_version: int | None = None,
        allow_v5_rebuild: bool = False,
        retry_station_asset: bool = False,
    ) -> tuple[int, ...]:
        changes = load_schema_changes(self._migrations_dir, self._station_schema_path)
        connection = self._connection_factory()
        lock_name: str | None = None
        try:
            _assert_connection(connection)
            lock_name = catalog_schema_lock_name(_selected_database(connection))
            _acquire_lock(connection, lock_name, self._lock_timeout_seconds)

            if _table_exists(connection, LEDGER_TABLE):
                records = validate_ledger(
                    _load_ledger(connection),
                    changes,
                    complete=False,
                    retry_version=retry_version,
                    retry_station_asset=retry_station_asset,
                    allow_asset_upgrade=True,
                )
            else:
                if retry_version is not None or retry_station_asset:
                    raise MigrationError("cannot retry without an existing schema ledger")
                records = {}

            _assert_no_running_jobs(connection, allow_repairable_catalog=True)
            _repair_stale_catalog_runs(connection)
            _assert_no_running_jobs(connection)
            if not _table_exists(connection, LEDGER_TABLE):
                _create_ledger(connection)

            applied: list[int] = []
            retrying_superseded = retry_version in SUPERSEDED_VERSIONS
            for change in changes:
                if not change.active and change.version != retry_version:
                    continue
                existing = records.get(change.key)
                force_convergence = retrying_superseded and change.version == 15
                if (
                    existing is not None
                    and existing["state"] == "applied"
                    and existing["sha256"] == change.sha256
                    and not force_convergence
                ):
                    continue
                _claim(connection, change, existing)
                try:
                    if change.version == 5 and allow_v5_rebuild:
                        _execute(connection, "SET @PARTSOUQ_ALLOW_V5_VEHICLE_REBUILD = 1")
                    _execute_change(connection, change)
                    if change.version is not None:
                        _assert_no_owned_routines(connection, (change,))
                    _finish(connection, change)
                except Exception as exc:
                    if not _is_connection_loss(exc):
                        _fail_best_effort(connection, change, exc)
                    raise MigrationError(f"{change.key} failed") from exc
                if change.version is not None:
                    applied.append(change.version)

            _assert_no_owned_routines(connection, changes)
            validate_ledger(_load_ledger(connection), changes, complete=True)
            return tuple(applied)
        finally:
            if lock_name is not None:
                _release_lock_best_effort(connection, lock_name)
            connection.close()


def _assert_connection(connection: MigrationConnection) -> None:
    if connection.client_flag & CLIENT.MULTI_STATEMENTS:
        raise MigrationError("migration connection must not enable CLIENT.MULTI_STATEMENTS")
    if not connection.get_autocommit():
        raise MigrationError("migration connection must use autocommit")


def _selected_database(connection: MigrationConnection) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS database_name")
        row = cursor.fetchone()
    database = row.get("database_name") if row else None
    if not isinstance(database, str) or not database:
        raise MigrationError("migration connection must select an explicit database")
    return database


def _table_exists(connection: MigrationConnection, table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS row_count FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'",
            (table,),
        )
        row = cursor.fetchone()
    return bool(row and row["row_count"] == 1)


def _create_ledger(connection: MigrationConnection) -> None:
    _execute(
        connection,
        """
        CREATE TABLE catalog_schema_ledger (
          change_key VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
          kind ENUM('migration','asset') NOT NULL,
          version SMALLINT UNSIGNED NULL,
          filename VARCHAR(191) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
          state ENUM('applying','applied','failed') NOT NULL,
          attempt_count INT UNSIGNED NOT NULL,
          started_at DATETIME(6) NOT NULL,
          finished_at DATETIME(6) NULL,
          error_text VARCHAR(255) NULL,
          UNIQUE KEY uq_catalog_schema_ledger_filename (filename),
          UNIQUE KEY uq_catalog_schema_ledger_version (version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    )


def _load_ledger(connection: MigrationConnection) -> list[Mapping[str, object]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT change_key,kind,version,filename,sha256,state,attempt_count,"
            "started_at,finished_at,error_text FROM catalog_schema_ledger ORDER BY change_key"
        )
        return list(cursor.fetchall())


def _assert_no_running_jobs(
    connection: MigrationConnection, *, allow_repairable_catalog: bool = False
) -> None:
    for table in (
        "crawl_runs",
        "nhtsa_sync_runs",
        "scheduled_job_runs",
        "admin_crawl_requests",
    ):
        if not _table_exists(connection, table):
            continue
        repairable_catalog = (
            table == "crawl_runs"
            and allow_repairable_catalog
            and _table_exists(connection, "scheduled_job_runs")
            and _column_exists(connection, "crawl_runs", "scheduled_job_run_id")
        )
        with connection.cursor() as cursor:
            if repairable_catalog:
                cursor.execute(
                    "SELECT COUNT(*) AS row_count FROM crawl_runs AS runs "
                    "LEFT JOIN scheduled_job_runs AS jobs "
                    "ON jobs.id=runs.scheduled_job_run_id WHERE runs.status='running' "
                    "AND NOT (runs.scheduled_job_run_id IS NOT NULL AND jobs.id IS NOT NULL "
                    "AND jobs.job_name='catalog' AND jobs.status='failed' "
                    "AND jobs.finished_at IS NOT NULL AND jobs.exit_code IS NOT NULL "
                    "AND jobs.exit_code <> 0 AND (SELECT COUNT(*) FROM crawl_runs AS linked "
                    "WHERE linked.scheduled_job_run_id=jobs.id)=1)"
                )
            else:
                cursor.execute(
                    f"SELECT COUNT(*) AS row_count FROM `{table}` WHERE status='running'"
                )
            row = cursor.fetchone()
        if row and int(row["row_count"]) > 0:
            raise MigrationError(f"running jobs exist in {table}; stop writers before migration")


def _repair_stale_catalog_runs(connection: MigrationConnection) -> None:
    if (
        not _table_exists(connection, "crawl_runs")
        or not _table_exists(connection, "scheduled_job_runs")
        or not _column_exists(connection, "crawl_runs", "scheduled_job_run_id")
    ):
        return
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE crawl_runs AS runs "
            "JOIN scheduled_job_runs AS jobs ON jobs.id=runs.scheduled_job_run_id "
            "JOIN (SELECT scheduled_job_run_id FROM crawl_runs "
            "WHERE scheduled_job_run_id IS NOT NULL GROUP BY scheduled_job_run_id "
            "HAVING COUNT(*)=1) AS unique_links "
            "ON unique_links.scheduled_job_run_id=jobs.id "
            "SET runs.status='interrupted',"
            "runs.finished_at=COALESCE(runs.finished_at,jobs.finished_at),"
            "runs.error_msg=CONCAT_WS('\\n',NULLIF(runs.error_msg,''),"
            "'migration preflight: linked catalog scheduler failed; stale run interrupted') "
            "WHERE runs.status='running' AND runs.scheduled_job_run_id IS NOT NULL "
            "AND jobs.job_name='catalog' AND jobs.status='failed' "
            "AND jobs.finished_at IS NOT NULL AND jobs.exit_code IS NOT NULL "
            "AND jobs.exit_code <> 0"
        )


def _column_exists(connection: MigrationConnection, table: str, column: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS row_count FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (table, column),
        )
        row = cursor.fetchone()
    return bool(row and row["row_count"] == 1)


def _claim(
    connection: MigrationConnection,
    change: SchemaChange,
    existing: Mapping[str, object] | None,
) -> None:
    with connection.cursor() as cursor:
        if existing is None:
            cursor.execute(
                "INSERT INTO catalog_schema_ledger "
                "(change_key,kind,version,filename,sha256,state,attempt_count,started_at,"
                "finished_at,error_text) VALUES (%s,%s,%s,%s,%s,'applying',1,NOW(6),NULL,NULL)",
                (change.key, change.kind, change.version, change.filename, change.sha256),
            )
            return
        cursor.execute(
            "UPDATE catalog_schema_ledger SET filename=%s,sha256=%s,state='applying',"
            "attempt_count=attempt_count+1,started_at=NOW(6),finished_at=NULL,error_text=NULL "
            "WHERE change_key=%s AND sha256=%s AND state=%s",
            (
                change.filename,
                change.sha256,
                change.key,
                existing["sha256"],
                existing["state"],
            ),
        )
        if cursor.rowcount != 1:
            raise MigrationError(f"could not claim {change.key}")


def _finish(connection: MigrationConnection, change: SchemaChange) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE catalog_schema_ledger SET state='applied',finished_at=NOW(6),error_text=NULL "
            "WHERE change_key=%s AND sha256=%s AND state='applying'",
            (change.key, change.sha256),
        )
        if cursor.rowcount != 1:
            raise MigrationError(f"could not complete {change.key}")


def _fail_best_effort(
    connection: MigrationConnection, change: SchemaChange, error: Exception
) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE catalog_schema_ledger SET state='failed',finished_at=NOW(6),error_text=%s "
                "WHERE change_key=%s AND sha256=%s AND state='applying'",
                (_error_label(error), change.key, change.sha256),
            )
    except pymysql.MySQLError:
        pass


def _execute_change(connection: MigrationConnection, change: SchemaChange) -> None:
    for ordinal, statement in enumerate(change.statements, start=1):
        try:
            _execute(connection, statement)
        except pymysql.MySQLError as exc:
            errno = exc.args[0] if exc.args else "unknown"
            raise MigrationError(
                f"{change.key} statement {ordinal} failed with MySQL errno {errno}"
            ) from exc


def _execute(connection: MigrationConnection, statement: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(statement)
        while cursor.nextset():
            pass


def _assert_no_owned_routines(
    connection: MigrationConnection,
    changes: Sequence[SchemaChange],
) -> None:
    created = {
        match.group(1)
        for change in changes
        for statement in change.statements
        if (match := _CREATE_PROCEDURE.search(statement)) is not None
    }
    dropped = {
        match.group(1)
        for change in changes
        for statement in change.statements
        if (match := _DROP_PROCEDURE.search(statement)) is not None
    }
    names = sorted(created & dropped)
    if not names:
        return
    placeholders = ",".join(("%s",) * len(names))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT ROUTINE_NAME FROM information_schema.ROUTINES "
            "WHERE ROUTINE_SCHEMA=DATABASE() AND ROUTINE_TYPE='PROCEDURE' "
            f"AND ROUTINE_NAME IN ({placeholders}) "
            "ORDER BY ROUTINE_NAME",
            tuple(names),
        )
        remaining = [str(row["ROUTINE_NAME"]) for row in cursor.fetchall()]
    if remaining:
        raise MigrationError("migration-owned routines remain: " + ", ".join(remaining))


def catalog_schema_lock_name(database: str) -> str:
    digest = hashlib.sha256(database.encode()).hexdigest()[:32]
    return f"partsouq:catalog-migrations:{digest}"


def _acquire_lock(connection: MigrationConnection, name: str, timeout: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s,%s) AS acquired", (name, timeout))
        row = cursor.fetchone()
    if not row or row["acquired"] != 1:
        raise MigrationError("another catalog migration runner holds the database lock")


def _release_lock_best_effort(connection: MigrationConnection, name: str) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (name,))
    except pymysql.MySQLError:
        pass


def _is_connection_loss(error: Exception) -> bool:
    source = error.__cause__ if isinstance(error.__cause__, Exception) else error
    if isinstance(source, pymysql.err.InterfaceError):
        return True
    return (
        isinstance(source, pymysql.err.OperationalError)
        and bool(source.args)
        and source.args[0] in (2006, 2013)
    )


def _error_label(error: Exception) -> str:
    source = error.__cause__ if isinstance(error.__cause__, pymysql.MySQLError) else error
    errno = source.args[0] if isinstance(source, pymysql.MySQLError) and source.args else None
    suffix = f" errno={errno}" if errno is not None else ""
    return f"{type(error).__name__}{suffix}"[:255]


def _connect_from_config() -> MigrationConnection:
    return pymysql.connect(
        host=cast(str, DB_CONFIG["host"]),
        port=cast(int, DB_CONFIG["port"]),
        user=cast(str, DB_CONFIG["user"]),
        password=cast(str, DB_CONFIG["password"]),
        database=cast(str, DB_CONFIG["database"]),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=DictCursor,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply or verify catalog schema migrations")
    parser.add_argument("command", choices=("apply", "check"))
    parser.add_argument("--retry", type=int)
    parser.add_argument("--retry-station-asset", action="store_true")
    parser.add_argument("--allow-v5-rebuild", action="store_true")
    parser.add_argument("--lock-timeout-seconds", type=int, default=0)
    args = parser.parse_args()
    runner = CatalogMigrationRunner(lock_timeout_seconds=args.lock_timeout_seconds)
    if args.command == "check":
        if args.retry is not None or args.retry_station_asset or args.allow_v5_rebuild:
            parser.error("check does not accept retry or rebuild options")
        runner.check()
        print("catalog schema migrations: ready")
        return
    applied = runner.apply(
        retry_version=args.retry,
        allow_v5_rebuild=args.allow_v5_rebuild,
        retry_station_asset=args.retry_station_asset,
    )
    applied_text = ",".join(f"{version:03d}" for version in applied) or "none"
    print(f"catalog schema migrations applied: {applied_text}")
