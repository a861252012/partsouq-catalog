from __future__ import annotations

from pathlib import Path
from typing import Any

from partsouq_crawler.db.repository import Repository
from partsouq_crawler.exporters import write_csv, write_jsonl

EXPORT_COLUMNS = (
    "Brand",
    "Name",
    "Model",
    "Prod period",
    "Production from",
    "Production to",
    "產品英文名稱",
    "Number",
    "零件大分類",
    "零件中分類",
    "零件小分類",
    "Category path",
    "Range",
    "Part range from",
    "Part range to",
    "Diagram range",
    "Condition",
    "Description",
    "Options",
    "Diagram code",
    "Diagram name",
    "Callout",
    "Quantity",
    "Note",
    "Source URL",
    "Source mode",
    "Archive source",
    "Captured at",
    "Archive truncation",
    "Response ID",
    "Response SHA-256",
    "Confidence",
    "Derivation",
    "Verified fitment",
)


class ExportService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def rows(
        self,
        *,
        include_compatibility_hints: bool = False,
        include_unverified_fitments: bool = False,
    ) -> list[dict[str, Any]]:
        verified_clause = "" if include_unverified_fitments else "WHERE f.is_verified = 1"
        cursor = await self.repository.connection.execute(
            f"""
            SELECT
                v.catalog_brand AS brand,
                v.name_raw AS vehicle_name,
                v.model_raw AS model,
                v.prod_period_raw AS prod_period,
                v.production_from,
                v.production_to,
                pn.name_en_raw AS part_name,
                pn.number_raw AS part_number,
                tn.path_raw AS category_path,
                po.part_range_raw,
                po.part_from,
                po.part_to,
                d.diagram_range_raw,
                po.part_condition_raw,
                v.description_raw,
                v.options_raw,
                d.diagram_code_raw,
                d.diagram_name_raw,
                po.callout_raw,
                po.quantity_raw,
                po.note_raw,
                f.source_url,
                CASE WHEN ac.id IS NULL THEN 'live_http' ELSE 'historical_archive' END
                  AS source_mode,
                ac.archive_source,
                ac.captured_at,
                ac.truncation_reason,
                rs.response_id,
                hr.body_sha256,
                f.confidence,
                f.derivation,
                f.is_verified
            FROM fitments f
            JOIN part_occurrences po ON po.id = f.part_occurrence_id
            JOIN part_numbers pn ON pn.id = f.part_number_id
            JOIN vehicle_configurations v ON v.id = f.vehicle_configuration_id
            JOIN diagrams d ON d.id = f.diagram_id
            LEFT JOIN taxonomy_nodes tn ON tn.id = d.taxonomy_node_id
            JOIN record_sources rs ON rs.record_type = 'fitment' AND rs.record_id = f.id
            JOIN http_responses hr ON hr.id = rs.response_id
            LEFT JOIN archive_captures ac ON ac.response_id = hr.id
            {verified_clause}
            ORDER BY pn.number_normalized, v.id, d.id, po.id
            """  # noqa: S608 - clause is selected from a fixed boolean option.
        )
        rows = [self._fitment_row(dict(row)) for row in await cursor.fetchall()]
        if include_compatibility_hints:
            rows.extend(await self._hint_rows())
        return rows

    async def export(
        self,
        path: Path,
        *,
        include_compatibility_hints: bool = False,
        include_unverified_fitments: bool = False,
    ) -> int:
        rows = await self.rows(
            include_compatibility_hints=include_compatibility_hints,
            include_unverified_fitments=include_unverified_fitments,
        )
        if path.suffix.lower() == ".jsonl":
            return write_jsonl(path, rows)
        if path.suffix.lower() == ".csv":
            return write_csv(path, rows)
        raise ValueError("export output must end in .csv or .jsonl")

    @staticmethod
    def _fitment_row(row: dict[str, Any]) -> dict[str, Any]:
        category_path = row["category_path"] or ""
        categories = category_path.split(" > ") if category_path else []
        values = (
            row["brand"],
            row["vehicle_name"],
            row["model"],
            row["prod_period"],
            row["production_from"],
            row["production_to"],
            row["part_name"],
            row["part_number"],
            categories[0] if len(categories) > 0 else None,
            categories[1] if len(categories) > 1 else None,
            categories[2] if len(categories) > 2 else None,
            category_path,
            row["part_range_raw"],
            row["part_from"],
            row["part_to"],
            row["diagram_range_raw"],
            row["part_condition_raw"],
            row["description_raw"],
            row["options_raw"],
            row["diagram_code_raw"],
            row["diagram_name_raw"],
            row["callout_raw"],
            row["quantity_raw"],
            row["note_raw"],
            row["source_url"],
            row["source_mode"],
            row["archive_source"],
            row["captured_at"],
            row["truncation_reason"],
            row["response_id"],
            row["body_sha256"],
            row["confidence"],
            row["derivation"],
            bool(row["is_verified"]),
        )
        return dict(zip(EXPORT_COLUMNS, values, strict=True))

    async def _hint_rows(self) -> list[dict[str, Any]]:
        cursor = await self.repository.connection.execute(
            """
            SELECT h.brand_text, h.model_text, h.compatibility_text, h.source_url,
                   pn.name_en_raw, pn.number_raw, rs.response_id, hr.body_sha256
            FROM compatibility_hints h
            JOIN part_numbers pn ON pn.id = h.part_number_id
            JOIN record_sources rs
              ON rs.record_type = 'compatibility_hint' AND rs.record_id = h.id
            JOIN http_responses hr ON hr.id = rs.response_id
            ORDER BY pn.number_normalized, h.id
            """
        )
        output: list[dict[str, Any]] = []
        for row in await cursor.fetchall():
            item: dict[str, Any] = {column: None for column in EXPORT_COLUMNS}
            item.update(
                {
                    "Brand": row["brand_text"],
                    "Name": row["model_text"],
                    "產品英文名稱": row["name_en_raw"],
                    "Number": row["number_raw"],
                    "Note": row["compatibility_text"],
                    "Source URL": row["source_url"],
                    "Source mode": "live_http",
                    "Response ID": row["response_id"],
                    "Response SHA-256": row["body_sha256"],
                    "Confidence": 0.4,
                    "Derivation": "search_compatibility_hint",
                    "Verified fitment": False,
                }
            )
            output.append(item)
        return output
