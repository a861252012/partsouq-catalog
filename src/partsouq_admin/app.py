from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pymysql
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymysql.connections import Connection
from pymysql.cursors import DictCursor
from starlette.middleware.trustedhost import TrustedHostMiddleware

from partsouq_catalog.config import DB_CONFIG
from partsouq_crawler.nhtsa.api import normalize_vin
from partsouq_crawler.parsers.common import normalize_part_number as normalize_catalog_part_number
from partsouq_station_admin.repository import redact_sensitive_url

STATIC_DIR = Path(__file__).resolve().parent / "static"
VIN_PREFIX_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{3,11}$")
ALLOWED_PAGE_SIZES = {10, 25, 30, 50, 100, 200}
BOUNDED_ACCEPTANCE_TARGET = 10_000
DEFAULT_TRUSTED_HOSTS = ("127.0.0.1", "localhost", "testserver", "partsouq.localhost")
TRUSTED_HOSTS = tuple(
    dict.fromkeys(
        (
            *DEFAULT_TRUSTED_HOSTS,
            *(
                host.strip()
                for host in os.getenv("PARTSOUQ_ADMIN_TRUSTED_HOSTS", "").split(",")
                if host.strip()
            ),
        )
    )
)
_VIN_MAPPING_STALE_SQL = (
    "d.vin IS NULL OR current_catalog.vehicle_id IS NULL "
    "OR (a.source_name = 'manual-sparse-override' AND ("
    "NULLIF(TRIM(a.source_reference), '') IS NULL "
    "OR NOT (a.model_year <=> d.model_year) OR NOT EXISTS ("
    "SELECT 1 FROM v_current_catalog_parts AS sparse "
    "WHERE sparse.vehicle_id = a.partsouq_vehicle_id "
    "AND (sparse.production_from IS NOT NULL OR sparse.production_to IS NOT NULL) "
    "AND (sparse.production_from IS NULL OR d.model_year >= "
    "CAST(LEFT(sparse.production_from, 4) AS UNSIGNED)) "
    "AND (sparse.production_to IS NULL OR d.model_year <= "
    "CAST(LEFT(sparse.production_to, 4) AS UNSIGNED)) "
    "AND CAST(REGEXP_REPLACE(UPPER(sparse.brand), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY) "
    "AND CAST(a.make_name AS BINARY) <=> CAST(d.make_name AS BINARY) "
    "AND CAST(a.model_name AS BINARY) <=> CAST(COALESCE("
    "NULLIF(TRIM(d.model_name), ''), sparse.model) AS BINARY) "
    "AND CAST(a.engine AS BINARY) <=> CAST(COALESCE("
    "NULLIF(TRIM(d.engine_model), ''), sparse.engine) AS BINARY) "
    "AND CAST(a.trim_name AS BINARY) <=> CAST(COALESCE("
    "NULLIF(TRIM(d.trim_name), ''), sparse.trim_name) AS BINARY) "
    "AND (NULLIF(TRIM(d.model_name), '') IS NULL OR "
    "CAST(REGEXP_REPLACE(UPPER(sparse.model), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.model_name), '[^A-Z0-9]', '') AS BINARY)) "
    "AND (NULLIF(TRIM(d.engine_model), '') IS NULL OR "
    "CAST(REGEXP_REPLACE(UPPER(sparse.engine), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.engine_model), '[^A-Z0-9]', '') AS BINARY)) "
    "AND (NULLIF(TRIM(d.trim_name), '') IS NULL OR "
    "CAST(REGEXP_REPLACE(UPPER(sparse.trim_name), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.trim_name), '[^A-Z0-9]', '') AS BINARY)))) "
    "OR (a.source_name = 'manual-name-override' AND ("
    "NULLIF(TRIM(a.source_reference), '') IS NULL "
    "OR NOT (a.model_year <=> d.model_year) "
    "OR NOT (CAST(a.make_name AS BINARY) <=> CAST(d.make_name AS BINARY)) "
    "OR NOT (CAST(a.model_name AS BINARY) <=> CAST(d.model_name AS BINARY)) "
    "OR NOT (CAST(a.engine AS BINARY) <=> CAST(d.engine_model AS BINARY)) "
    "OR NOT (CAST(a.trim_name AS BINARY) <=> CAST(d.trim_name AS BINARY)) "
    "OR NOT EXISTS (SELECT 1 FROM v_current_catalog_parts AS reviewed "
    "WHERE reviewed.vehicle_id = a.partsouq_vehicle_id "
    "AND (reviewed.production_from IS NOT NULL OR reviewed.production_to IS NOT NULL) "
    "AND (reviewed.production_from IS NULL OR d.model_year >= "
    "CAST(LEFT(reviewed.production_from, 4) AS UNSIGNED)) "
    "AND (reviewed.production_to IS NULL OR d.model_year <= "
    "CAST(LEFT(reviewed.production_to, 4) AS UNSIGNED)) "
    "AND CAST(REGEXP_REPLACE(UPPER(reviewed.brand), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY)))) "
    "OR (a.source_name NOT IN ('manual-name-override', 'manual-sparse-override') AND ("
    "NULLIF(TRIM(d.model_name), '') IS NULL "
    "OR NULLIF(TRIM(d.engine_model), '') IS NULL "
    "OR NULLIF(TRIM(d.trim_name), '') IS NULL "
    "OR NOT (a.model_year <=> d.model_year) "
    "OR NOT (CAST(REGEXP_REPLACE(UPPER(a.make_name), '[^A-Z0-9]', '') AS BINARY) "
    "<=> CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY)) "
    "OR NOT (CAST(REGEXP_REPLACE(UPPER(a.model_name), '[^A-Z0-9]', '') AS BINARY) "
    "<=> CAST(REGEXP_REPLACE(UPPER(d.model_name), '[^A-Z0-9]', '') AS BINARY)) "
    "OR NOT (CAST(REGEXP_REPLACE(UPPER(a.engine), '[^A-Z0-9]', '') AS BINARY) "
    "<=> CAST(REGEXP_REPLACE(UPPER(d.engine_model), '[^A-Z0-9]', '') AS BINARY)) "
    "OR NOT (CAST(REGEXP_REPLACE(UPPER(a.trim_name), '[^A-Z0-9]', '') AS BINARY) "
    "<=> CAST(REGEXP_REPLACE(UPPER(d.trim_name), '[^A-Z0-9]', '') AS BINARY)) "
    "OR NOT EXISTS ("
    "SELECT 1 FROM v_current_catalog_parts AS exact "
    "WHERE exact.vehicle_id = a.partsouq_vehicle_id "
    "AND CAST(REGEXP_REPLACE(UPPER(exact.brand), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY) "
    "AND CAST(REGEXP_REPLACE(UPPER(exact.model), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.model_name), '[^A-Z0-9]', '') AS BINARY) "
    "AND CAST(REGEXP_REPLACE(UPPER(exact.engine), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.engine_model), '[^A-Z0-9]', '') AS BINARY) "
    "AND CAST(REGEXP_REPLACE(UPPER(exact.trim_name), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.trim_name), '[^A-Z0-9]', '') AS BINARY) "
    "AND (exact.production_from IS NULL OR d.model_year >= "
    "CAST(LEFT(exact.production_from, 4) AS UNSIGNED)) "
    "AND (exact.production_to IS NULL OR d.model_year <= "
    "CAST(LEFT(exact.production_to, 4) AS UNSIGNED))) "
    "OR 1 <> (SELECT COUNT(DISTINCT exact_candidate.vehicle_id) "
    "FROM v_current_catalog_parts AS exact_candidate "
    "WHERE exact_candidate.vehicle_id IS NOT NULL "
    "AND (exact_candidate.production_from IS NOT NULL "
    "OR exact_candidate.production_to IS NOT NULL) "
    "AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.brand), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY) "
    "AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.model), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.model_name), '[^A-Z0-9]', '') AS BINARY) "
    "AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.engine), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.engine_model), '[^A-Z0-9]', '') AS BINARY) "
    "AND CAST(REGEXP_REPLACE(UPPER(exact_candidate.trim_name), '[^A-Z0-9]', '') AS BINARY) = "
    "CAST(REGEXP_REPLACE(UPPER(d.trim_name), '[^A-Z0-9]', '') AS BINARY) "
    "AND (exact_candidate.production_from IS NULL OR d.model_year >= "
    "CAST(LEFT(exact_candidate.production_from, 4) AS UNSIGNED)) "
    "AND (exact_candidate.production_to IS NULL OR d.model_year <= "
    "CAST(LEFT(exact_candidate.production_to, 4) AS UNSIGNED))))))"
)

type Row = dict[str, Any]

app = FastAPI(title="PartSouq Catalog Backoffice", version="0.1.0")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(TRUSTED_HOSTS))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class InputModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class VehicleMappingInput(InputModel):
    vin_prefix: str = Field(min_length=3, max_length=11)
    make_name: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=256)
    model_year: int | None = Field(default=None, ge=1886, le=2100)
    engine: str | None = Field(default=None, max_length=256)
    trim_name: str | None = Field(default=None, max_length=256)
    source_name: str = Field(default="manual", min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=1024)

    @field_validator("vin_prefix", mode="before")
    @classmethod
    def validate_vin_prefix(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("VIN 前綴必須是字串")
        value = value.upper()
        if not VIN_PREFIX_RE.fullmatch(value):
            raise ValueError("VIN 僅接受 3 至 11 碼 WMI/VDS 前綴，不接受完整 17 碼 VIN")
        return value


class VinInput(InputModel):
    vin: str = Field(min_length=17, max_length=17)

    @field_validator("vin", mode="before")
    @classmethod
    def validate_vin(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("VIN 必須是字串")
        return normalize_vin(value)


class VinVehicleMappingInput(VinInput):
    partsouq_vehicle_id: int = Field(ge=1)
    source_name: str = Field(default="manual-confirmed", min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=1024)
    allow_name_override: bool = False

    @field_validator("source_name")
    @classmethod
    def canonicalize_reserved_source_name(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized in {"manual-name-override", "manual-sparse-override"}:
            return normalized
        return value

    @model_validator(mode="after")
    def validate_name_override(self) -> VinVehicleMappingInput:
        if self.allow_name_override and not (self.source_reference or "").strip():
            raise ValueError("跨來源車款不一致時，必須填寫人工確認依據")
        if self.source_name == "manual-sparse-override":
            raise ValueError("部分解碼人工確認來源名稱只允許由人工確認流程設定")
        if self.source_name == "manual-name-override" and not self.allow_name_override:
            raise ValueError("人工 override 來源名稱只允許由人工確認流程設定")
        return self


class VinVehicleMappingUpdateInput(VinVehicleMappingInput):
    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def validate_naive_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("mapping 版本時間不得包含時區")
        return value


class PartTranslationInput(InputModel):
    english_name: str = Field(min_length=1, max_length=512)
    chinese_name: str = Field(min_length=1, max_length=512)
    common_chinese_name: str | None = Field(default=None, max_length=512)
    source_name: str = Field(default="manual", min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=1024)


class PartFitmentInput(InputModel):
    part_number: str = Field(min_length=1, max_length=64)
    vin_prefix: str | None = Field(default=None, min_length=3, max_length=11)
    make_name: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=256)
    model_year_from: int | None = Field(default=None, ge=1886, le=2100)
    model_year_to: int | None = Field(default=None, ge=1886, le=2100)
    engine: str | None = Field(default=None, max_length=256)
    trim_name: str | None = Field(default=None, max_length=256)
    source_name: str = Field(default="manual", min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=1024)

    @field_validator("part_number")
    @classmethod
    def normalize_part_number(cls, value: str) -> str:
        normalized = normalize_catalog_part_number(value)
        if not normalized:
            raise ValueError("零件號碼正規化後不可為空")
        return normalized

    @field_validator("vin_prefix", mode="before")
    @classmethod
    def validate_optional_vin_prefix(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("VIN 前綴必須是字串")
        value = value.upper()
        if not VIN_PREFIX_RE.fullmatch(value):
            raise ValueError("VIN 僅接受 3 至 11 碼 WMI/VDS 前綴，不接受完整 17 碼 VIN")
        return value

    @model_validator(mode="after")
    def validate_model_year_range(self) -> PartFitmentInput:
        if (
            self.model_year_from is not None
            and self.model_year_to is not None
            and self.model_year_from > self.model_year_to
        ):
            raise ValueError("起始年份不得晚於結束年份")
        return self


class CategoryLabelInput(InputModel):
    category_main: str = Field(min_length=1, max_length=256)
    category_group: str = Field(default="", max_length=256)
    category_small: str = Field(default="", max_length=256)
    chinese_label: str = Field(min_length=1, max_length=512)
    common_chinese_label: str | None = Field(default=None, max_length=512)
    source_name: str = Field(default="manual", min_length=1, max_length=64)


class ReconciliationInput(InputModel):
    channel: Literal["part", "vehicle", "category", "translation"]
    subject_key: str = Field(min_length=1, max_length=512)
    left_value: Row | list[object] | str | int | float | bool | None = None
    right_value: Row | list[object] | str | int | float | bool | None = None
    resolution_note: str | None = None


class ReconciliationUpdate(InputModel):
    status: Literal["open", "matched", "rejected"]
    resolution_note: str | None = None


class CrawlRequestInput(InputModel):
    job_name: Literal["nhtsa-bulk", "nhtsa-api", "nhtsa-vin"]
    requested_scope: str = Field(default="all", min_length=1, max_length=64)


class QuarantineResolveInput(InputModel):
    expected_run_key: str = Field(min_length=1, max_length=128)
    resolution: str = Field(default="", max_length=255)


def _connect() -> Connection[DictCursor]:
    return pymysql.connect(
        host=str(DB_CONFIG["host"]),
        port=int(str(DB_CONFIG["port"])),
        user=str(DB_CONFIG["user"]),
        password=str(DB_CONFIG["password"]),
        database=str(DB_CONFIG["database"]),
        cursorclass=DictCursor,
        autocommit=True,
    )


def _fetch_all(sql: str, params: tuple[object, ...] = ()) -> list[Row]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows: list[Row] = []
            for fetched in cursor.fetchall():
                row = dict(fetched)
                source_url = row.get("source_url")
                if isinstance(source_url, str):
                    row["source_url"] = redact_sensitive_url(source_url)
                rows.append(row)
            return rows
    finally:
        connection.close()


def _fetch_one(sql: str, params: tuple[object, ...] = ()) -> Row | None:
    rows = _fetch_all(sql, params)
    return rows[0] if rows else None


def _execute(sql: str, params: tuple[object, ...]) -> int:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return int(cursor.lastrowid)
    finally:
        connection.close()


def require_admin_token(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    token = os.getenv("PARTSOUQ_ADMIN_TOKEN")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PARTSOUQ_ADMIN_TOKEN 尚未設定，後台 API 已停用",
        )
    if x_admin_token is None or not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="後台 token 無效")


def _row_or_404(table: str, row_id: int) -> Row:
    row = _fetch_one(f"SELECT * FROM {table} WHERE id = %s", (row_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="找不到資料")
    return row


def _insert_or_conflict(sql: str, params: tuple[object, ...]) -> int:
    try:
        return _execute(sql, params)
    except pymysql.err.IntegrityError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="資料已存在") from error


def _update_or_conflict(sql: str, params: tuple[object, ...]) -> None:
    try:
        _execute(sql, params)
    except pymysql.err.IntegrityError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="資料已存在") from error


def _validate_page_size(page_size: int) -> None:
    if page_size not in ALLOWED_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="pageSize 僅允許 " + "、".join(str(size) for size in sorted(ALLOWED_PAGE_SIZES)),
        )


