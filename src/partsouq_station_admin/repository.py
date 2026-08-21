from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from partsouq_crawler.parsers.common import normalize_part_number
from partsouq_station_admin.db import Database

MAX_PAGE_SIZE = 200
FANOUT_LIMIT = 100
PAGE_SIZES = (10, 25, 30, 50, 100, 200)

_FORMAL_SOURCE_TABLES = {
    "vehicle_configurations": "station_admin_formal_vehicle_configurations",
    "taxonomy_nodes": "station_admin_formal_taxonomy_nodes",
    "diagrams": "station_admin_formal_diagrams",
    "part_numbers": "station_admin_formal_part_numbers",
    "part_occurrences": "station_admin_formal_part_occurrences",
    "fitments": "station_admin_formal_fitments",
}
_HISTORICAL_SAMPLE_TABLES = {
    "part_numbers": "station_admin_historical_sample_part_numbers",
    "part_occurrences": "station_admin_historical_sample_part_occurrences",
    "fitments": "station_admin_historical_sample_fitments",
}

_SENSITIVE_QUERY_KEYS = frozenset({"ssd", "token", "key", "apikey", "api_key"})

_SOURCE_LOCK_SQL = {
    "vehicle_configurations": """
        SELECT v.id, m.id AS model_id, b.id AS brand_id
        FROM vehicles AS v
        JOIN models AS m ON m.id = v.model_id
        JOIN brands AS b ON b.id = m.brand_id
        WHERE v.id = %s
        FOR SHARE
    """,
    "diagrams": """
        SELECT g.id, c.id AS category_id
        FROM groups_t AS g
        JOIN categories AS c ON c.id = g.category_id
        WHERE g.id = %s
        FOR SHARE
    """,
    "part_numbers": """
        SELECT p.id, g.id AS group_id, c.id AS category_id, v.id AS vehicle_id,
               m.id AS model_id, b.id AS brand_id
        FROM parts AS p
        JOIN groups_t AS g ON g.id = p.group_id
        JOIN categories AS c ON c.id = g.category_id
        JOIN vehicles AS v ON v.id = c.vehicle_id
        JOIN models AS m ON m.id = v.model_id
        JOIN brands AS b ON b.id = m.brand_id
        WHERE p.id = %s
        FOR SHARE
    """,
    "part_occurrences": """
        SELECT p.id, g.id AS group_id, c.id AS category_id
        FROM parts AS p
        JOIN groups_t AS g ON g.id = p.group_id
        JOIN categories AS c ON c.id = g.category_id
        WHERE p.id = %s
        FOR SHARE
    """,
    "fitments": """
        SELECT p.id, g.id AS group_id, c.id AS category_id, v.id AS vehicle_id,
               published.part_id AS published_part_id
        FROM parts AS p
        JOIN groups_t AS g ON g.id = p.group_id
        JOIN categories AS c ON c.id = g.category_id
        JOIN vehicles AS v ON v.id = c.vehicle_id
        LEFT JOIN published_parts AS published ON published.part_id = p.id
        WHERE p.id = %s
        FOR SHARE
    """,
    "part_term_mappings": """
        SELECT t.id, p.id AS part_id
        FROM admin_part_translations AS t
        LEFT JOIN parts AS p ON p.name = t.english_name
        WHERE t.id = %s
        FOR SHARE
    """,
    "vin_vehicle_mappings": """
        SELECT d.vin, m.id AS mapping_id
        FROM nhtsa_vin_decodes AS d
        LEFT JOIN admin_vehicle_mappings AS m ON m.vin = d.vin
        WHERE CAST(CONV(SUBSTRING(SHA2(d.vin, 256), 1, 15), 16, 10) AS UNSIGNED) = %s
        FOR SHARE
    """,
    "vin_part_fitments": """
        SELECT m.id, d.vin, p.part_id
        FROM admin_vehicle_mappings AS m
        JOIN nhtsa_vin_decodes AS d ON d.vin = m.vin
        JOIN published_parts AS p ON p.vehicle_id = m.partsouq_vehicle_id
        WHERE m.id = %s AND p.part_id = %s
        FOR SHARE
    """,
    "reconciliation_cases": """
        SELECT r.id
        FROM admin_reconciliation_items AS r
        WHERE r.id = %s
        FOR SHARE
    """,
}


