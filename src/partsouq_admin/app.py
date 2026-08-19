from __future__ import annotations

import hmac
import json
import os
import re
from pathlib import Path
from typing import Annotated, Literal

import pymysql
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymysql.cursors import DictCursor

from partsouq_catalog.config import DB_CONFIG
from partsouq_crawler.nhtsa.api import normalize_vin
from partsouq_crawler.parsers.common import normalize_part_number as normalize_catalog_part_number

STATIC_DIR = Path(__file__).resolve().parent / "static"
VIN_PREFIX_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{3,11}$")
ALLOWED_PAGE_SIZES = {10, 25, 50, 100, 200}

app = FastAPI(title="PartSouq Catalog Backoffice", version="0.1.0")
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
    def validate_vin_prefix(cls, value: str) -> str:
        value = value.upper()
        if not VIN_PREFIX_RE.fullmatch(value):
            raise ValueError("VIN 僅接受 3 至 11 碼 WMI/VDS 前綴，不接受完整 17 碼 VIN")
        return value


class VinInput(InputModel):
    vin: str = Field(min_length=17, max_length=17)

    @field_validator("vin", mode="before")
    @classmethod
    def validate_vin(cls, value: str) -> str:
        return normalize_vin(value)


class VinVehicleMappingInput(VinInput):
    partsouq_vehicle_id: int = Field(ge=1)
    source_name: str = Field(default="manual-confirmed", min_length=1, max_length=64)
    source_reference: str | None = Field(default=None, max_length=1024)
    allow_name_override: bool = False

    @model_validator(mode="after")
    def validate_name_override(self) -> VinVehicleMappingInput:
        if self.allow_name_override and not self.source_reference:
            raise ValueError("跨來源名稱不一致時，必須填寫人工確認依據")
        return self


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
    def validate_optional_vin_prefix(cls, value: str | None) -> str | None:
        if value is None:
            return None
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
    left_value: dict | list | str | int | float | bool | None = None
    right_value: dict | list | str | int | float | bool | None = None
    resolution_note: str | None = None


class ReconciliationUpdate(InputModel):
    status: Literal["open", "matched", "rejected"]
    resolution_note: str | None = None


class CrawlRequestInput(InputModel):
    job_name: Literal["catalog", "nhtsa-bulk", "nhtsa-api", "nhtsa-vin"]
    requested_scope: str = Field(default="all", min_length=1, max_length=64)


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(**DB_CONFIG, cursorclass=DictCursor, autocommit=True)


def _fetch_all(sql: str, params: tuple[object, ...] = ()) -> list[dict]:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    finally:
        connection.close()


def _fetch_one(sql: str, params: tuple[object, ...] = ()) -> dict | None:
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
            detail="PARTSOUQ_ADMIN_TOKEN 尚未設定，寫入功能已停用",
        )
    if x_admin_token is None or not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="後台 token 無效")


def _row_or_404(table: str, row_id: int) -> dict:
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pageSize 僅允許 10、25、50、100、200",
        )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/admin")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    _fetch_one("SELECT 1 AS ok")
    return {"status": "ok"}