def _pagination(total: int, page: int, page_size: int) -> tuple[int, int, int]:
    total_pages = (total + page_size - 1) // page_size
    current_page = min(page, total_pages) if total_pages else 1
    return current_page, total_pages, (current_page - 1) * page_size


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    _fetch_one(
        "SELECT part_quarantine.id FROM part_quarantine "
        "FORCE INDEX (idx_quarantine_list) "
        "STRAIGHT_JOIN groups_t ON groups_t.id = part_quarantine.group_id "
        "WHERE part_quarantine.resolved_at IS NULL "
        "ORDER BY part_quarantine.updated_at DESC, part_quarantine.id DESC LIMIT 1"
    )
    _fetch_one(
        "SELECT part_quarantine.id FROM part_quarantine "
        "FORCE INDEX (idx_quarantine_run_key_resolved_updated) "
        "STRAIGHT_JOIN groups_t ON groups_t.id = part_quarantine.group_id "
        "WHERE part_quarantine.resolved_at IS NULL "
        "AND part_quarantine.run_key = %s "
        "ORDER BY part_quarantine.updated_at DESC, part_quarantine.id DESC LIMIT 1",
        ("__health__",),
    )
    readiness_tables = "\nCROSS JOIN ".join(
        (
            "brands",
            "models",
            "vehicles",
            "categories",
            "groups_t",
            "parts",
            "published_parts",
            "published_parts_previous",
            "bounded_parts",
            "catalog_desired_bounded_scope",
            "crawl_runs",
            "nhtsa_sync_runs",
            "nhtsa_source_artifacts",
            "nhtsa_current_artifacts",
            "nhtsa_vin_decodes",
            "admin_vehicle_mappings",
            "admin_part_translations",
            "admin_part_fitments",
            "admin_category_labels",
            "admin_reconciliation_items",
            "admin_crawl_requests",
            "scheduled_job_runs",
            "part_quarantine",
            "v_current_catalog_parts",
            "v_vin_part_fitments",
            "admin_override_heads",
            "station_admin_effective_parts",
        )
    )
    _fetch_one(f"SELECT 1 AS ready FROM {readiness_tables} LIMIT 0")
    provenance_contract = _fetch_one(
        "SELECT "
        "EXISTS (SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts' "
        "AND COLUMN_NAME = 'crawl_run_id' AND COLUMN_TYPE = 'int' "
        "AND IS_NULLABLE = 'YES') AS current_column_ready, "
        "EXISTS (SELECT 1 FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'published_parts_previous' "
        "AND COLUMN_NAME = 'crawl_run_id' AND COLUMN_TYPE = 'int' "
        "AND IS_NULLABLE = 'YES') AS previous_column_ready, "
        "(SELECT IF(COUNT(*) = 1 AND MIN(NON_UNIQUE) = 1 "
        "AND MIN(COLUMN_NAME) = 'crawl_run_id' AND MIN(SEQ_IN_INDEX) = 1 "
        "AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0 "
        "AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0 "
        "AND MIN(COLLATION) = 'A' AND MAX(COLLATION) = 'A' "
        "AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE' "
        "AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES', 1, 0) "
        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'published_parts' "
        "AND INDEX_NAME = 'idx_published_crawl_run') AS current_index_ready, "
        "(SELECT IF(COUNT(*) = 1 AND MIN(NON_UNIQUE) = 1 "
        "AND MIN(COLUMN_NAME) = 'crawl_run_id' AND MIN(SEQ_IN_INDEX) = 1 "
        "AND COALESCE(SUM(EXPRESSION IS NOT NULL), 0) = 0 "
        "AND COALESCE(SUM(SUB_PART IS NOT NULL), 0) = 0 "
        "AND MIN(COLLATION) = 'A' AND MAX(COLLATION) = 'A' "
        "AND MIN(INDEX_TYPE) = 'BTREE' AND MAX(INDEX_TYPE) = 'BTREE' "
        "AND MIN(IS_VISIBLE) = 'YES' AND MAX(IS_VISIBLE) = 'YES', 1, 0) "
        "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'published_parts_previous' "
        "AND INDEX_NAME = 'idx_published_crawl_run') AS previous_index_ready, "
        "EXISTS (SELECT 1 FROM information_schema.KEY_COLUMN_USAGE AS key_columns "
        "JOIN information_schema.REFERENTIAL_CONSTRAINTS AS constraints "
        "ON constraints.CONSTRAINT_SCHEMA = key_columns.CONSTRAINT_SCHEMA "
        "AND constraints.TABLE_NAME = key_columns.TABLE_NAME "
        "AND constraints.CONSTRAINT_NAME = key_columns.CONSTRAINT_NAME "
        "WHERE key_columns.CONSTRAINT_SCHEMA = DATABASE() "
        "AND key_columns.TABLE_NAME = 'published_parts' "
        "AND key_columns.CONSTRAINT_NAME = 'fk_published_crawl_run' "
        "AND key_columns.COLUMN_NAME = 'crawl_run_id' "
        "AND key_columns.REFERENCED_TABLE_SCHEMA = DATABASE() "
        "AND key_columns.REFERENCED_TABLE_NAME = 'crawl_runs' "
        "AND key_columns.REFERENCED_COLUMN_NAME = 'id' "
        "AND constraints.UPDATE_RULE = 'NO ACTION' "
        "AND constraints.DELETE_RULE = 'NO ACTION') AS current_foreign_key_ready, "
        "EXISTS (SELECT 1 FROM information_schema.KEY_COLUMN_USAGE AS key_columns "
        "JOIN information_schema.REFERENTIAL_CONSTRAINTS AS constraints "
        "ON constraints.CONSTRAINT_SCHEMA = key_columns.CONSTRAINT_SCHEMA "
        "AND constraints.TABLE_NAME = key_columns.TABLE_NAME "
        "AND constraints.CONSTRAINT_NAME = key_columns.CONSTRAINT_NAME "
        "WHERE key_columns.CONSTRAINT_SCHEMA = DATABASE() "
        "AND key_columns.TABLE_NAME = 'published_parts_previous' "
        "AND key_columns.CONSTRAINT_NAME = 'fk_published_previous_crawl_run' "
        "AND key_columns.COLUMN_NAME = 'crawl_run_id' "
        "AND key_columns.REFERENCED_TABLE_SCHEMA = DATABASE() "
        "AND key_columns.REFERENCED_TABLE_NAME = 'crawl_runs' "
        "AND key_columns.REFERENCED_COLUMN_NAME = 'id' "
        "AND constraints.UPDATE_RULE = 'NO ACTION' "
        "AND constraints.DELETE_RULE = 'NO ACTION') AS previous_foreign_key_ready, "
        "EXISTS (SELECT 1 FROM information_schema.VIEWS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_current_catalog_parts' "
        "AND LOCATE('bounded_parts', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('verified_bounded_evidence', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('verified_bounded_records', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('evidence_record_sha256', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('partsouq_http_artifacts', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('partsouq_artifact_records', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('evidence_status', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('live_http', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('trigger_mode', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('daemon', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('catalog_desired_bounded_scope', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('desired_scope', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('scope_brand', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('scope_model', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('scope_vehicle_year_floor', LOWER(VIEW_DEFINITION)) > 0 "
        "AND LOCATE('formal_full_parts', LOWER(VIEW_DEFINITION)) = 0 "
        "AND LOCATE('published_parts', LOWER(VIEW_DEFINITION)) = 0) "
        "AS formal_view_ready, "
        "(SELECT IF(COUNT(*) = 2, 1, 0) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'v_current_catalog_parts' "
        "AND COLUMN_NAME IN ('dataset_scope', 'source_crawl_run_id')) "
        "AS formal_view_columns_ready, "
        "(SELECT IF(COUNT(*) = 5, 1, 0) FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() "
        "AND TABLE_NAME = 'catalog_desired_bounded_scope' "
        "AND COLUMN_NAME IN ('singleton_id', 'scope_brand', 'scope_model', "
        "'scope_vehicle_year_floor', 'updated_at')) AS desired_scope_columns_ready, "
        "EXISTS (SELECT 1 FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_SCHEMA = DATABASE() "
        "AND TRIGGER_NAME = 'prevent_bounded_parts_update' "
        "AND EVENT_OBJECT_TABLE = 'bounded_parts' "
        "AND ACTION_TIMING = 'BEFORE' "
        "AND EVENT_MANIPULATION = 'UPDATE' "
        "AND LOCATE('SIGNAL SQLSTATE', UPPER(ACTION_STATEMENT)) > 0) "
        "AS bounded_snapshot_immutable_ready"
    )
    if provenance_contract is None or any(
        int(provenance_contract.get(key) or 0) != 1
        for key in (
            "current_column_ready",
            "previous_column_ready",
            "current_index_ready",
            "previous_index_ready",
            "current_foreign_key_ready",
            "previous_foreign_key_ready",
            "formal_view_ready",
            "formal_view_columns_ready",
            "desired_scope_columns_ready",
            "bounded_snapshot_immutable_ready",
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="current catalog scope contract 尚未完成 migration 033",
        )
    return {"status": "ok"}


@app.get("/api/database-summary", dependencies=[Depends(require_admin_token)])
def database_summary() -> Row:
    counts = (
        _fetch_one(
            "SELECT "
            "(SELECT COUNT(*) FROM brands) AS brands, "
            "(SELECT COUNT(*) FROM models) AS models, "
            "(SELECT COUNT(*) FROM vehicles) AS vehicles, "
            "(SELECT COUNT(*) FROM categories) AS categories, "
            "(SELECT COUNT(*) FROM groups_t) AS groups_count, "
            "(SELECT COUNT(*) FROM parts) AS parts, "
            "(SELECT COUNT(*) FROM v_current_catalog_parts) AS published_fitment_rows, "
            "(SELECT COUNT(DISTINCT part_number) FROM v_current_catalog_parts) "
            "AS unique_part_numbers, "
            "(SELECT COUNT(DISTINCT vehicle_id) FROM v_current_catalog_parts) AS unique_vehicles, "
            "(SELECT COALESCE(SUM(a.source_rows), 0) "
            "FROM nhtsa_current_artifacts AS c "
            "JOIN nhtsa_source_artifacts AS a ON a.id = c.artifact_id) "
            "AS nhtsa_current_records, "
            "(SELECT COUNT(*) FROM nhtsa_current_artifacts) AS nhtsa_current_artifacts, "
            "(SELECT COUNT(*) FROM nhtsa_sync_runs) AS nhtsa_sync_runs, "
            "(SELECT COUNT(*) FROM nhtsa_sync_runs WHERE status = 'completed') "
            "AS nhtsa_completed_runs, "
            "(SELECT COUNT(*) FROM nhtsa_sync_runs WHERE status = 'failed') "
            "AS nhtsa_failed_runs, "
            "(SELECT COALESCE(SUM(rejected_rows), 0) FROM nhtsa_sync_runs) "
            "AS nhtsa_rejected_rows, "
            "(SELECT COUNT(*) FROM nhtsa_vin_decodes) AS nhtsa_vin_decodes, "
            "(SELECT COUNT(DISTINCT terminal_artifact.source_key) "
            "FROM nhtsa_source_artifacts AS terminal_artifact "
            "WHERE terminal_artifact.dataset_name = 'vpic_vin_decodes' "
            "AND terminal_artifact.status = 'undecodable' "
            "AND NOT EXISTS (SELECT 1 FROM nhtsa_vin_decodes AS decoded_vin "
            "JOIN nhtsa_source_artifacts AS decoded_artifact "
            "ON decoded_artifact.id = decoded_vin.source_artifact_id "
            "WHERE decoded_artifact.dataset_name = terminal_artifact.dataset_name "
            "AND decoded_artifact.source_key = terminal_artifact.source_key)) "
            "AS nhtsa_terminal_undecodable_vins, "
            "(SELECT COUNT(*) FROM admin_part_translations) AS admin_part_translations, "
            "(SELECT COUNT(*) FROM admin_part_fitments) AS admin_part_fitments, "
            "(SELECT COUNT(*) FROM admin_category_labels) AS admin_category_labels, "
            "(SELECT COUNT(*) FROM admin_reconciliation_items) AS admin_reconciliation_items, "
            "(SELECT COUNT(*) FROM admin_crawl_requests) AS admin_crawl_requests, "
            "(SELECT COUNT(*) FROM scheduled_job_runs) AS scheduled_job_runs, "
            "(SELECT COUNT(*) FROM part_quarantine) AS quarantine_total, "
            "(SELECT COUNT(*) FROM part_quarantine WHERE resolved_at IS NULL) "
            "AS quarantine_unresolved, "
            "current_catalog.*, desired_scope.*, bounded.* FROM ("
            "SELECT MAX(dataset_scope) AS current_catalog_scope, "
            "MAX(source_crawl_run_id) AS current_catalog_crawl_run_id, "
            "COUNT(*) AS current_catalog_rows, "
            "COUNT(DISTINCT part_number_normalized) AS current_unique_part_numbers, "
            "COUNT(CASE WHEN OCTET_LENGTH(part_name) <> CHAR_LENGTH(part_name) "
            "THEN 1 END) AS current_non_ascii_part_name_rows "
            "FROM v_current_catalog_parts) AS current_catalog CROSS JOIN ("
            "SELECT MAX(desired.scope_brand) AS desired_scope_brand, "
            "MAX(desired.scope_model) AS desired_scope_model, "
            "MAX(desired.scope_vehicle_year_floor) AS desired_scope_vehicle_year_floor, "
            "MAX(desired.updated_at) AS desired_scope_updated_at "
            "FROM (SELECT 1 AS singleton) AS desired_anchor "
            "LEFT JOIN catalog_desired_bounded_scope AS desired "
            "ON desired.singleton_id = 1) AS desired_scope CROSS JOIN ("
            "SELECT MAX(bounded_run_context.id) AS bounded_crawl_run_id, "
            "MAX(bounded_run_context.run_key) AS bounded_run_key, "
            "MAX(bounded_run_context.dataset_kind) AS bounded_dataset_kind, "
            "MAX(bounded_run_context.status) AS bounded_run_status, "
            "MAX(bounded_run_context.scope_brand) AS bounded_scope_brand, "
            "MAX(bounded_run_context.scope_model) AS bounded_scope_model, "
            "MAX(bounded_run_context.scope_vehicle_year_floor) "
            "AS bounded_scope_vehicle_year_floor, "
            "MAX(bounded_run_context.target_parts) AS bounded_target_parts, "
            "MAX(bounded_run_context.parts_ok) AS bounded_run_parts_ok, "
            "MAX(bounded_run_context.started_at) AS bounded_started_at, "
            "MAX(bounded_run_context.finished_at) AS bounded_finished_at, "
            "MAX(bounded_run_context.error_msg) AS bounded_error_msg, "
            "MAX(bounded_run_context.evidence_status) AS bounded_evidence_status, "
            "MAX(bounded_run_context.evidence_manifest_sha256) "
            "AS bounded_evidence_manifest_sha256, "
            "MAX(bounded_run_context.evidence_dataset_sha256) "
            "AS bounded_evidence_dataset_sha256, "
            "MAX(bounded_run_context.evidence_artifact_count) "
            "AS bounded_evidence_artifact_count, "
            "MAX(bounded_run_context.evidence_record_count) "
            "AS bounded_evidence_record_count, "
            "MAX(bounded_run_context.evidence_original_bytes) "
            "AS bounded_evidence_original_bytes, "
            "MAX(bounded_run_context.evidence_stored_bytes) AS bounded_evidence_stored_bytes, "
            "MAX(bounded_run_context.evidence_verified_at) AS bounded_evidence_verified_at, "
            "MAX(evidence.active_artifact_count) AS bounded_active_artifact_count, "
            "MAX(evidence.live_artifact_count) AS bounded_live_artifact_count, "
            "MAX(evidence.page_type_count) AS bounded_evidence_page_type_count, "
            "MAX(evidence.accepted_record_count) AS bounded_accepted_evidence_records, "
            "MAX(bounded_run_context.scheduled_job_run_id) "
            "AS bounded_scheduled_job_run_id, "
            "MAX(bounded_run_context.scheduler_job_name) AS bounded_scheduler_job_name, "
            "MAX(bounded_run_context.scheduler_trigger_mode) "
            "AS bounded_scheduler_trigger_mode, "
            "MAX(bounded_run_context.scheduler_status) AS bounded_scheduler_status, "
            "MAX(bounded_run_context.scheduler_exit_code) AS bounded_scheduler_exit_code, "
            "MAX(bounded_run_context.scheduler_started_at) AS bounded_scheduler_started_at, "
            "MAX(bounded_run_context.scheduler_finished_at) "
            "AS bounded_scheduler_finished_at, "
            "MAX(bounded_run_context.scheduler_linked_crawl_runs) "
            "AS bounded_scheduler_linked_crawl_runs, "
            "MAX(CASE WHEN DATABASE() <> 'partsouq_catalog' "
            "OR LOWER(COALESCE(bounded_run_context.run_key, '')) LIKE 'sample-%%' "
            "OR LOWER(COALESCE(bounded_run_context.error_msg, '')) "
            "REGEXP 'browser-assisted|fixture|synthetic|fake' "
            "OR bounded_run_context.scheduler_output_non_live = 1 "
            "THEN 1 ELSE 0 END) AS bounded_non_live_data_marker, "
            "COUNT(bp.part_id) AS bounded_fitment_rows, "
            "MIN(bp.crawl_run_id) AS bounded_snapshot_min_run_id, "
            "MAX(bp.crawl_run_id) AS bounded_snapshot_max_run_id, "
            "COUNT(DISTINCT bp.part_number_normalized) AS bounded_unique_part_numbers, "
            "COUNT(DISTINCT bp.vehicle_id) AS bounded_unique_vehicles, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL "
            "AND OCTET_LENGTH(bp.part_name) <> CHAR_LENGTH(bp.part_name) THEN 1 END) "
            "AS bounded_english_name_unverified_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL AND ("
            "NULLIF(TRIM(bp.part_number), '') IS NULL "
            "OR NULLIF(TRIM(bp.part_name), '') IS NULL "
            "OR NULLIF(TRIM(bp.brand), '') IS NULL "
            "OR NULLIF(TRIM(bp.model), '') IS NULL "
            "OR NULLIF(TRIM(bp.vehicle_name), '') IS NULL "
            "OR NULLIF(TRIM(bp.vehicle_code), '') IS NULL) THEN 1 END) "
            "AS bounded_required_field_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL AND (bp.model_id IS NULL "
            "OR bp.vehicle_id IS NULL OR bp.category_id IS NULL OR bp.group_id IS NULL) "
            "THEN 1 END) AS bounded_id_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL AND ("
            "NULLIF(TRIM(bp.vehicle_vid), '') IS NULL "
            "OR NULLIF(TRIM(bp.category_cid), '') IS NULL "
            "OR NULLIF(TRIM(bp.group_code), '') IS NULL "
            "OR NULLIF(TRIM(bp.group_uid), '') IS NULL "
            "OR NULLIF(TRIM(bp.code), '') IS NULL) THEN 1 END) "
            "AS bounded_source_id_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL AND (p.id IS NULL OR g.id IS NULL "
            "OR c.id IS NULL OR v.id IS NULL OR m.id IS NULL OR b.id IS NULL "
            "OR p.group_id <> bp.group_id OR g.category_id <> bp.category_id "
            "OR c.vehicle_id <> bp.vehicle_id OR v.model_id <> bp.model_id) "
            "THEN 1 END) AS bounded_orphan_relation_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL "
            "AND bp.production_from IS NULL AND bp.production_to IS NULL THEN 1 END) "
            "AS bounded_vehicle_range_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL "
            "AND bp.part_from IS NULL AND bp.part_to IS NULL THEN 1 END) "
            "AS bounded_part_range_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL "
            "AND bp.production_from IS NULL AND bp.production_to IS NULL "
            "AND bp.part_from IS NULL AND bp.part_to IS NULL THEN 1 END) "
            "AS bounded_effective_year_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL "
            "AND NULLIF(TRIM(bp.category_main), '') IS NULL THEN 1 END) "
            "AS bounded_category_main_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL "
            "AND NULLIF(TRIM(bp.category_group), '') IS NULL THEN 1 END) "
            "AS bounded_category_group_missing_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL AND ("
            "LOWER(bp.source_url) LIKE 'https://partsouq.com/en/catalog/genuine/unit?%%' "
            "OR LOWER(bp.source_url) LIKE "
            "'https://www.partsouq.com/en/catalog/genuine/unit?%%') THEN 1 END) "
            "AS bounded_official_source_url_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL AND ("
            "NULLIF(TRIM(bp.source_url), '') IS NULL OR NOT ("
            "LOWER(bp.source_url) LIKE 'https://partsouq.com/en/catalog/genuine/unit?%%' "
            "OR LOWER(bp.source_url) LIKE "
            "'https://www.partsouq.com/en/catalog/genuine/unit?%%')) THEN 1 END) "
            "AS bounded_invalid_source_url_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL "
            "AND bp.crawl_run_id <> bounded_run_context.id THEN 1 END) "
            "AS bounded_run_mismatch_rows, "
            "COUNT(CASE WHEN overrides.id IS NOT NULL THEN 1 END) "
            "AS bounded_active_override_rows, "
            "COUNT(CASE WHEN bp.part_id IS NOT NULL AND ("
            "CAST(bp.part_number AS BINARY) <> CAST(p.part_number AS BINARY) "
            "OR bp.part_number_normalized <> "
            "UPPER(REGEXP_REPLACE(bp.part_number, '[[:space:]-]+', '')) "
            "OR CAST(bp.part_name AS BINARY) <> CAST(p.name AS BINARY) "
            "OR CAST(bp.brand AS BINARY) <> CAST(b.name AS BINARY) "
            "OR CAST(bp.model AS BINARY) <> CAST(m.name AS BINARY)) THEN 1 END) "
            "AS bounded_source_value_mismatch_rows "
            "FROM (SELECT 1 AS singleton) AS anchor "
            "LEFT JOIN (SELECT latest_bounded_run.*, "
            "scheduled_job.job_name AS scheduler_job_name, "
            "scheduled_job.trigger_mode AS scheduler_trigger_mode, "
            "scheduled_job.status AS scheduler_status, "
            "scheduled_job.exit_code AS scheduler_exit_code, "
            "scheduled_job.started_at AS scheduler_started_at, "
            "scheduled_job.finished_at AS scheduler_finished_at, "
            "scheduler_links.crawl_run_count AS scheduler_linked_crawl_runs, "
            "CASE WHEN LOWER(COALESCE(scheduled_job.output_text, '')) "
            "REGEXP 'browser-assisted|fixture|synthetic|fake' THEN 1 ELSE 0 END "
            "AS scheduler_output_non_live FROM (SELECT id, run_key, dataset_kind, status, "
            "target_parts, parts_ok, started_at, finished_at, error_msg, "
            "scope_brand, scope_model, scope_vehicle_year_floor, "
            "scheduled_job_run_id, evidence_status, evidence_manifest_sha256, "
            "evidence_dataset_sha256, evidence_artifact_count, evidence_record_count, "
            "evidence_original_bytes, evidence_stored_bytes, evidence_verified_at "
            "FROM crawl_runs WHERE dataset_kind = 'bounded' "
            "ORDER BY started_at DESC, id DESC LIMIT 1) AS latest_bounded_run "
            "LEFT JOIN scheduled_job_runs AS scheduled_job "
            "ON scheduled_job.id = latest_bounded_run.scheduled_job_run_id "
            "LEFT JOIN (SELECT scheduled_job_run_id, COUNT(*) AS crawl_run_count "
            "FROM crawl_runs WHERE scheduled_job_run_id IS NOT NULL "
            "GROUP BY scheduled_job_run_id) AS scheduler_links "
            "ON scheduler_links.scheduled_job_run_id = scheduled_job.id "
            "LIMIT 1) AS bounded_run_context ON TRUE "
            "LEFT JOIN (SELECT crawl_run_id, COUNT(*) AS active_artifact_count, "
            "SUM(capture_kind = 'live_http') AS live_artifact_count, "
            "COUNT(DISTINCT page_type) AS page_type_count, "
            "SUM(accepted_record_count) AS accepted_record_count "
            "FROM partsouq_http_artifacts WHERE verification_status = 'verified' "
            "GROUP BY crawl_run_id) AS evidence "
            "ON evidence.crawl_run_id = bounded_run_context.id "
            "LEFT JOIN (SELECT current_bounded.*, "
            "current_bounded.source_crawl_run_id AS crawl_run_id "
            "FROM v_current_catalog_parts AS current_bounded "
            "WHERE current_bounded.dataset_scope = 'bounded') AS bp "
            "ON bp.source_crawl_run_id = bounded_run_context.id "
            "LEFT JOIN admin_override_heads AS overrides "
            "ON overrides.entity_type = 'part_numbers' "
            "AND overrides.source_record_id = bp.part_id AND overrides.status = 'active' "
            "LEFT JOIN parts AS p ON p.id = bp.part_id "
            "LEFT JOIN groups_t AS g ON g.id = bp.group_id "
            "LEFT JOIN categories AS c ON c.id = bp.category_id "
            "LEFT JOIN vehicles AS v ON v.id = bp.vehicle_id "
            "LEFT JOIN models AS m ON m.id = bp.model_id "
            "LEFT JOIN brands AS b ON b.id = m.brand_id) AS bounded"
        )
        or {}
    )
    nhtsa_datasets = _fetch_all(
        "SELECT a.dataset_name, COALESCE(SUM(a.source_rows), 0) AS row_count "
        "FROM nhtsa_current_artifacts AS c "
        "JOIN nhtsa_source_artifacts AS a ON a.id = c.artifact_id "
        "GROUP BY a.dataset_name ORDER BY a.dataset_name"
    )
    mappings = (
        _fetch_one(
            "SELECT COUNT(*) AS total, "
            "COUNT(CASE WHEN a.vin IS NULL THEN 1 END) AS manual, "
            f"COUNT(CASE WHEN a.vin IS NOT NULL AND NOT ({_VIN_MAPPING_STALE_SQL}) "
            "THEN 1 END) AS confirmed, "
            f"COUNT(CASE WHEN a.vin IS NOT NULL AND ({_VIN_MAPPING_STALE_SQL}) "
            "THEN 1 END) AS stale "
            "FROM admin_vehicle_mappings AS a "
            "LEFT JOIN nhtsa_vin_decodes AS d ON d.vin = a.vin "
            "LEFT JOIN (SELECT DISTINCT vehicle_id FROM v_current_catalog_parts "
            "WHERE vehicle_id IS NOT NULL) AS current_catalog "
            "ON current_catalog.vehicle_id = a.partsouq_vehicle_id"
        )
        or {}
    )
    published_quality = (
        _fetch_one(
            "SELECT "
            "COUNT(CASE WHEN NULLIF(TRIM(pp.part_number), '') IS NULL "
            "OR NULLIF(TRIM(pp.part_name), '') IS NULL "
            "OR NULLIF(TRIM(pp.brand), '') IS NULL "
            "OR NULLIF(TRIM(pp.model), '') IS NULL "
            "OR NULLIF(TRIM(pp.vehicle_name), '') IS NULL THEN 1 END) "
            "AS required_field_missing_rows, "
            "COUNT(CASE WHEN pp.part_id IS NULL OR pp.model_id IS NULL OR pp.vehicle_id IS NULL "
            "OR pp.category_id IS NULL OR pp.group_id IS NULL THEN 1 END) AS id_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(pp.vehicle_vid), '') IS NULL "
            "OR NULLIF(TRIM(pp.category_cid), '') IS NULL "
            "OR NULLIF(TRIM(pp.group_uid), '') IS NULL "
            "OR NULLIF(TRIM(pp.code), '') IS NULL THEN 1 END) AS source_id_missing_rows, "
            "0 AS orphan_relation_rows, "
            "COUNT(CASE WHEN pp.production_from IS NULL AND pp.production_to IS NULL THEN 1 END) "
            "AS vehicle_range_missing_rows, "
            "COUNT(CASE WHEN pp.part_from IS NULL AND pp.part_to IS NULL THEN 1 END) "
            "AS part_range_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(pp.category_main), '') IS NULL THEN 1 END) "
            "AS category_main_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(pp.category_group), '') IS NULL THEN 1 END) "
            "AS category_group_missing_rows "
            "FROM v_current_catalog_parts AS pp"
        )
        or {}
    )
    sample_quality = (
        _fetch_one(
            "SELECT COUNT(p.id) AS row_count, "
            "COUNT(DISTINCT p.part_number) AS unique_part_numbers, "
            "COUNT(CASE WHEN NULLIF(TRIM(p.part_number), '') IS NULL "
            "OR NULLIF(TRIM(p.name), '') IS NULL OR NULLIF(TRIM(b.name), '') IS NULL "
            "OR NULLIF(TRIM(m.name), '') IS NULL OR NULLIF(TRIM(v.name), '') IS NULL "
            "THEN 1 END) AS required_field_missing_rows, "
            "COUNT(CASE WHEN p.id IS NULL OR m.id IS NULL OR v.id IS NULL "
            "OR c.id IS NULL OR g.id IS NULL THEN 1 END) AS id_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(v.vid), '') IS NULL OR NULLIF(TRIM(c.cid), '') IS NULL "
            "OR NULLIF(TRIM(g.uid), '') IS NULL OR NULLIF(TRIM(p.code), '') IS NULL "
            "THEN 1 END) AS source_id_missing_rows, "
            "COUNT(CASE WHEN g.id IS NULL OR c.id IS NULL OR v.id IS NULL OR m.id IS NULL "
            "OR b.id IS NULL THEN 1 END) AS orphan_relation_rows, "
            "COUNT(CASE WHEN v.production_from IS NULL AND v.production_to IS NULL THEN 1 END) "
            "AS vehicle_range_missing_rows, "
            "COUNT(CASE WHEN p.part_from IS NULL AND p.part_to IS NULL THEN 1 END) "
            "AS part_range_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(c.name), '') IS NULL THEN 1 END) "
            "AS category_main_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(g.name), '') IS NULL THEN 1 END) "
            "AS category_group_missing_rows "
            "FROM parts AS p "
            "JOIN crawl_runs AS r ON r.id = p.seen_run_id "
            "LEFT JOIN groups_t AS g ON g.id = p.group_id "
            "LEFT JOIN categories AS c ON c.id = g.category_id "
            "LEFT JOIN vehicles AS v ON v.id = c.vehicle_id "
            "LEFT JOIN models AS m ON m.id = v.model_id "
            "LEFT JOIN brands AS b ON b.id = m.brand_id "
            "WHERE r.id = (SELECT id FROM crawl_runs WHERE status = 'sample' "
            "ORDER BY started_at DESC, id DESC LIMIT 1)"
        )
        or {}
    )
    nhtsa_quality = (
        _fetch_one(
            "SELECT COUNT(CASE WHEN NULLIF(TRIM(vin), '') IS NULL "
            "OR NULLIF(TRIM(make_name), '') IS NULL OR model_year IS NULL "
            "THEN 1 END) AS required_field_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(model_name), '') IS NULL THEN 1 END) "
            "AS model_name_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(engine_configuration), '') IS NULL THEN 1 END) "
            "AS engine_configuration_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(engine_model), '') IS NULL THEN 1 END) "
            "AS engine_model_missing_rows, "
            "COUNT(CASE WHEN displacement_l IS NULL THEN 1 END) "
            "AS displacement_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(trim_name), '') IS NULL THEN 1 END) "
            "AS trim_name_missing_rows "
            "FROM nhtsa_vin_decodes"
        )
        or {}
    )
    latest_crawl_run = _fetch_one(
        "SELECT id, run_key, status, started_at, finished_at, brands_ok, models_ok, "
        "vehicles_ok, groups_ok, parts_ok, parts_new, LEFT(error_msg, 500) AS error_msg "
        "FROM crawl_runs ORDER BY started_at DESC, id DESC LIMIT 1"
    )
    latest_sample_run = _fetch_one(
        "SELECT id, run_key, status, started_at, finished_at, brands_ok, models_ok, "
        "vehicles_ok, groups_ok, parts_ok, parts_new, LEFT(error_msg, 500) AS error_msg "
        "FROM crawl_runs WHERE status = 'sample' ORDER BY started_at DESC, id DESC LIMIT 1"
    )

    nhtsa_count = int(counts.get("nhtsa_vin_decodes", 0))
    terminal_undecodable_count = int(counts.get("nhtsa_terminal_undecodable_vins", 0))
    nhtsa_current_records = int(counts.get("nhtsa_current_records", 0))
    confirmed_count = int(mappings.get("confirmed", 0))
    mappings["unconfirmed_vin_decodes"] = max(nhtsa_count - confirmed_count, 0)
    sample_target = int(os.getenv("PSQ_LIMIT_PARTS", "1000"))
    sample_rows = int(sample_quality.get("row_count", 0))
    current_catalog_rows = int(counts.get("current_catalog_rows") or 0)
    bounded_rows = int(counts.get("bounded_fitment_rows") or 0)
    bounded_target = int(counts.get("bounded_target_parts") or 0)
    bounded_run_parts = int(counts.get("bounded_run_parts_ok") or 0)
    bounded_scheduler_exit = counts.get("bounded_scheduler_exit_code")
    desired_scope = (
        counts.get("desired_scope_brand"),
        counts.get("desired_scope_model"),
        (
            int(counts["desired_scope_vehicle_year_floor"])
            if counts.get("desired_scope_vehicle_year_floor") is not None
            else None
        ),
    )
    latest_bounded_scope = (
        counts.get("bounded_scope_brand"),
        counts.get("bounded_scope_model"),
        (
            int(counts["bounded_scope_vehicle_year_floor"])
            if counts.get("bounded_scope_vehicle_year_floor") is not None
            else None
        ),
    )
    desired_scope_configured = (
        isinstance(desired_scope[0], str)
        and bool(desired_scope[0])
        and isinstance(desired_scope[1], str)
        and bool(desired_scope[1])
        and desired_scope[2] is not None
    )
    bounded_scope_matches_desired = (
        desired_scope_configured and latest_bounded_scope == desired_scope
    )
    bounded_quality = {
        "required_field_missing_rows": int(counts.get("bounded_required_field_missing_rows") or 0),
        "id_missing_rows": int(counts.get("bounded_id_missing_rows") or 0),
        "source_id_missing_rows": int(counts.get("bounded_source_id_missing_rows") or 0),
        "orphan_relation_rows": int(counts.get("bounded_orphan_relation_rows") or 0),
        "vehicle_range_missing_rows": int(counts.get("bounded_vehicle_range_missing_rows") or 0),
        "part_range_missing_rows": int(counts.get("bounded_part_range_missing_rows") or 0),
        "effective_year_missing_rows": int(counts.get("bounded_effective_year_missing_rows") or 0),
        "category_main_missing_rows": int(counts.get("bounded_category_main_missing_rows") or 0),
        "category_group_missing_rows": int(counts.get("bounded_category_group_missing_rows") or 0),
        "invalid_source_url_rows": int(counts.get("bounded_invalid_source_url_rows") or 0),
        "run_mismatch_rows": int(counts.get("bounded_run_mismatch_rows") or 0),
        "active_override_rows": int(counts.get("bounded_active_override_rows") or 0),
        "source_value_mismatch_rows": int(counts.get("bounded_source_value_mismatch_rows") or 0),
        "english_name_unverified_rows": int(
            counts.get("bounded_english_name_unverified_rows") or 0
        ),
        "non_live_data_marker": int(counts.get("bounded_non_live_data_marker") or 0),
    }
    evidence_artifact_count = int(counts.get("bounded_evidence_artifact_count") or 0)
    active_artifact_count = int(counts.get("bounded_active_artifact_count") or 0)
    live_artifact_count = int(counts.get("bounded_live_artifact_count") or 0)
    evidence_record_count = int(counts.get("bounded_evidence_record_count") or 0)
    accepted_evidence_records = int(counts.get("bounded_accepted_evidence_records") or 0)
    evidence_page_type_count = int(counts.get("bounded_evidence_page_type_count") or 0)
    evidence_hashes_valid = all(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
        for value in (
            counts.get("bounded_evidence_manifest_sha256"),
            counts.get("bounded_evidence_dataset_sha256"),
        )
    )
    live_mapping_evidence = (
        counts.get("bounded_evidence_status") == "verified"
        and counts.get("bounded_evidence_verified_at") is not None
        and evidence_hashes_valid
        and evidence_artifact_count > 0
        and active_artifact_count == evidence_artifact_count
        and live_artifact_count == evidence_artifact_count
        and evidence_page_type_count == 6
        and evidence_record_count == BOUNDED_ACCEPTANCE_TARGET
        and accepted_evidence_records == BOUNDED_ACCEPTANCE_TARGET
        and bounded_target == BOUNDED_ACCEPTANCE_TARGET
        and bounded_run_parts == BOUNDED_ACCEPTANCE_TARGET
        and bounded_rows == BOUNDED_ACCEPTANCE_TARGET
        and int(counts.get("bounded_official_source_url_rows") or 0) == bounded_rows
        and not bounded_quality["invalid_source_url_rows"]
        and not bounded_quality["non_live_data_marker"]
        and int(counts.get("bounded_evidence_original_bytes") or 0) > 0
        and int(counts.get("bounded_evidence_stored_bytes") or 0) > 0
    )
    bounded_blocking_reasons = []
    bounded_crawl_run_id = counts.get("bounded_crawl_run_id")
    if not desired_scope_configured:
        bounded_blocking_reasons.append("bounded_desired_scope_not_configured")
    elif bounded_crawl_run_id is not None and not bounded_scope_matches_desired:
        bounded_blocking_reasons.append("bounded_scope_mismatch")
    if bounded_crawl_run_id is None:
        bounded_blocking_reasons.append("no_bounded_crawl_run")
    else:
        if counts.get("bounded_dataset_kind") != "bounded":
            bounded_blocking_reasons.append("bounded_dataset_kind_invalid")
        if counts.get("bounded_run_status") != "bounded_success":
            bounded_blocking_reasons.append("bounded_run_not_successful")
        if bounded_target != BOUNDED_ACCEPTANCE_TARGET:
            bounded_blocking_reasons.append("bounded_target_not_10000")
        if bounded_run_parts != bounded_target:
            bounded_blocking_reasons.append("bounded_run_count_mismatch")
        if bounded_rows != bounded_target:
            bounded_blocking_reasons.append("bounded_snapshot_count_mismatch")
        if counts.get("bounded_scheduled_job_run_id") is None:
            bounded_blocking_reasons.append("bounded_scheduler_not_linked")
        elif counts.get("bounded_scheduler_job_name") != "catalog":
            bounded_blocking_reasons.append("bounded_scheduler_job_invalid")
        elif counts.get("bounded_scheduler_trigger_mode") != "daemon":
            bounded_blocking_reasons.append("bounded_scheduler_trigger_not_daemon")
        elif counts.get("bounded_scheduler_status") != "completed" or bounded_scheduler_exit != 0:
            bounded_blocking_reasons.append("bounded_scheduler_not_completed")
        elif int(counts.get("bounded_scheduler_linked_crawl_runs") or 0) != 1:
            bounded_blocking_reasons.append("bounded_scheduler_link_not_unique")
        if bounded_quality["non_live_data_marker"]:
            bounded_blocking_reasons.append("bounded_non_live_data_marker")
        if not live_mapping_evidence:
            bounded_blocking_reasons.append("bounded_live_mapping_evidence_not_verified")

    bounded_quality_reasons = {
        "required_field_missing_rows": "bounded_required_fields_missing",
        "id_missing_rows": "bounded_ids_missing",
        "source_id_missing_rows": "bounded_source_ids_missing",
        "orphan_relation_rows": "bounded_orphan_relations",
        "effective_year_missing_rows": "bounded_vehicle_years_missing",
        "category_main_missing_rows": "bounded_categories_missing",
        "category_group_missing_rows": "bounded_categories_missing",
        "invalid_source_url_rows": "bounded_source_url_invalid",
        "run_mismatch_rows": "bounded_snapshot_run_mismatch",
        "active_override_rows": "bounded_active_overrides_present",
        "source_value_mismatch_rows": "bounded_source_values_mismatch",
    }
    for metric, reason in bounded_quality_reasons.items():
        if bounded_quality[metric] and reason not in bounded_blocking_reasons:
            bounded_blocking_reasons.append(reason)
    bounded_ready = not bounded_blocking_reasons

    demo_blocking_reasons = []
    production_pending_reasons = []
    if not bounded_ready:
        production_pending_reasons.append("verified_bounded_catalog_not_ready")
    if sample_rows < sample_target:
        demo_blocking_reasons.append("sample_rows_below_target")
    sample_required_checks = (
        "required_field_missing_rows",
        "id_missing_rows",
        "source_id_missing_rows",
        "orphan_relation_rows",
        "vehicle_range_missing_rows",
        "category_main_missing_rows",
        "category_group_missing_rows",
    )
    if sample_rows and any(int(sample_quality.get(key, 0)) for key in sample_required_checks):
        demo_blocking_reasons.append("sample_parts_data_quality_failed")
    if nhtsa_current_records == 0:
        demo_blocking_reasons.append("no_nhtsa_reference_data")
        production_pending_reasons.append("no_nhtsa_reference_data")
    if nhtsa_count == 0 and terminal_undecodable_count == 0:
        production_pending_reasons.append("awaiting_authorized_vin")
    elif nhtsa_count == 0:
        production_pending_reasons.append("no_usable_vin_decode")
    elif confirmed_count == 0:
        production_pending_reasons.append("no_confirmed_vin_mapping")
    if nhtsa_count and int(nhtsa_quality.get("required_field_missing_rows", 0)):
        production_pending_reasons.append("nhtsa_required_fields_missing")
    if int(mappings.get("stale", 0)) or int(mappings["unconfirmed_vin_decodes"]):
        production_pending_reasons.append("stale_or_unconfirmed_vin_mapping")
    if not live_mapping_evidence:
        production_pending_reasons.append("partsouq_live_mapping_evidence_not_verified")
    production_pending_reasons.append("partsouq_small_category_source_unavailable")
    production_pending_reasons.append("partsouq_english_name_language_not_verified")

    demo_ready = not demo_blocking_reasons
    production_ready = not production_pending_reasons

    return {
        "normalized": {
            "brands": counts.get("brands", 0),
            "models": counts.get("models", 0),
            "vehicles": counts.get("vehicles", 0),
            "categories": counts.get("categories", 0),
            "groups": counts.get("groups_count", 0),
            "parts": counts.get("parts", 0),
        },
        "published": {
            "fitment_rows": counts.get("published_fitment_rows", 0),
            "unique_part_numbers": counts.get("unique_part_numbers", 0),
            "unique_vehicles": counts.get("unique_vehicles", 0),
        },
        "current_catalog": {
            "dataset_scope": counts.get("current_catalog_scope"),
            "crawl_run_id": counts.get("current_catalog_crawl_run_id"),
            "fitment_rows": current_catalog_rows,
            "unique_part_numbers": int(counts.get("current_unique_part_numbers") or 0),
            "name_language": {
                "status": "not_verified" if current_catalog_rows else "not_available",
                "non_ascii_rows": int(counts.get("current_non_ascii_part_name_rows") or 0),
                "screening": "non_ascii_conservative_only",
            },
        },
        "bounded": {
            "dataset_status": (
                "verified_bounded" if bounded_crawl_run_id is not None else "not_available"
            ),
            "crawl_run_id": bounded_crawl_run_id,
            "run_key": counts.get("bounded_run_key"),
            "dataset_kind": counts.get("bounded_dataset_kind"),
            "status": counts.get("bounded_run_status"),
            "target_rows": bounded_target,
            "run_rows": bounded_run_parts,
            "fitment_rows": bounded_rows,
            "snapshot_crawl_run_id": (
                counts.get("bounded_snapshot_min_run_id")
                if counts.get("bounded_snapshot_min_run_id")
                == counts.get("bounded_snapshot_max_run_id")
                else None
            ),
            "unique_part_numbers": int(counts.get("bounded_unique_part_numbers") or 0),
            "unique_vehicles": int(counts.get("bounded_unique_vehicles") or 0),
            "desired_scope": {
                "brand": desired_scope[0],
                "model": desired_scope[1],
                "vehicle_year_floor": desired_scope[2],
                "updated_at": counts.get("desired_scope_updated_at"),
            },
            "latest_run_scope": {
                "brand": latest_bounded_scope[0],
                "model": latest_bounded_scope[1],
                "vehicle_year_floor": latest_bounded_scope[2],
            },
            "scope_matches_desired": bounded_scope_matches_desired,
            "started_at": counts.get("bounded_started_at"),
            "finished_at": counts.get("bounded_finished_at"),
            "error": counts.get("bounded_error_msg"),
            "scheduler": {
                "run_id": counts.get("bounded_scheduled_job_run_id"),
                "job_name": counts.get("bounded_scheduler_job_name"),
                "trigger_mode": counts.get("bounded_scheduler_trigger_mode"),
                "status": counts.get("bounded_scheduler_status"),
                "exit_code": bounded_scheduler_exit,
                "started_at": counts.get("bounded_scheduler_started_at"),
                "finished_at": counts.get("bounded_scheduler_finished_at"),
            },
            "source_provenance": {
                "official_source_url_rows": int(
                    counts.get("bounded_official_source_url_rows") or 0
                ),
                "invalid_source_url_rows": bounded_quality["invalid_source_url_rows"],
                "evidence_level": (
                    "verified_live_http_replay_mapping_chain"
                    if live_mapping_evidence
                    else "not_verified"
                ),
                "raw_http_artifact_status": (
                    "raw_hash_and_sanitized_parser_body_persisted"
                    if live_mapping_evidence
                    else "not_verified"
                ),
                "live_http_evidence": live_mapping_evidence,
                "evidence_status": counts.get("bounded_evidence_status"),
                "manifest_sha256": counts.get("bounded_evidence_manifest_sha256"),
                "dataset_sha256": counts.get("bounded_evidence_dataset_sha256"),
                "artifact_count": evidence_artifact_count,
                "record_count": evidence_record_count,
                "required_page_types": [
                    "genuine",
                    "locate",
                    "pick",
                    "vehicle",
                    "category",
                    "unit",
                ],
                "verified_page_type_count": evidence_page_type_count,
                "non_live_data_marker": bool(bounded_quality["non_live_data_marker"]),
            },
            "part_range_source": {
                "populated_rows": max(bounded_rows - bounded_quality["part_range_missing_rows"], 0),
                "missing_rows": bounded_quality["part_range_missing_rows"],
                "status": (
                    "not_available"
                    if bounded_rows == 0
                    else "unavailable_vehicle_range_used"
                    if bounded_quality["part_range_missing_rows"] == bounded_rows
                    else "partially_available"
                    if bounded_quality["part_range_missing_rows"]
                    else "complete"
                ),
            },
            "name_language": {
                "status": "not_verified" if bounded_rows else "not_available",
                "english_name_unverified_rows": bounded_quality["english_name_unverified_rows"],
                "screening": "non_ascii_conservative_only",
            },
            "ready": bounded_ready,
            "blocking_reasons": bounded_blocking_reasons,
        },
        "nhtsa": {
            "current_records": nhtsa_current_records,
            "current_artifacts": int(counts.get("nhtsa_current_artifacts", 0)),
            "datasets": [
                {
                    "dataset_name": row.get("dataset_name"),
                    "row_count": int(row.get("row_count", 0)),
                }
                for row in nhtsa_datasets
            ],
            "sync_runs": {
                "total": int(counts.get("nhtsa_sync_runs", 0)),
                "completed": int(counts.get("nhtsa_completed_runs", 0)),
                "failed": int(counts.get("nhtsa_failed_runs", 0)),
            },
            "rejected_rows": int(counts.get("nhtsa_rejected_rows", 0)),
            "vin_decodes": nhtsa_count,
            "terminal_undecodable_vins": terminal_undecodable_count,
            "vin_decode_status": (
                "decoded"
                if nhtsa_count
                else "terminal_undecodable"
                if terminal_undecodable_count
                else "awaiting_authorized_vin"
            ),
        },
        "mappings": mappings,
        "admin": {
            "part_translations": counts.get("admin_part_translations", 0),
            "part_fitments": counts.get("admin_part_fitments", 0),
            "category_labels": counts.get("admin_category_labels", 0),
            "reconciliation_items": counts.get("admin_reconciliation_items", 0),
            "crawl_requests": counts.get("admin_crawl_requests", 0),
            "scheduled_job_runs": counts.get("scheduled_job_runs", 0),
        },
        "quarantine": {
            "total": int(counts.get("quarantine_total", 0)),
            "unresolved": int(counts.get("quarantine_unresolved", 0)),
        },
        "data_quality": {
            "current_catalog": published_quality,
            "sample": sample_quality,
            "bounded": bounded_quality,
            "nhtsa": nhtsa_quality,
            "small_category": {
                "source_status": "unavailable_in_current_partsouq_hierarchy",
                "crawled_rows": 0,
            },
        },
        "sample_progress": {
            "target_rows": sample_target,
            "latest_run_rows": sample_rows,
            "unique_part_numbers": int(sample_quality.get("unique_part_numbers", 0)),
            "current_catalog_rows": counts.get("published_fitment_rows", 0),
        },
        "latest_crawl_run": latest_crawl_run,
        "latest_sample_run": latest_sample_run,
        "latest_bounded_run": {
            "id": bounded_crawl_run_id,
            "run_key": counts.get("bounded_run_key"),
            "status": counts.get("bounded_run_status"),
            "target_parts": bounded_target,
            "parts_ok": bounded_run_parts,
            "scheduled_job_run_id": counts.get("bounded_scheduled_job_run_id"),
            "scope": {
                "brand": latest_bounded_scope[0],
                "model": latest_bounded_scope[1],
                "vehicle_year_floor": latest_bounded_scope[2],
            },
        }
        if bounded_crawl_run_id is not None
        else None,
        "identifier_semantics": {
            "part_id": "shared_database_internal_id",
            "model_id": "shared_database_internal_id",
            "vehicle_id": "shared_database_internal_id",
            "category_id": "shared_database_internal_id",
            "group_id": "shared_database_internal_id",
            "vehicle_vid": "partsouq_url_parameter",
            "category_cid": "partsouq_url_parameter",
            "group_uid": "partsouq_url_parameter",
            "part_code": "partsouq_part_table_code_not_model_id",
        },
        "demo_ready": demo_ready,
        "bounded_ready": bounded_ready,
        "production_ready": production_ready,
        "demo_blocking_reasons": demo_blocking_reasons,
        "production_pending_reasons": production_pending_reasons,
        "requirements_met": production_ready,
        "blocking_reasons": production_pending_reasons,
    }


@app.get("/api/parts", dependencies=[Depends(require_admin_token)])
def list_parts(
    part_number: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, alias="pageSize"),
) -> dict[str, object]:
    _validate_page_size(page_size)
    effective_number = "COALESCE(ep.part_number_override, current_part.part_number)"
    effective_name = "COALESCE(ep.part_name_override, current_part.part_name)"
    current_from = (
        " FROM v_current_catalog_parts AS current_part "
        "LEFT JOIN station_admin_effective_parts AS ep "
        "ON ep.part_id = current_part.part_id"
    )
    where_clause = " WHERE COALESCE(ep.override_status, 'active') <> 'retired'"
    params: tuple[object, ...] = ()
    if part_number:
        normalized = normalize_catalog_part_number(part_number)
        if not normalized:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="零件號碼正規化後不可為空",
            )
        current_from += (
            " JOIN (SELECT candidate.part_id "
            "FROM v_current_catalog_parts AS candidate "
            "LEFT JOIN station_admin_effective_parts AS candidate_override "
            "ON candidate_override.part_id = candidate.part_id "
            "WHERE candidate.part_number_normalized = %s "
            "AND candidate_override.number_normalized_override IS NULL "
            "UNION ALL SELECT candidate_override.part_id "
            "FROM station_admin_effective_parts AS candidate_override "
            "JOIN v_current_catalog_parts AS candidate "
            "ON candidate.part_id = candidate_override.part_id "
            "WHERE candidate_override.number_normalized_override = %s "
            "AND candidate_override.override_status = 'active') AS matched_part "
            "ON matched_part.part_id = current_part.part_id"
        )
        params = (normalized, normalized)

    count_row = _fetch_one(
        "SELECT COUNT(*) AS total, MAX(current_part.dataset_scope) AS dataset_scope, "
        "MAX(current_part.source_crawl_run_id) AS source_crawl_run_id"
        + current_from
        + where_clause,
        params,
    )
    total = int((count_row or {}).get("total", 0))
    current_page, total_pages, offset = _pagination(total, page, page_size)
    items = _fetch_all(
        "SELECT 'verified_bounded' AS dataset_status, "
        "current_part.dataset_scope, current_part.source_crawl_run_id, "
        "current_part.part_id, current_part.model_id, current_part.vehicle_id, "
        "current_part.vehicle_vid, current_part.category_id, current_part.category_cid, "
        "current_part.group_id, current_part.group_code, current_part.group_uid, "
        "current_part.code AS part_code, "
        f"{effective_number} AS part_number, {effective_name} AS part_name, "
        "current_part.brand, current_part.model, current_part.vehicle_name, "
        "current_part.vehicle_code, current_part.prod_period, current_part.production_from, "
        "current_part.production_to, current_part.engine, current_part.trim_name, "
        "current_part.part_range, current_part.part_from, current_part.part_to, "
        "current_part.category_main, current_part.category_group, current_part.source_url, "
        "current_part.snapshot_at, "
        "COALESCE(ep.override_revision, 0) AS station_override_revision"
        + current_from
        + where_clause
        + " ORDER BY current_part.snapshot_at DESC, current_part.part_id DESC "
        "LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )
    return {
        "items": items,
        "datasetScope": (count_row or {}).get("dataset_scope"),
        "crawlRunId": (count_row or {}).get("source_crawl_run_id"),
        "page": current_page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }


@app.get("/api/sample-parts", dependencies=[Depends(require_admin_token)])
def list_sample_parts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, alias="pageSize"),
) -> dict[str, object]:
    _validate_page_size(page_size)
    sample_from = (
        " FROM parts AS p "
        "LEFT JOIN station_admin_effective_parts AS ep ON ep.part_id = p.id "
        "JOIN crawl_runs AS r ON r.id = p.seen_run_id "
        "JOIN groups_t AS g ON g.id = p.group_id "
        "JOIN categories AS c ON c.id = g.category_id "
        "JOIN vehicles AS v ON v.id = c.vehicle_id "
        "JOIN models AS m ON m.id = v.model_id "
        "JOIN brands AS b ON b.id = m.brand_id "
        "WHERE r.id = (SELECT id FROM crawl_runs WHERE status = 'sample' "
        "ORDER BY started_at DESC, id DESC LIMIT 1) "
        "AND COALESCE(ep.override_status, 'active') <> 'retired'"
    )
    count_row = _fetch_one("SELECT COUNT(*) AS total" + sample_from)
    total = int((count_row or {}).get("total", 0))
    current_page, total_pages, offset = _pagination(total, page, page_size)
    items = _fetch_all(
        "SELECT 'sample_not_published' AS dataset_status, p.id AS part_id, m.id AS model_id, "
        "v.id AS vehicle_id, v.vid AS vehicle_vid, c.id AS category_id, c.cid AS category_cid, "
        "g.id AS group_id, g.code AS group_code, g.uid AS group_uid, p.code AS part_code, "
        "COALESCE(ep.part_number_override, p.part_number) AS part_number, "
        "COALESCE(ep.part_name_override, p.name) AS part_name, "
        "b.name AS brand, m.name AS model, "
        "v.name AS vehicle_name, v.model_code AS vehicle_code, v.prod_period, "
        "v.production_from, v.production_to, v.engine, v.grade AS trim_name, "
        "p.range_str AS part_range, p.part_from, p.part_to, c.name AS category_main, "
        "g.name AS category_group, g.url AS source_url, p.updated_at AS snapshot_at, "
        "COALESCE(ep.override_revision, 0) AS station_override_revision "
        + sample_from
        + " ORDER BY p.id ASC LIMIT %s OFFSET %s",
        (page_size, offset),
    )
    return {
        "items": items,
        "page": current_page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }


