from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from decimal import Decimal, InvalidOperation
from typing import Any

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from partsouq_catalog.admission import catalog_writer_admission
from partsouq_crawler.nhtsa.api import normalize_vin, vin_source_key
from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.models import (
    ArtifactMember,
    DownloadedArtifact,
    NhtsaRunLease,
    ParsedRecord,
    RejectedRow,
)

BULK_PARSER_NAME = "nhtsa_bulk_json"
BULK_PARSER_VERSION = "4"
RUN_LEASE_SECONDS = 180
HEARTBEAT_DB_TIMEOUT_SECONDS = 15
VPIC_API_DATASETS = frozenset(
    {
        "vpic_makes",
        "vpic_models",
        "vpic_manufacturers",
        "vpic_variables",
        "vpic_variable_values",
    }
)
CSSI_API_DATASETS = frozenset({"cssi_stations"})
BULK_DATASETS_BY_SCOPE = {
    "all": frozenset(
        {
            "safety_ratings",
            "recalls",
            "investigations",
            "complaints",
            "manufacturer_communications_summary",
            "manufacturer_communications",
        }
    ),
    "safety-ratings": frozenset({"safety_ratings"}),
    "recalls": frozenset({"recalls"}),
    "investigations": frozenset({"investigations"}),
    "complaints": frozenset({"complaints"}),
    "manufacturer-communications-summary": frozenset({"manufacturer_communications_summary"}),
    "manufacturer-communications": frozenset({"manufacturer_communications"}),
}


class NhtsaLeaseLostError(RuntimeError):
    pass