@app.get("/api/database-summary")
def database_summary() -> dict:
    counts = (
        _fetch_one(
            "SELECT "
            "(SELECT COUNT(*) FROM brands) AS brands, "
            "(SELECT COUNT(*) FROM models) AS models, "
            "(SELECT COUNT(*) FROM vehicles) AS vehicles, "
            "(SELECT COUNT(*) FROM categories) AS categories, "
            "(SELECT COUNT(*) FROM groups_t) AS groups_count, "
            "(SELECT COUNT(*) FROM parts) AS parts, "
            "(SELECT COUNT(*) FROM published_parts) AS published_fitment_rows, "
            "(SELECT COUNT(DISTINCT part_number) FROM published_parts) AS unique_part_numbers, "
            "(SELECT COUNT(DISTINCT vehicle_id) FROM published_parts) AS unique_vehicles, "
            "(SELECT COUNT(*) FROM nhtsa_current_records) AS nhtsa_current_records, "
            "(SELECT COUNT(*) FROM nhtsa_current_artifacts) AS nhtsa_current_artifacts, "
            "(SELECT COUNT(*) FROM nhtsa_sync_runs) AS nhtsa_sync_runs, "
            "(SELECT COUNT(*) FROM nhtsa_sync_runs WHERE status = 'completed') "
            "AS nhtsa_completed_runs, "
            "(SELECT COUNT(*) FROM nhtsa_sync_runs WHERE status = 'failed') "
            "AS nhtsa_failed_runs, "
            "(SELECT COALESCE(SUM(rejected_rows), 0) FROM nhtsa_sync_runs) "
            "AS nhtsa_rejected_rows, "
            "(SELECT COUNT(*) FROM nhtsa_vin_decodes) AS nhtsa_vin_decodes, "
            "(SELECT COUNT(*) FROM admin_part_translations) AS admin_part_translations, "
            "(SELECT COUNT(*) FROM admin_part_fitments) AS admin_part_fitments, "
            "(SELECT COUNT(*) FROM admin_category_labels) AS admin_category_labels, "
            "(SELECT COUNT(*) FROM admin_reconciliation_items) AS admin_reconciliation_items, "
            "(SELECT COUNT(*) FROM admin_crawl_requests) AS admin_crawl_requests, "
            "(SELECT COUNT(*) FROM scheduled_job_runs) AS scheduled_job_runs"
        )
        or {}
    )
    nhtsa_datasets = _fetch_all(
        "SELECT dataset_name, COUNT(*) AS row_count FROM nhtsa_current_records "
        "GROUP BY dataset_name ORDER BY dataset_name"
    )
    mappings = (
        _fetch_one(
            "SELECT COUNT(*) AS total, "
            "COUNT(CASE WHEN a.vin IS NULL THEN 1 END) AS manual, "
            "COUNT(CASE WHEN a.vin IS NOT NULL AND d.vin IS NOT NULL "
            "AND published.vehicle_id IS NOT NULL "
            "AND CAST(a.make_name AS BINARY) = CAST(d.make_name AS BINARY) "
            "AND CAST(a.model_name AS BINARY) = CAST(d.model_name AS BINARY) "
            "AND a.model_year <=> d.model_year THEN 1 END) AS confirmed, "
            "COUNT(CASE WHEN a.vin IS NOT NULL AND (d.vin IS NULL "
            "OR published.vehicle_id IS NULL "
            "OR CAST(a.make_name AS BINARY) <> CAST(d.make_name AS BINARY) "
            "OR CAST(a.model_name AS BINARY) <> CAST(d.model_name AS BINARY) "
            "OR NOT (a.model_year <=> d.model_year)) THEN 1 END) AS stale "
            "FROM admin_vehicle_mappings AS a "
            "LEFT JOIN nhtsa_vin_decodes AS d ON d.vin = a.vin "
            "LEFT JOIN (SELECT DISTINCT vehicle_id FROM published_parts "
            "WHERE vehicle_id IS NOT NULL) AS published "
            "ON published.vehicle_id = a.partsouq_vehicle_id"
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
            "COUNT(CASE WHEN pp.part_id IS NOT NULL AND (p.id IS NULL OR g.id IS NULL "
            "OR c.id IS NULL OR v.id IS NULL OR m.id IS NULL OR p.group_id <> pp.group_id "
            "OR g.category_id <> pp.category_id OR c.vehicle_id <> pp.vehicle_id "
            "OR v.model_id <> pp.model_id) THEN 1 END) AS orphan_relation_rows, "
            "COUNT(CASE WHEN pp.production_from IS NULL AND pp.production_to IS NULL THEN 1 END) "
            "AS vehicle_range_missing_rows, "
            "COUNT(CASE WHEN pp.part_from IS NULL AND pp.part_to IS NULL THEN 1 END) "
            "AS part_range_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(pp.category_main), '') IS NULL THEN 1 END) "
            "AS category_main_missing_rows, "
            "COUNT(CASE WHEN NULLIF(TRIM(pp.category_group), '') IS NULL THEN 1 END) "
            "AS category_group_missing_rows "
            "FROM published_parts AS pp "
            "LEFT JOIN parts AS p ON p.id = pp.part_id "
            "LEFT JOIN groups_t AS g ON g.id = pp.group_id "
            "LEFT JOIN categories AS c ON c.id = pp.category_id "
            "LEFT JOIN vehicles AS v ON v.id = pp.vehicle_id "
            "LEFT JOIN models AS m ON m.id = pp.model_id"
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
            "OR NULLIF(TRIM(make_name), '') IS NULL OR NULLIF(TRIM(model_name), '') IS NULL "
            "OR model_year IS NULL OR NULLIF(TRIM(engine_configuration), '') IS NULL "
            "OR NULLIF(TRIM(engine_model), '') IS NULL OR displacement_l IS NULL "
            "OR NULLIF(TRIM(trim_name), '') IS NULL THEN 1 END) AS required_field_missing_rows "
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
    nhtsa_current_records = int(counts.get("nhtsa_current_records", 0))
    confirmed_count = int(mappings.get("confirmed", 0))
    mappings["unconfirmed_vin_decodes"] = max(nhtsa_count - confirmed_count, 0)
    sample_target = int(os.getenv("PSQ_LIMIT_PARTS", "1000"))
    sample_rows = int(sample_quality.get("row_count", 0))

    demo_blocking_reasons = []
    production_pending_reasons = []
    if int(counts.get("published_fitment_rows", 0)) == 0:
        production_pending_reasons.append("full_catalog_not_published")
    if sample_rows < sample_target:
        demo_blocking_reasons.append("sample_rows_below_target")
    if any(int(published_quality.get(key, 0)) for key in published_quality):
        production_pending_reasons.append("published_parts_data_quality_failed")
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
    if nhtsa_count == 0:
        production_pending_reasons.append("awaiting_authorized_vin")
    elif confirmed_count == 0:
        production_pending_reasons.append("no_confirmed_vin_mapping")
    if nhtsa_count and int(nhtsa_quality.get("required_field_missing_rows", 0)):
        production_pending_reasons.append("nhtsa_required_fields_missing")
    if int(mappings.get("stale", 0)) or int(mappings["unconfirmed_vin_decodes"]):
        production_pending_reasons.append("stale_or_unconfirmed_vin_mapping")
    production_pending_reasons.append("partsouq_small_category_source_unavailable")

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
            "vin_decode_status": ("awaiting_authorized_vin" if nhtsa_count == 0 else "decoded"),
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
        "data_quality": {
            "published": published_quality,
            "sample": sample_quality,
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
            "published_rows": counts.get("published_fitment_rows", 0),
        },
        "latest_crawl_run": latest_crawl_run,
        "latest_sample_run": latest_sample_run,
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
        "production_ready": production_ready,
        "demo_blocking_reasons": demo_blocking_reasons,
        "production_pending_reasons": production_pending_reasons,
        "requirements_met": production_ready,
        "blocking_reasons": production_pending_reasons,
    }


@app.get("/api/parts")
def list_parts(
    part_number: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, alias="pageSize"),
) -> dict[str, object]:
    _validate_page_size(page_size)
    where_clause = ""
    params: tuple[object, ...] = ()
    if part_number:
        normalized = normalize_catalog_part_number(part_number)
        if not normalized:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="零件號碼正規化後不可為空",
            )
        where_clause = " WHERE REGEXP_REPLACE(UPPER(part_number), '[[:space:]-]+', '') LIKE %s"
        params = (f"%{normalized}%",)

    count_row = _fetch_one(
        "SELECT COUNT(*) AS total FROM published_parts" + where_clause,
        params,
    )
    total = int((count_row or {}).get("total", 0))
    offset = (page - 1) * page_size
    items = _fetch_all(
        "SELECT 'published' AS dataset_status, part_id, model_id, vehicle_id, vehicle_vid, "
        "category_id, category_cid, group_id, group_code, group_uid, code AS part_code, "
        "part_number, part_name, brand, model, vehicle_name, vehicle_code, "
        "prod_period, production_from, production_to, engine, trim_name, "
        "part_range, part_from, part_to, category_main, category_group, source_url, snapshot_at "
        "FROM published_parts"
        + where_clause
        + " ORDER BY snapshot_at DESC, part_id DESC LIMIT %s OFFSET %s",
        (*params, page_size, offset),
    )
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": (total + page_size - 1) // page_size,
    }


