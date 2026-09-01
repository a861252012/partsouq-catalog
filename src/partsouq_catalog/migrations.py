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
STALE_SCHEDULER_EXIT_CODE = 125
LEGACY_NHTSA_LINK_WINDOW_SECONDS = 5

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
    (23, "023_partsouq_http_diagnostics.sql", "83951039e522c3bdf9caf887bd46ebf65594649f4ac57b4810a35a140da08918"),
    (24, "024_nhtsa_run_leases.sql", "7faeb30a4aa83a1aca8c768bf3a3170d0bed5783fa9aa53c2d207419dcc17182"),
    (25, "025_tw_vncs_vehicles.sql", "dbe9d714c90da6d1ad56911ca31f3ba2fa02582a4d50fee9ca017b9307d329f0"),
    (26, "026_vin_decode_nullable_model.sql", "3fc054b17c8194a9b298a414ef15710cafadb5d973642ec168b85669dd19ab5e"),
    (27, "027_partsouq_empty_unit_evidence.sql", "773295d258f82ba9620720155458f51d4533d5ca79763cfd31982391d63193fd"),
    (28, "028_sparse_vin_part_fitments.sql", "6342c8b24eff14484cc7f972a73434a740a68f122cbfa00c4f6b8eedcd18ccdf"),
    (29, "029_model_aligned_bounded_scope.sql", "829d0041fe328f3fcf2ff7f2d22504efe9763d0ff005c53dbfda459ab8b8189b"),
    (30, "030_current_bounded_scope_contract.sql", "33263b30a414ec1aeeb247c31e0521ebd841b02e3574efd04e81cee3bec350cd"),
    (31, "031_verified_bounded_current_view.sql", "445406e39dac2c4c2702c6389358625654b366b8a4b1f5302801a8294cd238ab"),
    (32, "032_bounded_snapshot_evidence_binding.sql", "8eb4c7cb652080f0f9b35d6315ffbaefa70d2416d14fc5a7c315345e1e7a781a"),
    (33, "033_bounded_snapshot_immutable.sql", "7b366aa536a155cc552547ed2553cdd6fad98daf0d51262eca98b63616d2d61f"),
    (34, "034_vin_fitment_strict_count.sql", "01d4131332cb49080dcf9687bc70e4c7af3194e1306e04653614dfb706726c9e"),
    (35, "035_revoke_invalid_legacy_snapshot_rows.sql", "d728ab56b7ca6c5f4838a48e41c43d240cb3b64a64733b4d93ccdb5f19c94357"),
    (36, "036_bounded_group_receipt_contract.sql", "84277d048b553415528597661d9dccf0df3f54007cc1ac33031953cbe6ee70ff"),
    (37, "037_sparse_override_bridges_unknown_catalog_fields.sql", "b5d75b9f77b885159a9315544f7edc3a2cb1d20acaa38afaa645b8c8727ed561"),
    (38, "038_vncs_source_identity_upsert.sql", "c0a336f266db25475145c31a8186299e6cdc0fd440e59f2e3f40862c62c0fb66"),
)
# fmt: on
ACTIVE_VERSIONS = tuple(
    version for version, _, _ in CATALOG_MANIFEST if version not in SUPERSEDED_VERSIONS
)
STATION_ADMIN_ASSET_HASHES = (
    "5e566d9b931be3d658a78dbf2636fad53500dcbb33641671dd85b12ad7c4fd02",
    "4745c64212cb9cd3d0c7a7bd2579dd443d6183fc906558efa05c857d67a754c1",
    "a42990b80a047f4ae94e211050c0bfd9bdd5fc7078aaf0b2b7d6effdcc2fdda7",
    "e21e60e351d49c97f4f3b83d68bb8464cb1adcedc5b2156662459a572ffeb50e",
    "ebbfe574c7352e1fd7d82469c31a6e37a6248b735b5228e248b0564e2c480ac3",
    "a61c613e39b8347419bd094ba6f9311953954d8e4332a581c7546c353b4aa0a0",
    "8e5d44311baf9e5d64f9030cbf6e373d806ab2bd59f0dea238e45694ac206825",
    "b93deb7e99ec0ab4911cdbacc096c645943f40821850ee83dbb9abf6aa2d47cf",
    "915a656a1db64d724434fe728e4d3d4521c8dad902bedcf47cb3e23a339bd6ea",
    "496b6b02360537573fd1d3f6910dccd0564f2371a07f7ab91f0ebb1152c6224d",
    "78ca6f42d5ddf0421d666530f6a5db3de159c6602cf713b3bc0bd5247a056456",
    "afea8c129b4b32b52b2eee234c1bf3e520f58958cd255ace68eeeb9bc9970008",
    "28b8d65fea6001fc50c7daf4ff6b0a116b5643557dc0060ea620fa0d9b634b03",
)
STATION_ADMIN_ASSET = (
    "asset:station-admin",
    "station_admin.sql",
    STATION_ADMIN_ASSET_HASHES[-1],
)

_HTTP_DIAGNOSTIC_COLUMNS = (
    ("id", "bigint unsigned", "NO"),
    ("crawl_run_id", "int", "NO"),
    ("scheduled_job_run_id", "bigint unsigned", "NO"),
    ("group_id", "int", "NO"),
    ("reason", "varchar(32)", "NO"),
    ("public_source_url", "varchar(1024)", "NO"),
    ("source_url_sha256", "char(64)", "NO"),
    ("raw_body_sha256", "char(64)", "NO"),
    ("body_sha256", "char(64)", "NO"),
    ("compression", "varchar(16)", "NO"),
    ("body_blob", "mediumblob", "NO"),
    ("original_bytes", "int unsigned", "NO"),
    ("stored_bytes", "int unsigned", "NO"),
    ("sanitizer_version", "varchar(64)", "NO"),
    ("http_status", "smallint unsigned", "NO"),
    ("content_type", "varchar(128)", "NO"),
    ("fetched_at", "datetime(6)", "NO"),
    ("elapsed_ms", "int unsigned", "NO"),
    ("attempt", "smallint unsigned", "NO"),
    ("parser_name", "varchar(128)", "NO"),
    ("parser_version", "varchar(64)", "NO"),
    ("parser_context_json", "json", "NO"),
    ("parser_context_sha256", "char(64)", "NO"),
    ("created_at", "datetime(6)", "NO"),
    ("updated_at", "datetime(6)", "NO"),
)
_HTTP_DIAGNOSTIC_ASCII_COLUMNS = frozenset(
    {
        "reason",
        "source_url_sha256",
        "raw_body_sha256",
        "body_sha256",
        "compression",
        "sanitizer_version",
        "parser_version",
        "parser_context_sha256",
    }
)
_HTTP_DIAGNOSTIC_CHECK_CLAUSES = {
    "chk_partsouq_diagnostic_context": (
        "(json_valid(`parser_context_json`) and "
        "(not(regexp_like(lower(cast(`parser_context_json` as char charset utf8mb4)),"
        '_utf8mb4\\\'("ssd"[[:space:]]*:|ssd=|cf_clearance|phpsessid|authorization|'
        "set-cookie)\\'))))"
    ),
    "chk_partsouq_diagnostic_hashes": (
        "(regexp_like(`source_url_sha256`,_utf8mb4\\'^[0-9a-f]{64}$\\') and "
        "regexp_like(`raw_body_sha256`,_utf8mb4\\'^[0-9a-f]{64}$\\') and "
        "regexp_like(`body_sha256`,_utf8mb4\\'^[0-9a-f]{64}$\\') and "
        "regexp_like(`parser_context_sha256`,_utf8mb4\\'^[0-9a-f]{64}$\\'))"
    ),
    "chk_partsouq_diagnostic_http": (
        "((`content_type` <> _utf8mb4\\'\\') and (`elapsed_ms` >= 0) and (`attempt` > 0))"
    ),
    "chk_partsouq_diagnostic_parser": (
        "((`parser_name` = _utf8mb4\\'parse_parts\\') and (`parser_version` <> _utf8mb4\\'\\'))"
    ),
    "chk_partsouq_diagnostic_public_url": (
        "((`public_source_url` like "
        "_utf8mb4\\'https://partsouq.com/en/catalog/genuine/unit?%\\') and "
        "(not(regexp_like(lower(`public_source_url`),_utf8mb4\\'(^|[?&])ssd=\\'))) "
        "and (not((`public_source_url` like _utf8mb4\\'%#%\\'))))"
    ),
    "chk_partsouq_diagnostic_reason": (
        "(((cast(`reason` as char charset binary) = "
        "cast(_utf8mb4\\'http_not_found\\' as char charset binary)) and "
        "(`http_status` = 404)) or ((cast(`reason` as char charset binary) = "
        "cast(_utf8mb4\\'empty_parse\\' as char charset binary)) and "
        "(`http_status` = 200)))"
    ),
    "chk_partsouq_diagnostic_sanitizer": (
        "(cast(`sanitizer_version` as char charset binary) = "
        "cast(_utf8mb4\\'partsouq-html-public-v2\\' as char charset binary))"
    ),
    "chk_partsouq_diagnostic_storage": (
        "((cast(`compression` as char charset binary) = "
        "cast(_utf8mb4\\'zlib\\' as char charset binary)) and "
        "(`original_bytes` > 0) and (`stored_bytes` > 0) and "
        "(`stored_bytes` = length(`body_blob`)))"
    ),
}

