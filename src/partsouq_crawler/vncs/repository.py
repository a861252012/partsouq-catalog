from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from partsouq_catalog.admission import catalog_writer_admission
from partsouq_catalog.migrations import split_mysql_script
from partsouq_crawler.vncs.config import VncsConfig
from partsouq_crawler.vncs.models import ParsedRecord, VncsRunHandle

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})


class VncsMySQLRepository:
    def __init__(self, connection: Connection[DictCursor]) -> None:
        self.connection = connection

    @classmethod
    def create(
        cls,
        config: VncsConfig,
        *,
        timeout_seconds: int = 600,
    ) -> VncsMySQLRepository:
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

    def ensure_schema(self) -> None:
        """套用模組 schema（CREATE TABLE IF NOT EXISTS，與 migration 025 同構）。"""
        statements = split_mysql_script(SCHEMA_PATH.read_text(encoding="utf-8"))
        with (
            catalog_writer_admission(self.connection),
            self.connection.cursor() as cursor,
        ):
            for statement in statements:
                cursor.execute(statement)
            self.connection.commit()

    def start_run(
        self,
        run_key: str,
        *,
        scheduled_job_run_id: int | None = None,
    ) -> VncsRunHandle:
        with (
            catalog_writer_admission(self.connection),
            self.transaction() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                """
                INSERT INTO vncs_sync_runs(
                    scheduled_job_run_id, run_key, status, started_at
                ) VALUES (%s, %s, 'running', UTC_TIMESTAMP(6))
                """,
                (scheduled_job_run_id, run_key),
            )
            return VncsRunHandle(id=int(cursor.lastrowid))

    def finish_run(
        self,
        handle: VncsRunHandle,
        *,
        status: str,
        rows_seen: int,
        rows_upserted: int,
        malformed_rows: int,
        error_message: str | None = None,
    ) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"unsupported VNCS terminal status: {status}")
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE vncs_sync_runs
                SET status = %s, rows_seen = %s, rows_upserted = %s,
                    malformed_rows = %s, error_message = %s, ended_at = UTC_TIMESTAMP(6)
                WHERE id = %s AND status = 'running'
                """,
                (
                    status,
                    rows_seen,
                    rows_upserted,
                    malformed_rows,
                    error_message,
                    handle.id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("VNCS sync run is no longer running")

    def upsert_vehicles(self, records: Sequence[ParsedRecord]) -> int:
        """批次 upsert；VIN 撞 uq_vncs_vin 時更新既有列，非 VIN 引擎碼不參與唯一。"""
        if not records:
            return 0
        values = [
            (
                record.vehicle_kind,
                record.make,
                record.model_raw,
                record.displacement_cc,
                record.body_rule,
                record.transmission,
                record.doors,
                record.style,
                record.model_year,
                record.model_group_code,
                record.body_or_engine_code,
                record.is_vin,
                record.period,
                record.approval_date,
                record.check_code,
                record.source_url,
                record.payload_json,
            )
            for record in records
        ]
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO tw_vncs_vehicles(
                    vehicle_kind, make, model_raw, displacement_cc, body_rule,
                    transmission, doors, style, model_year, model_group_code,
                    body_or_engine_code, is_vin, period, approval_date, check_code,
                    source_url, payload_json, first_seen_at, last_synced_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
                )
                ON DUPLICATE KEY UPDATE
                    vehicle_kind = VALUES(vehicle_kind), make = VALUES(make),
                    model_raw = VALUES(model_raw), displacement_cc = VALUES(displacement_cc),
                    body_rule = VALUES(body_rule), transmission = VALUES(transmission),
                    doors = VALUES(doors), style = VALUES(style),
                    model_year = VALUES(model_year),
                    model_group_code = VALUES(model_group_code),
                    period = VALUES(period), approval_date = VALUES(approval_date),
                    check_code = VALUES(check_code), source_url = VALUES(source_url),
                    payload_json = VALUES(payload_json), last_synced_at = UTC_TIMESTAMP(6)
                """,
                values,
            )
            return int(cursor.rowcount)

    def clear_for_tests(self) -> None:
        database = (
            self.connection.db.decode()
            if isinstance(self.connection.db, bytes)
            else str(self.connection.db)
        )
        if not database.endswith("_test"):
            raise ValueError("refusing to clear a non-test VNCS database")
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM tw_vncs_vehicles")
            cursor.execute("DELETE FROM vncs_sync_runs")
            cursor.execute("DELETE FROM scheduled_job_runs WHERE job_name = 'vncs'")

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
