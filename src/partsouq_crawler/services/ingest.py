from __future__ import annotations

import json
from typing import Any

import aiosqlite

from partsouq_crawler.db.repository import Repository, utc_now
from partsouq_crawler.models.records import ParsedPage, PartRecord, VehicleRecord
from partsouq_crawler.parsers.base import PARSER_VERSION
from partsouq_crawler.parsers.common import is_assembly_name, normalize_part_number
from partsouq_crawler.services.provenance import add_source


class IngestService:
    parser_name = "catalog_parser"

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def ingest(
        self,
        *,
        run_id: int,
        response_id: int,
        source_url: str,
        parsed: ParsedPage,
        verified_fitments: bool = True,
        fitment_derivation: str = "genuine_catalog_diagram_part_occurrence",
    ) -> int:
        inserted = 0
        async with self.repository.transaction() as connection:
            vehicle_id: int | None = None
            if parsed.vehicle is not None:
                vehicle_id, created = await self._vehicle(
                    connection, parsed.vehicle, source_url, response_id
                )
                inserted += created

            taxonomy_ids: dict[tuple[str, ...], int] = {}
            if vehicle_id is not None:
                for taxonomy in parsed.taxonomies:
                    parent_id: int | None = None
                    for depth, name in enumerate(taxonomy.path):
                        path = taxonomy.path[: depth + 1]
                        node_id, created = await self._taxonomy(
                            connection,
                            vehicle_id=vehicle_id,
                            parent_id=parent_id,
                            depth=depth,
                            code=taxonomy.codes[depth] if depth < len(taxonomy.codes) else None,
                            name=name,
                            path=path,
                            source_url=source_url,
                            response_id=response_id,
                        )
                        taxonomy_ids[path] = node_id
                        parent_id = node_id
                        inserted += created

            diagram_ids: dict[str, int] = {}
            if vehicle_id is not None:
                for diagram in parsed.diagrams:
                    taxonomy_id = taxonomy_ids.get(diagram.category_path)
                    if taxonomy_id is None and len(taxonomy_ids) == 1:
                        taxonomy_id = next(iter(taxonomy_ids.values()))
                    diagram_id, created = await self._diagram(
                        connection,
                        vehicle_id=vehicle_id,
                        taxonomy_id=taxonomy_id,
                        code=diagram.code_raw,
                        name=diagram.name_raw,
                        range_raw=diagram.range_raw,
                        range_from=diagram.diagram_from,
                        range_to=diagram.diagram_to,
                        metadata=diagram.metadata,
                        source_url=source_url,
                        response_id=response_id,
                    )
                    diagram_ids[diagram.code_raw or f"id:{diagram_id}"] = diagram_id
                    if diagram.name_raw:
                        diagram_ids[f"name:{diagram.name_raw}"] = diagram_id
                    inserted += created

            for part in parsed.parts:
                part_id, created = await self._part_number(
                    connection,
                    part=part,
                    fallback_brand=parsed.vehicle.catalog_brand if parsed.vehicle else None,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                selected_diagram_id = self._choose_diagram(part, diagram_ids)
                if vehicle_id is None or selected_diagram_id is None:
                    continue
                occurrence_id, created = await self._occurrence(
                    connection,
                    part_id=part_id,
                    diagram_id=selected_diagram_id,
                    vehicle_id=vehicle_id,
                    part=part,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                _, created = await self._fitment(
                    connection,
                    occurrence_id=occurrence_id,
                    part_id=part_id,
                    vehicle_id=vehicle_id,
                    diagram_id=selected_diagram_id,
                    part=part,
                    source_url=source_url,
                    response_id=response_id,
                    is_verified=verified_fitments,
                    derivation=fitment_derivation,
                )
                inserted += created

            for hint in parsed.compatibility_hints:
                hint_part = PartRecord(number_raw=hint.part_number_raw)
                part_id, created = await self._part_number(
                    connection,
                    part=hint_part,
                    fallback_brand=hint.brand_text,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                row_id, created = await self._get_or_create(
                    connection,
                    select_sql="""
                        SELECT id FROM compatibility_hints
                        WHERE part_number_id = ? AND compatibility_text = ? AND source_url = ?
                    """,
                    select_values=(part_id, hint.compatibility_text, source_url),
                    insert_sql="""
                        INSERT INTO compatibility_hints(
                            part_number_id, brand_text, model_text, compatibility_text,
                            source_url, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    insert_values=(
                        part_id,
                        hint.brand_text,
                        hint.model_text,
                        hint.compatibility_text,
                        source_url,
                        utc_now(),
                    ),
                )
                await self._source(
                    connection, "compatibility_hint", row_id, response_id, source_url
                )
                inserted += created

            for relation in parsed.part_relations:
                relation_part = PartRecord(number_raw=relation.from_part_number_raw)
                part_id, created = await self._part_number(
                    connection,
                    part=relation_part,
                    fallback_brand=None,
                    source_url=source_url,
                    response_id=response_id,
                )
                inserted += created
                row_id, created = await self._get_or_create(
                    connection,
                    select_sql="""
                        SELECT id FROM part_relations
                        WHERE from_part_number_id = ? AND to_part_number_raw = ?
                          AND relation_type = ? AND source_url = ?
                    """,
                    select_values=(
                        part_id,
                        relation.to_part_number_raw,
                        relation.relation_type,
                        source_url,
                    ),
                    insert_sql="""
                        INSERT INTO part_relations(
                            from_part_number_id, to_part_number_raw,
                            to_part_number_normalized, relation_type, relation_text,
                            confidence, source_url, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    insert_values=(
                        part_id,
                        relation.to_part_number_raw,
                        normalize_part_number(relation.to_part_number_raw),
                        relation.relation_type,
                        relation.relation_text,
                        relation.confidence,
                        source_url,
                        utc_now(),
                    ),
                )
                await self._source(connection, "part_relation", row_id, response_id, source_url)
                inserted += created

            await connection.execute(
                """
                UPDATE crawl_runs
                SET records_extracted = records_extracted + ?, updated_at = ?
                WHERE id = ?
                """,
                (inserted, utc_now(), run_id),
            )
        return inserted

    async def _vehicle(
        self,
        connection: aiosqlite.Connection,
        vehicle: VehicleRecord,
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        now = utc_now()
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM vehicle_configurations
                WHERE catalog_brand IS ? AND vehicle_external_id IS ?
                  AND model_raw IS ? AND prod_period_raw IS ? AND source_url = ?
            """,
            select_values=(
                vehicle.catalog_brand,
                vehicle.vehicle_external_id,
                vehicle.model_raw,
                vehicle.prod_period_raw,
                source_url,
            ),
            insert_sql="""
                INSERT INTO vehicle_configurations(
                    catalog_brand, brand_raw, brand_normalized, name_raw, model_raw,
                    description_raw, options_raw, prod_period_raw, production_from,
                    production_to, production_precision, catalog_code,
                    vehicle_external_id, metadata_json, source_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                vehicle.catalog_brand,
                vehicle.brand_raw,
                vehicle.brand_normalized,
                vehicle.name_raw,
                vehicle.model_raw,
                vehicle.description_raw,
                vehicle.options_raw,
                vehicle.prod_period_raw,
                vehicle.production_from,
                vehicle.production_to,
                vehicle.production_precision,
                vehicle.catalog_code,
                vehicle.vehicle_external_id,
                json.dumps(vehicle.metadata, sort_keys=True),
                source_url,
                now,
                now,
            ),
        )
        await self._source(connection, "vehicle_configuration", row_id, response_id, source_url)
        return row_id, created

    async def _taxonomy(
        self,
        connection: aiosqlite.Connection,
        *,
        vehicle_id: int,
        parent_id: int | None,
        depth: int,
        code: str | None,
        name: str,
        path: tuple[str, ...],
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        path_raw = " > ".join(path)
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM taxonomy_nodes
                WHERE vehicle_configuration_id = ? AND path_raw = ?
            """,
            select_values=(vehicle_id, path_raw),
            insert_sql="""
                INSERT INTO taxonomy_nodes(
                    vehicle_configuration_id, parent_id, depth, code_raw,
                    name_raw, path_raw, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(vehicle_id, parent_id, depth, code, name, path_raw, source_url),
        )
        await self._source(connection, "taxonomy_node", row_id, response_id, source_url)
        return row_id, created

    async def _diagram(
        self,
        connection: aiosqlite.Connection,
        *,
        vehicle_id: int,
        taxonomy_id: int | None,
        code: str | None,
        name: str | None,
        range_raw: str | None,
        range_from: str | None,
        range_to: str | None,
        metadata: dict[str, Any],
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM diagrams
                WHERE vehicle_configuration_id = ? AND diagram_code_raw IS ?
                  AND diagram_name_raw IS ? AND diagram_range_raw IS ? AND source_url = ?
            """,
            select_values=(vehicle_id, code, name, range_raw, source_url),
            insert_sql="""
                INSERT INTO diagrams(
                    vehicle_configuration_id, taxonomy_node_id, diagram_code_raw,
                    diagram_name_raw, diagram_range_raw, diagram_from, diagram_to,
                    metadata_json, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                vehicle_id,
                taxonomy_id,
                code,
                name,
                range_raw,
                range_from,
                range_to,
                json.dumps(metadata, sort_keys=True),
                source_url,
            ),
        )
        await self._source(connection, "diagram", row_id, response_id, source_url)
        return row_id, created

    async def _part_number(
        self,
        connection: aiosqlite.Connection,
        *,
        part: PartRecord,
        fallback_brand: str | None,
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        brand = part.part_brand_raw or fallback_brand
        assembly, reason = is_assembly_name(part.name_en_raw)
        now = utc_now()
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM part_numbers
                WHERE part_brand_raw IS ? AND number_raw = ?
                ORDER BY id LIMIT 1
            """,
            select_values=(brand, part.number_raw),
            insert_sql="""
                INSERT INTO part_numbers(
                    part_brand_raw, number_raw, number_normalized, name_en_raw,
                    is_assembly_inferred, assembly_inference_reason, source_url,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                brand,
                part.number_raw,
                normalize_part_number(part.number_raw),
                part.name_en_raw,
                assembly,
                reason,
                source_url,
                now,
                now,
            ),
        )
        await self._source(connection, "part_number", row_id, response_id, source_url)
        return row_id, created

    async def _occurrence(
        self,
        connection: aiosqlite.Connection,
        *,
        part_id: int,
        diagram_id: int,
        vehicle_id: int,
        part: PartRecord,
        source_url: str,
        response_id: int,
    ) -> tuple[int, int]:
        row_id, created = await self._get_or_create(
            connection,
            select_sql="""
                SELECT id FROM part_occurrences
                WHERE part_number_id = ? AND diagram_id = ? AND callout_raw IS ?
                  AND quantity_raw IS ? AND part_range_raw IS ?
                  AND part_condition_raw IS ? AND note_raw IS ? AND source_url = ?
            """,
            select_values=(
                part_id,
                diagram_id,
                part.callout_raw,
                part.quantity_raw,
                part.part_range_raw,
                part.condition_raw,
                part.note_raw,
                source_url,
            ),
            insert_sql="""
                INSERT INTO part_occurrences(
                    part_number_id, diagram_id, vehicle_configuration_id,
                    callout_raw, quantity_raw, part_range_raw, part_from, part_to,
                    part_condition_raw, note_raw, row_metadata_json, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                part_id,
                diagram_id,
                vehicle_id,
                part.callout_raw,
                part.quantity_raw,
                part.part_range_raw,
                part.part_from,
                part.part_to,
                part.condition_raw,
                part.note_raw,
                json.dumps(part.row_metadata, sort_keys=True),
                source_url,
            ),
        )
        await self._source(connection, "part_occurrence", row_id, response_id, source_url)
        return row_id, created

    async def _fitment(
        self,
        connection: aiosqlite.Connection,
        *,
        occurrence_id: int,
        part_id: int,
        vehicle_id: int,
        diagram_id: int,
        part: PartRecord,
        source_url: str,
        response_id: int,
        is_verified: bool,
        derivation: str,
    ) -> tuple[int, int]:
        row_id, created = await self._get_or_create(
            connection,
            select_sql="SELECT id FROM fitments WHERE part_occurrence_id = ? AND derivation = ?",
            select_values=(occurrence_id, derivation),
            insert_sql="""
                INSERT INTO fitments(
                    part_occurrence_id, part_number_id, vehicle_configuration_id,
                    diagram_id, is_verified, derivation, confidence,
                    effective_from, effective_to, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            insert_values=(
                occurrence_id,
                part_id,
                vehicle_id,
                diagram_id,
                is_verified,
                derivation,
                1.0 if is_verified else 0.7,
                part.part_from,
                part.part_to,
                source_url,
            ),
        )
        await self._source(connection, "fitment", row_id, response_id, source_url)
        return row_id, created

    @staticmethod
    def _choose_diagram(part: PartRecord, diagram_ids: dict[str, int]) -> int | None:
        if part.diagram_code_raw and part.diagram_code_raw in diagram_ids:
            return diagram_ids[part.diagram_code_raw]
        if part.diagram_name_raw:
            diagram_id = diagram_ids.get(f"name:{part.diagram_name_raw}")
            if diagram_id is not None:
                return diagram_id
        unique_ids = set(diagram_ids.values())
        if len(unique_ids) == 1:
            return next(iter(unique_ids))
        return None

    async def _source(
        self,
        connection: aiosqlite.Connection,
        record_type: str,
        record_id: int,
        response_id: int,
        source_url: str,
    ) -> None:
        await add_source(
            connection,
            record_type=record_type,
            record_id=record_id,
            response_id=response_id,
            parser_name=self.parser_name,
            parser_version=PARSER_VERSION,
            source_url=source_url,
        )

    @staticmethod
    async def _get_or_create(
        connection: aiosqlite.Connection,
        *,
        select_sql: str,
        select_values: tuple[object, ...],
        insert_sql: str,
        insert_values: tuple[object, ...],
    ) -> tuple[int, int]:
        cursor = await connection.execute(select_sql, select_values)
        row = await cursor.fetchone()
        if row is not None:
            return int(row["id"]), 0
        cursor = await connection.execute(insert_sql, insert_values)
        row_id = int(cursor.lastrowid or 0)
        if not row_id:
            raise RuntimeError("insert did not return an id")
        return row_id, 1