@app.get("/api/sample-parts")
def list_sample_parts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, alias="pageSize"),
) -> dict[str, object]:
    _validate_page_size(page_size)
    sample_from = (
        " FROM parts AS p "
        "JOIN crawl_runs AS r ON r.id = p.seen_run_id "
        "JOIN groups_t AS g ON g.id = p.group_id "
        "JOIN categories AS c ON c.id = g.category_id "
        "JOIN vehicles AS v ON v.id = c.vehicle_id "
        "JOIN models AS m ON m.id = v.model_id "
        "JOIN brands AS b ON b.id = m.brand_id "
        "WHERE r.id = (SELECT id FROM crawl_runs WHERE status = 'sample' "
        "ORDER BY started_at DESC, id DESC LIMIT 1)"
    )
    count_row = _fetch_one("SELECT COUNT(*) AS total" + sample_from)
    total = int((count_row or {}).get("total", 0))
    offset = (page - 1) * page_size
    items = _fetch_all(
        "SELECT 'sample_not_published' AS dataset_status, p.id AS part_id, m.id AS model_id, "
        "v.id AS vehicle_id, v.vid AS vehicle_vid, c.id AS category_id, c.cid AS category_cid, "
        "g.id AS group_id, g.code AS group_code, g.uid AS group_uid, p.code AS part_code, "
        "p.part_number, p.name AS part_name, b.name AS brand, m.name AS model, "
        "v.name AS vehicle_name, v.model_code AS vehicle_code, v.prod_period, "
        "v.production_from, v.production_to, v.engine, v.grade AS trim_name, "
        "p.range_str AS part_range, p.part_from, p.part_to, c.name AS category_main, "
        "g.name AS category_group, g.url AS source_url, p.updated_at AS snapshot_at "
        + sample_from
        + " ORDER BY p.id ASC LIMIT %s OFFSET %s",
        (page_size, offset),
    )
    return {
        "items": items,
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": (total + page_size - 1) // page_size,
    }