class NhtsaMySQLRepository:
    def __init__(self, connection: Connection[DictCursor]) -> None:
        self.connection = connection

    @classmethod
    def create(
        cls,
        config: NhtsaConfig,
        *,
        timeout_seconds: int = 600,
    ) -> NhtsaMySQLRepository:
        connection = pymysql.connect(
            host=config.mysql_host,
            port=config.mysql_port,
            user=config.mysql_user,
            password=config.mysql_password,
            database=config.mysql_database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            write_timeout=timeout_seconds,
        )
        return cls(connection)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[Connection[DictCursor]]:
        self.connection.begin()
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            with suppress(pymysql.MySQLError):
                self.connection.rollback()
            raise

    def start_run(
        self,
        run_key: str,
        scope_name: str,
        source_keys: Sequence[str],
        *,
        scheduled_job_run_id: int,
        expected_job_name: str,
    ) -> NhtsaRunLease:
        token = secrets.token_hex(32)
        with (
            catalog_writer_admission(self.connection),
            self.transaction() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                SELECT id, scheduled_job_run_id
                FROM nhtsa_sync_runs
                WHERE BINARY lease_slot = BINARY 'writer'
                FOR UPDATE
                """
            )
            previous = cursor.fetchone()
            cursor.execute(
                """
                SELECT child.job_name, child.status, child.parent_scheduled_job_run_id,
                       parent.job_name AS parent_job_name, parent.status AS parent_status
                FROM scheduled_job_runs AS child
                LEFT JOIN scheduled_job_runs AS parent
                  ON parent.id = child.parent_scheduled_job_run_id
                WHERE child.id = %s
                FOR UPDATE
                """,
                (scheduled_job_run_id,),
            )
            scheduled_job = cursor.fetchone()
            if (
                scheduled_job is None
                or scheduled_job["job_name"] != expected_job_name
                or scheduled_job["status"] != "running"
                or (
                    scheduled_job["parent_scheduled_job_run_id"] is not None
                    and (
                        scheduled_job["parent_job_name"] != "nhtsa"
                        or scheduled_job["parent_status"] != "running"
                    )
                )
            ):
                raise ValueError("NHTSA run requires its matching running scheduler job")
            if previous is not None:
                cursor.execute(
                    """
                    UPDATE nhtsa_sync_runs
                    SET status = 'interrupted', lease_slot = NULL, lease_token = NULL,
                        lease_expires_at = NULL, error_message = 'expired NHTSA lease recovered',
                        updated_at = UTC_TIMESTAMP(6), ended_at = UTC_TIMESTAMP(6)
                    WHERE id = %s AND status = 'running'
                      AND lease_expires_at <= UTC_TIMESTAMP(6)
                    """,
                    (previous["id"],),
                )
                if cursor.rowcount != 1:
                    raise NhtsaLeaseLostError("another NHTSA writer owns the active lease")
                cursor.execute(
                    """
                    UPDATE scheduled_job_runs
                    SET status = 'failed', finished_at = COALESCE(finished_at, UTC_TIMESTAMP()),
                        exit_code = COALESCE(exit_code, 125)
                    WHERE id = %s AND status = 'running'
                    """,
                    (previous["scheduled_job_run_id"],),
                )
            cursor.execute(
                """
                INSERT INTO nhtsa_sync_runs(
                    scheduled_job_run_id, run_key, scope_name, status, source_keys_json,
                    lease_slot, lease_token, started_at, updated_at, heartbeat_at,
                    lease_expires_at
                ) VALUES (
                    %s, %s, %s, 'running', %s, 'writer', %s,
                    UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), UTC_TIMESTAMP(6),
                    TIMESTAMPADD(SECOND, %s, UTC_TIMESTAMP(6))
                )
                """,
                (
                    scheduled_job_run_id,
                    run_key,
                    scope_name,
                    json.dumps(source_keys),
                    token,
                    RUN_LEASE_SECONDS,
                ),
            )
            return NhtsaRunLease(int(cursor.lastrowid), token, scheduled_job_run_id)

    def heartbeat(self, lease: NhtsaRunLease) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            self._assert_active_lease(cursor, lease)
            cursor.execute(
                """
                UPDATE nhtsa_sync_runs
                SET heartbeat_at = UTC_TIMESTAMP(6), updated_at = UTC_TIMESTAMP(6),
                    lease_expires_at = TIMESTAMPADD(SECOND, %s, UTC_TIMESTAMP(6))
                WHERE id = %s AND scheduled_job_run_id = %s AND status = 'running'
                  AND BINARY lease_token = BINARY %s
                """,
                (
                    RUN_LEASE_SECONDS,
                    lease.id,
                    lease.scheduled_job_run_id,
                    lease.token,
                ),
            )
            if cursor.rowcount != 1:
                raise NhtsaLeaseLostError("NHTSA run lease was lost")

    def finish_run(
        self,
        lease: NhtsaRunLease,
        *,
        status: str,
        downloaded: int,
        reused: int,
        source_rows: int,
        new_versions: int,
        rejected_rows: int,
        error_message: str | None = None,
    ) -> None:
        if status not in {"failed", "interrupted"}:
            raise ValueError(f"unsupported NHTSA terminal status: {status}")
        with self.transaction() as connection, connection.cursor() as cursor:
            self._assert_active_lease(cursor, lease)
            self._finish_run(
                cursor,
                lease,
                status=status,
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
                error_message=error_message,
            )

    def current_artifact(
        self,
        dataset_name: str,
        source_key: str,
    ) -> dict[str, object] | None:
        with self.transaction() as connection, connection.cursor() as cursor:
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
        with self.transaction() as connection, connection.cursor() as cursor:
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
        lease: NhtsaRunLease,
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
            self._assert_active_lease(cursor, lease)
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

    def refresh_artifact_storage(
        self,
        lease: NhtsaRunLease,
        artifact_id: int,
        download: DownloadedArtifact,
    ) -> None:
        if download.path is None or download.sha256 is None:
            raise ValueError("downloaded artifact path and sha256 are required")
        with self.transaction() as connection, connection.cursor() as cursor:
            self._assert_active_lease(cursor, lease)
            cursor.execute(
                "SELECT sha256 FROM nhtsa_source_artifacts WHERE id = %s FOR UPDATE",
                (artifact_id,),
            )
            artifact = cursor.fetchone()
            if artifact is None or artifact["sha256"] != download.sha256:
                raise ValueError("download does not match the reused NHTSA artifact")
            cursor.execute(
                """
                UPDATE nhtsa_source_artifacts
                SET stored_path = %s, byte_count = %s
                WHERE id = %s
                """,
                (str(download.path), download.byte_count, artifact_id),
            )

    def store_member(
        self,
        lease: NhtsaRunLease,
        artifact_id: int,
        member: ArtifactMember,
    ) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            self._assert_active_lease(cursor, lease)
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
        with self.transaction() as connection, connection.cursor() as cursor:
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

    def reset_artifact_import(self, lease: NhtsaRunLease, artifact_id: int) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            self._assert_active_lease(cursor, lease)
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

    def insert_records(
        self,
        lease: NhtsaRunLease,
        artifact_id: int,
        records: Sequence[ParsedRecord],
    ) -> int:
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
            self._assert_active_lease(cursor, lease)
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

    def insert_rejections(
        self,
        lease: NhtsaRunLease,
        artifact_id: int,
        rows: Sequence[RejectedRow],
    ) -> None:
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
            self._assert_active_lease(cursor, lease)
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
        lease: NhtsaRunLease,
        artifact_id: int,
        *,
        source_rows: int,
        new_versions: int,
        rejected_rows: int,
    ) -> None:
        status = "imported" if rejected_rows == 0 else "quarantined"
        with self.transaction() as connection, connection.cursor() as cursor:
            self._assert_active_lease(cursor, lease)
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
        lease: NhtsaRunLease,
        artifact_id: int,
        error_message: str,
        *,
        only_if_unpublished: bool = False,
    ) -> None:
        with self.transaction() as connection, connection.cursor() as cursor:
            self._assert_active_lease(cursor, lease)
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

    def complete_run_and_publish_artifacts(
        self,
        lease: NhtsaRunLease,
        artifacts: Sequence[tuple[str, str, int]],
        *,
        replace_datasets: Sequence[str] = (),
        downloaded: int,
        reused: int,
        source_rows: int,
        new_versions: int,
        rejected_rows: int,
    ) -> None:
        artifact_ids = [artifact_id for _, _, artifact_id in artifacts]
        if not artifact_ids and not replace_datasets:
            raise ValueError("no NHTSA artifacts to publish")
        with self.transaction() as connection, connection.cursor() as cursor:
            run = self._assert_active_lease(cursor, lease)
            self._validate_publish_scope(run, artifacts, replace_datasets)
            selected_source_rows = 0
            if artifact_ids:
                placeholders = ",".join(["%s"] * len(artifact_ids))
                cursor.execute(
                    f"""
                    SELECT id, dataset_name, source_key, status, verified_at, imported_at,
                           source_rows, rejected_rows,
                           (SELECT COUNT(*) FROM nhtsa_artifact_records
                            WHERE artifact_id = nhtsa_source_artifacts.id) AS record_rows,
                           (SELECT COUNT(*) FROM nhtsa_rejected_rows
                            WHERE artifact_id = nhtsa_source_artifacts.id)
                               AS persisted_rejected_rows
                    FROM nhtsa_source_artifacts
                    WHERE id IN ({placeholders})
                    """,
                    artifact_ids,
                )
                rows = cursor.fetchall()
                artifacts_by_id = {int(row["id"]): row for row in rows}
                if len(rows) != len(artifact_ids) or any(
                    artifact_id not in artifacts_by_id
                    or artifacts_by_id[artifact_id]["dataset_name"] != dataset_name
                    or artifacts_by_id[artifact_id]["source_key"] != source_key
                    or artifacts_by_id[artifact_id]["status"] != "imported"
                    or artifacts_by_id[artifact_id]["verified_at"] is None
                    or artifacts_by_id[artifact_id]["imported_at"] is None
                    or int(artifacts_by_id[artifact_id]["rejected_rows"]) != 0
                    or int(artifacts_by_id[artifact_id]["persisted_rejected_rows"]) != 0
                    or int(artifacts_by_id[artifact_id]["record_rows"])
                    != int(artifacts_by_id[artifact_id]["source_rows"])
                    for dataset_name, source_key, artifact_id in artifacts
                ):
                    raise ValueError("all NHTSA artifacts must be imported without rejections")
                selected_source_rows = sum(
                    int(artifacts_by_id[artifact_id]["source_rows"]) for artifact_id in artifact_ids
                )
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
            if source_rows != selected_source_rows or rejected_rows != 0:
                raise ValueError("NHTSA run counters do not match selected artifacts")
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
                    dataset_name, source_key, artifact_id, published_run_id, published_at
                ) VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    artifact_id = VALUES(artifact_id),
                    published_run_id = VALUES(published_run_id),
                    published_at = UTC_TIMESTAMP(6)
                """,
                [
                    (dataset_name, source_key, artifact_id, lease.id)
                    for dataset_name, source_key, artifact_id in artifacts
                ],
            )
            self._finish_run(
                cursor,
                lease,
                status="completed",
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
                error_message=None,
            )

    def complete_run_and_publish_vin_decode(
        self,
        lease: NhtsaRunLease,
        artifact_id: int,
        normalized_vin: str,
        payload: Mapping[str, object],
        *,
        downloaded: int,
        reused: int,
        source_rows: int,
        new_versions: int,
        rejected_rows: int,
    ) -> dict[str, object]:
        vin = normalize_vin(normalized_vin)
        source_key = vin_source_key(vin)
        if payload.get("VIN") != vin:
            raise ValueError("NHTSA VIN decode response does not match the requested VIN")
        make_name = str(payload.get("Make") or "").strip()
        model_name = str(payload.get("Model") or "").strip() or None
        model_year_raw = str(payload.get("ModelYear") or "").strip()
        engine_configuration = str(payload.get("EngineConfiguration") or "").strip() or None
        engine_model = str(payload.get("EngineModel") or "").strip() or None
        displacement_raw = str(payload.get("DisplacementL") or "").strip()
        trim_name = str(payload.get("Trim") or "").strip() or None
        error_code = str(payload.get("ErrorCode") or "").strip()
        # 使用者決策：Engine／Trim／Displacement 先留空等回填；vPIC 對部分
        # 車型（尤其歐系）本來就不回 Model。因此只有 Make／ModelYear 是
        # 必要欄位，其餘缺席一律存 NULL（部分解碼是預期行為，不是錯誤）。
        required_fields = {
            "Make": make_name,
            "ModelYear": model_year_raw,
        }
        missing_fields = [name for name, value in required_fields.items() if not value]
        if missing_fields:
            raise ValueError(
                "NHTSA VIN decode is missing required fields: " + ", ".join(missing_fields)
            )
        if not model_year_raw.isdigit():
            raise ValueError(f"NHTSA VIN decode returned an invalid ModelYear: {model_year_raw}")
        model_year = int(model_year_raw)
        if not 1886 <= model_year <= 2100:
            raise ValueError(f"NHTSA VIN decode returned an invalid model year: {model_year}")
        if error_code != "0":
            raise ValueError(
                f"NHTSA VIN decode failed with ErrorCode={error_code}: "
                f"{str(payload.get('ErrorText') or '').strip()}"
            )
        displacement_l: Decimal | None = None
        if displacement_raw:
            try:
                displacement_l = Decimal(displacement_raw)
            except InvalidOperation as error:
                raise ValueError(
                    f"NHTSA VIN decode returned an invalid DisplacementL: {displacement_raw}"
                ) from error
            if not displacement_l.is_finite() or displacement_l < 0:
                raise ValueError(
                    f"NHTSA VIN decode returned an invalid DisplacementL: {displacement_raw}"
                )
        payload_json = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        natural_key_sha256 = hashlib.sha256(vin.encode()).hexdigest()
        record_sha256 = hashlib.sha256(payload_json.encode()).hexdigest()

        with self.transaction() as connection, connection.cursor() as cursor:
            run = self._assert_active_lease(cursor, lease)
            scope_name, source_keys = self._lease_scope(run)
            if scope_name != "api-vin" or source_keys != (source_key,):
                raise ValueError("VIN decode lease scope does not match the requested VIN")
            if source_rows != 1 or rejected_rows != 0:
                raise ValueError("VIN decode run counters do not match one accepted record")
            cursor.execute(
                """
                SELECT dataset_name, source_key, source_url, http_status, status,
                       verified_at, imported_at, source_rows, rejected_rows,
                       (SELECT COUNT(*) FROM nhtsa_artifact_records
                        WHERE artifact_id = nhtsa_source_artifacts.id) AS record_rows,
                       (SELECT COUNT(*) FROM nhtsa_rejected_rows
                        WHERE artifact_id = nhtsa_source_artifacts.id) AS persisted_rejected_rows
                FROM nhtsa_source_artifacts WHERE id = %s
                """,
                (artifact_id,),
            )
            artifact = cursor.fetchone()
            if (
                artifact is None
                or artifact["dataset_name"] != "vpic_vin_decodes"
                or artifact["source_key"] != source_key
                or int(artifact["http_status"]) != 200
                or artifact["status"] != "imported"
                or artifact["verified_at"] is None
                or artifact["imported_at"] is None
                or int(artifact["source_rows"]) != 1
                or int(artifact["rejected_rows"]) != 0
                or int(artifact["record_rows"]) != 1
                or int(artifact["persisted_rejected_rows"]) != 0
            ):
                raise ValueError("VIN decode artifact is not publishable")
            cursor.execute(
                """
                SELECT artifact_record.dataset_name,
                       artifact_record.natural_key_sha256,
                       artifact_record.record_sha256,
                       record_version.natural_key_text,
                       record_version.external_id,
                       record_version.payload_json
                FROM nhtsa_artifact_records AS artifact_record
                JOIN nhtsa_record_versions AS record_version
                  ON record_version.dataset_name = artifact_record.dataset_name
                 AND record_version.natural_key_sha256 = artifact_record.natural_key_sha256
                 AND record_version.record_sha256 = artifact_record.record_sha256
                WHERE artifact_record.artifact_id = %s
                """,
                (artifact_id,),
            )
            records = cursor.fetchall()
            record = records[0] if len(records) == 1 else None
            try:
                stored_payload_value = record["payload_json"] if record else None
                stored_payload = (
                    json.loads(stored_payload_value)
                    if isinstance(stored_payload_value, str)
                    else stored_payload_value
                )
            except (TypeError, json.JSONDecodeError):
                stored_payload = None
            stored_payload_json = (
                json.dumps(
                    stored_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if isinstance(stored_payload, dict)
                else None
            )
            if (
                record is None
                or record["dataset_name"] != "vpic_vin_decodes"
                or record["natural_key_sha256"] != natural_key_sha256
                or record["natural_key_text"] != vin
                or record["external_id"] != vin
                or record["record_sha256"] != record_sha256
                or stored_payload_json != payload_json
                or stored_payload is None
                or stored_payload.get("VIN") != vin
            ):
                raise ValueError("VIN decode artifact record does not match the requested VIN")
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
                    engine_configuration,
                    engine_model or None,
                    displacement_l,
                    trim_name,
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
                    dataset_name, source_key, artifact_id, published_run_id, published_at
                ) VALUES ('vpic_vin_decodes', %s, %s, %s, UTC_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    artifact_id = VALUES(artifact_id),
                    published_run_id = VALUES(published_run_id),
                    published_at = UTC_TIMESTAMP(6)
                """,
                (source_key, artifact_id, lease.id),
            )
            self._finish_run(
                cursor,
                lease,
                status="completed",
                downloaded=downloaded,
                reused=reused,
                source_rows=source_rows,
                new_versions=new_versions,
                rejected_rows=rejected_rows,
                error_message=None,
            )
        return {
            "vin": vin,
            "make_name": make_name,
            "model_name": model_name,
            "model_year": model_year,
            "engine_configuration": engine_configuration,
            "engine_model": engine_model or None,
            "displacement_l": str(displacement_l),
            "trim_name": trim_name,
        }

    def status_report(self) -> dict[str, Any]:
        with self.transaction() as connection, connection.cursor() as cursor:
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
            cursor.execute(
                "DELETE FROM scheduled_job_runs "
                "WHERE parent_scheduled_job_run_id IS NOT NULL "
                "AND job_name IN ('nhtsa-bulk', 'nhtsa-api', 'nhtsa-vin')"
            )
            cursor.execute(
                "DELETE FROM scheduled_job_runs "
                "WHERE job_name IN ('nhtsa', 'nhtsa-bulk', 'nhtsa-api', 'nhtsa-vin')"
            )

    def _lease_scope(self, run: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
        scope_name = run.get("scope_name")
        raw_source_keys = run.get("source_keys_json")
        try:
            source_keys: object = (
                json.loads(raw_source_keys) if isinstance(raw_source_keys, str) else raw_source_keys
            )
        except json.JSONDecodeError as error:
            raise ValueError("NHTSA run has invalid source_keys_json") from error
        if (
            not isinstance(scope_name, str)
            or not isinstance(source_keys, list)
            or any(not isinstance(source_key, str) for source_key in source_keys)
        ):
            raise ValueError("NHTSA run has invalid publication scope")
        return scope_name, tuple(source_keys)

    def _validate_publish_scope(
        self,
        run: Mapping[str, object],
        artifacts: Sequence[tuple[str, str, int]],
        replace_datasets: Sequence[str],
    ) -> None:
        scope_name, lease_source_keys = self._lease_scope(run)
        artifact_datasets = [dataset_name for dataset_name, _, _ in artifacts]
        artifact_source_keys = [source_key for _, source_key, _ in artifacts]
        artifact_identities = list(zip(artifact_datasets, artifact_source_keys, strict=True))
        replacement_datasets = tuple(replace_datasets)
        if len(set(artifact_identities)) != len(artifact_identities):
            raise ValueError("NHTSA publication contains duplicate artifact identities")
        if len(set(replacement_datasets)) != len(replacement_datasets):
            raise ValueError("NHTSA publication contains duplicate replacement datasets")

        api_scope = {
            "api-vpic": (("vpic",), VPIC_API_DATASETS),
            "api-cssi": (("cssi",), CSSI_API_DATASETS),
            "api-all": (("vpic", "cssi"), VPIC_API_DATASETS | CSSI_API_DATASETS),
        }.get(scope_name)
        if api_scope is not None:
            expected_source_keys, allowed_datasets = api_scope
            if lease_source_keys != expected_source_keys:
                raise ValueError("NHTSA API lease source keys do not match its scope")
            if set(replacement_datasets) != allowed_datasets:
                raise ValueError(
                    "NHTSA API replacement datasets must exactly match the lease scope"
                )
        elif scope_name in BULK_DATASETS_BY_SCOPE:
            allowed_datasets = BULK_DATASETS_BY_SCOPE[scope_name]
            if replacement_datasets:
                raise ValueError("NHTSA bulk publication cannot replace datasets")
            if (
                len(set(lease_source_keys)) != len(lease_source_keys)
                or len(set(artifact_source_keys)) != len(artifact_source_keys)
                or set(artifact_source_keys) != set(lease_source_keys)
            ):
                raise ValueError("NHTSA bulk artifacts do not match the lease source keys")
        else:
            raise ValueError(f"unsupported NHTSA publication scope: {scope_name}")

        if set(artifact_datasets) - allowed_datasets:
            raise ValueError("NHTSA artifacts are outside the lease dataset scope")

    def _assert_active_lease(
        self,
        cursor: DictCursor,
        lease: NhtsaRunLease,
    ) -> dict[str, object]:
        cursor.execute(
            """
            SELECT id, scope_name, source_keys_json FROM nhtsa_sync_runs
            WHERE id = %s AND scheduled_job_run_id = %s AND status = 'running'
              AND BINARY lease_token = BINARY %s
            FOR UPDATE
            """,
            (lease.id, lease.scheduled_job_run_id, lease.token),
        )
        run = cursor.fetchone()
        if run is None:
            raise NhtsaLeaseLostError("NHTSA run lease was lost")
        cursor.execute(
            """
            SELECT lease_expires_at > UTC_TIMESTAMP(6) AS lease_is_active
            FROM nhtsa_sync_runs WHERE id = %s
            """,
            (lease.id,),
        )
        active = cursor.fetchone()
        if active is None or not bool(active["lease_is_active"]):
            raise NhtsaLeaseLostError("NHTSA run lease was lost")
        cursor.execute(
            """
            SELECT child.job_name AS child_job_name,
                   child.status AS child_status,
                   child.trigger_mode AS child_trigger_mode,
                   child.parent_scheduled_job_run_id AS parent_id,
                   parent.job_name AS parent_job_name,
                   parent.status AS parent_status,
                   parent.trigger_mode AS parent_trigger_mode
            FROM scheduled_job_runs AS child
            LEFT JOIN scheduled_job_runs AS parent
              ON parent.id = child.parent_scheduled_job_run_id
            WHERE child.id = %s
            FOR UPDATE
            """,
            (lease.scheduled_job_run_id,),
        )
        lineage = cursor.fetchone()
        scope_name = str(run["scope_name"])
        if scope_name.startswith("api-"):
            expected_child = "nhtsa-vin" if scope_name == "api-vin" else "nhtsa-api"
        else:
            expected_child = "nhtsa-bulk"
        # 合法觸發來源只有系統排程：daemon（--job nhtsa*）或 queue（後台
        # admin_crawl_requests 派發）；手動/direct 觸發一律拒絕 finalize。
        if (
            lineage is None
            or lineage["child_job_name"] != expected_child
            or lineage["child_status"] != "running"
            or lineage["child_trigger_mode"] not in ("daemon", "queue")
            or (
                lineage["parent_id"] is not None
                and (
                    lineage["parent_job_name"] != "nhtsa"
                    or lineage["parent_status"] != "running"
                    or lineage["parent_trigger_mode"] not in ("daemon", "queue")
                )
            )
        ):
            raise NhtsaLeaseLostError("NHTSA scheduler lineage was lost")
        return dict(run)

    def _finish_run(
        self,
        cursor: DictCursor,
        lease: NhtsaRunLease,
        *,
        status: str,
        downloaded: int,
        reused: int,
        source_rows: int,
        new_versions: int,
        rejected_rows: int,
        error_message: str | None,
    ) -> None:
        cursor.execute(
            """
            UPDATE nhtsa_sync_runs
            SET status = %s, artifacts_downloaded = %s, artifacts_reused = %s,
                source_rows = %s, new_versions = %s, rejected_rows = %s,
                error_message = %s, updated_at = UTC_TIMESTAMP(6),
                ended_at = UTC_TIMESTAMP(6), lease_slot = NULL,
                lease_token = NULL, lease_expires_at = NULL
            WHERE id = %s AND scheduled_job_run_id = %s AND status = 'running'
              AND BINARY lease_token = BINARY %s
            """,
            (
                status,
                downloaded,
                reused,
                source_rows,
                new_versions,
                rejected_rows,
                error_message,
                lease.id,
                lease.scheduled_job_run_id,
                lease.token,
            ),
        )
        if cursor.rowcount != 1:
            raise NhtsaLeaseLostError("NHTSA run lease was lost")
        cursor.execute(
            """
            UPDATE scheduled_job_runs
            SET status = %s, finished_at = UTC_TIMESTAMP(), exit_code = %s
            WHERE id = %s AND status = 'running'
            """,
            (
                "completed" if status == "completed" else "failed",
                0 if status == "completed" else 1,
                lease.scheduled_job_run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise NhtsaLeaseLostError("NHTSA scheduler lease was lost")

    def _optional_int(self, value: str | None) -> int | None:
        if value is None or not value.isdigit():
            return None
        return int(value)
