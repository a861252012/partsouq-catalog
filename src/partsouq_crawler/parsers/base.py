from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

from lxml import html
from lxml.html import HtmlElement

from partsouq_crawler.crawl.classifier import classify_page
from partsouq_crawler.crawl.discovery import discover_links
from partsouq_crawler.models.records import (
    CompatibilityHintRecord,
    DiagramRecord,
    ParsedPage,
    PartRecord,
    PartRelationRecord,
    TaxonomyRecord,
)
from partsouq_crawler.parsers.brands.audi import AudiBrandAdapter
from partsouq_crawler.parsers.brands.base import BaseBrandAdapter
from partsouq_crawler.parsers.brands.generic import GenericBrandAdapter
from partsouq_crawler.parsers.brands.renault import RenaultBrandAdapter
from partsouq_crawler.parsers.brands.toyota import ToyotaBrandAdapter
from partsouq_crawler.parsers.common import clean_text, parse_unambiguous_range

PARSER_VERSION = "2"
RecordT = TypeVar("RecordT")


class ParseError(ValueError):
    pass


class CatalogParser:
    def parse(self, url: str, body: bytes, charset: str = "utf-8") -> ParsedPage:
        page_type = classify_page(url, body, charset)
        try:
            text = body.decode(charset, errors="replace")
            document = html.fromstring(text, base_url=url)
        except (LookupError, ValueError) as error:
            raise ParseError(f"invalid HTML: {error}") from error

        metadata = self._metadata(document)
        parsed = ParsedPage(
            page_type=page_type,
            links=discover_links(body, url, charset),
            metadata=metadata,
            terminal_expected=page_type == "parts",
        )
        adapter = self._adapter(metadata)
        parsed.vehicle = adapter.adapt(metadata)
        parsed.taxonomies = self._taxonomies(document)
        parsed.diagrams = self._diagrams(document, metadata)
        parsed.parts = self._parts(document, metadata)
        if page_type == "search":
            parsed.compatibility_hints = self._compatibility_hints(document, url)
            parsed.part_relations = self._relations(document)
        if parsed.terminal_expected and not parsed.parts:
            raise ParseError("terminal parts page contains no parseable part rows")
        return parsed

    @staticmethod
    def _metadata(document: HtmlElement) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for term in document.xpath("//dl/dt"):
            key = clean_text(term.text_content())
            siblings = term.xpath("following-sibling::dd[1]")
            value = clean_text(siblings[0].text_content()) if siblings else None
            if key and value:
                metadata[key.rstrip(":")] = value
        for row in document.xpath("//table//tr[count(th | td) = 2]"):
            cells = row.xpath("./th | ./td")
            key = clean_text(cells[0].text_content())
            value = clean_text(cells[1].text_content())
            if key and value and len(key) <= 80:
                metadata.setdefault(key.rstrip(":"), value)
        for element in document.xpath("//*[@data-label and @data-value]"):
            key = clean_text(element.get("data-label"))
            value = clean_text(element.get("data-value"))
            if key and value:
                metadata.setdefault(key.rstrip(":"), value)
        for table in document.xpath("//table"):
            header_rows = table.xpath(".//tr[th]")
            data_rows = table.xpath(".//tr[td]")
            if not header_rows or not data_rows:
                continue
            headers = [clean_text(cell.text_content()) for cell in header_rows[0].xpath("./th")]
            values = [clean_text(cell.text_content()) for cell in data_rows[0].xpath("./td")]
            normalized_headers = {header.lower() for header in headers if header}
            if not normalized_headers.intersection({"brand", "make"}):
                continue
            for key, value in zip(headers, values, strict=False):
                if key and value:
                    metadata.setdefault(key.rstrip(":"), value)
        breadcrumb = CatalogParser._breadcrumb(document)
        if len(breadcrumb) >= 3 and breadcrumb[0].lower().startswith("genuine parts catalog"):
            metadata.setdefault("Brand", breadcrumb[1])
            metadata.setdefault("Name", breadcrumb[2])
        return metadata

    @staticmethod
    def _adapter(metadata: dict[str, str]) -> BaseBrandAdapter:
        lowered = {key.lower(): value.lower() for key, value in metadata.items()}
        brand = lowered.get("brand") or lowered.get("make") or ""
        if "toyota" in brand:
            return ToyotaBrandAdapter()
        if "audi" in brand:
            return AudiBrandAdapter()
        if "renault" in brand:
            return RenaultBrandAdapter()
        return GenericBrandAdapter()

    @staticmethod
    def _taxonomies(document: HtmlElement) -> list[TaxonomyRecord]:
        path = CatalogParser._breadcrumb(document)
        return [TaxonomyRecord(tuple(path))] if path else []

    @staticmethod
    def _breadcrumb(document: HtmlElement) -> list[str]:
        candidates = document.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' breadcrumb ')]"
            "//*[self::a or self::li or self::span][normalize-space()]"
        )
        path: list[str] = []
        for element in candidates:
            value = clean_text(element.text_content())
            if value and (not path or path[-1] != value):
                path.append(value)
        return path

    def _diagrams(self, document: HtmlElement, metadata: dict[str, str]) -> list[DiagramRecord]:
        records: list[DiagramRecord] = []
        for table in document.xpath("//table"):
            headers = self._table_headers(table)
            code_index = self._header_index(headers, ("diagram code", "diagram", "unit code"))
            name_index = self._header_index(headers, ("diagram name", "unit name"))
            range_index = self._header_index(headers, ("diagram range", "range"))
            if code_index is None or name_index is None:
                continue
            for cells in self._data_rows(table):
                code = self._cell(cells, code_index)
                name = self._cell(cells, name_index)
                range_raw = self._cell(cells, range_index)
                if code or name:
                    parsed_range = parse_unambiguous_range(range_raw)
                    records.append(
                        DiagramRecord(
                            code,
                            name,
                            range_raw,
                            parsed_range.start,
                            parsed_range.end,
                        )
                    )
        if not records:
            lowered = {key.lower(): value for key, value in metadata.items()}
            code = lowered.get("diagram code") or lowered.get("diagram")
            name = lowered.get("diagram name")
            range_raw = lowered.get("diagram range")
            if code or name:
                parsed_range = parse_unambiguous_range(range_raw)
                records.append(
                    DiagramRecord(code, name, range_raw, parsed_range.start, parsed_range.end)
                )
        if not records:
            for heading in document.xpath(
                "//div[contains(concat(' ', normalize-space(@class), ' '), ' unit-header ')]"
                "//h2[normalize-space()]"
            ):
                name = clean_text(heading.text_content())
                if name:
                    records.append(DiagramRecord(None, name))
        return self._unique(records)

    def _parts(self, document: HtmlElement, metadata: dict[str, str]) -> list[PartRecord]:
        records: list[PartRecord] = []
        for table in document.xpath("//table"):
            headers = self._table_headers(table)
            number_index = self._header_index(
                headers, ("part number", "number", "part no", "part #")
            )
            if number_index is None:
                continue
            indexes = {
                "name": self._header_index(headers, ("part name", "description", "name")),
                "diagram": self._header_index(headers, ("diagram code", "diagram")),
                "callout": self._header_index(headers, ("callout", "ref", "position", "code")),
                "quantity": self._header_index(headers, ("quantity", "qty", "qty required")),
                "range": self._header_index(headers, ("part range", "range", "date_range")),
                "condition": self._header_index(headers, ("condition", "applicability")),
                "note": self._header_index(headers, ("note", "remarks")),
            }
            diagram_name = self._containing_diagram_name(table)
            for cells in self._data_rows(table):
                number = self._cell(cells, number_index)
                if not number:
                    continue
                range_raw = self._cell(cells, indexes["range"])
                parsed_range = parse_unambiguous_range(range_raw)
                records.append(
                    PartRecord(
                        number_raw=number,
                        name_en_raw=self._cell(cells, indexes["name"]),
                        diagram_code_raw=self._cell(cells, indexes["diagram"])
                        or metadata.get("Diagram Code")
                        or metadata.get("Diagram"),
                        diagram_name_raw=diagram_name,
                        callout_raw=self._cell(cells, indexes["callout"]),
                        quantity_raw=self._cell(cells, indexes["quantity"]),
                        part_range_raw=range_raw,
                        part_from=parsed_range.start,
                        part_to=parsed_range.end,
                        condition_raw=self._cell(cells, indexes["condition"]),
                        note_raw=self._cell(cells, indexes["note"]),
                        row_metadata={
                            header: self._cell(cells, index) for index, header in enumerate(headers)
                        },
                    )
                )
        return self._unique(records)

    @staticmethod
    def _containing_diagram_name(table: HtmlElement) -> str | None:
        panels = table.xpath(
            "ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' panel ')][1]"
        )
        if not panels:
            return None
        headings = panels[0].xpath(
            ".//div[contains(concat(' ', normalize-space(@class), ' '), ' unit-header ')]"
            "//h2[normalize-space()]"
        )
        return clean_text(headings[0].text_content()) if headings else None

    def _compatibility_hints(
        self, document: HtmlElement, url: str
    ) -> list[CompatibilityHintRecord]:
        query_number = parse_qs(urlsplit(url).query).get("q", [""])[0]
        records: list[CompatibilityHintRecord] = []
        for element in document.xpath("//*[@data-compatibility]"):
            text = clean_text(element.get("data-compatibility"))
            number = clean_text(element.get("data-part-number")) or query_number
            if text and number:
                records.append(
                    CompatibilityHintRecord(
                        part_number_raw=number,
                        brand_text=clean_text(element.get("data-brand")),
                        model_text=clean_text(element.get("data-model")),
                        compatibility_text=text,
                    )
                )
        return self._unique(records)

    def _relations(self, document: HtmlElement) -> list[PartRelationRecord]:
        records: list[PartRelationRecord] = []
        for element in document.xpath(
            "//*[@data-relation-type and @data-from-part and @data-to-part]"
        ):
            relation_type = clean_text(element.get("data-relation-type"))
            from_number = clean_text(element.get("data-from-part"))
            to_number = clean_text(element.get("data-to-part"))
            if relation_type and from_number and to_number:
                records.append(
                    PartRelationRecord(
                        from_part_number_raw=from_number,
                        to_part_number_raw=to_number,
                        relation_type=relation_type,
                        relation_text=clean_text(element.text_content()),
                    )
                )
        return self._unique(records)

    @staticmethod
    def _table_headers(table: HtmlElement) -> list[str]:
        rows = table.xpath(".//tr[th]")
        if not rows:
            return []
        return [
            (clean_text(cell.text_content()) or "").lower() for cell in rows[0].xpath("./th | ./td")
        ]

    @staticmethod
    def _data_rows(table: HtmlElement) -> Iterable[list[HtmlElement]]:
        for row in table.xpath(".//tr[td]"):
            cells = row.xpath("./td")
            if cells:
                yield cells

    @staticmethod
    def _header_index(headers: list[str], aliases: tuple[str, ...]) -> int | None:
        for index, header in enumerate(headers):
            if header in aliases:
                return index
        return None

    @staticmethod
    def _cell(cells: list[HtmlElement], index: int | None) -> str | None:
        if index is None or index >= len(cells):
            return None
        return clean_text(cells[index].text_content())

    @staticmethod
    def _unique(records: Sequence[RecordT]) -> list[RecordT]:
        output: list[RecordT] = []
        seen: set[str] = set()
        for record in records:
            key = repr(record)
            if key not in seen:
                output.append(record)
                seen.add(key)
        return output