@app.get("/api/parts/{part_number}/fitments")
def part_fitments(part_number: str) -> dict[str, list[dict]]:
    normalized = normalize_catalog_part_number(part_number)
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="零件號碼正規化後不可為空",
        )
    return {
        "catalog": _fetch_all(
            "SELECT part_id, model_id, vehicle_id, vehicle_id AS partsouq_vehicle_id, "
            "vehicle_vid, category_id, category_cid, group_id, group_code, group_uid, "
            "code AS part_code, brand, brand AS make_name, model, model AS model_name, "
            "vehicle_name, vehicle_code, prod_period, production_from, production_to, engine, "
            "trim_name, part_number, part_name, part_range, part_from, part_to, "
            "category_main, category_group, "
            "source_url, snapshot_at "
            "FROM published_parts WHERE "
            "REGEXP_REPLACE(UPPER(part_number), '[[:space:]-]+', '') = %s "
            "ORDER BY brand, model, vehicle_name",
            (normalized,),
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
) -> list[dict]:
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
def create_vehicle_mapping(payload: VehicleMappingInput) -> dict:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_vehicle_mappings "
        "(vin_prefix, make_name, model_name, model_year, engine, trim_name, source_name, source_reference) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_vehicle_mappings", row_id)


@app.put("/api/vehicle-mappings/{row_id}", dependencies=[Depends(require_admin_token)])
def update_vehicle_mapping(row_id: int, payload: VehicleMappingInput) -> dict:
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
) -> list[dict]:
    params: list[object] = []
    clause = "WHERE a.vin IS NOT NULL"
    if vin:
        clause += " AND a.vin = %s"
        try:
            params.append(normalize_vin(vin))
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
    params.append(limit)
    return _fetch_all(
        "SELECT a.*, b.name AS partsouq_brand, m.name AS partsouq_model, "
        "v.name AS partsouq_vehicle_name, v.model_code AS partsouq_vehicle_code, "
        "CASE WHEN published.vehicle_id IS NULL "
        "OR CAST(a.make_name AS BINARY) <> CAST(d.make_name AS BINARY) "
        "OR CAST(a.model_name AS BINARY) <> CAST(d.model_name AS BINARY) "
        "OR NOT (a.model_year <=> d.model_year) "
        "THEN 'stale' ELSE 'confirmed' END "
        "AS vehicle_mapping_status "
        "FROM admin_vehicle_mappings AS a "
        "JOIN nhtsa_vin_decodes AS d ON d.vin = a.vin "
        "JOIN vehicles AS v ON v.id = a.partsouq_vehicle_id "
        "JOIN models AS m ON m.id = v.model_id "
        "JOIN brands AS b ON b.id = m.brand_id "
        "LEFT JOIN (SELECT DISTINCT vehicle_id FROM published_parts "
        "WHERE vehicle_id IS NOT NULL) AS published "
        "ON published.vehicle_id = a.partsouq_vehicle_id "
        f"{clause} ORDER BY a.updated_at DESC LIMIT %s",
        tuple(params),
    )