@app.get("/api/bounded-parts", dependencies=[Depends(require_admin_token)])
def list_bounded_parts(
    part_number: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, alias="pageSize"),
) -> dict[str, object]:
    _validate_page_size(page_size)
    bounded_from = (
        " FROM v_current_catalog_parts AS bp "
        "LEFT JOIN station_admin_effective_parts AS ep ON ep.part_id = bp.part_id"
    )
    bounded_where = (
        " WHERE bp.dataset_scope = 'bounded' "
        "AND COALESCE(ep.override_status, 'active') <> 'retired'"
    )
    params: tuple[object, ...] = ()
    if part_number:
        normalized = normalize_catalog_part_number(part_number)
        if not normalized:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="零件號碼正規化後不可為空",
            )
        bounded_from += (
            " JOIN (SELECT candidate.part_id FROM v_current_catalog_parts AS candidate "
            "LEFT JOIN station_admin_effective_parts AS candidate_override "
            "ON candidate_override.part_id = candidate.part_id "
            "WHERE candidate.dataset_scope = 'bounded' "
            "AND candidate.part_number_normalized = %s "
            "AND candidate_override.number_normalized_override IS NULL "
            "UNION ALL SELECT candidate_override.part_id "
            "FROM station_admin_effective_parts AS candidate_override "
            "JOIN v_current_catalog_parts AS candidate "
            "ON candidate.part_id = candidate_override.part_id "
            "WHERE candidate.dataset_scope = 'bounded' "
            "AND candidate_override.number_normalized_override = %s "
            "AND candidate_override.override_status = 'active') AS matched_part "
            "ON matched_part.part_id = bp.part_id"
        )
        params = (normalized, normalized)
    count_row = _fetch_one(
        "SELECT COUNT(*) AS total, MAX(bp.source_crawl_run_id) AS crawl_run_id"
        + bounded_from
        + bounded_where,
        params,
    )
    total = int((count_row or {}).get("total", 0))
    current_page, total_pages, offset = _pagination(total, page, page_size)
    items = _fetch_all(
        "SELECT 'verified_bounded' AS dataset_status, "
        "bp.source_crawl_run_id AS crawl_run_id, bp.part_id, bp.model_id, "
        "bp.vehicle_id, bp.vehicle_vid, "
        "bp.category_id, bp.category_cid, bp.group_id, bp.group_code, bp.group_uid, "
        "bp.code AS part_code, COALESCE(ep.part_number_override, bp.part_number) "
        "AS part_number, COALESCE(ep.part_name_override, bp.part_name) AS part_name, "
        "bp.brand, bp.model, bp.vehicle_name, bp.vehicle_code, bp.prod_period, "
        "bp.production_from, bp.production_to, bp.engine, bp.trim_name, bp.part_range, "
        "bp.part_from, bp.part_to, bp.category_main, bp.category_group, bp.source_url, "
        "bp.snapshot_at, COALESCE(ep.override_revision, 0) AS station_override_revision"
        + bounded_from
        + bounded_where
        + " ORDER BY bp.part_id ASC LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )
    return {
        "items": items,
        "crawlRunId": (count_row or {}).get("crawl_run_id"),
        "page": current_page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }


@app.get("/api/parts/{part_number}/fitments", dependencies=[Depends(require_admin_token)])
def part_fitments(part_number: str) -> dict[str, list[Row]]:
    normalized = normalize_catalog_part_number(part_number)
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="零件號碼正規化後不可為空",
        )
    return {
        "catalog": _fetch_all(
            "SELECT pp.part_id, pp.dataset_scope, pp.source_crawl_run_id, "
            "pp.model_id, pp.vehicle_id, "
            "pp.vehicle_id AS partsouq_vehicle_id, pp.vehicle_vid, pp.category_id, "
            "pp.category_cid, pp.group_id, pp.group_code, pp.group_uid, pp.code AS part_code, "
            "pp.brand, pp.brand AS make_name, pp.model, pp.model AS model_name, "
            "pp.vehicle_name, pp.vehicle_code, pp.prod_period, pp.production_from, "
            "pp.production_to, pp.engine, pp.trim_name, "
            "COALESCE(ep.part_number_override, pp.part_number) AS part_number, "
            "COALESCE(ep.part_name_override, pp.part_name) AS part_name, "
            "pp.part_range, pp.part_from, pp.part_to, pp.category_main, pp.category_group, "
            "pp.source_url, pp.snapshot_at, "
            "COALESCE(ep.override_revision, 0) AS station_override_revision "
            "FROM v_current_catalog_parts AS pp "
            "JOIN (SELECT candidate.part_id "
            "FROM v_current_catalog_parts AS candidate "
            "LEFT JOIN station_admin_effective_parts AS candidate_override "
            "ON candidate_override.part_id = candidate.part_id "
            "WHERE candidate.part_number_normalized = %s "
            "AND candidate_override.number_normalized_override IS NULL "
            "UNION ALL SELECT candidate_override.part_id "
            "FROM station_admin_effective_parts AS candidate_override "
            "JOIN v_current_catalog_parts AS candidate "
            "ON candidate.part_id = candidate_override.part_id "
            "WHERE candidate_override.number_normalized_override = %s "
            "AND candidate_override.override_status = 'active') AS matched_part "
            "ON matched_part.part_id = pp.part_id "
            "LEFT JOIN station_admin_effective_parts AS ep ON ep.part_id = pp.part_id "
            "WHERE COALESCE(ep.override_status, 'active') <> 'retired' "
            "ORDER BY pp.brand, pp.model, pp.vehicle_name",
            (normalized, normalized),
        ),
        "manual": _fetch_all(
            "SELECT * FROM admin_part_fitments WHERE "
            "REGEXP_REPLACE(UPPER(part_number), '[[:space:]-]+', '') = %s "
            "ORDER BY make_name, model_name, model_year_from",
            (normalized,),
        ),
    }


