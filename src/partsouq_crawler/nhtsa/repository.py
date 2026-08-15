from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pymysql
from pymysql.connections import Connection
from pymysql.constants import CLIENT
from pymysql.cursors import DictCursor

from partsouq_crawler.nhtsa.api import normalize_vin
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.models import (
    ArtifactMember,
    DownloadedArtifact,
    ParsedRecord,
    RejectedRow,
)

BULK_PARSER_NAME = "nhtsa_bulk_json"
BULK_PARSER_VERSION = "4"


class NhtsaMySQLRepository:
    def __init__(self, connection: Connection[DictCursor]) -> None:
        self.connection = connection

    @classmethod
    def create(cls, config: NhtsaConfig) -> NhtsaMySQLRepository:
        connection = pymysql.connect(
            host=config.mysql_host,
            port=config.mysql_port,
            user=config.mysql_user,
            password=config.mysql_password,
            database=config.mysql_database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
            client_flag=CLIENT.MULTI_STATEMENTS,
            read_timeout=600,
            write_timeout=600,
        )
        repository = cls(connection)
        repository.apply_schema()
        return repository

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection[DictCursor]]:
        self.connection.begin()
        try:
            yield self.connection
        except BaseException:
            with suppress(pymysql.MySQLError):
                self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def apply_schema(self) -> None:
        schema_path = Path(__file__).with_name("mysql_schema.sql")
        schema = schema_path.read_text(encoding="utf-8")
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(schema)
            while cursor.nextset():
                pass
        self._apply_artifact_lineage_migration()

    def _apply_artifact_lineage_migration(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM nhtsa_schema_migrations WHERE version = 2")
            if cursor.fetchone():
                return
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'nhtsa_artifact_records'
                  AND INDEX_NAME = 'PRIMARY'
                ORDER BY SEQ_IN_INDEX
                """
            )
            primary_columns = tuple(str(row["COLUMN_NAME"]) for row in cursor)

        with self.transaction() as connection, connection.cursor() as cursor:
            if primary_columns != ("artifact_id", "member_name", "source_line"):
                cursor.execute(
                    """
                    ALTER TABLE nhtsa_artifact_records
                        DROP PRIMARY KEY,
                        DROP INDEX uq_nhtsa_artifact_line,
                        ADD PRIMARY KEY (artifact_id, member_name, source_line),
                        ADD INDEX idx_nhtsa_artifact_natural_key (
                            artifact_id, dataset_name, natural_key_sha256, record_sha256
                        )
                    """
                )
            cursor.execute(
                """
                INSERT INTO nhtsa_schema_migrations(version, applied_at)
                VALUES (2, UTC_TIMESTAMP(6))
                """
            )

    def start_run(self, run_key: str, scope_name: str, source_keys: Sequence[str]) -> int:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO nhtsa_sync_runs(
                    run_key, scope_name, status, source_keys_json, started_at, updated_at
                ) VALUES (%s, %s, 'running', %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                """,
                (run_key, scope_name, json.dumps(source_keys)),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
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
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhtsa_sync_runs
                SET status = %s, artifacts_downloaded = %s, artifacts_reused = %s,
                    source_rows = %s, new_versions = %s, rejected_rows = %s,
                    error_message = %s, updated_at = UTC_TIMESTAMP(6),
                    ended_at = UTC_TIMESTAMP(6)
                WHERE id = %s
                """,
                (
                    status,
                    downloaded,
                    reused,
                    source_rows,
                    new_versions,
                    rejected_rows,
                    error_message,
                    run_id,
                ),
            )

    def current_artifact(
        self,
        dataset_name: str,
        source_key: str,
    ) -> dict[str, object] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT a.*
                FROM nhtsa_current_artifacts AS c
                JOIN nhtsa_source_artifacts AS a ON a.id = c.artifact_id
                WHERE c.dataset_name = %s AND c.source_key = %s
                """,
                (dataset_name, source_key),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def artifact_by_content(
        self,
        dataset_name: str,
        source_key: str,
        sha256: str,
        parser_version: str,
    ) -> dict[str, object] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM nhtsa_source_artifacts
                WHERE dataset_name = %s AND source_key = %s
                  AND sha256 = %s AND parser_version = %s
                """,
                (dataset_name, source_key, sha256, parser_version),
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def create_artifact(
        self,
        *,
        dataset_name: str,
        source_key: str,
        source_url: str,
        download: DownloadedArtifact,
        parser_name: str,
        parser_version: str,
    ) -> int:
        if download.path is None or download.sha256 is None:
            raise ValueError("downloaded artifact path and sha256 are required")
        headers = download.response_headers
        content_length = self._optional_int(headers.get("content-length"))
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO nhtsa_source_artifacts(
                    dataset_name, source_key, source_url, http_status,
                    response_headers_json, etag, last_modified, content_type,
                    content_length, sha256, stored_path, byte_count,
                    parser_name, parser_version, status, downloaded_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, 'downloaded', UTC_TIMESTAMP(6)
                )
                ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)
                """,
                (
                    dataset_name,
                    source_key,
                    source_url,
                    download.http_status,
                    json.dumps(headers, sort_keys=True),
                    headers.get("etag"),
                    headers.get("last-modified"),
                    headers.get("content-type"),
                    content_length,
                    download.sha256,
                    str(download.path),
                    download.byte_count,
                    parser_name,
                    parser_version,
                ),
            )
            return int(cursor.lastrowid)

    def store_member(self, artifact_id: int, member: ArtifactMember) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM nhtsa_artifact_members WHERE artifact_id = %s",
                (artifact_id,),
            )
            cursor.execute(
                """
                INSERT INTO nhtsa_artifact_members(
                    artifact_id, member_name, uncompressed_bytes, compressed_bytes,
                    crc32, field_names_json, schema_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    artifact_id,
                    member.name,
                    member.uncompressed_bytes,
                    member.compressed_bytes,
                    member.crc32,
                    json.dumps(member.field_names),
                    member.schema_sha256,
                ),
            )
            cursor.execute(
                """
                UPDATE nhtsa_source_artifacts
                SET status = 'verified', verified_at = UTC_TIMESTAMP(6), error_message = NULL
                WHERE id = %s
                """,
                (artifact_id,),
            )

    def current_schema(self, dataset_name: str, source_key: str) -> str | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.schema_sha256
                FROM nhtsa_current_artifacts AS c
                JOIN nhtsa_artifact_members AS m ON m.artifact_id = c.artifact_id
                WHERE c.dataset_name = %s AND c.source_key = %s
                """,
                (dataset_name, source_key),
            )
            row = cursor.fetchone()
        return str(row["schema_sha256"]) if row else None

    def reset_artifact_import(self, artifact_id: int) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM nhtsa_artifact_records WHERE artifact_id = %s", (artifact_id,)
            )
            cursor.execute("DELETE FROM nhtsa_rejected_rows WHERE artifact_id = %s", (artifact_id,))
            cursor.execute(
                """
                UPDATE nhtsa_source_artifacts
                SET status = 'importing', source_rows = 0, new_versions = 0,
                    rejected_rows = 0, imported_at = NULL, error_message = NULL
                WHERE id = %s
                """,
                (artifact_id,),
            )

    def insert_records(self, artifact_id: int, records: Sequence[ParsedRecord]) -> int:
        if not records:
            return 0
        version_values = [
            (
                record.dataset_name,
                record.natural_key_sha256,
                record.record_sha256,
                record.natural_key_text,
                record.external_id,
                record.make_name,
                record.model_name,
                record.model_year,
                record.campaign_number,
                record.component_name,
                record.summary_text,
                record.payload_json,
            )
            for record in records
        ]
        mapping_values = [
            (
                artifact_id,
                record.dataset_name,
                record.natural_key_sha256,
                record.record_sha256,
                record.member_name,
                record.source_line,
            )
            for record in records
        ]
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT IGNORE INTO nhtsa_record_versions(
                    dataset_name, natural_key_sha256, record_sha256, natural_key_text,
                    external_id, make_name, model_name, model_year, campaign_number,
                    component_name, summary_text, payload_json, first_observed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                """,
                version_values,
            )
            new_versions = cursor.rowcount
            cursor.executemany(
                """
                INSERT INTO nhtsa_artifact_records(
                    artifact_id, dataset_name, natural_key_sha256, record_sha256,
                    member_name, source_line
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                mapping_values,
            )
        return int(new_versions)

    def insert_rejections(self, artifact_id: int, rows: Sequence[RejectedRow]) -> None:
        if not rows:
            return
        values = [
            (
                artifact_id,
                row.member_name,
                row.source_line,
                row.raw_sha256,
                row.error_type,
                row.error_message,
                row.raw_text,
            )
            for row in rows
        ]
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO nhtsa_rejected_rows(
                    artifact_id, member_name, source_line, raw_sha256,
                    error_type, error_message, raw_text, rejected_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    raw_sha256 = VALUES(raw_sha256), error_type = VALUES(error_type),
                    error_message = VALUES(error_message), raw_text = VALUES(raw_text),
                    rejected_at = UTC_TIMESTAMP(6)
                """,
                values,
            )

    def complete_artifact(
        self,
        artifact_id: int,
        *,
        source_rows: int,
        new_versions: int,
        rejected_rows: int,
    ) -> None:
        status = "imported" if rejected_rows == 0 else "quarantined"
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE nhtsa_source_artifacts
                SET status = %s, source_rows = %s, new_versions = %s,
                    rejected_rows = %s, imported_at = UTC_TIMESTAMP(6),
                    error_message = CASE
                        WHEN %s > 0 THEN 'one or more rows were rejected' ELSE NULL
                    END
                WHERE id = %s
                """,
                (status, source_rows, new_versions, rejected_rows, rejected_rows, artifact_id),
            )

    def quarantine_artifact(
        self,
        artifact_id: int,
        error_message: str,
        *,
        only_if_unpublished: bool = False,
    ) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            if only_if_unpublished:
                cursor.execute(
                    """
                    UPDATE nhtsa_source_artifacts AS artifact
                    LEFT JOIN nhtsa_current_artifacts AS current
                      ON current.artifact_id = artifact.id
                    SET artifact.status = 'quarantined', artifact.error_message = %s
                    WHERE artifact.id = %s AND current.artifact_id IS NULL
                    """,
                    (error_message, artifact_id),
                )
                return
            cursor.execute(
                """
                UPDATE nhtsa_source_artifacts
                SET status = 'quarantined', error_message = %s
                WHERE id = %s
                """,
                (error_message, artifact_id),
            )

    def publish_artifacts(
        self,
        artifacts: Sequence[tuple[str, str, int]],
        *,
        replace_datasets: Sequence[str] = (),
    ) -> None:
        artifact_ids = [artifact_id for _, _, artifact_id in artifacts]
        if not artifact_ids:
            raise ValueError("no NHTSA artifacts to publish")
        placeholders = ",".join(["%s"] * len(artifact_ids))
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id, status, rejected_rows
                FROM nhtsa_source_artifacts
                WHERE id IN ({placeholders})
                """,
                artifact_ids,
            )
            rows = cursor.fetchall()
            if len(rows) != len(artifact_ids) or any(
                row["status"] != "imported" or int(row["rejected_rows"]) != 0 for row in rows
            ):
                raise ValueError("all NHTSA artifacts must be imported without rejections")
            cursor.execute(
                f"""
                SELECT dataset_name, natural_key_sha256,
                       COUNT(DISTINCT record_sha256) AS version_count
                FROM nhtsa_artifact_records
                WHERE artifact_id IN ({placeholders})
                GROUP BY dataset_name, natural_key_sha256
                HAVING COUNT(DISTINCT artifact_id) > 1
                   AND COUNT(DISTINCT record_sha256) > 1
                LIMIT 1
                """,
                artifact_ids,
            )
            duplicate = cursor.fetchone()
            if duplicate:
                raise ValueError(
                    "duplicate natural key across selected artifacts: "
                    f"{duplicate['dataset_name']}:{duplicate['natural_key_sha256']}"
                )
            if replace_datasets:
                dataset_placeholders = ",".join(["%s"] * len(replace_datasets))
                cursor.execute(
                    f"""
                    DELETE FROM nhtsa_current_artifacts
                    WHERE dataset_name IN ({dataset_placeholders})
                    """,
                    tuple(replace_datasets),
                )
            cursor.executemany(
                """
                INSERT INTO nhtsa_current_artifacts(
                    dataset_name, source_key, artifact_id, published_at
                ) VALUES (%s, %s, %s, UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    artifact_id = VALUES(artifact_id), published_at = UTC_TIMESTAMP(6)
                """,
                [
                    (dataset_name, source_key, artifact_id)
                    for dataset_name, source_key, artifact_id in artifacts
                ],
            )

    def publish_vin_decode(
        self,
        artifact_id: int,
        source_key: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        vin = normalize_vin(str(payload.get("VIN") or ""))
        make_name = str(payload.get("Make") or "").strip()
        model_name = str(payload.get("Model") or "").strip()
        model_year_raw = str(payload.get("ModelYear") or "").strip()
        error_code = str(payload.get("ErrorCode") or "").strip()
        if not make_name or not model_name or not model_year_raw.isdigit():
            raise ValueError("NHTSA VIN decode is missing VIN, Make, Model, or ModelYear")
        model_year = int(model_year_raw)
        if not 1886 <= model_year <= 2100:
            raise ValueError(f"NHTSA VIN decode returned an invalid model year: {model_year}")
        if error_code != "0":
            raise ValueError(
                f"NHTSA VIN decode failed with ErrorCode={error_code}: "
                f"{str(payload.get('ErrorText') or '').strip()}"
            )
        displacement_raw = str(payload.get("DisplacementL") or "").strip()
        try:
            displacement_l = Decimal(displacement_raw) if displacement_raw else None
        except InvalidOperation as error:
            raise ValueError(
                f"NHTSA VIN decode returned an invalid DisplacementL: {displacement_raw}"
            ) from error
        if displacement_l is not None and (not displacement_l.is_finite() or displacement_l < 0):
            raise ValueError(
                f"NHTSA VIN decode returned an invalid DisplacementL: {displacement_raw}"
            )
        payload_json = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT dataset_name, source_key, source_url, status, rejected_rows
                FROM nhtsa_source_artifacts WHERE id = %s
                """,
                (artifact_id,),
            )
            artifact = cursor.fetchone()
            if (
                artifact is None
                or artifact["dataset_name"] != "vpic_vin_decodes"
                or artifact["source_key"] != source_key
                or artifact["status"] != "imported"
                or int(artifact["rejected_rows"]) != 0
            ):
                raise ValueError("VIN decode artifact is not publishable")
            cursor.execute(
                """
                INSERT INTO nhtsa_vin_decodes(
                    vin, make_name, model_name, model_year, engine_configuration,
                    engine_model, displacement_l, trim_name, series_name, error_code,
                    error_text, payload_json, source_url, source_artifact_id, decoded_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, UTC_TIMESTAMP(6)
                )
                ON DUPLICATE KEY UPDATE
                    make_name = VALUES(make_name), model_name = VALUES(model_name),
                    model_year = VALUES(model_year),
                    engine_configuration = VALUES(engine_configuration),
                    engine_model = VALUES(engine_model), displacement_l = VALUES(displacement_l),
                    trim_name = VALUES(trim_name), series_name = VALUES(series_name),
                    error_code = VALUES(error_code), error_text = VALUES(error_text),
                    payload_json = VALUES(payload_json), source_url = VALUES(source_url),
                    source_artifact_id = VALUES(source_artifact_id), decoded_at = VALUES(decoded_at)
                """,
                (
                    vin,
                    make_name,
                    model_name,
                    model_year,
                    str(payload.get("EngineConfiguration") or "").strip() or None,
                    str(payload.get("EngineModel") or "").strip() or None,
                    displacement_l,
                    str(payload.get("Trim") or "").strip() or None,
                    str(payload.get("Series") or "").strip() or None,
                    error_code,
                    str(payload.get("ErrorText") or "").strip() or None,
                    payload_json,
                    str(artifact["source_url"]),
                    artifact_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO nhtsa_current_artifacts(
                    dataset_name, source_key, artifact_id, published_at
                ) VALUES ('vpic_vin_decodes', %s, %s, UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    artifact_id = VALUES(artifact_id), published_at = UTC_TIMESTAMP(6)
                """,
                (source_key, artifact_id),
            )
        return {
            "vin": vin,
            "make_name": make_name,
            "model_name": model_name,
            "model_year": model_year,
            "engine_configuration": str(payload.get("EngineConfiguration") or "").strip() or None,
            "engine_model": str(payload.get("EngineModel") or "").strip() or None,
            "displacement_l": str(displacement_l) if displacement_l is not None else None,
            "trim_name": str(payload.get("Trim") or "").strip() or None,
        }

    def status_report(self) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.dataset_name, SUM(a.source_rows) AS row_count
                FROM nhtsa_current_artifacts AS c
                JOIN nhtsa_source_artifacts AS a ON a.id = c.artifact_id
                GROUP BY c.dataset_name ORDER BY c.dataset_name
                """
            )
            current_counts = {str(row["dataset_name"]): int(row["row_count"]) for row in cursor}
            cursor.execute(
                """
                SELECT status, COUNT(*) AS artifact_count
                FROM nhtsa_source_artifacts GROUP BY status ORDER BY status
                """
            )
            artifact_counts = {str(row["status"]): int(row["artifact_count"]) for row in cursor}
            cursor.execute(
                """
                SELECT a.id, a.dataset_name, a.source_key,
                       (SELECT COUNT(*) FROM nhtsa_artifact_records AS r
                        WHERE r.artifact_id = a.id) AS persisted_record_rows,
                       (SELECT COUNT(*) FROM nhtsa_rejected_rows AS q
                        WHERE q.artifact_id = a.id) AS persisted_rejected_rows
                FROM nhtsa_source_artifacts AS a
                WHERE a.status = 'importing'
                ORDER BY a.id
                """
            )
            active_imports = [dict(row) for row in cursor]
            cursor.execute(
                """
                SELECT dataset_name, source_key, artifact_id, published_at
                FROM nhtsa_current_artifacts ORDER BY dataset_name, source_key
                """
            )
            current_artifacts = [dict(row) for row in cursor]
            cursor.execute(
                """
                SELECT id, run_key, scope_name, status, started_at, ended_at,
                       artifacts_downloaded, artifacts_reused, source_rows,
                       new_versions, rejected_rows, error_message
                FROM nhtsa_sync_runs ORDER BY id DESC LIMIT 10
                """
            )
            recent_runs = [dict(row) for row in cursor]
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_rejected_rows")
            rejected_row = cursor.fetchone()
            rejected = int(rejected_row["row_count"]) if rejected_row else 0
            cursor.execute("SELECT COUNT(*) AS row_count FROM nhtsa_vin_decodes")
            vin_row = cursor.fetchone()
            vin_decodes = int(vin_row["row_count"]) if vin_row else 0
        return {
            "database": self.connection.db.decode()
            if isinstance(self.connection.db, bytes)
            else self.connection.db,
            "current_record_counts": current_counts,
            "artifact_status_counts": artifact_counts,
            "active_imports": active_imports,
            "current_artifacts": current_artifacts,
            "rejected_rows": rejected,
            "vin_decodes": vin_decodes,
            "recent_runs": recent_runs,
        }

    def clear_for_tests(self) -> None:
        database = (
            self.connection.db.decode()
            if isinstance(self.connection.db, bytes)
            else str(self.connection.db)
        )
        if not database.endswith("_test"):
            raise ValueError("refusing to clear a non-test NHTSA database")
        tables = (
            "nhtsa_current_artifacts",
            "nhtsa_vin_decodes",
            "nhtsa_rejected_rows",
            "nhtsa_artifact_records",
            "nhtsa_record_versions",
            "nhtsa_artifact_members",
            "nhtsa_source_artifacts",
            "nhtsa_sync_runs",
        )
        with self.transaction() as connection, connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f"DELETE FROM {table}")

    def _optional_int(self, value: str | None) -> int | None:
        if value is None or not value.isdigit():
            return None
        return int(value)