@app.post(
    "/api/vin-vehicle-mappings",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_vin_vehicle_mapping(payload: VinVehicleMappingInput) -> dict:
    values = _validated_vin_vehicle_mapping(payload)
    row_id = _insert_or_conflict(
        "INSERT INTO admin_vehicle_mappings "
        "(vin_prefix, vin, partsouq_vehicle_id, make_name, model_name, model_year, engine, "
        "trim_name, source_name, source_reference) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        values,
    )
    return _row_or_404("admin_vehicle_mappings", row_id)


@app.put(
    "/api/vin-vehicle-mappings/{row_id}",
    dependencies=[Depends(require_admin_token)],
)
def update_vin_vehicle_mapping(row_id: int, payload: VinVehicleMappingInput) -> dict:
    current = _row_or_404("admin_vehicle_mappings", row_id)
    if not current.get("vin"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="這筆資料不是完整 VIN 車款對應",
        )
    values = _validated_vin_vehicle_mapping(payload)
    _update_or_conflict(
        "UPDATE admin_vehicle_mappings SET vin_prefix=%s, vin=%s, partsouq_vehicle_id=%s, "
        "make_name=%s, model_name=%s, model_year=%s, engine=%s, trim_name=%s, "
        "source_name=%s, source_reference=%s WHERE id=%s",
        (*values, row_id),
    )
    return _row_or_404("admin_vehicle_mappings", row_id)


def _validated_vin_vehicle_mapping(payload: VinVehicleMappingInput) -> tuple[object, ...]:
    decoded = _fetch_one("SELECT * FROM nhtsa_vin_decodes WHERE vin = %s", (payload.vin,))
    if decoded is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="請先完成這組 VIN 的 NHTSA 解碼",
        )
    candidates = list_vin_vehicle_candidates(payload.vin)
    is_exact_candidate = payload.partsouq_vehicle_id in {
        int(candidate["partsouq_vehicle_id"]) for candidate in candidates
    }
    if not is_exact_candidate and not payload.allow_name_override:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="這個 PartSouq 車款不是正規化品牌、型號與年份相符的候選",
        )
    if not is_exact_candidate:
        override_candidate = _fetch_one(
            "SELECT DISTINCT p.vehicle_id FROM published_parts AS p "
            "JOIN nhtsa_vin_decodes AS d ON d.vin = %s "
            "WHERE p.vehicle_id = %s "
            "AND (p.production_from IS NOT NULL OR p.production_to IS NOT NULL) "
            "AND (p.production_from IS NULL "
            "OR d.model_year >= CAST(LEFT(p.production_from, 4) AS UNSIGNED)) "
            "AND (p.production_to IS NULL "
            "OR d.model_year <= CAST(LEFT(p.production_to, 4) AS UNSIGNED))",
            (payload.vin, payload.partsouq_vehicle_id),
        )
        if override_candidate is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="人工指定的車款不在目前已發布型錄或年份區間內",
            )
    engine = " / ".join(
        value
        for value in (decoded.get("engine_configuration"), decoded.get("engine_model"))
        if value
    )
    return (
        payload.vin[:11],
        payload.vin,
        payload.partsouq_vehicle_id,
        decoded["make_name"],
        decoded["model_name"],
        decoded["model_year"],
        engine or None,
        decoded.get("trim_name"),
        "manual-name-override" if not is_exact_candidate else payload.source_name,
        payload.source_reference,
    )