@app.get("/api/vehicle-mappings", dependencies=[Depends(require_admin_token)])
def list_vehicle_mappings(
    vin_prefix: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[Row]:
    if vin_prefix:
        return _fetch_all(
            "SELECT * FROM admin_vehicle_mappings WHERE vin_prefix LIKE %s ORDER BY vin_prefix LIMIT %s",
            (f"{vin_prefix.upper()}%", limit),
        )
    return _fetch_all(
        "SELECT * FROM admin_vehicle_mappings ORDER BY updated_at DESC LIMIT %s", (limit,)
    )


@app.post(
    "/api/vehicle-mappings",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_vehicle_mapping(payload: VehicleMappingInput) -> Row:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_vehicle_mappings "
        "(vin_prefix, make_name, model_name, model_year, engine, trim_name, source_name, source_reference) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_vehicle_mappings", row_id)


@app.put("/api/vehicle-mappings/{row_id}", dependencies=[Depends(require_admin_token)])
def update_vehicle_mapping(row_id: int, payload: VehicleMappingInput) -> Row:
    current = _row_or_404("admin_vehicle_mappings", row_id)
    if current.get("vin"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="完整 VIN 對應請使用 VIN 車款確認功能",
        )
    _update_or_conflict(
        "UPDATE admin_vehicle_mappings SET vin_prefix=%s, make_name=%s, model_name=%s, model_year=%s, "
        "engine=%s, trim_name=%s, source_name=%s, source_reference=%s WHERE id=%s",
        (*payload.model_dump().values(), row_id),
    )
    return _row_or_404("admin_vehicle_mappings", row_id)


@app.get(
    "/api/vin-vehicle-mappings",
    dependencies=[Depends(require_admin_token)],
)
def list_vin_vehicle_mappings(
    vin: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[Row]:
    params: list[object] = []
    clause = "WHERE a.vin IS NOT NULL"
    if vin:
        clause += " AND a.vin = %s"
        try:
            params.append(normalize_vin(vin))
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
    params.append(limit)
    return _fetch_all(
        "SELECT a.*, b.name AS partsouq_brand, m.name AS partsouq_model, "
        "v.name AS partsouq_vehicle_name, v.model_code AS partsouq_vehicle_code, "
        f"CASE WHEN {_VIN_MAPPING_STALE_SQL} THEN 'stale' "
        "WHEN a.source_name IN ('manual-name-override', 'manual-sparse-override') "
        "THEN 'confirmed_manual_override' ELSE 'confirmed' END "
        "AS vehicle_mapping_status "
        "FROM admin_vehicle_mappings AS a "
        "JOIN nhtsa_vin_decodes AS d ON d.vin = a.vin "
        "JOIN vehicles AS v ON v.id = a.partsouq_vehicle_id "
        "JOIN models AS m ON m.id = v.model_id "
        "JOIN brands AS b ON b.id = m.brand_id "
        "LEFT JOIN (SELECT DISTINCT vehicle_id FROM v_current_catalog_parts "
        "WHERE vehicle_id IS NOT NULL) AS current_catalog "
        "ON current_catalog.vehicle_id = a.partsouq_vehicle_id "
        f"{clause} ORDER BY a.updated_at DESC LIMIT %s",
        tuple(params),
    )


@app.post(
    "/api/vin-vehicle-mappings",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_vin_vehicle_mapping(payload: VinVehicleMappingInput) -> Row:
    values = _validated_vin_vehicle_mapping(payload)
    row_id = _insert_or_conflict(
        "INSERT INTO admin_vehicle_mappings "
        "(vin_prefix, vin, partsouq_vehicle_id, make_name, model_name, model_year, engine, "
        "trim_name, source_name, source_reference) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        values,
    )
    return _row_or_404("admin_vehicle_mappings", row_id)


@app.get(
    "/api/vin-vehicle-mappings/{row_id}",
    dependencies=[Depends(require_admin_token)],
)
def get_vin_vehicle_mapping(row_id: int) -> Row:
    row = _fetch_one(
        "SELECT * FROM admin_vehicle_mappings WHERE id = %s AND vin IS NOT NULL",
        (row_id,),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="找不到資料")
    return row


@app.put(
    "/api/vin-vehicle-mappings/{row_id}",
    dependencies=[Depends(require_admin_token)],
)
def update_vin_vehicle_mapping(row_id: int, payload: VinVehicleMappingUpdateInput) -> Row:
    current = _row_or_404("admin_vehicle_mappings", row_id)
    if not current.get("vin"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="這筆資料不是完整 VIN 車款對應",
        )
    values = _validated_vin_vehicle_mapping(payload)
    connection = _connect()
    try:
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_vehicle_mappings SET vin_prefix=%s, vin=%s, "
                "partsouq_vehicle_id=%s, make_name=%s, model_name=%s, model_year=%s, "
                "engine=%s, trim_name=%s, source_name=%s, source_reference=%s, "
                "updated_at=IF(updated_at >= CURRENT_TIMESTAMP, "
                "updated_at + INTERVAL 1 SECOND, CURRENT_TIMESTAMP) "
                "WHERE id=%s AND updated_at=%s",
                (*values, row_id, payload.expected_updated_at),
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="車款對應已由其他使用者更新，請重新整理後再修改",
                )
        connection.commit()
    except pymysql.err.IntegrityError as error:
        connection.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="資料已存在") from error
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return _row_or_404("admin_vehicle_mappings", row_id)


@app.get("/api/quarantine", dependencies=[Depends(require_admin_token)])
def list_quarantine(
    state: Literal["unresolved", "all"] = "unresolved",
    run_key: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, alias="pageSize", ge=1, le=200),
) -> Row:
    """列出 part_quarantine（無名稱料號列的紀錄，使用者決定的
    「忽略 + 紀錄」政策）。state=unresolved 只回未處置列。"""
    _validate_page_size(page_size)
    clause = "WHERE 1=1"
    params: list[object] = []
    if state == "unresolved":
        clause += " AND part_quarantine.resolved_at IS NULL"
    if run_key:
        clause += " AND part_quarantine.run_key = %s"
        params.append(run_key)
    # state=all 依使用者裁決恢復「未處置優先」：resolved_at IS NULL 的列
    # （表達式 0）排在已處置列（1）前面，再依 updated_at DESC, id DESC
    # 確保分頁排序穩定。此路徑為低頻歷史檢視，filesort 屬可接受設計。
    order_by = (
        "ORDER BY (part_quarantine.resolved_at IS NOT NULL), "
        "part_quarantine.updated_at DESC, part_quarantine.id DESC"
        if state == "all"
        else "ORDER BY part_quarantine.updated_at DESC, part_quarantine.id DESC"
    )
    # 預設 unresolved 路徑是索引可服務的 ORDER BY；FORCE INDEX +
    # STRAIGHT_JOIN 鎖定「part_quarantine 驅動 + 反向掃描」的執行計畫，
    # 否則 MySQL 會依資料量自由切換（例如改由 groups_t 驅動）而退回
    # filesort。run_key 路徑用 (run_key, resolved_at, updated_at)：等值
    # run_key + resolved_at IS NULL 形成連續索引範圍，範圍內反向掃描
    # 滿足 ORDER BY，偏斜資料（大量已處置 + 少數未處置）時只掃未處置
    # 列。state=all 的排序含表達式，不做此鎖定。
    if state == "unresolved":
        from_clause = (
            f"FROM part_quarantine FORCE INDEX ("
            f"{'idx_quarantine_run_key_resolved_updated' if run_key else 'idx_quarantine_list'}) "
            f"STRAIGHT_JOIN groups_t ON groups_t.id = part_quarantine.group_id"
        )
    else:
        from_clause = "FROM part_quarantine JOIN groups_t ON groups_t.id = part_quarantine.group_id"
    total_row = _fetch_one(f"SELECT COUNT(*) AS n FROM part_quarantine {clause}", tuple(params))
    total = int((total_row or {}).get("n", 0))
    current_page, total_pages, offset = _pagination(total, page, page_size)
    rows = _fetch_all(
        f"SELECT part_quarantine.id, part_quarantine.part_number, "
        f"part_quarantine.range_str, part_quarantine.reason, part_quarantine.code, "
        f"part_quarantine.quantity, part_quarantine.note, part_quarantine.run_key, "
        f"part_quarantine.resolved_at, part_quarantine.resolution, "
        f"part_quarantine.updated_at, groups_t.code AS group_code, groups_t.uid "
        f"{from_clause} {clause} "
        f"{order_by} "
        f"LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )
    return {
        "items": rows,
        "page": current_page,
        "pageSize": page_size,
        "total": total,
        "totalPages": total_pages,
    }


@app.post(
    "/api/quarantine/{row_id}/resolve",
    dependencies=[Depends(require_admin_token)],
)
def resolve_quarantine(row_id: int, payload: QuarantineResolveInput) -> Row:
    """把 quarantine 列標記為已處置（resolved_at = now）。

    同一料號在後續 run 再次出現時，爬蟲會重開處置狀態
    （resolved_at / resolution 清空），重新回到未處置清單。
    """
    connection = _connect()
    try:
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM part_quarantine WHERE id = %s FOR UPDATE",
                (row_id,),
            )
            current = cursor.fetchone()
            if current is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="找不到資料")
            if (
                current.get("run_key") != payload.expected_run_key
                or current.get("resolved_at") is not None
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="資料已由其他排程或使用者更新，請重新整理後再處置",
                )
            cursor.execute(
                "UPDATE part_quarantine "
                "SET resolved_at = NOW(), resolution = %s, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s AND run_key = %s AND resolved_at IS NULL",
                (payload.resolution, row_id, payload.expected_run_key),
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="資料已由其他排程或使用者更新，請重新整理後再處置",
                )
            cursor.execute(
                "SELECT * FROM part_quarantine WHERE id = %s AND run_key = %s",
                (row_id, payload.expected_run_key),
            )
            updated = cursor.fetchone()
            if updated is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="處置結果無法讀回，請重新整理後確認",
                )
        connection.commit()
        return dict(updated)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _validated_vin_vehicle_mapping(payload: VinVehicleMappingInput) -> tuple[object, ...]:
    decoded = _fetch_one("SELECT * FROM nhtsa_vin_decodes WHERE vin = %s", (payload.vin,))
    if decoded is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="請先完成這組 VIN 的 NHTSA 解碼",
        )
    required_decode_fields = (
        "make_name",
        "model_name",
        "model_year",
        "engine_model",
        "trim_name",
    )
    sparse_decode = any(
        value is None or (isinstance(value, str) and not value.strip())
        for value in (decoded.get(field) for field in required_decode_fields)
    )
    if sparse_decode and not payload.allow_name_override:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="這組 VIN 的 NHTSA 解碼資料不完整，需附人工確認依據才能建立車款對應",
        )
    candidates = list_vin_vehicle_candidates(payload.vin)
    selected_candidate = next(
        (
            candidate
            for candidate in candidates
            if int(candidate["partsouq_vehicle_id"]) == payload.partsouq_vehicle_id
        ),
        None,
    )
    is_exact_candidate = (
        selected_candidate is not None and selected_candidate.get("candidate_status") == "exact"
    )
    if not is_exact_candidate and not payload.allow_name_override:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "這個候選仍需人工審查；請勾選人工確認並填寫依據"
                if selected_candidate is not None
                else "這個 PartSouq 車款不在品牌、型號、年份與引擎相容的候選內"
            ),
        )
    override_candidate = selected_candidate if not is_exact_candidate else None
    if not is_exact_candidate:
        override_compatibility = (
            "AND CAST(REGEXP_REPLACE(UPPER(p.brand), '[^A-Z0-9]', '') AS BINARY) = "
            "CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY) "
            + (
                "AND (NULLIF(TRIM(d.model_name), '') IS NULL OR "
                "CAST(REGEXP_REPLACE(UPPER(p.model), '[^A-Z0-9]', '') AS BINARY) = "
                "CAST(REGEXP_REPLACE(UPPER(d.model_name), '[^A-Z0-9]', '') AS BINARY)) "
                "AND (NULLIF(TRIM(d.engine_model), '') IS NULL OR "
                "CAST(REGEXP_REPLACE(UPPER(p.engine), '[^A-Z0-9]', '') AS BINARY) = "
                "CAST(REGEXP_REPLACE(UPPER(d.engine_model), '[^A-Z0-9]', '') AS BINARY)) "
                "AND (NULLIF(TRIM(d.trim_name), '') IS NULL OR "
                "CAST(REGEXP_REPLACE(UPPER(p.trim_name), '[^A-Z0-9]', '') AS BINARY) = "
                "CAST(REGEXP_REPLACE(UPPER(d.trim_name), '[^A-Z0-9]', '') AS BINARY)) "
                if sparse_decode
                else ""
            )
        )
        if override_candidate is None:
            override_candidate = _fetch_one(
                "SELECT DISTINCT p.vehicle_id, p.brand AS partsouq_brand, "
                "p.model AS partsouq_model, p.engine AS partsouq_engine, "
                "p.trim_name AS partsouq_trim_name FROM v_current_catalog_parts AS p "
                "JOIN nhtsa_vin_decodes AS d ON d.vin = %s "
                "WHERE p.vehicle_id = %s "
                "AND NULLIF(TRIM(p.brand), '') IS NOT NULL "
                "AND NULLIF(TRIM(p.model), '') IS NOT NULL "
                "AND (p.production_from IS NOT NULL OR p.production_to IS NOT NULL) "
                "AND (p.production_from IS NULL "
                "OR d.model_year >= CAST(LEFT(p.production_from, 4) AS UNSIGNED)) "
                "AND (p.production_to IS NULL "
                "OR d.model_year <= CAST(LEFT(p.production_to, 4) AS UNSIGNED)) "
                f"{override_compatibility}LIMIT 1",
                (payload.vin, payload.partsouq_vehicle_id),
            )
        if override_candidate is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "人工指定的車款不在目前已發布型錄、年份區間內，或與已有的 NHTSA 欄位不符"
                    if sparse_decode
                    else "人工指定的車款不在目前已發布型錄或年份區間內"
                ),
            )
    engine = decoded.get("engine_model")
    model_name = decoded.get("model_name")
    trim_name = decoded.get("trim_name")
    if sparse_decode and override_candidate is not None:
        model_name = model_name or override_candidate.get("partsouq_model")
        candidate_engine = override_candidate.get("partsouq_engine") or override_candidate.get(
            "engine"
        )
        if not engine and isinstance(candidate_engine, str):
            engine = candidate_engine
        trim_name = (
            trim_name
            or override_candidate.get("partsouq_trim_name")
            or override_candidate.get("trim_name")
        )
    return (
        payload.vin[:11],
        payload.vin,
        payload.partsouq_vehicle_id,
        decoded["make_name"],
        model_name,
        decoded["model_year"],
        engine or None,
        trim_name,
        (
            "manual-sparse-override"
            if sparse_decode
            else "manual-name-override"
            if not is_exact_candidate
            else payload.source_name
        ),
        payload.source_reference,
    )