def redact_sensitive_url(value: str) -> str:
    parts = urlsplit(value)
    query = urlencode(
        [
            (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class AdminDataError(ValueError):
    pass


class RecordNotFoundError(AdminDataError):
    pass


class RevisionConflictError(AdminDataError):
    pass


@dataclass(frozen=True, slots=True)
class EntitySpec:
    key: str
    title: str
    table: str
    record_type: str
    source_fields: tuple[str, ...]
    editable_fields: tuple[str, ...]
    search_fields: tuple[str, ...]
    display_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordView:
    entity_type: str
    identity_key: str
    source_record_id: int | None
    manual_uuid: str | None
    payload: dict[str, Any]
    source_payload: dict[str, Any] | None
    status: str
    revision: int
    base_sha256: str
    updated_at: object | None


@dataclass(frozen=True, slots=True)
class RecordPage:
    records: tuple[RecordView, ...]
    query: str
    include_retired: bool
    page: int
    page_size: int
    total: int
    total_pages: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RecordDetail:
    record: RecordView
    events: tuple[dict[str, Any], ...]
    events_truncated: bool
    provenance: tuple[dict[str, Any], ...]
    provenance_truncated: bool


_VEHICLE_FIELDS = (
    "catalog_brand_id",
    "catalog_model_id",
    "vehicle_configuration_id",
    "catalog_brand",
    "brand_raw",
    "brand_normalized",
    "name_raw",
    "model_raw",
    "description_raw",
    "options_raw",
    "prod_period_raw",
    "production_from",
    "production_to",
    "production_precision",
    "catalog_code",
    "vehicle_external_id",
    "metadata_json",
    "source_url",
    "created_at",
    "updated_at",
)
_VEHICLE_EDITABLE_FIELDS = (
    "catalog_brand",
    "brand_raw",
    "brand_normalized",
    "name_raw",
    "model_raw",
    "description_raw",
    "options_raw",
    "prod_period_raw",
    "production_from",
    "production_to",
    "production_precision",
    "catalog_code",
    "vehicle_external_id",
    "metadata_json",
)
_DIAGRAM_FIELDS = (
    "vehicle_configuration_id",
    "taxonomy_node_id",
    "diagram_code_raw",
    "diagram_name_raw",
    "diagram_range_raw",
    "diagram_from",
    "diagram_to",
    "metadata_json",
    "source_url",
)
_PART_NUMBER_FIELDS = (
    "catalog_model_id",
    "vehicle_configuration_id",
    "source_part_code",
    "part_brand_raw",
    "number_raw",
    "number_normalized",
    "name_en_raw",
    "is_assembly_inferred",
    "assembly_inference_reason",
    "source_url",
    "created_at",
    "updated_at",
)
_PART_NUMBER_EDITABLE_FIELDS = (
    "part_brand_raw",
    "number_raw",
    "name_en_raw",
    "is_assembly_inferred",
    "assembly_inference_reason",
)
_OCCURRENCE_FIELDS = (
    "part_number_id",
    "diagram_id",
    "vehicle_configuration_id",
    "callout_raw",
    "quantity_raw",
    "part_range_raw",
    "part_from",
    "part_to",
    "part_condition_raw",
    "note_raw",
    "row_metadata_json",
    "source_url",
)
_FITMENT_FIELDS = (
    "part_occurrence_id",
    "part_number_id",
    "vehicle_configuration_id",
    "diagram_id",
    "is_verified",
    "derivation",
    "confidence",
    "effective_from",
    "effective_to",
    "source_url",
)
_TAXONOMY_FIELDS = (
    "vehicle_configuration_id",
    "parent_id",
    "depth",
    "code_raw",
    "name_raw",
    "path_raw",
    "source_url",
)
_PART_TERM_FIELDS = (
    "part_number_id",
    "name_en_raw",
    "name_en_normalized",
    "name_zh_tw",
    "common_names_zh_tw",
    "mapping_status",
    "source_kind",
    "confidence",
    "source_url",
    "observed_at",
    "created_at",
    "updated_at",
)
_VIN_VEHICLE_FIELDS = (
    "vin",
    "make_name",
    "model_name",
    "series_name",
    "body_class",
    "vehicle_type",
    "model_year",
    "manufacturer_name",
    "trim_name",
    "engine_configuration",
    "engine_cylinders",
    "displacement_l_raw",
    "engine_model",
    "engine_manufacturer",
    "fuel_type_primary",
    "drive_type",
    "transmission_style",
    "plant_country",
    "partsouq_vehicle_configuration_id",
    "decode_status",
    "error_code",
    "error_text",
    "source_kind",
    "response_id",
    "decoded_at",
    "created_at",
    "updated_at",
)
_VIN_VEHICLE_EDITABLE_FIELDS = (
    "vin",
    "make_name",
    "model_name",
    "series_name",
    "body_class",
    "vehicle_type",
    "model_year",
    "manufacturer_name",
    "trim_name",
    "engine_configuration",
    "engine_cylinders",
    "displacement_l_raw",
    "engine_model",
    "engine_manufacturer",
    "fuel_type_primary",
    "drive_type",
    "transmission_style",
    "plant_country",
    "partsouq_vehicle_configuration_id",
    "decode_status",
    "error_code",
    "error_text",
    "source_kind",
)
_VIN_PART_FIELDS = (
    "vin_vehicle_mapping_id",
    "part_number_id",
    "vehicle_configuration_id",
    "is_verified",
    "derivation",
    "confidence",
    "source_url",
    "observed_at",
    "created_at",
    "updated_at",
)
_RECONCILIATION_FIELDS = (
    "case_type",
    "subject_type",
    "subject_key",
    "severity",
    "status",
    "current_json",
    "candidate_json",
    "evidence_json",
    "comments_json",
    "assigned_to",
    "resolution",
    "source_run_key",
    "opened_at",
    "updated_at",
    "resolved_at",
)

FIELD_LABELS: dict[str, str] = {
    "catalog_brand_id": "品牌 DB ID",
    "catalog_model_id": "型號 DB ID",
    "vehicle_configuration_id": "車款 DB ID",
    "source_part_code": "零件表 Code／圖號呼叫碼",
    "catalog_brand": "型錄品牌",
    "brand_raw": "原始品牌",
    "name_raw": "名稱",
    "model_raw": "型號",
    "catalog_code": "型錄代碼",
    "production_from": "生產起始",
    "production_to": "生產結束",
    "code_raw": "分類代碼",
    "path_raw": "分類路徑",
    "depth": "分類層級",
    "diagram_code_raw": "分解圖代碼",
    "diagram_name_raw": "分解圖名稱",
    "part_brand_raw": "適用車輛品牌（非零件品牌）",
    "number_raw": "零件碼",
    "number_normalized": "標準化零件碼",
    "name_en_raw": "英文零件名稱",
    "name_en_normalized": "標準化英文名稱",
    "name_zh_tw": "中文零件名稱",
    "common_names_zh_tw": "中文常用俗稱",
    "mapping_status": "對照狀態",
    "vin": "車身號碼 VIN",
    "make_name": "品牌",
    "model_name": "型號",
    "series_name": "樣式／系列",
    "body_class": "車身樣式",
    "vehicle_type": "車種",
    "model_year": "年份",
    "manufacturer_name": "製造商",
    "partsouq_vehicle_configuration_id": "PartSouq 車型資料 ID",
    "decode_status": "解碼狀態",
    "error_code": "NHTSA 錯誤碼",
    "error_text": "NHTSA 回覆",
    "part_number_id": "零件資料 ID",
    "vin_vehicle_mapping_id": "VIN 車型資料 ID",
    "trim_name": "車型等級 Trim",
    "engine_configuration": "引擎形式",
    "engine_cylinders": "汽缸數",
    "displacement_l_raw": "排氣量（公升）",
    "engine_model": "引擎型號",
    "engine_manufacturer": "引擎製造商",
    "fuel_type_primary": "主要燃料",
    "drive_type": "驅動方式",
    "transmission_style": "變速箱形式",
    "plant_country": "生產國",
    "is_verified": "已人工確認",
    "derivation": "判定依據",
    "confidence": "信心分數",
    "case_type": "案件類型",
    "subject_type": "資料類型",
    "subject_key": "資料識別",
    "severity": "嚴重度",
    "status": "處理狀態",
    "current_json": "目前資料",
    "candidate_json": "候選資料",
    "evidence_json": "證據",
    "comments_json": "對帳留言",
    "assigned_to": "負責人",
    "resolution": "結案說明",
    "source_run_key": "爬蟲批次",
    "source_kind": "資料來源",
    "source_url": "來源網址",
    "observed_at": "觀測時間",
    "decoded_at": "解碼時間",
    "opened_at": "開案時間",
    "resolved_at": "結案時間",
}

JSON_FIELDS = frozenset(
    {
        "metadata_json",
        "row_metadata_json",
        "common_names_zh_tw",
        "current_json",
        "candidate_json",
        "evidence_json",
        "comments_json",
    }
)
BOOLEAN_FIELDS = frozenset({"is_assembly_inferred", "is_verified"})
INTEGER_FIELDS = frozenset(
    {
        "vehicle_configuration_id",
        "catalog_brand_id",
        "catalog_model_id",
        "taxonomy_node_id",
        "parent_id",
        "depth",
        "part_number_id",
        "diagram_id",
        "part_occurrence_id",
        "vin_vehicle_mapping_id",
        "model_year",
        "partsouq_vehicle_configuration_id",
        "response_id",
    }
)
NUMBER_FIELDS = frozenset({"confidence"})

ENTITY_SPECS: dict[str, EntitySpec] = {
    "vehicle_configurations": EntitySpec(
        key="vehicle_configurations",
        title="車型設定",
        table="station_admin_vehicle_configurations",
        record_type="vehicle_configuration",
        source_fields=_VEHICLE_FIELDS,
        editable_fields=_VEHICLE_EDITABLE_FIELDS,
        search_fields=("catalog_brand", "model_raw", "name_raw", "catalog_code"),
        display_fields=(
            "catalog_model_id",
            "vehicle_configuration_id",
            "catalog_brand",
            "model_raw",
            "name_raw",
            "catalog_code",
            "vehicle_external_id",
            "production_from",
            "production_to",
        ),
    ),
    "taxonomy_nodes": EntitySpec(
        key="taxonomy_nodes",
        title="零件分類（大／中；無獨立小分類來源）",
        table="station_admin_taxonomy_nodes",
        record_type="taxonomy_node",
        source_fields=_TAXONOMY_FIELDS,
        editable_fields=_TAXONOMY_FIELDS[:-1] + ("name_zh_tw", "common_names_zh_tw"),
        search_fields=("code_raw", "name_raw", "path_raw"),
        display_fields=("depth", "code_raw", "name_raw", "name_zh_tw", "path_raw"),
    ),
    "diagrams": EntitySpec(
        key="diagrams",
        title="分解圖",
        table="station_admin_diagrams",
        record_type="diagram",
        source_fields=_DIAGRAM_FIELDS,
        editable_fields=_DIAGRAM_FIELDS[:-1],
        search_fields=("diagram_code_raw", "diagram_name_raw"),
        display_fields=("diagram_code_raw", "diagram_name_raw", "vehicle_configuration_id"),
    ),
    "part_numbers": EntitySpec(
        key="part_numbers",
        title="零件來源列／號碼（料號可重複）",
        table="station_admin_part_numbers",
        record_type="part_number",
        source_fields=_PART_NUMBER_FIELDS,
        editable_fields=_PART_NUMBER_EDITABLE_FIELDS,
        search_fields=("part_brand_raw", "number_normalized", "name_en_raw"),
        display_fields=(
            "catalog_model_id",
            "vehicle_configuration_id",
            "source_part_code",
            "part_brand_raw",
            "number_raw",
            "name_en_raw",
        ),
    ),
    "part_occurrences": EntitySpec(
        key="part_occurrences",
        title="零件出現紀錄",
        table="station_admin_part_occurrences",
        record_type="part_occurrence",
        source_fields=_OCCURRENCE_FIELDS,
        editable_fields=_OCCURRENCE_FIELDS[:-1],
        search_fields=("callout_raw", "part_range_raw"),
        display_fields=(
            "part_number_id",
            "vehicle_configuration_id",
            "callout_raw",
            "quantity_raw",
        ),
    ),
    "fitments": EntitySpec(
        key="fitments",
        title="適用關係",
        table="station_admin_fitments",
        record_type="fitment",
        source_fields=_FITMENT_FIELDS,
        editable_fields=_FITMENT_FIELDS[:-1],
        search_fields=("derivation", "effective_from", "effective_to"),
        display_fields=(
            "part_number_id",
            "vehicle_configuration_id",
            "is_verified",
            "confidence",
        ),
    ),
    "part_term_mappings": EntitySpec(
        key="part_term_mappings",
        title="零件中英／俗稱對照",
        table="station_admin_part_term_mappings",
        record_type="part_term_mapping",
        source_fields=_PART_TERM_FIELDS,
        editable_fields=(
            "part_number_id",
            "name_en_raw",
            "name_en_normalized",
            "name_zh_tw",
            "common_names_zh_tw",
            "mapping_status",
            "source_kind",
            "confidence",
        ),
        search_fields=("name_en_normalized", "name_zh_tw", "mapping_status"),
        display_fields=(
            "name_en_raw",
            "name_zh_tw",
            "common_names_zh_tw",
            "mapping_status",
        ),
    ),
    "vin_vehicle_mappings": EntitySpec(
        key="vin_vehicle_mappings",
        title="VIN 對應車型",
        table="station_admin_vin_vehicle_mappings",
        record_type="vin_vehicle_mapping",
        source_fields=_VIN_VEHICLE_FIELDS,
        editable_fields=_VIN_VEHICLE_EDITABLE_FIELDS,
        search_fields=("vin", "make_name", "model_name", "series_name"),
        display_fields=(
            "vin",
            "make_name",
            "model_name",
            "series_name",
            "model_year",
            "trim_name",
            "engine_configuration",
            "displacement_l_raw",
            "partsouq_vehicle_configuration_id",
        ),
    ),
    "vin_part_fitments": EntitySpec(
        key="vin_part_fitments",
        title="VIN 適用零件",
        table="station_admin_vin_part_fitments",
        record_type="vin_part_fitment",
        source_fields=_VIN_PART_FIELDS,
        editable_fields=_VIN_PART_FIELDS[:7],
        search_fields=("derivation",),
        display_fields=(
            "vin_vehicle_mapping_id",
            "part_number_id",
            "vehicle_configuration_id",
            "is_verified",
        ),
    ),
    "reconciliation_cases": EntitySpec(
        key="reconciliation_cases",
        title="對帳頻道",
        table="station_admin_reconciliation_cases",
        record_type="reconciliation_case",
        source_fields=_RECONCILIATION_FIELDS,
        editable_fields=("severity", "status", "comments_json", "assigned_to", "resolution"),
        search_fields=("case_type", "subject_type", "subject_key", "status", "assigned_to"),
        display_fields=("severity", "status", "case_type", "subject_key", "assigned_to"),
    ),
}


def field_label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def field_kind(field: str) -> str:
    if field in JSON_FIELDS:
        return "json"
    if field in BOOLEAN_FIELDS:
        return "boolean"
    if field in INTEGER_FIELDS:
        return "integer"
    if field in NUMBER_FIELDS:
        return "number"
    return "text"


def entity_spec(entity_type: str) -> EntitySpec:
    try:
        return ENTITY_SPECS[entity_type]
    except KeyError as error:
        raise AdminDataError(f"不支援的資料類型：{entity_type}") from error


def canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


class AdminRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def request_vin_decode(self, vin: str, *, actor: str) -> None:
        reason = "request NHTSA VIN decode"
        actor, reason = self._audit_fields(actor, reason)
        with self.database.transaction():
            result = self.database.execute(
                "write.request-vin-decode",
                """
                INSERT INTO admin_crawl_requests(
                    job_name, requested_scope, status, requested_at
                ) VALUES ('nhtsa-vin', %s, 'pending', UTC_TIMESTAMP(6))
                """,
                (vin,),
            )
            self.database.execute(
                "write.audit-vin-decode-request",
                """
                INSERT INTO admin_crawl_request_audits(
                    request_id, actor, reason, created_at
                ) VALUES (%s, %s, %s, UTC_TIMESTAMP(6))
                """,
                (result.lastrowid, actor, reason),
            )

    def quarantine_summary(self) -> dict[str, int]:
        row = (
            self.database.fetch_one(
                "dashboard.quarantine-summary",
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(resolved_at IS NULL), 0) AS unresolved
                FROM part_quarantine
                """,
            )
            or {}
        )
        return {
            "total": int(row.get("total", 0)),
            "unresolved": int(row.get("unresolved", 0)),
        }

    def list_quarantine(
        self,
        *,
        state: str = "unresolved",
        run_key: str | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        """列出 part_quarantine（無名稱料號列，使用者決定的「忽略＋紀錄」政策）。"""
        if state not in {"unresolved", "all"}:
            raise AdminDataError("狀態只接受 unresolved 或 all")
        if limit not in PAGE_SIZES:
            raise AdminDataError("每頁筆數只接受 10、25、30、50、100 或 200")
        if page < 1:
            raise AdminDataError("頁碼不可小於 1")
        clause = "WHERE 1=1"
        params: list[object] = []
        if state == "unresolved":
            clause += " AND part_quarantine.resolved_at IS NULL"
        if run_key:
            clause += " AND part_quarantine.run_key = %s"
            params.append(run_key)
        total_row = self.database.fetch_one(
            "quarantine.count",
            f"SELECT COUNT(*) AS total FROM part_quarantine {clause}",
            tuple(params),
        )
        total = int((total_row or {}).get("total", 0))
        total_pages = max(1, math.ceil(total / limit))
        current_page = min(page, total_pages)
        offset = (current_page - 1) * limit
        rows = self.database.fetch_all(
            "quarantine.list",
            f"""
            SELECT part_quarantine.id, part_quarantine.part_number,
                   part_quarantine.range_str, part_quarantine.reason,
                   part_quarantine.code, part_quarantine.quantity,
                   part_quarantine.note, part_quarantine.run_key,
                   part_quarantine.resolved_at, part_quarantine.resolution,
                   part_quarantine.updated_at,
                   groups_t.code AS group_code, groups_t.uid
            FROM part_quarantine
            JOIN groups_t ON groups_t.id = part_quarantine.group_id
            {clause}
            ORDER BY part_quarantine.updated_at DESC,
                     part_quarantine.id DESC
            LIMIT %s OFFSET %s
            """,
            (*params, limit, offset),
        )
        return {
            "items": tuple(rows),
            "page": current_page,
            "pageSize": limit,
            "total": total,
            "totalPages": total_pages,
        }

    def resolve_quarantine(self, row_id: int, resolution: str) -> None:
        if len(resolution) > 255:
            raise AdminDataError("處置說明不可超過 255 字元")
        with self.database.transaction():
            existing = self.database.fetch_one(
                "quarantine.lock-row",
                "SELECT id FROM part_quarantine WHERE id = %s FOR UPDATE",
                (row_id,),
            )
            if existing is None:
                raise RecordNotFoundError("找不到這筆 quarantine 紀錄")
            self.database.execute(
                "quarantine.resolve",
                """
                UPDATE part_quarantine
                SET resolved_at = NOW(), resolution = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (resolution, row_id),
            )

    def dashboard_counts(self) -> dict[str, dict[str, int]]:
        source_columns = ",\n".join(
            f"(SELECT COUNT(*) FROM {_FORMAL_SOURCE_TABLES.get(spec.key, spec.table)}) "
            f"AS `{spec.key}`"
            for spec in ENTITY_SPECS.values()
        )
        source = (
            self.database.fetch_one(
                "dashboard.source-counts",
                f"SELECT {source_columns}",
            )
            or {}
        )
        override_rows = self.database.fetch_all(
            "dashboard.override-counts",
            """
            SELECT entity_type,
                   SUM(source_record_id IS NULL) AS manual_count,
                   SUM(source_record_id IS NOT NULL) AS override_count,
                   SUM(status = 'retired') AS retired_count
            FROM admin_override_heads
            GROUP BY entity_type
            """,
        )
        overrides = {str(row["entity_type"]): row for row in override_rows}
        return {
            key: {
                "source": int(source.get(key, 0)),
                "manual": int(overrides.get(key, {}).get("manual_count", 0)),
                "overrides": int(overrides.get(key, {}).get("override_count", 0)),
                "retired": int(overrides.get(key, {}).get("retired_count", 0)),
            }
            for key in ENTITY_SPECS
        }

    def system_data_summary(self) -> dict[str, Any]:
        row = (
            self.database.fetch_one(
                "dashboard.system-data-summary",
                """
            SELECT
                (SELECT COUNT(*) FROM parts) AS partsouq_normalized_rows,
                (SELECT COUNT(DISTINCT part_number) FROM parts)
                    AS partsouq_distinct_part_numbers,
                (SELECT COUNT(*) FROM published_parts) AS partsouq_published_rows,
                (
                    SELECT COALESCE(SUM(a.source_rows), 0)
                    FROM nhtsa_current_artifacts AS c
                    JOIN nhtsa_source_artifacts AS a ON a.id = c.artifact_id
                ) AS nhtsa_current_records,
                (SELECT COUNT(*) FROM nhtsa_vin_decodes) AS nhtsa_vin_decodes,
                current_catalog.*,
                bounded.*
            FROM (
                SELECT MAX(dataset_scope) AS partsouq_current_scope,
                       COUNT(*) AS partsouq_current_rows,
                       COUNT(DISTINCT part_number_normalized)
                           AS partsouq_current_distinct_part_numbers
                FROM v_current_catalog_parts
            ) AS current_catalog
            CROSS JOIN (
                SELECT MAX(r.id) AS bounded_crawl_run_id,
                       MAX(r.target_parts) AS bounded_target_parts,
                       MAX(r.status) AS bounded_status,
                       MAX(r.scheduled_job_run_id) AS bounded_scheduled_job_run_id,
                       MAX(jobs.trigger_mode) AS bounded_scheduler_trigger_mode,
                       MAX(jobs.status) AS bounded_scheduler_status,
                       MAX(jobs.exit_code) AS bounded_scheduler_exit_code,
                       MAX(scheduler_links.crawl_run_count)
                           AS bounded_scheduler_linked_crawl_runs,
                       MAX(CASE WHEN RIGHT(DATABASE(), 5) = '_test'
                           OR LOWER(COALESCE(r.run_key, '')) LIKE 'sample-%%'
                           OR LOWER(COALESCE(jobs.output_text, ''))
                               REGEXP 'browser-assisted|fixture|synthetic|fake'
                           THEN 1 ELSE 0 END) AS bounded_non_live_data_marker,
                       COUNT(bp.part_id) AS partsouq_bounded_rows,
                       COUNT(overrides.id) AS bounded_active_override_rows,
                       COUNT(DISTINCT bp.part_number_normalized)
                           AS partsouq_bounded_distinct_part_numbers
                FROM (SELECT 1 AS singleton) AS anchor
                LEFT JOIN (
                    SELECT id, run_key, target_parts, status, scheduled_job_run_id
                    FROM crawl_runs
                    WHERE dataset_kind = 'bounded'
                    ORDER BY started_at DESC, id DESC
                    LIMIT 1
                ) AS r ON TRUE
                LEFT JOIN scheduled_job_runs AS jobs ON jobs.id = r.scheduled_job_run_id
                LEFT JOIN (
                    SELECT scheduled_job_run_id, COUNT(*) AS crawl_run_count
                    FROM crawl_runs
                    WHERE scheduled_job_run_id IS NOT NULL
                    GROUP BY scheduled_job_run_id
                ) AS scheduler_links
                    ON scheduler_links.scheduled_job_run_id = jobs.id
                LEFT JOIN bounded_parts AS bp ON bp.crawl_run_id = r.id
                LEFT JOIN admin_override_heads AS overrides
                    ON overrides.entity_type = 'part_numbers'
                   AND overrides.source_record_id = bp.part_id
                   AND overrides.status = 'active'
            ) AS bounded
            """,
            )
            or {}
        )
        return {
            "partsouq_normalized_rows": int(row.get("partsouq_normalized_rows", 0)),
            "partsouq_distinct_part_numbers": int(row.get("partsouq_distinct_part_numbers", 0)),
            "partsouq_published_rows": int(row.get("partsouq_published_rows", 0)),
            "partsouq_current_scope": row.get("partsouq_current_scope"),
            "partsouq_current_rows": int(row.get("partsouq_current_rows", 0)),
            "partsouq_current_distinct_part_numbers": int(
                row.get("partsouq_current_distinct_part_numbers", 0)
            ),
            "partsouq_bounded_rows": int(row.get("partsouq_bounded_rows", 0)),
            "partsouq_bounded_distinct_part_numbers": int(
                row.get("partsouq_bounded_distinct_part_numbers", 0)
            ),
            "bounded_crawl_run_id": row.get("bounded_crawl_run_id"),
            "bounded_target_parts": int(row.get("bounded_target_parts") or 0),
            "bounded_status": row.get("bounded_status"),
            "bounded_scheduled_job_run_id": row.get("bounded_scheduled_job_run_id"),
            "bounded_scheduler_trigger_mode": row.get("bounded_scheduler_trigger_mode"),
            "bounded_scheduler_status": row.get("bounded_scheduler_status"),
            "bounded_scheduler_exit_code": row.get("bounded_scheduler_exit_code"),
            "bounded_scheduler_linked_crawl_runs": int(
                row.get("bounded_scheduler_linked_crawl_runs") or 0
            ),
            "bounded_non_live_data_marker": int(row.get("bounded_non_live_data_marker") or 0),
            "bounded_active_override_rows": int(row.get("bounded_active_override_rows") or 0),
            "nhtsa_current_records": int(row.get("nhtsa_current_records", 0)),
            "nhtsa_vin_decodes": int(row.get("nhtsa_vin_decodes", 0)),
        }

    def crawl_monitoring(self) -> dict[str, tuple[dict[str, Any], ...]]:
        job_runs = self.database.fetch_all(
            "monitor.scheduled-job-runs",
            """
            SELECT id, job_name, trigger_mode, status, started_at, finished_at, exit_code,
                   LEFT(output_text, 1000) AS output_text
            FROM scheduled_job_runs
            ORDER BY id DESC
            LIMIT 50
            """,
        )
        crawl_runs = self.database.fetch_all(
            "monitor.crawl-runs",
            """
            SELECT id, run_key, dataset_kind, target_parts, scheduled_job_run_id,
                   status, started_at, finished_at,
                   brands_ok, models_ok, vehicles_ok, groups_ok,
                   parts_ok, parts_new, error_msg
            FROM crawl_runs
            ORDER BY id DESC
            LIMIT 50
            """,
        )
        requests = self.database.fetch_all(
            "monitor.admin-crawl-requests",
            """
            SELECT requests.id, requests.job_name,
                   CASE
                     WHEN requests.job_name = 'nhtsa-vin'
                     THEN CONCAT(
                         LEFT(requests.requested_scope, 3),
                         '**********',
                         RIGHT(requests.requested_scope, 4)
                     )
                     ELSE requests.requested_scope
                   END AS requested_scope,
                   requests.status, audit.actor AS requested_by,
                   requests.requested_at, requests.started_at,
                   requests.finished_at, requests.error_message
            FROM admin_crawl_requests AS requests
            LEFT JOIN admin_crawl_request_audits AS audit
              ON audit.request_id = requests.id
            ORDER BY requests.id DESC
            LIMIT 100
            """,
        )
        return {
            "job_runs": tuple(job_runs),
            "crawl_runs": tuple(crawl_runs),
            "requests": tuple(requests),
        }

    def list_records(
        self,
        entity_type: str,
        *,
        query: str = "",
        page: int = 1,
        limit: int = 50,
        include_retired: bool = False,
        source_scope: str = "formal",
    ) -> RecordPage:
        spec = entity_spec(entity_type)
        if source_scope == "formal":
            source_table = _FORMAL_SOURCE_TABLES.get(spec.key)
        elif source_scope == "historical_sample":
            source_table = _HISTORICAL_SAMPLE_TABLES.get(spec.key)
            if source_table is None:
                raise AdminDataError("這個資料類型沒有歷史 sample 檢視")
        else:
            raise AdminDataError("資料來源只接受正式資料或歷史 sample")
        if source_table is not None:
            spec = replace(spec, table=source_table)
        size = min(max(limit, 1), MAX_PAGE_SIZE)
        if page < 1:
            raise AdminDataError("頁碼不可小於 1")
        normalized_query = query.strip()
        total = self._record_count(spec, normalized_query, include_retired)
        total_pages = max(1, math.ceil(total / size))
        current_page = min(page, total_pages)
        offset = (current_page - 1) * size
        visible_keys = self._page_keys(
            spec,
            normalized_query,
            offset,
            size,
            include_retired,
        )

        source_ids = [int(row["sort_id"]) for row in visible_keys if int(row["kind_order"]) == 0]
        manual_ids = [int(row["sort_id"]) for row in visible_keys if int(row["kind_order"]) == 1]
        source_rows = self._source_batch(spec, source_ids, size)
        manual_rows = self._manual_batch(spec, manual_ids, size)

        source_by_id = {int(row["id"]): self._source_record(spec, row) for row in source_rows}
        manual_by_id = {
            int(row["override_head_id"]): self._manual_record(spec, row) for row in manual_rows
        }
        records: list[RecordView] = []
        for key in visible_keys:
            kind = int(key["kind_order"])
            sort_id = int(key["sort_id"])
            record = source_by_id.get(sort_id) if kind == 0 else manual_by_id.get(sort_id)
            if record is not None:
                records.append(record)

        start = offset + 1 if records else 0
        end = offset + len(records)
        return RecordPage(
            tuple(records),
            normalized_query,
            include_retired,
            current_page,
            size,
            total,
            total_pages,
            start,
            end,
        )

    def _record_count(
        self,
        spec: EntitySpec,
        query: str,
        include_retired: bool,
    ) -> int:
        if query:
            row = self._search_record_count(spec, query, include_retired)
        else:
            row = self.database.fetch_one(
                f"list.count.{spec.key}",
                f"""
                SELECT
                    (
                        SELECT COUNT(*)
                        FROM {spec.table} AS s
                        LEFT JOIN admin_override_heads AS h
                          ON h.entity_type = %s AND h.source_record_id = s.id
                        WHERE (%s = 1 OR COALESCE(h.status, 'active') <> 'retired')
                    )
                    +
                    (
                        SELECT COUNT(*)
                        FROM admin_override_heads AS h
                        WHERE h.entity_type = %s
                          AND h.source_record_id IS NULL
                          AND (%s = 1 OR h.status <> 'retired')
                    ) AS total
                """,
                (spec.key, int(include_retired), spec.key, int(include_retired)),
            )
        return int((row or {}).get("total", 0))

    def _search_record_count(
        self,
        spec: EntitySpec,
        query: str,
        include_retired: bool,
    ) -> dict[str, Any] | None:
        source_search_values = self._source_search_values(spec, query)
        override_search_value = f"%{query}%"
        candidate_sql = " UNION ".join(
            f"SELECT id FROM {spec.table} WHERE `{field}` LIKE %s" for field in spec.search_fields
        )
        effective_search = " OR ".join(
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(h.payload_json, '$."
            + field
            + "')), CAST(s.`"
            + field
            + "` AS CHAR)) LIKE %s"
            for field in spec.search_fields
        )
        return self.database.fetch_one(
            f"list.count.{spec.key}",
            f"""
            SELECT COUNT(*) AS total
            FROM (
                SELECT candidates.id
                FROM ({candidate_sql}) AS candidates
                WHERE NOT EXISTS (
                    SELECT 1 FROM admin_override_heads AS existing
                    WHERE existing.entity_type = %s
                      AND existing.source_record_id = candidates.id
                )
                UNION ALL
                SELECT s.id
                FROM admin_override_heads AS h
                JOIN {spec.table} AS s ON s.id = h.source_record_id
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NOT NULL
                  AND (%s = 1 OR h.status <> 'retired')
                  AND ({effective_search})
                UNION ALL
                SELECT h.id
                FROM admin_override_heads AS h
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NULL
                  AND CAST(h.payload_json AS CHAR) LIKE %s
                  AND (%s = 1 OR h.status <> 'retired')
            ) AS matches
            """,
            [
                *source_search_values,
                spec.key,
                spec.key,
                int(include_retired),
                *source_search_values,
                spec.key,
                override_search_value,
                int(include_retired),
            ],
        )

    def _page_keys(
        self,
        spec: EntitySpec,
        query: str,
        offset: int,
        limit: int,
        include_retired: bool,
    ) -> list[dict[str, Any]]:
        if query:
            return self._search_page_keys(spec, query, offset, limit, include_retired)

        sql = f"""
            (
                SELECT 0 AS kind_order, s.id AS sort_id
                FROM {spec.table} AS s
                LEFT JOIN admin_override_heads AS h
                  ON h.entity_type = %s AND h.source_record_id = s.id
                WHERE (%s = 1 OR COALESCE(h.status, 'active') <> 'retired')
            )
            UNION ALL
            (
                SELECT 1 AS kind_order, h.id AS sort_id
                FROM admin_override_heads AS h
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NULL
                  AND (%s = 1 OR h.status <> 'retired')
            )
            ORDER BY kind_order ASC, sort_id DESC
            LIMIT %s OFFSET %s
        """
        params: list[object] = [
            spec.key,
            int(include_retired),
            spec.key,
            int(include_retired),
            limit,
            offset,
        ]
        return self.database.fetch_all(f"list.keys.{spec.key}", sql, params)

    def _search_page_keys(
        self,
        spec: EntitySpec,
        query: str,
        offset: int,
        limit: int,
        include_retired: bool,
    ) -> list[dict[str, Any]]:
        source_search_values = self._source_search_values(spec, query)
        override_search_value = f"%{query}%"
        candidate_sql = " UNION ".join(
            f"SELECT id FROM {spec.table} WHERE `{field}` LIKE %s" for field in spec.search_fields
        )
        effective_search = " OR ".join(
            "COALESCE(JSON_UNQUOTE(JSON_EXTRACT(h.payload_json, '$."
            + field
            + "')), CAST(s.`"
            + field
            + "` AS CHAR)) LIKE %s"
            for field in spec.search_fields
        )
        sql = f"""
            (
                SELECT 0 AS kind_order, candidates.id AS sort_id
                FROM ({candidate_sql}) AS candidates
                WHERE NOT EXISTS (
                    SELECT 1 FROM admin_override_heads AS existing
                    WHERE existing.entity_type = %s
                      AND existing.source_record_id = candidates.id
                )
            )
            UNION ALL
            (
                SELECT 0 AS kind_order, s.id AS sort_id
                FROM admin_override_heads AS h
                JOIN {spec.table} AS s ON s.id = h.source_record_id
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NOT NULL
                  AND (%s = 1 OR h.status <> 'retired')
                  AND ({effective_search})
            )
            UNION ALL
            (
                SELECT 1 AS kind_order, h.id AS sort_id
                FROM admin_override_heads AS h
                WHERE h.entity_type = %s
                  AND h.source_record_id IS NULL
                  AND CAST(h.payload_json AS CHAR) LIKE %s
                  AND (%s = 1 OR h.status <> 'retired')
            )
            ORDER BY kind_order ASC, sort_id DESC
            LIMIT %s OFFSET %s
        """
        params: list[object] = [
            *source_search_values,
            spec.key,
            spec.key,
            int(include_retired),
            *source_search_values,
            spec.key,
            override_search_value,
            int(include_retired),
            limit,
            offset,
        ]
        return self.database.fetch_all(f"list.keys.{spec.key}", sql, params)

    @staticmethod
    def _source_search_values(spec: EntitySpec, query: str) -> list[str]:
        normalized_query = normalize_part_number(query)
        return [
            f"{normalized_query or query}%"
            if spec.key == "part_numbers" and field == "number_normalized"
            else f"{query}%"
            for field in spec.search_fields
        ]

    def _source_batch(
        self,
        spec: EntitySpec,
        source_ids: list[int],
        page_size: int,
    ) -> list[dict[str, Any]]:
        padded_ids = [*source_ids, *([0] * (page_size - len(source_ids)))]
        placeholders = ", ".join(["%s"] * page_size)
        fields = ", ".join(f"s.`{field}`" for field in spec.source_fields)
        return self.database.fetch_all(
            f"list.source-batch.{spec.key}",
            f"""
            SELECT s.id, {fields},
                   h.id AS override_head_id, h.identity_key, h.manual_uuid,
                   h.payload_json AS override_payload_json, h.status AS override_status,
                   h.revision AS override_revision, h.base_sha256 AS override_base_sha256,
                   h.updated_at AS override_updated_at
            FROM {spec.table} AS s
            LEFT JOIN admin_override_heads AS h
              ON h.entity_type = %s AND h.source_record_id = s.id
            WHERE s.id IN ({placeholders})
            """,
            [spec.key, *padded_ids],
        )

    def _manual_batch(
        self,
        spec: EntitySpec,
        head_ids: list[int],
        page_size: int,
    ) -> list[dict[str, Any]]:
        padded_ids = [*head_ids, *([0] * (page_size - len(head_ids)))]
        placeholders = ", ".join(["%s"] * page_size)
        return self.database.fetch_all(
            f"list.manual-batch.{spec.key}",
            f"""
            SELECT id AS override_head_id, identity_key, source_record_id, manual_uuid,
                   payload_json AS override_payload_json, status AS override_status,
                   revision AS override_revision, base_sha256 AS override_base_sha256,
                   updated_at AS override_updated_at
            FROM admin_override_heads
            WHERE entity_type = %s AND source_record_id IS NULL
              AND id IN ({placeholders})
            """,
            [spec.key, *padded_ids],
        )

    def get_record(self, entity_type: str, identity_key: str) -> RecordDetail:
        spec = entity_spec(entity_type)
        source_id, manual_uuid = self._parse_identity(identity_key)
        base = self._detail_base(spec, source_id or 0)
        head = self.database.fetch_one(
            f"detail.head.{spec.key}",
            """
            SELECT id AS override_head_id, identity_key, source_record_id, manual_uuid,
                   payload_json AS override_payload_json, status AS override_status,
                   revision AS override_revision, base_sha256 AS override_base_sha256,
                   updated_at AS override_updated_at
            FROM admin_override_heads
            WHERE entity_type = %s AND identity_key = %s
            """,
            (spec.key, identity_key),
        )
        if source_id is not None:
            if base is None:
                raise RecordNotFoundError("找不到來源資料")
            combined = {**base, **(head or {})}
            record = self._source_record(spec, combined)
        else:
            if head is None or str(head.get("manual_uuid")) != manual_uuid:
                raise RecordNotFoundError("找不到人工資料")
            record = self._manual_record(spec, head)

        head_id = int(head["override_head_id"]) if head else 0
        events = self.database.fetch_all(
            f"detail.events.{spec.key}",
            """
            SELECT id, action, revision, base_sha256, before_json, after_json,
                   actor, reason, created_at
            FROM admin_override_events
            WHERE head_id = %s
            ORDER BY revision DESC
            LIMIT %s
            """,
            (head_id, FANOUT_LIMIT + 1),
        )
        provenance = self._provenance(spec, record)
        return RecordDetail(
            record=record,
            events=tuple(self._decode_row(row) for row in events[:FANOUT_LIMIT]),
            events_truncated=len(events) > FANOUT_LIMIT,
            provenance=tuple(self._decode_row(row) for row in provenance[:FANOUT_LIMIT]),
            provenance_truncated=len(provenance) > FANOUT_LIMIT,
        )

    def _provenance(
        self,
        spec: EntitySpec,
        record: RecordView,
    ) -> list[dict[str, Any]]:
        if record.source_record_id is None or record.source_payload is None:
            return []
        if spec.key == "vin_vehicle_mappings":
            response_id = record.source_payload.get("response_id")
            if not response_id:
                return []
            return self.database.fetch_all(
                "detail.provenance.vin-vehicle-mappings",
                """
                SELECT id, parser_name, parser_version, source_url,
                       imported_at AS extracted_at, id AS response_id,
                       http_status, sha256 AS body_sha256,
                       downloaded_at AS fetched_at,
                       NULL AS archive_source, NULL AS collection_name,
                       NULL AS captured_at
                FROM nhtsa_source_artifacts
                WHERE id = %s
                LIMIT 1
                """,
                (response_id,),
            )

        source_url = record.source_payload.get("source_url")
        if not source_url:
            return []
        extracted_at = (
            record.source_payload.get("observed_at")
            or record.source_payload.get("updated_at")
            or record.source_payload.get("created_at")
        )
        parser_name = (
            "partsouq_catalog_adapter"
            if spec.key
            in {
                "vehicle_configurations",
                "taxonomy_nodes",
                "diagrams",
                "part_numbers",
                "part_occurrences",
                "fitments",
            }
            else "station_admin_adapter"
        )
        return [
            {
                "id": record.source_record_id,
                "source_record_id": record.source_record_id,
                "parser_name": parser_name,
                "parser_version": "unified-mysql-v1",
                "source_url": redact_sensitive_url(str(source_url)),
                "extracted_at": extracted_at,
                "response_id": None,
                "http_status": None,
                "body_sha256": None,
                "fetched_at": extracted_at,
                "archive_source": None,
                "collection_name": None,
                "captured_at": None,
            }
        ]

    def _detail_base(self, spec: EntitySpec, source_id: int) -> dict[str, Any] | None:
        fields = ", ".join(f"`{field}`" for field in spec.source_fields)
        return self.database.fetch_one(
            f"detail.base.{spec.key}",
            f"SELECT id, {fields} FROM {spec.table} WHERE id = %s",
            (source_id,),
        )

    def create_manual(
        self,
        entity_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        reason: str,
    ) -> str:
        spec = entity_spec(entity_type)
        cleaned = self._clean_payload(spec, payload)
        if spec.key == "part_numbers" and not all(
            cleaned.get(field) for field in ("number_raw", "name_en_raw")
        ):
            raise AdminDataError("人工零件資料必須包含料號與英文名稱")
        actor, reason = self._audit_fields(actor, reason)
        manual_uuid = str(uuid.uuid4())
        identity_key = f"manual:{manual_uuid}"
        empty_sha = canonical_sha256({})
        encoded = self._json(cleaned)
        with self.database.transaction():
            result = self.database.execute(
                f"write.create-head.{spec.key}",
                """
                INSERT INTO admin_override_heads(
                    entity_type, identity_key, source_record_id, manual_uuid,
                    payload_json, status, revision, base_sha256,
                    actor, reason, created_at, updated_at
                ) VALUES (%s, %s, NULL, %s, %s, 'active', 1, %s,
                          %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                """,
                (spec.key, identity_key, manual_uuid, encoded, empty_sha, actor, reason),
            )
            self._insert_event(
                spec,
                head_id=result.lastrowid,
                identity_key=identity_key,
                source_record_id=None,
                manual_uuid=manual_uuid,
                action="create",
                revision=1,
                base_sha256=empty_sha,
                before=None,
                after=cleaned,
                actor=actor,
                reason=reason,
            )
        return identity_key

    def update_record(
        self,
        entity_type: str,
        identity_key: str,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        expected_base_sha256: str,
        actor: str,
        reason: str,
    ) -> int:
        return self._change_record(
            entity_type,
            identity_key,
            action="update",
            payload=payload,
            expected_revision=expected_revision,
            expected_base_sha256=expected_base_sha256,
            actor=actor,
            reason=reason,
        )

    def retire_record(
        self,
        entity_type: str,
        identity_key: str,
        *,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> int:
        return self._change_record(
            entity_type,
            identity_key,
            action="retire",
            payload=None,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )

    def restore_record(
        self,
        entity_type: str,
        identity_key: str,
        *,
        expected_revision: int,
        actor: str,
        reason: str,
    ) -> int:
        return self._change_record(
            entity_type,
            identity_key,
            action="restore",
            payload=None,
            expected_revision=expected_revision,
            actor=actor,
            reason=reason,
        )

    def _change_record(
        self,
        entity_type: str,
        identity_key: str,
        *,
        action: str,
        payload: dict[str, Any] | None,
        expected_revision: int,
        expected_base_sha256: str | None = None,
        actor: str,
        reason: str,
    ) -> int:
        spec = entity_spec(entity_type)
        source_id, manual_uuid = self._parse_identity(identity_key)
        actor, reason = self._audit_fields(actor, reason)
        with self.database.transaction():
            base = self._locked_base(spec, source_id or 0)
            head = self.database.fetch_one(
                f"write.lock-head.{spec.key}",
                """
                SELECT id AS override_head_id, identity_key, source_record_id, manual_uuid,
                       payload_json AS override_payload_json, status AS override_status,
                       revision AS override_revision, base_sha256 AS override_base_sha256
                FROM admin_override_heads
                WHERE entity_type = %s AND identity_key = %s
                FOR UPDATE
                """,
                (spec.key, identity_key),
            )
            if source_id is not None and base is None:
                raise RecordNotFoundError("找不到來源資料")
            if source_id is None and head is None:
                raise RecordNotFoundError("找不到人工資料")

            current_revision = int(head.get("override_revision", 0)) if head else 0
            if current_revision != expected_revision:
                raise RevisionConflictError(
                    f"資料已被修改；預期版本 {expected_revision}，目前版本 {current_revision}"
                )
            source_payload = self._source_payload(spec, base) if base else {}
            base_sha256 = canonical_sha256(source_payload)
            if action == "update" and expected_base_sha256 != base_sha256:
                raise RevisionConflictError("爬蟲來源資料已更新；請重新載入後再套用人工修改")
            current_override = self._json_object(head.get("override_payload_json")) if head else {}
            before = {**source_payload, **current_override}
            status = str(head.get("override_status", "active")) if head else "active"

            if action == "update":
                if payload is None:
                    raise AdminDataError("更新內容不可為空")
                cleaned = self._clean_payload(spec, payload)
                next_payload = dict(current_override)
                for field, value in cleaned.items():
                    if field in BOOLEAN_FIELDS and value is None:
                        next_payload.pop(field, None)
                    else:
                        next_payload[field] = value
                if source_id is not None:
                    next_payload = {
                        field: value
                        for field, value in next_payload.items()
                        if value != source_payload.get(field)
                    }
                next_status = status
            else:
                next_payload = current_override
                next_status = "retired" if action == "retire" else "active"
                if action == "retire" and status == "retired":
                    raise AdminDataError("資料已停用")
                if action == "restore" and status != "retired":
                    raise AdminDataError("資料目前不是停用狀態")

            after = {**source_payload, **next_payload}
            next_revision = current_revision + 1
            encoded_payload = self._json(next_payload)
            if head is None:
                result = self.database.execute(
                    f"write.insert-head.{spec.key}",
                    """
                    INSERT INTO admin_override_heads(
                        entity_type, identity_key, source_record_id, manual_uuid,
                        payload_json, status, revision, base_sha256,
                        actor, reason, created_at, updated_at
                    ) VALUES (%s, %s, %s, NULL, %s, %s, %s, %s,
                              %s, %s, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))
                    """,
                    (
                        spec.key,
                        identity_key,
                        source_id,
                        encoded_payload,
                        next_status,
                        next_revision,
                        base_sha256,
                        actor,
                        reason,
                    ),
                )
                head_id = result.lastrowid
            else:
                head_id = int(head["override_head_id"])
                result = self.database.execute(
                    f"write.update-head.{spec.key}",
                    """
                    UPDATE admin_override_heads
                    SET payload_json = %s, status = %s, revision = %s,
                        base_sha256 = %s, actor = %s, reason = %s,
                        updated_at = UTC_TIMESTAMP(6)
                    WHERE id = %s AND revision = %s
                    """,
                    (
                        encoded_payload,
                        next_status,
                        next_revision,
                        base_sha256,
                        actor,
                        reason,
                        head_id,
                        current_revision,
                    ),
                )
                if result.rowcount != 1:
                    raise RevisionConflictError("資料版本衝突，請重新載入")

            self._insert_event(
                spec,
                head_id=head_id,
                identity_key=identity_key,
                source_record_id=source_id,
                manual_uuid=manual_uuid,
                action=action,
                revision=next_revision,
                base_sha256=base_sha256,
                before=before,
                after=after,
                actor=actor,
                reason=reason,
            )
        return next_revision

    def _locked_base(self, spec: EntitySpec, source_id: int) -> dict[str, Any] | None:
        if source_id > 0:
            lock_sql, lock_params = self._source_lock_query(spec, source_id)
            self.database.fetch_all(f"write.lock-source.{spec.key}", lock_sql, lock_params)
        fields = ", ".join(f"`{field}`" for field in spec.source_fields)
        return self.database.fetch_one(
            f"write.lock-base.{spec.key}",
            f"SELECT id, {fields} FROM {spec.table} WHERE id = %s",
            (source_id,),
        )

    @staticmethod
    def _source_lock_query(spec: EntitySpec, source_id: int) -> tuple[str, tuple[int, ...]]:
        if spec.key == "taxonomy_nodes":
            if source_id % 2 == 0:
                return (
                    """
                    SELECT c.id, g.id AS group_id
                    FROM categories AS c
                    LEFT JOIN groups_t AS g ON g.category_id = c.id
                    WHERE c.id = %s
                    FOR SHARE
                    """,
                    (source_id // 2,),
                )
            return (
                """
                SELECT g.id, c.id AS category_id
                FROM groups_t AS g
                JOIN categories AS c ON c.id = g.category_id
                WHERE g.id = %s
                FOR SHARE
                """,
                ((source_id - 1) // 2,),
            )
        if spec.key == "vin_part_fitments":
            mapping_id, part_id = divmod(source_id, 4294967296)
            return _SOURCE_LOCK_SQL[spec.key], (mapping_id, part_id)
        return _SOURCE_LOCK_SQL[spec.key], (source_id,)

    def _insert_event(
        self,
        spec: EntitySpec,
        *,
        head_id: int,
        identity_key: str,
        source_record_id: int | None,
        manual_uuid: str | None,
        action: str,
        revision: int,
        base_sha256: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        actor: str,
        reason: str,
    ) -> None:
        self.database.execute(
            f"write.append-event.{spec.key}",
            """
            INSERT INTO admin_override_events(
                head_id, entity_type, identity_key, source_record_id, manual_uuid,
                action, revision, base_sha256, before_json, after_json,
                actor, reason, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, UTC_TIMESTAMP(6))
            """,
            (
                head_id,
                spec.key,
                identity_key,
                source_record_id,
                manual_uuid,
                action,
                revision,
                base_sha256,
                self._json(before) if before is not None else None,
                self._json(after) if after is not None else None,
                actor,
                reason,
            ),
        )

    @staticmethod
    def _clean_payload(spec: EntitySpec, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(payload) - set(spec.editable_fields))
        if unknown:
            raise AdminDataError(f"不可編輯欄位：{', '.join(unknown)}")
        cleaned = {field: payload[field] for field in spec.editable_fields if field in payload}
        if not cleaned:
            raise AdminDataError("至少要提供一個可編輯欄位")
        if spec.key == "part_numbers":
            if "number_raw" in cleaned:
                raw_number = cleaned["number_raw"]
                if not isinstance(raw_number, str):
                    raise AdminDataError("料號必須是字串")
                raw_number = raw_number.strip()
                normalized = normalize_part_number(raw_number)
                if not normalized:
                    raise AdminDataError("料號正規化後不可為空")
                if len(raw_number) > 64 or len(normalized) > 64:
                    raise AdminDataError("料號不可超過 64 字元")
                cleaned["number_raw"] = raw_number
                cleaned["number_normalized"] = normalized
            if "name_en_raw" in cleaned:
                name = cleaned["name_en_raw"]
                if not isinstance(name, str) or not name.strip():
                    raise AdminDataError("英文名稱不可為空")
                cleaned["name_en_raw"] = name.strip()
                if len(cleaned["name_en_raw"]) > 512:
                    raise AdminDataError("英文名稱不可超過 512 字元")
        for field, value in cleaned.items():
            if value is None:
                continue
            if field in {"common_names_zh_tw", "comments_json"} and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                raise AdminDataError(f"{field_label(field)}必須是字串陣列")
            if field in JSON_FIELDS - {"common_names_zh_tw", "comments_json"} and not isinstance(
                value, dict
            ):
                raise AdminDataError(f"{field_label(field)}必須是 JSON object")
            if field in BOOLEAN_FIELDS and not isinstance(value, bool):
                raise AdminDataError(f"{field_label(field)}必須是布林值")
            if field in INTEGER_FIELDS:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise AdminDataError(f"{field_label(field)}必須是整數")
                minimum = 0 if field == "depth" else 1
                if value < minimum:
                    raise AdminDataError(f"{field_label(field)}不可小於 {minimum}")
                if field == "model_year" and value > 9998:
                    raise AdminDataError("年份不可大於 9998")
            if field in NUMBER_FIELDS:
                if not isinstance(value, int | float) or isinstance(value, bool):
                    raise AdminDataError(f"{field_label(field)}必須是數值")
                if not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
                    raise AdminDataError(f"{field_label(field)}必須介於 0 與 1")
        return cleaned

    @staticmethod
    def _audit_fields(actor: str, reason: str) -> tuple[str, str]:
        actor = actor.strip()
        reason = reason.strip()
        if not actor or not reason:
            raise AdminDataError("操作者與修改原因都是必填")
        if len(actor) > 191:
            raise AdminDataError("操作者名稱過長")
        return actor, reason

    @staticmethod
    def _parse_identity(identity_key: str) -> tuple[int | None, str | None]:
        if identity_key.startswith("source:"):
            try:
                source_id = int(identity_key.removeprefix("source:"))
            except ValueError as error:
                raise AdminDataError("無效的來源資料識別碼") from error
            if source_id < 1:
                raise AdminDataError("無效的來源資料識別碼")
            return source_id, None
        if identity_key.startswith("manual:"):
            manual_uuid = identity_key.removeprefix("manual:")
            try:
                parsed = str(uuid.UUID(manual_uuid))
            except ValueError as error:
                raise AdminDataError("無效的人工資料識別碼") from error
            return None, parsed
        raise AdminDataError("無效的資料識別碼")

    @classmethod
    def _source_record(cls, spec: EntitySpec, row: dict[str, Any]) -> RecordView:
        source_payload = cls._source_payload(spec, row)
        override = cls._json_object(row.get("override_payload_json"))
        source_id = int(row["id"])
        return RecordView(
            entity_type=spec.key,
            identity_key=f"source:{source_id}",
            source_record_id=source_id,
            manual_uuid=None,
            payload=cls._display_mapping({**source_payload, **override}),
            source_payload=cls._display_mapping(source_payload),
            status=str(row.get("override_status") or "active"),
            revision=int(row.get("override_revision") or 0),
            base_sha256=canonical_sha256(source_payload),
            updated_at=row.get("override_updated_at") or row.get("updated_at"),
        )

    @classmethod
    def _manual_record(cls, spec: EntitySpec, row: dict[str, Any]) -> RecordView:
        payload = cls._json_object(row.get("override_payload_json"))
        return RecordView(
            entity_type=spec.key,
            identity_key=str(row["identity_key"]),
            source_record_id=None,
            manual_uuid=str(row["manual_uuid"]),
            payload=cls._display_mapping(payload),
            source_payload=None,
            status=str(row.get("override_status") or "active"),
            revision=int(row.get("override_revision") or 1),
            base_sha256=str(row.get("override_base_sha256") or canonical_sha256({})),
            updated_at=row.get("override_updated_at"),
        )

    @classmethod
    def _source_payload(cls, spec: EntitySpec, row: dict[str, Any]) -> dict[str, Any]:
        return {field: cls._decode_value(row.get(field)) for field in spec.source_fields}

    @classmethod
    def _decode_row(cls, row: dict[str, Any]) -> dict[str, Any]:
        return {key: cls._display_value(key, value) for key, value in row.items()}

    @classmethod
    def _display_mapping(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {key: cls._display_value(key, value) for key, value in payload.items()}

    @classmethod
    def _display_value(cls, key: str, value: Any) -> Any:
        decoded = cls._decode_value(value)
        if key == "source_url" and isinstance(decoded, str):
            return redact_sensitive_url(decoded)
        if isinstance(decoded, dict):
            return cls._display_mapping(decoded)
        if isinstance(decoded, list):
            return [cls._display_value("", item) for item in decoded]
        return decoded

    @staticmethod
    def _decode_value(value: Any) -> Any:
        if isinstance(value, str) and value[:1] in ("{", "["):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @classmethod
    def _json_object(cls, value: Any) -> dict[str, Any]:
        decoded = cls._decode_value(value)
        return dict(decoded) if isinstance(decoded, dict) else {}

    @staticmethod
    def _json(payload: dict[str, Any] | None) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )

    @staticmethod
    def record_as_dict(record: RecordView) -> dict[str, Any]:
        return asdict(record)