@app.get(
    "/api/vins/{vin}/vehicle-candidates",
    dependencies=[Depends(require_admin_token)],
)
def list_vin_vehicle_candidates(vin: str) -> list[dict]:
    try:
        normalized = normalize_vin(vin)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    return _fetch_all(
        "SELECT DISTINCT p.vehicle_id AS partsouq_vehicle_id, p.brand AS partsouq_brand, "
        "p.model AS partsouq_model, p.vehicle_name, p.vehicle_code, "
        "p.prod_period, p.production_from, p.production_to, p.engine, p.trim_name, "
        "d.engine_configuration AS nhtsa_engine_configuration, "
        "d.engine_model AS nhtsa_engine_model, d.displacement_l AS nhtsa_displacement_l, "
        "d.trim_name AS nhtsa_trim_name, "
        "'normalized_make_model_year_in_published_range' AS candidate_reason "
        "FROM nhtsa_vin_decodes AS d "
        "JOIN published_parts AS p ON "
        "CAST(REGEXP_REPLACE(UPPER(p.brand), '[^A-Z0-9]', '') AS BINARY) = "
        "CAST(REGEXP_REPLACE(UPPER(d.make_name), '[^A-Z0-9]', '') AS BINARY) "
        "AND CAST(REGEXP_REPLACE(UPPER(p.model), '[^A-Z0-9]', '') AS BINARY) = "
        "CAST(REGEXP_REPLACE(UPPER(d.model_name), '[^A-Z0-9]', '') AS BINARY) "
        "WHERE d.vin = %s "
        "AND p.vehicle_id IS NOT NULL "
        "AND (p.production_from IS NOT NULL OR p.production_to IS NOT NULL) "
        "AND (p.production_from IS NULL "
        "OR d.model_year >= CAST(LEFT(p.production_from, 4) AS UNSIGNED)) "
        "AND (p.production_to IS NULL "
        "OR d.model_year <= CAST(LEFT(p.production_to, 4) AS UNSIGNED)) "
        "ORDER BY partsouq_brand, partsouq_model, vehicle_code",
        (normalized,),
    )


@app.get(
    "/api/vins/{vin}/parts",
    dependencies=[Depends(require_admin_token)],
)
def list_vin_parts(vin: str) -> list[dict]:
    try:
        normalized = normalize_vin(vin)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    return _fetch_all(
        "SELECT * FROM v_vin_part_fitments WHERE vin = %s "
        "ORDER BY part_number, partsouq_vehicle_id",
        (normalized,),
    )


@app.get("/api/part-translations")
def list_part_translations(
    query: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict]:
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
def create_part_translation(payload: PartTranslationInput) -> dict:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_part_translations "
        "(english_name, chinese_name, common_chinese_name, source_name, source_reference) "
        "VALUES (%s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_part_translations", row_id)