@app.get(
    "/api/vins/{vin}/vehicle-candidates",
    dependencies=[Depends(require_admin_token)],
)
def list_vin_vehicle_candidates(vin: str) -> list[Row]:
    try:
        normalized = normalize_vin(vin)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    candidate_rows = _fetch_all(
        "SELECT DISTINCT catalog_part.vehicle_id AS partsouq_vehicle_id, "
        "catalog_part.brand AS partsouq_brand, catalog_part.model AS partsouq_model, "
        "catalog_part.vehicle_name, catalog_part.vehicle_code, "
        "catalog_part.dataset_scope AS catalog_dataset_scope, "
        "catalog_part.source_crawl_run_id AS catalog_crawl_run_id, "
        "catalog_part.prod_period, catalog_part.production_from, "
        "catalog_part.production_to, catalog_part.engine, catalog_part.trim_name, "
        "vin_decode.engine_configuration AS nhtsa_engine_configuration, "
        "vin_decode.engine_model AS nhtsa_engine_model, "
        "vin_decode.displacement_l AS nhtsa_displacement_l, "
        "vin_decode.trim_name AS nhtsa_trim_name, "
        "CASE WHEN NULLIF(TRIM(vin_decode.engine_model), '') IS NOT NULL "
        "AND NULLIF(TRIM(vin_decode.trim_name), '') IS NOT NULL "
        "AND NULLIF(TRIM(catalog_part.engine), '') IS NOT NULL "
        "AND NULLIF(TRIM(catalog_part.trim_name), '') IS NOT NULL "
        "AND CAST(REGEXP_REPLACE(UPPER(catalog_part.engine), '[^A-Z0-9]', '') AS BINARY) = "
        "CAST(REGEXP_REPLACE(UPPER(vin_decode.engine_model), '[^A-Z0-9]', '') AS BINARY) "
        "AND CAST(REGEXP_REPLACE(UPPER(catalog_part.trim_name), '[^A-Z0-9]', '') AS BINARY) = "
        "CAST(REGEXP_REPLACE(UPPER(vin_decode.trim_name), '[^A-Z0-9]', '') AS BINARY) "
        "THEN 1 ELSE 0 END AS candidate_strict_match, "
        "CASE WHEN NULLIF(TRIM(vin_decode.engine_model), '') IS NULL "
        "OR NULLIF(TRIM(vin_decode.trim_name), '') IS NULL "
        "OR NULLIF(TRIM(catalog_part.engine), '') IS NULL "
        "OR NULLIF(TRIM(catalog_part.trim_name), '') IS NULL "
        "THEN 1 ELSE 0 END AS candidate_sparse "
        "FROM nhtsa_vin_decodes AS vin_decode "
        "JOIN v_current_catalog_parts AS catalog_part ON "
        "CAST(REGEXP_REPLACE(UPPER(catalog_part.brand), '[^A-Z0-9]', '') AS BINARY) = "
        "CAST(REGEXP_REPLACE(UPPER(vin_decode.make_name), '[^A-Z0-9]', '') AS BINARY) "
        "AND CAST(REGEXP_REPLACE(UPPER(catalog_part.model), '[^A-Z0-9]', '') AS BINARY) = "
        "CAST(REGEXP_REPLACE(UPPER(vin_decode.model_name), '[^A-Z0-9]', '') AS BINARY) "
        "AND (NULLIF(TRIM(vin_decode.engine_model), '') IS NULL "
        "OR NULLIF(TRIM(catalog_part.engine), '') IS NULL "
        "OR CAST(REGEXP_REPLACE(UPPER(catalog_part.engine), '[^A-Z0-9]', '') AS BINARY) = "
        "CAST(REGEXP_REPLACE(UPPER(vin_decode.engine_model), '[^A-Z0-9]', '') AS BINARY)) "
        "WHERE vin_decode.vin = %s "
        "AND catalog_part.vehicle_id IS NOT NULL "
        "AND (catalog_part.production_from IS NOT NULL "
        "OR catalog_part.production_to IS NOT NULL) "
        "AND (catalog_part.production_from IS NULL OR vin_decode.model_year >= "
        "CAST(LEFT(catalog_part.production_from, 4) AS UNSIGNED)) "
        "AND (catalog_part.production_to IS NULL OR vin_decode.model_year <= "
        "CAST(LEFT(catalog_part.production_to, 4) AS UNSIGNED)) "
        "ORDER BY partsouq_brand, partsouq_model, vehicle_code",
        (normalized,),
    )
    candidates_by_vehicle_id: dict[int, Row] = {}
    for candidate in candidate_rows:
        vehicle_id = int(candidate["partsouq_vehicle_id"])
        current = candidates_by_vehicle_id.get(vehicle_id)
        if current is None or (
            bool(candidate["candidate_strict_match"])
            and not bool(current["candidate_strict_match"])
        ):
            candidates_by_vehicle_id[vehicle_id] = candidate
    strict_vehicle_ids = {
        vehicle_id
        for vehicle_id, candidate in candidates_by_vehicle_id.items()
        if bool(candidate["candidate_strict_match"])
    }
    candidates: list[Row] = []
    for candidate in candidates_by_vehicle_id.values():
        strict_match = bool(candidate.pop("candidate_strict_match"))
        sparse = bool(candidate.pop("candidate_sparse"))
        if strict_match and len(strict_vehicle_ids) == 1:
            candidate["candidate_status"] = "exact"
            candidate["candidate_reason"] = (
                "normalized_make_model_year_engine_trim_in_current_range"
            )
        elif strict_match:
            candidate["candidate_status"] = "ambiguous_manual_review_required"
            candidate["candidate_reason"] = (
                "multiple_normalized_make_model_year_engine_trim_matches"
            )
        else:
            candidate["candidate_status"] = "manual_review_required"
            candidate["candidate_reason"] = (
                "normalized_make_model_year_sparse_optional_fields"
                if sparse
                else "normalized_make_model_year_engine_trim_manual_review"
            )
        candidates.append(candidate)
    return candidates


