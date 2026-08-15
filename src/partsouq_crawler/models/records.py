from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class VehicleRecord:
    catalog_brand: str | None = None
    brand_raw: str | None = None
    brand_normalized: str | None = None
    name_raw: str | None = None
    model_raw: str | None = None
    description_raw: str | None = None
    options_raw: str | None = None
    prod_period_raw: str | None = None
    production_from: str | None = None
    production_to: str | None = None
    production_precision: str | None = None
    catalog_code: str | None = None
    vehicle_external_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaxonomyRecord:
    path: tuple[str, ...]
    codes: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagramRecord:
    code_raw: str | None
    name_raw: str | None
    range_raw: str | None = None
    diagram_from: str | None = None
    diagram_to: str | None = None
    category_path: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PartRecord:
    number_raw: str
    name_en_raw: str | None = None
    part_brand_raw: str | None = None
    diagram_code_raw: str | None = None
    diagram_name_raw: str | None = None
    callout_raw: str | None = None
    quantity_raw: str | None = None
    part_range_raw: str | None = None
    part_from: str | None = None
    part_to: str | None = None
    condition_raw: str | None = None
    note_raw: str | None = None
    row_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompatibilityHintRecord:
    part_number_raw: str
    brand_text: str | None
    model_text: str | None
    compatibility_text: str


@dataclass(frozen=True, slots=True)
class PartRelationRecord:
    from_part_number_raw: str
    to_part_number_raw: str
    relation_type: str
    relation_text: str | None = None
    confidence: float = 0.6


@dataclass(slots=True)
class ParsedPage:
    page_type: str
    links: set[str] = field(default_factory=set)
    metadata: dict[str, str] = field(default_factory=dict)
    vehicle: VehicleRecord | None = None
    taxonomies: list[TaxonomyRecord] = field(default_factory=list)
    diagrams: list[DiagramRecord] = field(default_factory=list)
    parts: list[PartRecord] = field(default_factory=list)
    compatibility_hints: list[CompatibilityHintRecord] = field(default_factory=list)
    part_relations: list[PartRelationRecord] = field(default_factory=list)
    terminal_expected: bool = False