@app.put("/api/part-translations/{row_id}", dependencies=[Depends(require_admin_token)])
def update_part_translation(row_id: int, payload: PartTranslationInput) -> dict:
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
def create_part_fitment(payload: PartFitmentInput) -> dict:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_part_fitments "
        "(part_number, vin_prefix, make_name, model_name, model_year_from, model_year_to, engine, trim_name, "
        "source_name, source_reference) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_part_fitments", row_id)


@app.put("/api/part-fitments/{row_id}", dependencies=[Depends(require_admin_token)])
def update_part_fitment(row_id: int, payload: PartFitmentInput) -> dict:
    _row_or_404("admin_part_fitments", row_id)
    _update_or_conflict(
        "UPDATE admin_part_fitments SET part_number=%s, vin_prefix=%s, make_name=%s, model_name=%s, "
        "model_year_from=%s, model_year_to=%s, engine=%s, trim_name=%s, source_name=%s, "
        "source_reference=%s WHERE id=%s",
        (*payload.model_dump().values(), row_id),
    )
    return _row_or_404("admin_part_fitments", row_id)


@app.get("/api/categories")
def list_categories(limit: int = Query(default=200, ge=1, le=500)) -> list[dict]:
    return _fetch_all(
        "SELECT c.dataset_status, c.category_main, c.category_group, c.category_small, "
        "CASE WHEN c.category_small IS NULL THEN 'unavailable_in_current_partsouq_hierarchy' "
        "ELSE 'manual_only' END AS category_small_source_status, "
        "l.id, l.chinese_label, l.common_chinese_label, l.source_name, l.updated_at FROM ("
        "SELECT DISTINCT category_main, COALESCE(category_group, '') AS category_group, "
        "NULL AS category_small, 'published' AS dataset_status FROM published_parts UNION "
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
def create_category_label(payload: CategoryLabelInput) -> dict:
    row_id = _insert_or_conflict(
        "INSERT INTO admin_category_labels "
        "(category_main, category_group, category_small, chinese_label, common_chinese_label, source_name) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        tuple(payload.model_dump().values()),
    )
    return _row_or_404("admin_category_labels", row_id)


@app.put("/api/categories/{row_id}", dependencies=[Depends(require_admin_token)])
def update_category_label(row_id: int, payload: CategoryLabelInput) -> dict:
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
) -> list[dict]:
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
def create_reconciliation_item(payload: ReconciliationInput) -> dict:
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
def update_reconciliation_item(row_id: int, payload: ReconciliationUpdate) -> dict:
    _row_or_404("admin_reconciliation_items", row_id)
    _execute(
        "UPDATE admin_reconciliation_items SET status=%s, resolution_note=%s, "
        "resolved_at=IF(%s = 'open', NULL, UTC_TIMESTAMP()) WHERE id=%s",
        (payload.status, payload.resolution_note, payload.status, row_id),
    )
    return _row_or_404("admin_reconciliation_items", row_id)


@app.get("/api/crawl-requests", dependencies=[Depends(require_admin_token)])
def list_crawl_requests(limit: int = Query(default=100, ge=1, le=200)) -> list[dict]:
    return _fetch_all(
        "SELECT * FROM admin_crawl_requests ORDER BY requested_at DESC LIMIT %s", (limit,)
    )


@app.post(
    "/api/crawl-requests",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_token)],
)
def create_crawl_request(payload: CrawlRequestInput) -> dict:
    requested_scope = payload.requested_scope
    if payload.job_name == "nhtsa-vin":
        try:
            requested_scope = normalize_vin(requested_scope)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from error
    row_id = _execute(
        "INSERT INTO admin_crawl_requests (job_name, requested_scope) VALUES (%s, %s)",
        (payload.job_name, requested_scope),
    )
    return _row_or_404("admin_crawl_requests", row_id)


@app.get("/api/job-runs", dependencies=[Depends(require_admin_token)])
def list_job_runs(limit: int = Query(default=100, ge=1, le=200)) -> list[dict]:
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
) -> list[dict]:
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
    uvicorn.run("partsouq_admin.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