@app.get(
    "/api/vins/{vin}/parts",
    dependencies=[Depends(require_admin_token)],
)
def list_vin_parts(vin: str) -> list[Row]:
    try:
        normalized = normalize_vin(vin)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return _fetch_all(
        "SELECT f.vin, f.make_name, f.model_name, f.model_year, f.engine_configuration, "
        "f.engine_model, f.displacement_l, f.nhtsa_trim_name, f.nhtsa_source_url, "
        "f.nhtsa_source_artifact_id, f.part_id, f.catalog_dataset_scope, "
        "f.catalog_crawl_run_id, f.model_id, f.vehicle_id, f.vehicle_vid, "
        "f.category_id, f.category_cid, f.group_id, f.group_uid, f.code, f.group_code, "
        "f.partsouq_vehicle_id, f.partsouq_brand, f.partsouq_model, f.vehicle_name, "
        "f.vehicle_code, f.partsouq_engine, f.partsouq_trim_name, "
        "COALESCE(ep.part_number_override, f.part_number) AS part_number, "
        "COALESCE(ep.part_name_override, f.part_name) AS part_name, "
        "f.category_main, f.category_group, f.prod_period, f.part_range, "
        "f.fitment_from, f.fitment_to, f.source_url, f.mapping_id, f.mapping_source_name, "
        "f.mapping_source_reference, f.vehicle_mapping_status, f.fitment_status, "
        "COALESCE(ep.override_revision, 0) AS station_override_revision "
        "FROM v_vin_part_fitments AS f "
        "LEFT JOIN station_admin_effective_parts AS ep ON ep.part_id = f.part_id "
        "WHERE f.vin = %s "
        "AND COALESCE(ep.override_status, 'active') <> 'retired' ORDER BY "
        "COALESCE(ep.part_number_override, f.part_number), f.partsouq_vehicle_id",
        (normalized,),
    )