_BOUNDED_GROUP_RECEIPT_CHECK_CLAUSES = {
    "chk_bounded_group_receipt_status": "(statusin('done','partial'))",
    "chk_bounded_group_receipt_counts": (
        "((accepted_part_count<=parsed_part_count)and"
        "(((status='done')and(accepted_part_count=parsed_part_count))or"
        "((status='partial')and(accepted_part_count>0)and"
        "(accepted_part_count<parsed_part_count))))"
    ),
}


def _normalize_http_diagnostic_check_clause(clause: str) -> str:
    normalized = clause.strip().lower()
    normalized = re.sub(r"_(?:ascii|latin1|utf8mb4)(?=\\')", "", normalized)
    normalized = re.sub(
        r"charset (?:latin1|utf8mb4)",
        "charset text",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized)


def _normalize_nhtsa_check_clause(clause: str) -> str:
    normalized = clause.strip().lower().replace("`", "")
    normalized = normalized.replace("\\'", "'")
    normalized = re.sub(r"_(?:ascii|latin1|utf8mb4)(?=')", "", normalized)
    normalized = re.sub(
        r"cast\(([^()]+) as char charset binary\)",
        r"binary \1",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized)


def _normalize_bounded_group_receipt_check_clause(clause: str) -> str:
    """正規化 MySQL CHECK 的字面值與排版漂移，保留條件順序。"""
    normalized = clause.strip().lower().replace("`", "")
    normalized = normalized.replace("\\'", "'")
    normalized = re.sub(r"_(?:ascii|latin1|utf8mb4)(?=')", "", normalized)
    return re.sub(r"\s+", "", normalized)


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
            _assert_http_diagnostics_contract(connection)
            _assert_nhtsa_run_lease_contract(connection)
            _assert_bounded_group_receipt_contract(connection)
            _assert_station_admin_vin_decode_contract(connection)
        finally:
            connection.close()

    def apply(
        self,
        *,
        retry_version: int | None = None,
        allow_v5_rebuild: bool = False,
        retry_station_asset: bool = False,
        recover_stale_catalog_daemon_seconds: int | None = None,
        recover_stale_nhtsa_daemon_seconds: int | None = None,
    ) -> tuple[int, ...]:
        if (
            recover_stale_catalog_daemon_seconds is not None
            and recover_stale_catalog_daemon_seconds <= 0
        ):
            raise ValueError("stale catalog daemon age must be positive")
        if (
            recover_stale_nhtsa_daemon_seconds is not None
            and recover_stale_nhtsa_daemon_seconds <= 0
        ):
            raise ValueError("stale NHTSA daemon age must be positive")
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

            repairable_scheduler_id = None
            if recover_stale_catalog_daemon_seconds is not None:
                repairable_scheduler_id = _repairable_stale_catalog_daemon_id(
                    connection,
                    recover_stale_catalog_daemon_seconds,
                )
            repairable_nhtsa_runs: tuple[Mapping[str, object], ...] = ()
            if recover_stale_nhtsa_daemon_seconds is not None:
                repairable_nhtsa_runs = _repairable_stale_nhtsa_runs(
                    connection,
                    recover_stale_nhtsa_daemon_seconds,
                )
            repairable_nhtsa_scheduled_job_ids: list[int] = []
            for row in repairable_nhtsa_runs:
                if row["job_status"] == "running":
                    repairable_nhtsa_scheduled_job_ids.append(int(str(row["scheduled_job_run_id"])))
                if row.get("parent_job_status") == "running":
                    repairable_nhtsa_scheduled_job_ids.append(
                        int(str(row["parent_scheduled_job_run_id"]))
                    )
            _assert_no_running_jobs(
                connection,
                allow_repairable_catalog=True,
                repairable_scheduled_job_id=repairable_scheduler_id,
                repairable_nhtsa_run_ids=tuple(
                    int(str(row["run_id"])) for row in repairable_nhtsa_runs
                ),
                repairable_nhtsa_scheduled_job_ids=tuple(
                    dict.fromkeys(repairable_nhtsa_scheduled_job_ids)
                ),
            )
            _repair_stale_catalog_runs(connection, repairable_scheduler_id)
            _repair_stale_nhtsa_runs(connection, repairable_nhtsa_runs)
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
                    if change.version == 23:
                        _assert_http_diagnostics_contract(connection)
                    if change.version == 24:
                        _assert_nhtsa_run_lease_contract(connection)
                    if change.version == 36:
                        _assert_bounded_group_receipt_contract(connection)
                    if change.kind == "asset":
                        _assert_station_admin_vin_decode_contract(connection)
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
            _assert_http_diagnostics_contract(connection)
            _assert_nhtsa_run_lease_contract(connection)
            _assert_bounded_group_receipt_contract(connection)
            _assert_station_admin_vin_decode_contract(connection)
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
    connection: MigrationConnection,
    *,
    allow_repairable_catalog: bool = False,
    repairable_scheduled_job_id: int | None = None,
    repairable_nhtsa_run_ids: Sequence[int] = (),
    repairable_nhtsa_scheduled_job_ids: Sequence[int] = (),
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
            if table == "scheduled_job_runs" and (
                repairable_scheduled_job_id is not None or repairable_nhtsa_scheduled_job_ids
            ):
                repairable_job_ids = tuple(
                    dict.fromkeys(
                        (
                            ()
                            if repairable_scheduled_job_id is None
                            else (repairable_scheduled_job_id,)
                        )
                        + tuple(repairable_nhtsa_scheduled_job_ids)
                    )
                )
                placeholders = ",".join("%s" for _job_id in repairable_job_ids)
                cursor.execute(
                    "SELECT COUNT(*) AS row_count FROM scheduled_job_runs "
                    f"WHERE status='running' AND id NOT IN ({placeholders})",
                    repairable_job_ids,
                )
            elif table == "nhtsa_sync_runs" and repairable_nhtsa_run_ids:
                placeholders = ",".join("%s" for _run_id in repairable_nhtsa_run_ids)
                cursor.execute(
                    "SELECT COUNT(*) AS row_count FROM nhtsa_sync_runs "
                    f"WHERE status='running' AND id NOT IN ({placeholders})",
                    tuple(repairable_nhtsa_run_ids),
                )
            elif repairable_catalog:
                repairable_running_scheduler = ""
                parameters: tuple[object, ...] = ()
                if repairable_scheduled_job_id is not None:
                    repairable_running_scheduler = " OR (jobs.id=%s AND jobs.status='running')"
                    parameters = (repairable_scheduled_job_id,)
                cursor.execute(
                    "SELECT COUNT(*) AS row_count FROM crawl_runs AS runs "
                    "LEFT JOIN scheduled_job_runs AS jobs "
                    "ON jobs.id=runs.scheduled_job_run_id WHERE runs.status='running' "
                    "AND NOT (runs.scheduled_job_run_id IS NOT NULL AND jobs.id IS NOT NULL "
                    "AND jobs.job_name='catalog' AND ((jobs.status='failed' "
                    "AND jobs.finished_at IS NOT NULL AND jobs.exit_code IS NOT NULL "
                    "AND jobs.exit_code <> 0)"
                    f"{repairable_running_scheduler}) "
                    "AND (SELECT COUNT(*) FROM crawl_runs AS linked "
                    "WHERE linked.scheduled_job_run_id=jobs.id)=1)",
                    parameters,
                )
            else:
                cursor.execute(
                    f"SELECT COUNT(*) AS row_count FROM `{table}` WHERE status='running'"
                )
            row = cursor.fetchone()
        if row and int(row["row_count"]) > 0:
            raise MigrationError(f"running jobs exist in {table}; stop writers before migration")


def _repairable_stale_catalog_daemon_id(
    connection: MigrationConnection,
    minimum_age_seconds: int,
) -> int | None:
    required_columns = (
        ("scheduled_job_runs", "trigger_mode"),
        ("scheduled_job_runs", "started_at"),
        ("crawl_runs", "scheduled_job_run_id"),
    )
    if (
        not _table_exists(connection, "crawl_runs")
        or not _table_exists(connection, "scheduled_job_runs")
        or any(not _column_exists(connection, table, column) for table, column in required_columns)
    ):
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT jobs.id, "
            "(BINARY jobs.job_name=BINARY 'catalog' "
            "AND BINARY jobs.trigger_mode=BINARY 'daemon' "
            "AND jobs.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND) "
            "AS stale_catalog_daemon, "
            "COUNT(runs.id) AS linked_runs, "
            "COALESCE(SUM(CASE WHEN runs.id IS NOT NULL "
            "AND BINARY runs.status NOT IN (BINARY 'running', BINARY 'bounded_success', "
            "BINARY 'success', BINARY 'error', BINARY 'interrupted') "
            "THEN 1 ELSE 0 END),0) AS incompatible_runs "
            "FROM scheduled_job_runs AS jobs "
            "LEFT JOIN crawl_runs AS runs ON runs.scheduled_job_run_id=jobs.id "
            "WHERE BINARY jobs.status=BINARY 'running' "
            "GROUP BY jobs.id,jobs.job_name,jobs.trigger_mode,jobs.started_at "
            "ORDER BY jobs.id LIMIT 2",
            (minimum_age_seconds,),
        )
        rows = list(cursor.fetchall())
    if len(rows) != 1:
        return None
    row = rows[0]
    if (
        int(row["stale_catalog_daemon"]) != 1
        or int(row["linked_runs"]) > 1
        or int(row["incompatible_runs"]) != 0
    ):
        return None
    return int(row["id"])