@app.get("/api/part-translations", dependencies=[Depends(require_admin_token)])
def list_part_translations(
    query: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[Row]:
    if query:
        like = f"%{query}%"
        return _fetch_all(
            "SELECT * FROM admin_part_translations "
            "WHERE english_name LIKE %s OR chinese_name LIKE %s OR common_chinese_name LIKE %s "
            "ORDER BY updated_at DESC LIMIT %s",
            (like, like, like, limit),
        )
    return _fetch_all(
        "SELECT * FROM admin_part_translations ORDER BY updated_at DESC LIMIT %s", (limit,)
    )


@app.post(
    "/api/part-translations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_part_translation(payload: PartTranslationInput) -> Row:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_part_translations "
        "(english_name, chinese_name, common_chinese_name, source_name, source_reference) "
        "VALUES (%s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_part_translations", row_id)


@app.put("/api/part-translations/{row_id}", dependencies=[Depends(require_admin_token)])
def update_part_translation(row_id: int, payload: PartTranslationInput) -> Row:
    _row_or_404("admin_part_translations", row_id)
    _update_or_conflict(
        "UPDATE admin_part_translations SET english_name=%s, chinese_name=%s, common_chinese_name=%s, "
        "source_name=%s, source_reference=%s WHERE id=%s",
        (*payload.model_dump().values(), row_id),
    )
    return _row_or_404("admin_part_translations", row_id)


@app.post(
    "/api/part-fitments",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_part_fitment(payload: PartFitmentInput) -> Row:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_part_fitments "
        "(part_number, vin_prefix, make_name, model_name, model_year_from, model_year_to, engine, trim_name, "
        "source_name, source_reference) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_part_fitments", row_id)


@app.put("/api/part-fitments/{row_id}", dependencies=[Depends(require_admin_token)])
def update_part_fitment(row_id: int, payload: PartFitmentInput) -> Row:
    _row_or_404("admin_part_fitments", row_id)
    _update_or_conflict(
        "UPDATE admin_part_fitments SET part_number=%s, vin_prefix=%s, make_name=%s, model_name=%s, "
        "model_year_from=%s, model_year_to=%s, engine=%s, trim_name=%s, source_name=%s, "
        "source_reference=%s WHERE id=%s",
        (*payload.model_dump().values(), row_id),
    )
    return _row_or_404("admin_part_fitments", row_id)


@app.get("/api/categories", dependencies=[Depends(require_admin_token)])
def list_categories(limit: int = Query(default=200, ge=1, le=500)) -> list[Row]:
    return _fetch_all(
        "SELECT c.dataset_status, c.category_main, c.category_group, c.category_small, "
        "CASE WHEN c.category_small IS NULL THEN 'unavailable_in_current_partsouq_hierarchy' "
        "ELSE 'manual_only' END AS category_small_source_status, "
        "l.id, l.chinese_label, l.common_chinese_label, l.source_name, l.updated_at FROM ("
        "SELECT DISTINCT category_main, COALESCE(category_group, '') AS category_group, "
        "NULL AS category_small, 'verified_bounded' AS dataset_status "
        "FROM v_current_catalog_parts UNION "
        "SELECT DISTINCT c.name AS category_main, COALESCE(g.name, '') AS category_group, "
        "NULL AS category_small, 'sample_not_published' AS dataset_status "
        "FROM parts AS p JOIN crawl_runs AS r ON r.id = p.seen_run_id "
        "JOIN groups_t AS g ON g.id = p.group_id "
        "JOIN categories AS c ON c.id = g.category_id "
        "WHERE r.id = (SELECT id FROM crawl_runs WHERE status = 'sample' "
        "ORDER BY started_at DESC, id DESC LIMIT 1) UNION "
        "SELECT category_main, category_group, NULLIF(category_small, '') AS category_small, "
        "'manual_only' AS dataset_status "
        "FROM admin_category_labels) AS c LEFT JOIN admin_category_labels AS l "
        "ON l.category_main = c.category_main AND l.category_group = c.category_group "
        "AND l.category_small = COALESCE(c.category_small, '') "
        "ORDER BY c.category_main, c.category_group, "
        "c.category_small LIMIT %s",
        (limit,),
    )


@app.post(
    "/api/categories",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_category_label(payload: CategoryLabelInput) -> Row:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_category_labels "
        "(category_main, category_group, category_small, chinese_label, common_chinese_label, source_name) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_category_labels", row_id)


@app.put("/api/categories/{row_id}", dependencies=[Depends(require_admin_token)])
def update_category_label(row_id: int, payload: CategoryLabelInput) -> Row:
    _row_or_404("admin_category_labels", row_id)
    _update_or_conflict(
        "UPDATE admin_category_labels SET category_main=%s, category_group=%s, category_small=%s, "
        "chinese_label=%s, common_chinese_label=%s, source_name=%s WHERE id=%s",
        (*payload.model_dump().values(), row_id),
    )
    return _row_or_404("admin_category_labels", row_id)


@app.get("/api/reconciliation-items", dependencies=[Depends(require_admin_token)])
def list_reconciliation_items(
    item_status: Literal["open", "matched", "rejected"] | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[Row]:
    if item_status:
        rows = _fetch_all(
            "SELECT * FROM admin_reconciliation_items WHERE status=%s ORDER BY updated_at DESC LIMIT %s",
            (item_status, limit),
        )
    else:
        rows = _fetch_all(
            "SELECT * FROM admin_reconciliation_items ORDER BY updated_at DESC LIMIT %s", (limit,)
        )
    for row in rows:
        for key in ("left_value", "right_value"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return rows


@app.post(
    "/api/reconciliation-items",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_reconciliation_item(payload: ReconciliationInput) -> Row:
    row_id = _execute(
        "INSERT INTO admin_reconciliation_items "
        "(channel, subject_key, left_value, right_value, resolution_note) VALUES (%s, %s, %s, %s, %s)",
        (
            payload.channel,
            payload.subject_key,
            json.dumps(payload.left_value, ensure_ascii=False),
            json.dumps(payload.right_value, ensure_ascii=False),
            payload.resolution_note,
        ),
    )
    return _row_or_404("admin_reconciliation_items", row_id)


@app.put("/api/reconciliation-items/{row_id}", dependencies=[Depends(require_admin_token)])
def update_reconciliation_item(row_id: int, payload: ReconciliationUpdate) -> Row:
    _row_or_404("admin_reconciliation_items", row_id)
    _execute(
        "UPDATE admin_reconciliation_items SET status=%s, resolution_note=%s, "
        "resolved_at=IF(%s = 'open', NULL, UTC_TIMESTAMP()) WHERE id=%s",
        (payload.status, payload.resolution_note, payload.status, row_id),
    )
    return _row_or_404("admin_reconciliation_items", row_id)


@app.get("/api/crawl-requests", dependencies=[Depends(require_admin_token)])
def list_crawl_requests(limit: int = Query(default=100, ge=1, le=200)) -> list[Row]:
    return _fetch_all(
        "SELECT * FROM admin_crawl_requests ORDER BY requested_at DESC LIMIT %s", (limit,)
    )


@app.post(
    "/api/crawl-requests",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_crawl_request(payload: CrawlRequestInput) -> Row:
    requested_scope = payload.requested_scope
    if payload.job_name == "nhtsa-vin":
        try:
            requested_scope = normalize_vin(requested_scope)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error
    row_id = _execute(
        "INSERT INTO admin_crawl_requests (job_name, requested_scope) VALUES (%s, %s)",
        (payload.job_name, requested_scope),
    )
    return _row_or_404("admin_crawl_requests", row_id)


@app.get("/api/job-runs", dependencies=[Depends(require_admin_token)])
def list_job_runs(limit: int = Query(default=100, ge=1, le=200)) -> list[Row]:
    return _fetch_all(
        "SELECT id, job_name, status, started_at, finished_at, exit_code, "
        "LEFT(output_text, 1000) AS output_text FROM scheduled_job_runs "
        "ORDER BY started_at DESC LIMIT %s",
        (limit,),
    )


@app.get("/api/nhtsa/vehicles", dependencies=[Depends(require_admin_token)])
def list_nhtsa_vehicles(
    make_name: str | None = None,
    model_name: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[Row]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if make_name:
        clauses.append("make_name LIKE %s")
        params.append(f"%{make_name}%")
    if model_name:
        clauses.append("model_name LIKE %s")
        params.append(f"%{model_name}%")
    params.append(limit)
    return _fetch_all(
        "SELECT vin, make_name, model_name, model_year, engine_configuration, engine_model, "
        "displacement_l, trim_name, series_name, decoded_at FROM nhtsa_vin_decodes WHERE "
        + " AND ".join(clauses)
        + " ORDER BY make_name, model_name, model_year LIMIT %s",
        tuple(params),
    )


def main() -> None:
    bind_host = os.getenv("PARTSOUQ_ADMIN_BIND_HOST", "").strip() or "127.0.0.1"
    uvicorn.run(
        "partsouq_admin.app:app",
        host=bind_host,
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