def _repair_stale_catalog_runs(
    connection: MigrationConnection,
    scheduled_job_run_id: int | None = None,
) -> None:
    if (
        not _table_exists(connection, "crawl_runs")
        or not _table_exists(connection, "scheduled_job_runs")
        or not _column_exists(connection, "crawl_runs", "scheduled_job_run_id")
    ):
        return
    connection.begin()
    try:
        with connection.cursor() as cursor:
            if scheduled_job_run_id is not None:
                cursor.execute(
                    "UPDATE scheduled_job_runs SET status='failed',finished_at=UTC_TIMESTAMP(),"
                    "exit_code=%s,output_text=RIGHT(CONCAT(COALESCE(output_text,''),%s),60000) "
                    "WHERE id=%s AND status='running'",
                    (
                        STALE_SCHEDULER_EXIT_CODE,
                        "\nformal host daemon owns the local locks; stale scheduler recovered "
                        "before migration\n",
                        scheduled_job_run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise MigrationError("stale catalog scheduler marker changed during recovery")
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
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _repairable_stale_nhtsa_runs(
    connection: MigrationConnection,
    minimum_age_seconds: int,
) -> tuple[Mapping[str, object], ...]:
    required_columns = (
        ("scheduled_job_runs", "trigger_mode"),
        ("scheduled_job_runs", "started_at"),
        ("scheduled_job_runs", "finished_at"),
        ("scheduled_job_runs", "exit_code"),
        ("nhtsa_sync_runs", "run_key"),
        ("nhtsa_sync_runs", "started_at"),
        ("nhtsa_sync_runs", "updated_at"),
    )
    if (
        not _table_exists(connection, "nhtsa_sync_runs")
        or not _table_exists(connection, "scheduled_job_runs")
        or any(not _column_exists(connection, table, column) for table, column in required_columns)
    ):
        return ()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id,run_key,started_at,updated_at FROM nhtsa_sync_runs "
            "WHERE BINARY status=BINARY 'running' ORDER BY id"
        )
        running = tuple(cursor.fetchall())
        if not running:
            return ()
        direct_link = _column_exists(connection, "nhtsa_sync_runs", "scheduled_job_run_id")
        lease_columns = all(
            _column_exists(connection, "nhtsa_sync_runs", column)
            for column in ("lease_slot", "lease_token", "heartbeat_at", "lease_expires_at")
        )
        if direct_link:
            if not lease_columns:
                raise MigrationError(
                    "direct NHTSA scheduler links require the complete lease schema"
                )
            if not _column_exists(
                connection,
                "scheduled_job_runs",
                "parent_scheduled_job_run_id",
            ):
                raise MigrationError(
                    "direct NHTSA scheduler links require the parent lineage schema"
                )
            cursor.execute(
                "SELECT runs.id AS run_id,runs.run_key AS run_key,"
                "runs.started_at AS run_started_at,runs.updated_at AS run_updated_at,"
                "runs.error_message AS run_error_message,runs.lease_slot AS lease_slot,"
                "runs.lease_token AS lease_token,runs.heartbeat_at AS heartbeat_at,"
                "runs.lease_expires_at AS lease_expires_at,"
                "jobs.id AS scheduled_job_run_id,jobs.finished_at AS job_finished_at,"
                "jobs.started_at AS job_started_at,jobs.exit_code AS job_exit_code,"
                "jobs.job_name AS job_name,jobs.trigger_mode AS job_trigger_mode,"
                "jobs.status AS job_status,"
                "jobs.parent_scheduled_job_run_id AS parent_scheduled_job_run_id,"
                "parent.job_name AS parent_job_name,"
                "parent.trigger_mode AS parent_trigger_mode,"
                "parent.status AS parent_job_status,"
                "parent.started_at AS parent_job_started_at,"
                "parent.finished_at AS parent_job_finished_at,"
                "parent.exit_code AS parent_job_exit_code,"
                "'direct' AS link_mode "
                "FROM nhtsa_sync_runs AS runs JOIN scheduled_job_runs AS jobs "
                "ON jobs.id=runs.scheduled_job_run_id "
                "LEFT JOIN scheduled_job_runs AS parent "
                "ON parent.id=jobs.parent_scheduled_job_run_id "
                "WHERE BINARY runs.status=BINARY 'running' "
                "AND BINARY jobs.job_name IN ("
                "BINARY 'nhtsa-bulk',BINARY 'nhtsa-api',BINARY 'nhtsa-vin') "
                "AND BINARY jobs.trigger_mode IN (BINARY 'daemon',BINARY 'queue') "
                "AND BINARY runs.lease_slot=BINARY 'writer' "
                "AND runs.lease_token REGEXP '^[0-9a-f]{64}$' "
                "AND runs.heartbeat_at IS NOT NULL "
                "AND runs.lease_expires_at>runs.heartbeat_at "
                "AND runs.lease_expires_at<UTC_TIMESTAMP(6) "
                "AND runs.started_at<UTC_TIMESTAMP(6)-INTERVAL %s SECOND "
                "AND runs.updated_at<UTC_TIMESTAMP(6)-INTERVAL %s SECOND "
                "AND runs.heartbeat_at<UTC_TIMESTAMP(6)-INTERVAL %s SECOND "
                "AND jobs.started_at<=runs.started_at "
                "AND runs.started_at<=runs.heartbeat_at "
                "AND runs.heartbeat_at<=runs.updated_at "
                "AND runs.updated_at<runs.lease_expires_at "
                "AND ((BINARY jobs.status=BINARY 'failed' "
                "AND jobs.finished_at IS NOT NULL AND jobs.exit_code IS NOT NULL "
                "AND jobs.exit_code<>0 "
                "AND jobs.finished_at<UTC_TIMESTAMP()-INTERVAL %s SECOND "
                "AND runs.updated_at<TIMESTAMPADD(SECOND,1,jobs.finished_at)) OR ("
                "BINARY jobs.status=BINARY 'running' "
                "AND jobs.finished_at IS NULL AND jobs.exit_code IS NULL "
                "AND jobs.started_at<UTC_TIMESTAMP()-INTERVAL %s SECOND)) "
                "AND (jobs.parent_scheduled_job_run_id IS NULL OR ("
                "parent.id IS NOT NULL AND BINARY parent.job_name=BINARY 'nhtsa' "
                "AND BINARY parent.trigger_mode IN (BINARY 'daemon',BINARY 'queue') "
                "AND parent.started_at<=jobs.started_at "
                "AND ((BINARY parent.status=BINARY 'failed' "
                "AND parent.finished_at IS NOT NULL AND parent.exit_code IS NOT NULL "
                "AND parent.exit_code<>0 "
                "AND jobs.started_at<=parent.finished_at "
                "AND parent.finished_at<UTC_TIMESTAMP()-INTERVAL %s SECOND) OR ("
                "BINARY parent.status=BINARY 'running' "
                "AND parent.finished_at IS NULL AND parent.exit_code IS NULL "
                "AND parent.started_at<UTC_TIMESTAMP()-INTERVAL %s SECOND)) "
                "AND NOT EXISTS (SELECT 1 FROM scheduled_job_runs AS sibling "
                "WHERE sibling.parent_scheduled_job_run_id=parent.id "
                "AND sibling.id<>jobs.id AND BINARY sibling.status=BINARY 'running'))) "
                "ORDER BY runs.id,jobs.id",
                (
                    minimum_age_seconds,
                    minimum_age_seconds,
                    minimum_age_seconds,
                    minimum_age_seconds,
                    minimum_age_seconds,
                    minimum_age_seconds,
                    minimum_age_seconds,
                ),
            )
        else:
            cursor.execute(
                "SELECT runs.id AS run_id,runs.run_key AS run_key,"
                "runs.started_at AS run_started_at,runs.updated_at AS run_updated_at,"
                "runs.error_message AS run_error_message,NULL AS lease_slot,"
                "NULL AS lease_token,NULL AS heartbeat_at,NULL AS lease_expires_at,"
                "jobs.id AS scheduled_job_run_id,jobs.finished_at AS job_finished_at,"
                "jobs.started_at AS job_started_at,jobs.exit_code AS job_exit_code,"
                "jobs.job_name AS job_name,jobs.trigger_mode AS job_trigger_mode,"
                "jobs.status AS job_status,"
                "NULL AS parent_scheduled_job_run_id,NULL AS parent_job_name,"
                "NULL AS parent_trigger_mode,NULL AS parent_job_status,"
                "NULL AS parent_job_started_at,NULL AS parent_job_finished_at,"
                "NULL AS parent_job_exit_code,"
                "'legacy' AS link_mode "
                "FROM nhtsa_sync_runs AS runs JOIN scheduled_job_runs AS jobs "
                "ON BINARY jobs.job_name=BINARY CONCAT('nhtsa-',"
                "SUBSTRING_INDEX(SUBSTRING(runs.run_key,7),'-',1)) "
                "AND jobs.started_at>=STR_TO_DATE("
                "RIGHT(runs.run_key,16),'%%Y%%m%%dT%%H%%i%%sZ') "
                "AND jobs.started_at<=TIMESTAMPADD(SECOND,%s,STR_TO_DATE("
                "RIGHT(runs.run_key,16),'%%Y%%m%%dT%%H%%i%%sZ')) "
                "WHERE BINARY runs.status=BINARY 'running' "
                "AND BINARY runs.run_key REGEXP BINARY "
                "'^nhtsa-(bulk|api|vin)-[0-9]{8}T[0-9]{6}Z$' "
                "AND BINARY jobs.status=BINARY 'failed' "
                "AND BINARY jobs.trigger_mode IN (BINARY 'daemon',BINARY 'queue') "
                "AND jobs.finished_at IS NOT NULL AND jobs.exit_code IS NOT NULL "
                "AND jobs.exit_code<>0 "
                "AND runs.started_at>=jobs.started_at "
                "AND runs.started_at<=TIMESTAMPADD(SECOND,%s,STR_TO_DATE("
                "RIGHT(runs.run_key,16),'%%Y%%m%%dT%%H%%i%%sZ')) "
                "AND runs.started_at<=runs.updated_at "
                "AND runs.updated_at<TIMESTAMPADD(SECOND,1,jobs.finished_at) "
                "AND jobs.finished_at<UTC_TIMESTAMP(6)-INTERVAL %s SECOND "
                "AND runs.updated_at<UTC_TIMESTAMP(6)-INTERVAL %s SECOND "
                "ORDER BY runs.id,jobs.id",
                (
                    LEGACY_NHTSA_LINK_WINDOW_SECONDS,
                    LEGACY_NHTSA_LINK_WINDOW_SECONDS,
                    minimum_age_seconds,
                    minimum_age_seconds,
                ),
            )
        candidates = tuple(cursor.fetchall())
    running_ids = {int(row["id"]) for row in running}
    run_counts: dict[int, int] = {}
    job_counts: dict[int, int] = {}
    parent_counts: dict[int, int] = {}
    for row in candidates:
        run_id = int(row["run_id"])
        job_id = int(row["scheduled_job_run_id"])
        run_counts[run_id] = run_counts.get(run_id, 0) + 1
        job_counts[job_id] = job_counts.get(job_id, 0) + 1
        parent_id = row.get("parent_scheduled_job_run_id")
        if parent_id is not None:
            parsed_parent_id = int(parent_id)
            parent_counts[parsed_parent_id] = parent_counts.get(parsed_parent_id, 0) + 1
    if (
        {int(row["run_id"]) for row in candidates} != running_ids
        or any(count != 1 for count in run_counts.values())
        or any(count != 1 for count in job_counts.values())
        or any(count != 1 for count in parent_counts.values())
    ):
        raise MigrationError(
            "running NHTSA jobs do not have one unique recoverable scheduler child each"
        )
    return candidates


def _repair_stale_nhtsa_runs(
    connection: MigrationConnection,
    candidates: Sequence[Mapping[str, object]],
) -> None:
    if not candidates:
        return
    connection.begin()
    try:
        with connection.cursor() as cursor:
            direct_link = _column_exists(
                connection,
                "nhtsa_sync_runs",
                "scheduled_job_run_id",
            )
            lease_columns = tuple(
                column
                for column in ("lease_slot", "lease_token", "lease_expires_at")
                if _column_exists(connection, "nhtsa_sync_runs", column)
            )
            lease_assignments = "".join(f",{column}=NULL" for column in lease_columns)
            for row in candidates:
                cursor.execute(
                    "SELECT status,run_key,started_at,updated_at,error_message"
                    + (
                        ",scheduled_job_run_id,lease_slot,lease_token,heartbeat_at,"
                        "lease_expires_at,(lease_expires_at<UTC_TIMESTAMP(6)) AS lease_expired"
                        if direct_link
                        else ""
                    )
                    + " FROM nhtsa_sync_runs WHERE id=%s FOR UPDATE",
                    (row["run_id"],),
                )
                locked_run = cursor.fetchone()
                if (
                    locked_run is None
                    or locked_run["status"] != "running"
                    or locked_run["run_key"] != row["run_key"]
                    or locked_run["started_at"] != row["run_started_at"]
                    or locked_run["updated_at"] != row["run_updated_at"]
                    or (
                        direct_link
                        and (
                            locked_run["scheduled_job_run_id"] != row["scheduled_job_run_id"]
                            or locked_run["lease_slot"] != row["lease_slot"]
                            or locked_run["lease_token"] != row["lease_token"]
                            or locked_run["heartbeat_at"] != row["heartbeat_at"]
                            or locked_run["lease_expires_at"] != row["lease_expires_at"]
                            or int(locked_run["lease_expired"]) != 1
                        )
                    )
                ):
                    raise MigrationError("stale NHTSA run changed during exact recovery")

                cursor.execute(
                    "SELECT job_name,trigger_mode,"
                    + (
                        "parent_scheduled_job_run_id,"
                        if direct_link
                        else "NULL AS parent_scheduled_job_run_id,"
                    )
                    + "status,started_at,finished_at,exit_code "
                    "FROM scheduled_job_runs WHERE id=%s FOR UPDATE",
                    (row["scheduled_job_run_id"],),
                )
                locked_job = cursor.fetchone()
                if (
                    locked_job is None
                    or locked_job["job_name"] != row["job_name"]
                    or locked_job["trigger_mode"] != row["job_trigger_mode"]
                    or locked_job["parent_scheduled_job_run_id"]
                    != row["parent_scheduled_job_run_id"]
                    or locked_job["status"] != row["job_status"]
                    or locked_job["started_at"] != row["job_started_at"]
                    or locked_job["finished_at"] != row["job_finished_at"]
                    or locked_job["exit_code"] != row["job_exit_code"]
                ):
                    raise MigrationError("stale NHTSA scheduler marker changed during recovery")
                if row["job_status"] == "running":
                    cursor.execute(
                        "UPDATE scheduled_job_runs SET status='failed',"
                        "finished_at=UTC_TIMESTAMP(),exit_code=%s,output_text="
                        "RIGHT(CONCAT(COALESCE(output_text,''),%s),60000) "
                        "WHERE id=%s AND BINARY status=BINARY 'running' "
                        "AND BINARY job_name=BINARY %s "
                        "AND BINARY trigger_mode=BINARY %s "
                        "AND parent_scheduled_job_run_id<=>%s "
                        "AND started_at=%s AND finished_at IS NULL AND exit_code IS NULL",
                        (
                            STALE_SCHEDULER_EXIT_CODE,
                            "\nmigration preflight: expired NHTSA lease recovered\n",
                            row["scheduled_job_run_id"],
                            row["job_name"],
                            row["job_trigger_mode"],
                            row["parent_scheduled_job_run_id"],
                            row["job_started_at"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise MigrationError("stale NHTSA scheduler marker changed during recovery")

                link_predicate = ""
                link_parameters: tuple[object, ...] = ()
                if direct_link:
                    link_predicate = " AND scheduled_job_run_id=%s"
                    link_parameters = (row["scheduled_job_run_id"],)
                link_description = (
                    "directly linked" if row.get("link_mode") == "direct" else "legacy-matched"
                )
                cursor.execute(
                    "UPDATE nhtsa_sync_runs SET status='interrupted',"
                    "updated_at=GREATEST(updated_at,UTC_TIMESTAMP(6)),"
                    "ended_at=GREATEST(updated_at,UTC_TIMESTAMP(6)),"
                    "error_message=CONCAT_WS('\\n',NULLIF(error_message,''),%s)"
                    f"{lease_assignments} "
                    "WHERE id=%s AND BINARY status=BINARY 'running' "
                    "AND BINARY run_key=BINARY %s AND started_at=%s AND updated_at=%s"
                    f"{link_predicate}",
                    (
                        f"migration preflight: {link_description} scheduler child "
                        f"{int(str(row['scheduled_job_run_id']))} "
                        f"{'expired' if row['job_status'] == 'running' else 'failed'}; "
                        "stale run interrupted",
                        int(str(row["run_id"])),
                        row["run_key"],
                        row["run_started_at"],
                        row["run_updated_at"],
                    )
                    + link_parameters,
                )
                if cursor.rowcount != 1:
                    raise MigrationError("stale NHTSA run changed during exact recovery")

                parent_id = row.get("parent_scheduled_job_run_id")
                if parent_id is None:
                    continue
                cursor.execute(
                    "SELECT job_name,trigger_mode,status,started_at,finished_at,exit_code "
                    "FROM scheduled_job_runs WHERE id=%s FOR UPDATE",
                    (parent_id,),
                )
                locked_parent = cursor.fetchone()
                if (
                    locked_parent is None
                    or locked_parent["job_name"] != row["parent_job_name"]
                    or locked_parent["trigger_mode"] != row["parent_trigger_mode"]
                    or locked_parent["status"] != row["parent_job_status"]
                    or locked_parent["started_at"] != row["parent_job_started_at"]
                    or locked_parent["finished_at"] != row["parent_job_finished_at"]
                    or locked_parent["exit_code"] != row["parent_job_exit_code"]
                ):
                    raise MigrationError("stale NHTSA parent marker changed during recovery")
                if row["parent_job_status"] != "running":
                    continue
                cursor.execute(
                    "SELECT id FROM scheduled_job_runs "
                    "WHERE parent_scheduled_job_run_id=%s "
                    "AND BINARY status=BINARY 'running' FOR UPDATE",
                    (parent_id,),
                )
                if cursor.fetchone() is not None:
                    raise MigrationError("stale NHTSA parent still has an active child")
                cursor.execute(
                    "UPDATE scheduled_job_runs SET status='failed',"
                    "finished_at=UTC_TIMESTAMP(),exit_code=%s,output_text="
                    "RIGHT(CONCAT(COALESCE(output_text,''),%s),60000) "
                    "WHERE id=%s AND BINARY job_name=BINARY 'nhtsa' "
                    "AND BINARY trigger_mode=BINARY 'daemon' "
                    "AND BINARY status=BINARY 'running' AND started_at=%s "
                    "AND finished_at IS NULL AND exit_code IS NULL",
                    (
                        STALE_SCHEDULER_EXIT_CODE,
                        "\nmigration preflight: expired NHTSA child recovered; "
                        "composite parent interrupted\n",
                        parent_id,
                        row["parent_job_started_at"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise MigrationError("stale NHTSA parent marker changed during recovery")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _column_exists(connection: MigrationConnection, table: str, column: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS row_count FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (table, column),
        )
        row = cursor.fetchone()
    return bool(row and row["row_count"] == 1)


def _assert_http_diagnostics_contract(connection: MigrationConnection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COLUMN_NAME AS column_name,COLUMN_TYPE AS column_type,"
            "IS_NULLABLE AS is_nullable,COLUMN_DEFAULT AS column_default,EXTRA AS extra,"
            "CHARACTER_SET_NAME AS character_set,COLLATION_NAME AS collation_name "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='partsouq_http_diagnostics' "
            "ORDER BY ORDINAL_POSITION"
        )
        column_rows = tuple(cursor.fetchall())
        columns = tuple(
            (
                str(row["column_name"]),
                str(row["column_type"]).lower(),
                str(row["is_nullable"]),
            )
            for row in column_rows
        )
        cursor.execute(
            "SELECT INDEX_NAME AS index_name,NON_UNIQUE AS non_unique,"
            "SEQ_IN_INDEX AS sequence,COLUMN_NAME AS column_name,SUB_PART AS sub_part,"
            "INDEX_TYPE AS index_type,IS_VISIBLE AS is_visible "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='partsouq_http_diagnostics' "
            "ORDER BY BINARY INDEX_NAME,SEQ_IN_INDEX"
        )
        indexes = tuple(
            (
                str(row["index_name"]),
                int(row["non_unique"]),
                int(row["sequence"]),
                str(row["column_name"]),
                row["sub_part"],
                str(row["index_type"]),
                str(row["is_visible"]),
            )
            for row in cursor.fetchall()
        )
        cursor.execute(
            "SELECT rc.CONSTRAINT_NAME AS constraint_name,kcu.COLUMN_NAME AS column_name,"
            "kcu.REFERENCED_TABLE_NAME AS referenced_table,"
            "kcu.REFERENCED_COLUMN_NAME AS referenced_column,"
            "kcu.REFERENCED_TABLE_SCHEMA AS referenced_schema,"
            "rc.UPDATE_RULE AS update_rule,rc.DELETE_RULE AS delete_rule "
            "FROM information_schema.REFERENTIAL_CONSTRAINTS AS rc "
            "JOIN information_schema.KEY_COLUMN_USAGE AS kcu "
            "ON kcu.CONSTRAINT_SCHEMA=rc.CONSTRAINT_SCHEMA AND kcu.TABLE_NAME=rc.TABLE_NAME "
            "AND kcu.CONSTRAINT_NAME=rc.CONSTRAINT_NAME "
            "WHERE rc.CONSTRAINT_SCHEMA=DATABASE() "
            "AND rc.TABLE_NAME='partsouq_http_diagnostics' "
            "ORDER BY rc.CONSTRAINT_NAME,kcu.ORDINAL_POSITION"
        )
        foreign_keys = tuple(
            (
                str(row["constraint_name"]),
                str(row["column_name"]),
                str(row["referenced_table"]),
                str(row["referenced_column"]),
                str(row["referenced_schema"]),
                str(row["update_rule"]),
                str(row["delete_rule"]),
            )
            for row in cursor.fetchall()
        )
        cursor.execute(
            "SELECT checks.CONSTRAINT_NAME AS constraint_name,"
            "checks.CHECK_CLAUSE AS check_clause,constraints.ENFORCED AS enforced "
            "FROM information_schema.CHECK_CONSTRAINTS AS checks "
            "JOIN information_schema.TABLE_CONSTRAINTS AS constraints "
            "ON constraints.CONSTRAINT_SCHEMA=checks.CONSTRAINT_SCHEMA "
            "AND constraints.CONSTRAINT_NAME=checks.CONSTRAINT_NAME "
            "WHERE checks.CONSTRAINT_SCHEMA=DATABASE() "
            "AND constraints.TABLE_SCHEMA=DATABASE() "
            "AND constraints.TABLE_NAME='partsouq_http_diagnostics' "
            "AND constraints.CONSTRAINT_TYPE='CHECK' "
            "AND checks.CONSTRAINT_NAME LIKE 'chk_partsouq_diagnostic_%' "
            "ORDER BY checks.CONSTRAINT_NAME"
        )
        checks = {
            str(row["constraint_name"]): (
                _normalize_http_diagnostic_check_clause(str(row["check_clause"])),
                str(row["enforced"]),
            )
            for row in cursor.fetchall()
        }
        cursor.execute("SELECT DATABASE() AS database_name")
        database_row = cursor.fetchone()
        if not database_row or not database_row["database_name"]:
            raise MigrationError("partsouq_http_diagnostics database contract is unavailable")
        database_name = str(database_row["database_name"])
        cursor.execute(
            "SELECT ENGINE AS engine,TABLE_COLLATION AS table_collation "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME='partsouq_http_diagnostics' AND TABLE_TYPE='BASE TABLE'"
        )
        table = cursor.fetchone()

    default_contract = {
        "id": (None, "auto_increment"),
        "created_at": ("CURRENT_TIMESTAMP(6)", "DEFAULT_GENERATED"),
        "updated_at": (
            "CURRENT_TIMESTAMP(6)",
            "DEFAULT_GENERATED on update CURRENT_TIMESTAMP(6)",
        ),
    }
    columns_valid = columns == _HTTP_DIAGNOSTIC_COLUMNS
    for row in column_rows:
        name = str(row["column_name"])
        expected_default, expected_extra = default_contract.get(name, (None, ""))
        columns_valid = columns_valid and (
            row["column_default"] == expected_default and str(row["extra"]) == expected_extra
        )
        if name in _HTTP_DIAGNOSTIC_ASCII_COLUMNS:
            columns_valid = columns_valid and (
                row["character_set"] == "ascii" and row["collation_name"] == "ascii_bin"
            )
        elif str(row["column_type"]).lower().startswith(("char", "varchar")):
            columns_valid = columns_valid and row["character_set"] == "utf8mb4"
        else:
            columns_valid = columns_valid and row["character_set"] is None

    expected_checks = {
        name: (_normalize_http_diagnostic_check_clause(clause), "YES")
        for name, clause in _HTTP_DIAGNOSTIC_CHECK_CLAUSES.items()
    }
    checks_valid = checks == expected_checks
    expected_indexes = (
        ("PRIMARY", 0, 1, "id", None, "BTREE", "YES"),
        ("fk_partsouq_diagnostic_group", 1, 1, "group_id", None, "BTREE", "YES"),
        ("idx_partsouq_diagnostic_body", 1, 1, "body_sha256", None, "BTREE", "YES"),
        ("idx_partsouq_diagnostic_run_reason", 1, 1, "crawl_run_id", None, "BTREE", "YES"),
        ("idx_partsouq_diagnostic_run_reason", 1, 2, "reason", None, "BTREE", "YES"),
        ("idx_partsouq_diagnostic_run_reason", 1, 3, "updated_at", None, "BTREE", "YES"),
        (
            "idx_partsouq_diagnostic_schedule",
            1,
            1,
            "scheduled_job_run_id",
            None,
            "BTREE",
            "YES",
        ),
        ("uq_partsouq_diagnostic_group_reason", 0, 1, "crawl_run_id", None, "BTREE", "YES"),
        ("uq_partsouq_diagnostic_group_reason", 0, 2, "group_id", None, "BTREE", "YES"),
        ("uq_partsouq_diagnostic_group_reason", 0, 3, "reason", None, "BTREE", "YES"),
    )
    expected_foreign_keys = (
        (
            "fk_partsouq_diagnostic_group",
            "group_id",
            "groups_t",
            "id",
            database_name,
            "NO ACTION",
            "NO ACTION",
        ),
        (
            "fk_partsouq_diagnostic_run",
            "crawl_run_id",
            "crawl_runs",
            "id",
            database_name,
            "NO ACTION",
            "NO ACTION",
        ),
        (
            "fk_partsouq_diagnostic_schedule",
            "scheduled_job_run_id",
            "scheduled_job_runs",
            "id",
            database_name,
            "NO ACTION",
            "NO ACTION",
        ),
    )
    failures: list[str] = []
    if not columns_valid:
        failures.append("columns")
    if indexes != expected_indexes:
        failures.append("indexes")
    if foreign_keys != expected_foreign_keys:
        failures.append("foreign_keys")
    if not checks_valid:
        failures.append("checks")
    if (
        not table
        or table["engine"] != "InnoDB"
        or not str(table["table_collation"]).startswith("utf8mb4_")
    ):
        failures.append("table")
    if failures:
        raise MigrationError(
            "partsouq_http_diagnostics schema contract mismatch: " + ",".join(failures)
        )


def _assert_bounded_group_receipt_contract(connection: MigrationConnection) -> None:
    """確認 036 的 receipt 實體與正式 view 沒有被 ledger 假綠掩蓋。"""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              EXISTS (
                SELECT 1
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_group_receipts'
                  AND TABLE_TYPE = 'BASE TABLE'
                  AND ENGINE = 'InnoDB'
              ) AS receipt_table_ready,
              (
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_group_receipts'
              ) = 8
              AND (
                SELECT COUNT(*)
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_group_receipts'
                  AND (
                    (COLUMN_NAME = 'crawl_run_id' AND COLUMN_TYPE = 'int'
                     AND IS_NULLABLE = 'NO')
                    OR (COLUMN_NAME = 'group_id' AND COLUMN_TYPE = 'int'
                        AND IS_NULLABLE = 'NO')
                    OR (COLUMN_NAME = 'source_artifact_id'
                        AND COLUMN_TYPE = 'bigint unsigned' AND IS_NULLABLE = 'NO')
                    OR (COLUMN_NAME = 'status' AND COLUMN_TYPE = 'varchar(16)'
                        AND IS_NULLABLE = 'NO')
                    OR (COLUMN_NAME = 'parsed_part_count'
                        AND COLUMN_TYPE = 'int unsigned' AND IS_NULLABLE = 'NO')
                    OR (COLUMN_NAME = 'accepted_part_count'
                        AND COLUMN_TYPE = 'int unsigned' AND IS_NULLABLE = 'NO')
                    OR (COLUMN_NAME = 'skipped_record_count'
                        AND COLUMN_TYPE = 'int unsigned' AND IS_NULLABLE = 'NO')
                    OR (COLUMN_NAME = 'recorded_at' AND COLUMN_TYPE = 'datetime(6)'
                        AND IS_NULLABLE = 'NO')
                  )
              ) = 8 AS receipt_columns_ready,
              (
                SELECT COUNT(*)
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_group_receipts'
                  AND INDEX_NAME = 'PRIMARY'
              ) = 2
              AND (
                SELECT COALESCE(SUM(
                  (SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'crawl_run_id')
                  OR (SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'group_id')
                ), 0)
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_group_receipts'
                  AND INDEX_NAME = 'PRIMARY'
              ) = 2 AS receipt_primary_key_ready,
              (
                SELECT COUNT(*)
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_group_receipts'
                  AND INDEX_NAME = 'uq_bounded_group_receipt_artifact'
              ) = 1
              AND (
                SELECT COALESCE(SUM(
                  NON_UNIQUE = 0
                  AND SEQ_IN_INDEX = 1
                  AND COLUMN_NAME = 'source_artifact_id'
                ), 0)
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_group_receipts'
                  AND INDEX_NAME = 'uq_bounded_group_receipt_artifact'
              ) = 1 AS receipt_artifact_unique_ready,
              (
                SELECT COUNT(*)
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_parts'
                  AND INDEX_NAME = 'idx_bounded_run_group'
              ) = 2
              AND (
                SELECT COALESCE(SUM(
                  (SEQ_IN_INDEX = 1 AND COLUMN_NAME = 'crawl_run_id')
                  OR (SEQ_IN_INDEX = 2 AND COLUMN_NAME = 'group_id')
                ), 0)
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bounded_parts'
                  AND INDEX_NAME = 'idx_bounded_run_group'
              ) = 2 AS bounded_group_index_ready,
              EXISTS (
                SELECT 1
                FROM information_schema.REFERENTIAL_CONSTRAINTS AS constraint_rule
                JOIN information_schema.KEY_COLUMN_USAGE AS key_column
                  ON key_column.CONSTRAINT_SCHEMA = constraint_rule.CONSTRAINT_SCHEMA
                 AND key_column.TABLE_NAME = constraint_rule.TABLE_NAME
                 AND key_column.CONSTRAINT_NAME = constraint_rule.CONSTRAINT_NAME
                WHERE constraint_rule.CONSTRAINT_SCHEMA = DATABASE()
                  AND constraint_rule.TABLE_NAME = 'bounded_group_receipts'
                  AND constraint_rule.CONSTRAINT_NAME = 'fk_bounded_group_receipt_run'
                  AND key_column.COLUMN_NAME = 'crawl_run_id'
                  AND key_column.REFERENCED_TABLE_NAME = 'crawl_runs'
                  AND key_column.REFERENCED_COLUMN_NAME = 'id'
                  AND constraint_rule.DELETE_RULE = 'CASCADE'
              ) AS receipt_run_foreign_key_ready,
              (
                SELECT COUNT(*)
                FROM information_schema.REFERENTIAL_CONSTRAINTS AS constraint_rule
                JOIN information_schema.KEY_COLUMN_USAGE AS key_column
                  ON key_column.CONSTRAINT_SCHEMA = constraint_rule.CONSTRAINT_SCHEMA
                 AND key_column.TABLE_NAME = constraint_rule.TABLE_NAME
                 AND key_column.CONSTRAINT_NAME = constraint_rule.CONSTRAINT_NAME
                WHERE constraint_rule.CONSTRAINT_SCHEMA = DATABASE()
                  AND constraint_rule.TABLE_NAME = 'bounded_group_receipts'
                  AND constraint_rule.CONSTRAINT_NAME = 'fk_bounded_group_receipt_artifact'
                  AND (
                    (key_column.ORDINAL_POSITION = 1
                     AND key_column.COLUMN_NAME = 'source_artifact_id'
                     AND key_column.REFERENCED_TABLE_NAME = 'partsouq_http_artifacts'
                     AND key_column.REFERENCED_COLUMN_NAME = 'id')
                    OR (key_column.ORDINAL_POSITION = 2
                        AND key_column.COLUMN_NAME = 'crawl_run_id'
                        AND key_column.REFERENCED_TABLE_NAME = 'partsouq_http_artifacts'
                        AND key_column.REFERENCED_COLUMN_NAME = 'crawl_run_id')
                  )
              ) = 2 AS receipt_artifact_foreign_key_ready,
              EXISTS (
                SELECT 1
                FROM information_schema.CHECK_CONSTRAINTS AS check_constraint
                JOIN information_schema.TABLE_CONSTRAINTS AS table_constraint
                  ON table_constraint.CONSTRAINT_SCHEMA = check_constraint.CONSTRAINT_SCHEMA
                 AND table_constraint.CONSTRAINT_NAME = check_constraint.CONSTRAINT_NAME
                WHERE check_constraint.CONSTRAINT_SCHEMA = DATABASE()
                  AND table_constraint.TABLE_SCHEMA = DATABASE()
                  AND table_constraint.TABLE_NAME = 'bounded_group_receipts'
                  AND table_constraint.CONSTRAINT_TYPE = 'CHECK'
                  AND table_constraint.ENFORCED = 'YES'
                  AND check_constraint.CONSTRAINT_NAME = 'chk_bounded_group_receipt_status'
                  AND LOCATE('status', LOWER(check_constraint.CHECK_CLAUSE)) > 0
                  AND LOCATE('done', LOWER(check_constraint.CHECK_CLAUSE)) > 0
                  AND LOCATE('partial', LOWER(check_constraint.CHECK_CLAUSE)) > 0
              )
              AND EXISTS (
                SELECT 1
                FROM information_schema.CHECK_CONSTRAINTS AS check_constraint
                JOIN information_schema.TABLE_CONSTRAINTS AS table_constraint
                  ON table_constraint.CONSTRAINT_SCHEMA = check_constraint.CONSTRAINT_SCHEMA
                 AND table_constraint.CONSTRAINT_NAME = check_constraint.CONSTRAINT_NAME
                WHERE check_constraint.CONSTRAINT_SCHEMA = DATABASE()
                  AND table_constraint.TABLE_SCHEMA = DATABASE()
                  AND table_constraint.TABLE_NAME = 'bounded_group_receipts'
                  AND table_constraint.CONSTRAINT_TYPE = 'CHECK'
                  AND table_constraint.ENFORCED = 'YES'
                  AND check_constraint.CONSTRAINT_NAME = 'chk_bounded_group_receipt_counts'
                  AND LOCATE('accepted_part_count', LOWER(check_constraint.CHECK_CLAUSE)) > 0
                  AND LOCATE('parsed_part_count', LOWER(check_constraint.CHECK_CLAUSE)) > 0
                  AND LOCATE('status', LOWER(check_constraint.CHECK_CLAUSE)) > 0
                  AND LOCATE('done', LOWER(check_constraint.CHECK_CLAUSE)) > 0
                  AND LOCATE('partial', LOWER(check_constraint.CHECK_CLAUSE)) > 0
              ) AS receipt_checks_ready,
              EXISTS (
                SELECT 1
                FROM information_schema.VIEWS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'v_current_catalog_parts_evidence_base'
                  AND LOCATE('verified_bounded_evidence', LOWER(VIEW_DEFINITION)) > 0
                  AND LOCATE('verified_bounded_records', LOWER(VIEW_DEFINITION)) > 0
                  AND LOCATE('catalog_desired_bounded_scope', LOWER(VIEW_DEFINITION)) > 0
                  AND LOCATE('evidence_record_sha256', LOWER(VIEW_DEFINITION)) > 0
              ) AS evidence_base_view_ready,
              EXISTS (
                SELECT 1
                FROM information_schema.VIEWS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'v_current_catalog_parts'
                  AND LOCATE(
                    'v_current_catalog_parts_evidence_base', LOWER(VIEW_DEFINITION)
                  ) > 0
                  AND LOCATE('bounded_group_receipts', LOWER(VIEW_DEFINITION)) > 0
                  AND LOCATE('receipt_integrity', LOWER(VIEW_DEFINITION)) > 0
                  AND LOCATE('verified_bounded_group_receipts', LOWER(VIEW_DEFINITION)) > 0
              ) AS formal_receipt_view_ready,
              EXISTS (
                SELECT 1
                FROM information_schema.TRIGGERS
                WHERE TRIGGER_SCHEMA = DATABASE()
                  AND TRIGGER_NAME = 'prevent_bounded_group_receipt_update'
                  AND EVENT_OBJECT_TABLE = 'bounded_group_receipts'
                  AND ACTION_TIMING = 'BEFORE'
                  AND EVENT_MANIPULATION = 'UPDATE'
                  AND LOCATE('from crawl_runs', LOWER(ACTION_STATEMENT)) > 0
                  AND LOCATE('old.crawl_run_id', LOWER(ACTION_STATEMENT)) > 0
                  AND LOCATE('status = ''bounded_success''', LOWER(ACTION_STATEMENT)) > 0
                  AND LOCATE('signal sqlstate', LOWER(ACTION_STATEMENT)) > 0
              ) AS receipt_update_trigger_ready,
              EXISTS (
                SELECT 1
                FROM information_schema.TRIGGERS
                WHERE TRIGGER_SCHEMA = DATABASE()
                  AND TRIGGER_NAME = 'prevent_bounded_group_receipt_delete'
                  AND EVENT_OBJECT_TABLE = 'bounded_group_receipts'
                  AND ACTION_TIMING = 'BEFORE'
                  AND EVENT_MANIPULATION = 'DELETE'
                  AND LOCATE('from crawl_runs', LOWER(ACTION_STATEMENT)) > 0
                  AND LOCATE('old.crawl_run_id', LOWER(ACTION_STATEMENT)) > 0
                  AND LOCATE('status = ''bounded_success''', LOWER(ACTION_STATEMENT)) > 0
                  AND LOCATE('signal sqlstate', LOWER(ACTION_STATEMENT)) > 0
              ) AS receipt_delete_trigger_ready
            """
        )
        contract = cursor.fetchone()
        cursor.execute(
            """
            SELECT
              check_constraint.CONSTRAINT_NAME AS constraint_name,
              check_constraint.CHECK_CLAUSE AS check_clause,
              table_constraint.ENFORCED AS enforced
            FROM information_schema.CHECK_CONSTRAINTS AS check_constraint
            JOIN information_schema.TABLE_CONSTRAINTS AS table_constraint
              ON table_constraint.CONSTRAINT_SCHEMA = check_constraint.CONSTRAINT_SCHEMA
             AND table_constraint.CONSTRAINT_NAME = check_constraint.CONSTRAINT_NAME
            WHERE check_constraint.CONSTRAINT_SCHEMA = DATABASE()
              AND table_constraint.TABLE_SCHEMA = DATABASE()
              AND table_constraint.TABLE_NAME = 'bounded_group_receipts'
              AND table_constraint.CONSTRAINT_TYPE = 'CHECK'
              AND check_constraint.CONSTRAINT_NAME IN (
                'chk_bounded_group_receipt_status',
                'chk_bounded_group_receipt_counts'
              )
            ORDER BY check_constraint.CONSTRAINT_NAME
            """
        )
        receipt_checks = {
            str(row["constraint_name"]): (
                _normalize_bounded_group_receipt_check_clause(str(row["check_clause"])),
                str(row["enforced"]),
            )
            for row in cursor.fetchall()
        }
    contract_values = dict(contract or {})
    contract_values["receipt_checks_ready"] = int(
        receipt_checks
        == {name: (clause, "YES") for name, clause in _BOUNDED_GROUP_RECEIPT_CHECK_CLAUSES.items()}
    )
    contract_keys = (
        "receipt_table_ready",
        "receipt_columns_ready",
        "receipt_primary_key_ready",
        "receipt_artifact_unique_ready",
        "bounded_group_index_ready",
        "receipt_run_foreign_key_ready",
        "receipt_artifact_foreign_key_ready",
        "receipt_checks_ready",
        "evidence_base_view_ready",
        "formal_receipt_view_ready",
        "receipt_update_trigger_ready",
        "receipt_delete_trigger_ready",
    )
    missing = [key for key in contract_keys if int(contract_values.get(key, 0)) != 1]
    if missing:
        raise MigrationError("bounded group receipt schema contract mismatch: " + ",".join(missing))


def _assert_station_admin_vin_decode_contract(connection: MigrationConnection) -> None:
    """確認 station-admin 的 VIN 解碼完整度欄位仍由正式 view 提供。"""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.VIEWS
              WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'station_admin_vin_vehicle_mappings'
                AND LOCATE('decode_completeness', LOWER(VIEW_DEFINITION)) > 0
                AND LOCATE('partial_no_detail_data', LOWER(VIEW_DEFINITION)) > 0
                AND LOCATE('partial_missing_model', LOWER(VIEW_DEFINITION)) > 0
                AND LOCATE('partial_missing_powertrain_or_trim', LOWER(VIEW_DEFINITION)) > 0
                AND LOCATE('decoded_complete', LOWER(VIEW_DEFINITION)) > 0
                AND LOCATE('decoded_complete_with_warning', LOWER(VIEW_DEFINITION)) > 0
            )
            AND EXISTS (
              SELECT 1
              FROM information_schema.COLUMNS
              WHERE TABLE_SCHEMA = DATABASE()
                AND TABLE_NAME = 'station_admin_vin_vehicle_mappings'
                AND COLUMN_NAME = 'decode_completeness'
            ) AS station_decode_contract_ready
            """
        )
        contract = cursor.fetchone()
    if not contract or int(contract.get("station_decode_contract_ready", 0)) != 1:
        raise MigrationError("station-admin VIN decode schema contract mismatch")


def _assert_nhtsa_run_lease_contract(connection: MigrationConnection) -> None:
    required_tables = (
        "scheduled_job_runs",
        "nhtsa_sync_runs",
        "nhtsa_current_artifacts",
        "nhtsa_schema_migrations",
    )
    if any(not _table_exists(connection, table) for table in required_tables):
        raise MigrationError("NHTSA run lease schema contract is unavailable")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME AS table_name,COLUMN_NAME AS column_name,"
            "COLUMN_TYPE AS column_type,IS_NULLABLE AS is_nullable,"
            "COLUMN_DEFAULT AS column_default,EXTRA AS extra,"
            "CHARACTER_SET_NAME AS character_set,COLLATION_NAME AS collation_name "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND ("
            "(TABLE_NAME='scheduled_job_runs' AND COLUMN_NAME='parent_scheduled_job_run_id') OR "
            "(TABLE_NAME='nhtsa_sync_runs' AND COLUMN_NAME IN ("
            "'scheduled_job_run_id','lease_slot','lease_token','heartbeat_at',"
            "'lease_expires_at')) OR "
            "(TABLE_NAME='nhtsa_current_artifacts' AND COLUMN_NAME='published_run_id')) "
            "ORDER BY BINARY TABLE_NAME,ORDINAL_POSITION"
        )
        columns = {
            (str(row["table_name"]), str(row["column_name"])): (
                str(row["column_type"]).lower(),
                str(row["is_nullable"]),
                row["column_default"],
                str(row["extra"]),
                row["character_set"],
                row["collation_name"],
            )
            for row in cursor.fetchall()
        }
        cursor.execute(
            "SELECT TABLE_NAME AS table_name,INDEX_NAME AS index_name,"
            "NON_UNIQUE AS non_unique,SEQ_IN_INDEX AS sequence,"
            "COLUMN_NAME AS column_name,SUB_PART AS sub_part,INDEX_TYPE AS index_type,"
            "IS_VISIBLE AS is_visible FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND ("
            "(TABLE_NAME='scheduled_job_runs' AND INDEX_NAME='uq_scheduled_job_parent_stage') OR "
            "(TABLE_NAME='nhtsa_sync_runs' AND INDEX_NAME IN ("
            "'idx_nhtsa_sync_lease_expiry','uq_nhtsa_sync_scheduled_job',"
            "'uq_nhtsa_sync_lease_slot')) OR "
            "(TABLE_NAME='nhtsa_current_artifacts' "
            "AND INDEX_NAME='idx_nhtsa_current_published_run')) "
            "ORDER BY BINARY TABLE_NAME,BINARY INDEX_NAME,SEQ_IN_INDEX"
        )
        indexes = tuple(
            (
                str(row["table_name"]),
                str(row["index_name"]),
                int(row["non_unique"]),
                int(row["sequence"]),
                str(row["column_name"]),
                row["sub_part"],
                str(row["index_type"]),
                str(row["is_visible"]),
            )
            for row in cursor.fetchall()
        )
        cursor.execute("SELECT DATABASE() AS database_name")
        database_row = cursor.fetchone()
        database_name = str(database_row["database_name"]) if database_row else ""
        cursor.execute(
            "SELECT rc.TABLE_NAME AS table_name,rc.CONSTRAINT_NAME AS constraint_name,"
            "kcu.COLUMN_NAME AS column_name,kcu.REFERENCED_TABLE_NAME AS referenced_table,"
            "kcu.REFERENCED_COLUMN_NAME AS referenced_column,"
            "kcu.REFERENCED_TABLE_SCHEMA AS referenced_schema,"
            "rc.UPDATE_RULE AS update_rule,rc.DELETE_RULE AS delete_rule "
            "FROM information_schema.REFERENTIAL_CONSTRAINTS AS rc "
            "JOIN information_schema.KEY_COLUMN_USAGE AS kcu "
            "ON kcu.CONSTRAINT_SCHEMA=rc.CONSTRAINT_SCHEMA "
            "AND kcu.TABLE_NAME=rc.TABLE_NAME AND kcu.CONSTRAINT_NAME=rc.CONSTRAINT_NAME "
            "WHERE rc.CONSTRAINT_SCHEMA=DATABASE() AND rc.CONSTRAINT_NAME IN ("
            "'fk_scheduled_job_parent','fk_nhtsa_sync_scheduled_job',"
            "'fk_nhtsa_current_published_run') "
            "ORDER BY BINARY rc.TABLE_NAME,BINARY rc.CONSTRAINT_NAME,kcu.ORDINAL_POSITION"
        )
        foreign_keys = tuple(
            (
                str(row["table_name"]),
                str(row["constraint_name"]),
                str(row["column_name"]),
                str(row["referenced_table"]),
                str(row["referenced_column"]),
                str(row["referenced_schema"]),
                str(row["update_rule"]),
                str(row["delete_rule"]),
            )
            for row in cursor.fetchall()
        )
        cursor.execute(
            "SELECT constraints.TABLE_NAME AS table_name,"
            "checks.CONSTRAINT_NAME AS constraint_name,"
            "checks.CHECK_CLAUSE AS check_clause,constraints.ENFORCED AS enforced "
            "FROM information_schema.CHECK_CONSTRAINTS AS checks "
            "JOIN information_schema.TABLE_CONSTRAINTS AS constraints "
            "ON constraints.CONSTRAINT_SCHEMA=checks.CONSTRAINT_SCHEMA "
            "AND constraints.CONSTRAINT_NAME=checks.CONSTRAINT_NAME "
            "WHERE checks.CONSTRAINT_SCHEMA=DATABASE() "
            "AND constraints.TABLE_SCHEMA=DATABASE() AND ("
            "(constraints.TABLE_NAME='scheduled_job_runs' "
            "AND checks.CONSTRAINT_NAME='chk_scheduled_job_not_own_parent') OR "
            "(constraints.TABLE_NAME='nhtsa_sync_runs' "
            "AND checks.CONSTRAINT_NAME='chk_nhtsa_sync_status_lease')) "
            "ORDER BY BINARY constraints.TABLE_NAME,BINARY checks.CONSTRAINT_NAME"
        )
        checks = {
            (str(row["table_name"]), str(row["constraint_name"])): (
                _normalize_nhtsa_check_clause(str(row["check_clause"])),
                str(row["enforced"]),
            )
            for row in cursor.fetchall()
        }
        cursor.execute(
            "SELECT TABLE_NAME AS table_name,ENGINE AS engine,TABLE_COLLATION AS table_collation "
            "FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() "
            "AND TABLE_NAME IN ('scheduled_job_runs','nhtsa_sync_runs',"
            "'nhtsa_current_artifacts') ORDER BY BINARY TABLE_NAME"
        )
        tables = {
            str(row["table_name"]): (
                str(row["engine"]),
                str(row["table_collation"]),
            )
            for row in cursor.fetchall()
        }
        cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_schema_migrations WHERE version=2")
        version_row = cursor.fetchone()

    expected_columns = {
        ("scheduled_job_runs", "parent_scheduled_job_run_id"): (
            "bigint unsigned",
            "YES",
            None,
            "",
            None,
            None,
        ),
        ("nhtsa_sync_runs", "scheduled_job_run_id"): (
            "bigint unsigned",
            "YES",
            None,
            "",
            None,
            None,
        ),
        ("nhtsa_sync_runs", "lease_slot"): (
            "varchar(16)",
            "YES",
            None,
            "",
            "ascii",
            "ascii_bin",
        ),
        ("nhtsa_sync_runs", "lease_token"): (
            "char(64)",
            "YES",
            None,
            "",
            "ascii",
            "ascii_bin",
        ),
        ("nhtsa_sync_runs", "heartbeat_at"): (
            "datetime(6)",
            "YES",
            None,
            "",
            None,
            None,
        ),
        ("nhtsa_sync_runs", "lease_expires_at"): (
            "datetime(6)",
            "YES",
            None,
            "",
            None,
            None,
        ),
        ("nhtsa_current_artifacts", "published_run_id"): (
            "bigint unsigned",
            "NO",
            None,
            "",
            None,
            None,
        ),
    }
    expected_indexes = (
        (
            "nhtsa_current_artifacts",
            "idx_nhtsa_current_published_run",
            1,
            1,
            "published_run_id",
            None,
            "BTREE",
            "YES",
        ),
        (
            "nhtsa_sync_runs",
            "idx_nhtsa_sync_lease_expiry",
            1,
            1,
            "status",
            None,
            "BTREE",
            "YES",
        ),
        (
            "nhtsa_sync_runs",
            "idx_nhtsa_sync_lease_expiry",
            1,
            2,
            "lease_expires_at",
            None,
            "BTREE",
            "YES",
        ),
        (
            "nhtsa_sync_runs",
            "idx_nhtsa_sync_lease_expiry",
            1,
            3,
            "id",
            None,
            "BTREE",
            "YES",
        ),
        (
            "nhtsa_sync_runs",
            "uq_nhtsa_sync_lease_slot",
            0,
            1,
            "lease_slot",
            None,
            "BTREE",
            "YES",
        ),
        (
            "nhtsa_sync_runs",
            "uq_nhtsa_sync_scheduled_job",
            0,
            1,
            "scheduled_job_run_id",
            None,
            "BTREE",
            "YES",
        ),
        (
            "scheduled_job_runs",
            "uq_scheduled_job_parent_stage",
            0,
            1,
            "parent_scheduled_job_run_id",
            None,
            "BTREE",
            "YES",
        ),
        (
            "scheduled_job_runs",
            "uq_scheduled_job_parent_stage",
            0,
            2,
            "job_name",
            None,
            "BTREE",
            "YES",
        ),
    )
    expected_foreign_keys = (
        (
            "nhtsa_current_artifacts",
            "fk_nhtsa_current_published_run",
            "published_run_id",
            "nhtsa_sync_runs",
            "id",
            database_name,
            "NO ACTION",
            "NO ACTION",
        ),
        (
            "nhtsa_sync_runs",
            "fk_nhtsa_sync_scheduled_job",
            "scheduled_job_run_id",
            "scheduled_job_runs",
            "id",
            database_name,
            "NO ACTION",
            "NO ACTION",
        ),
        (
            "scheduled_job_runs",
            "fk_scheduled_job_parent",
            "parent_scheduled_job_run_id",
            "scheduled_job_runs",
            "id",
            database_name,
            "NO ACTION",
            "NO ACTION",
        ),
    )
    lease_check = (
        "(((binary status = binary 'running') and "
        "(scheduled_job_run_id is not null) and (lease_slot is not null) and "
        "(binary lease_slot = binary 'writer') and (lease_token is not null) and "
        "regexp_like(lease_token,'^[0-9a-f]{64}$') and "
        "(heartbeat_at is not null) and (lease_expires_at is not null) and "
        "(lease_expires_at > heartbeat_at) and (ended_at is null)) or "
        "((binary status in (binary 'completed',binary 'failed',"
        "binary 'interrupted')) and (lease_slot is null) and "
        "(lease_token is null) and (lease_expires_at is null) and "
        "(ended_at is not null)))"
    )
    expected_checks = {
        ("nhtsa_sync_runs", "chk_nhtsa_sync_status_lease"): (lease_check, "YES"),
    }
    failures: list[str] = []
    if columns != expected_columns:
        failures.append("columns")
    if indexes != expected_indexes:
        failures.append("indexes")
    if foreign_keys != expected_foreign_keys:
        failures.append("foreign_keys")
    if checks != expected_checks:
        failures.append("checks")
    if set(tables) != set(required_tables[:3]) or any(
        engine != "InnoDB" or not collation.startswith("utf8mb4_")
        for engine, collation in tables.values()
    ):
        failures.append("tables")
    if not version_row or int(version_row["row_count"]) != 1:
        failures.append("nhtsa_schema_version")
    if failures:
        raise MigrationError("NHTSA run lease schema contract mismatch: " + ",".join(failures))


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


if __name__ == "__main__":
    main()
