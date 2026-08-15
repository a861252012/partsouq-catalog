from __future__ import annotations

import json
import zipfile
from collections import Counter
from pathlib import Path

from partsouq_crawler.nhtsa.datasets import (
    BULK_SOURCES,
    CSSI_SOURCES,
    DATASET_SPECS,
    RECALL_FIELDS,
    BulkSource,
)
from partsouq_crawler.nhtsa.models import ParsedRecord, RejectedRow
from partsouq_crawler.nhtsa.parser import BulkArtifactParser, normalize_header


def _recall_row(record_id: str = "81717") -> list[str]:
    values = {field: "" for field in RECALL_FIELDS}
    values.update(
        {
            "RECORD_ID": record_id,
            "CAMPNO": "10V369000",
            "MAKETXT": "HONDA",
            "MODELTXT": "VT1300CT",
            "YEARTXT": "2010",
            "COMPNAME": "EQUIPMENT:OTHER:LABELS",
            "POTAFF": "3184",
            "DESC_DEFECT": "CERTIFICATION LABEL IS INCORRECT.",
            "MFR_COMP_PTNO": "R41-LABEL",
            "DO_NOT_DRIVE": "No",
            "PARK_OUTSIDE": "No",
        }
    )
    return [values[field] for field in RECALL_FIELDS]


def _write_zip(path: Path, member: str, rows: list[list[str]]) -> None:
    text = "".join("\t".join(row) + "\n" for row in rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, text.encode("cp1252"))


def test_recall_zip_inspection_and_record_provenance(tmp_path: Path) -> None:
    source = BulkSource(
        key="test_recalls",
        dataset_name="recalls",
        url="https://example.test/recalls.zip",
        expected_member="recalls.txt",
    )
    path = tmp_path / "recalls.zip"
    _write_zip(path, source.expected_member, [_recall_row()])

    parser = BulkArtifactParser()
    spec = DATASET_SPECS["recalls"]
    member = parser.inspect(path, source, spec)
    items = list(parser.iter_records(path, source, spec, member))

    assert member.field_names == RECALL_FIELDS
    assert len(member.schema_sha256) == 64
    assert len(items) == 1
    record = items[0]
    assert isinstance(record, ParsedRecord)
    assert record.external_id == "81717"
    assert record.make_name == "HONDA"
    assert record.model_year == 2010
    assert record.source_line == 1
    assert json.loads(record.payload_json)["MFR_COMP_PTNO"] == "R41-LABEL"


def test_recall_wrong_field_count_is_rejected(tmp_path: Path) -> None:
    source = BulkSource(
        key="bad_recalls",
        dataset_name="recalls",
        url="https://example.test/bad.zip",
        expected_member="bad.txt",
    )
    path = tmp_path / "bad.zip"
    _write_zip(path, source.expected_member, [_recall_row()[:-1]])

    parser = BulkArtifactParser()
    spec = DATASET_SPECS["recalls"]
    member = parser.inspect(path, source, spec)
    items = list(parser.iter_records(path, source, spec, member))

    assert len(items) == 1
    rejected = items[0]
    assert isinstance(rejected, RejectedRow)
    assert rejected.error_type == "FieldCountError"
    assert "expected 29 fields" in rejected.error_message


def test_official_blank_lines_are_not_treated_as_source_records(tmp_path: Path) -> None:
    source = BulkSource(
        key="recalls_with_blank_lines",
        dataset_name="recalls",
        url="https://example.test/recalls.zip",
        expected_member="recalls.txt",
    )
    path = tmp_path / "recalls.zip"
    first = "\t".join(_recall_row("1"))
    second = "\t".join(_recall_row("2"))
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(source.expected_member, f"{first}\n\n\t\t\n{second}\n".encode("cp1252"))

    parser = BulkArtifactParser()
    spec = DATASET_SPECS["recalls"]
    member = parser.inspect(path, source, spec)
    items = list(parser.iter_records(path, source, spec, member))

    assert [item.external_id for item in items if isinstance(item, ParsedRecord)] == ["1", "2"]
    assert [item.source_line for item in items if isinstance(item, ParsedRecord)] == [1, 4]


def test_undefined_cp1252_byte_is_preserved_in_normalized_payload(tmp_path: Path) -> None:
    source = BulkSource(
        key="recalls_with_undefined_cp1252",
        dataset_name="recalls",
        url="https://example.test/recalls.zip",
        expected_member="recalls.txt",
    )
    path = tmp_path / "recalls.zip"
    row = _recall_row()
    row[RECALL_FIELDS.index("DESC_DEFECT")] = "BEFORE UNDEFINED AFTER"
    body = "\t".join(row).encode("cp1252").replace(b"UNDEFINED", b"\x81") + b"\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(source.expected_member, body)

    parser = BulkArtifactParser()
    spec = DATASET_SPECS["recalls"]
    member = parser.inspect(path, source, spec)
    items = list(parser.iter_records(path, source, spec, member))

    assert len(items) == 1
    record = items[0]
    assert isinstance(record, ParsedRecord)
    assert json.loads(record.payload_json)["DESC_DEFECT"] == "BEFORE \x81 AFTER"


def test_flat_tsv_does_not_merge_rows_on_unbalanced_quote(tmp_path: Path) -> None:
    source = BulkSource(
        key="recalls_with_unbalanced_quote",
        dataset_name="recalls",
        url="https://example.test/recalls.zip",
        expected_member="recalls.txt",
    )
    path = tmp_path / "recalls.zip"
    first = _recall_row("1")
    first[RECALL_FIELDS.index("DESC_DEFECT")] = '"BROKEN QUOTE'
    second = _recall_row("2")
    _write_zip(path, source.expected_member, [first, second])

    parser = BulkArtifactParser()
    spec = DATASET_SPECS["recalls"]
    member = parser.inspect(path, source, spec)
    items = list(parser.iter_records(path, source, spec, member))

    assert [item.external_id for item in items if isinstance(item, ParsedRecord)] == ["1", "2"]
    assert [item.source_line for item in items if isinstance(item, ParsedRecord)] == [1, 2]


def test_header_normalization_matches_nhtsa_communication_fields() -> None:
    assert normalize_header("TSB/Document ID") == "TSB_DOCUMENT_ID"
    assert normalize_header("Concise Summary") == "CONCISE_SUMMARY"


def test_official_source_manifest_has_expected_non_overlapping_coverage() -> None:
    assert Counter(source.dataset_name for source in BULK_SOURCES) == {
        "safety_ratings": 1,
        "recalls": 2,
        "investigations": 1,
        "complaints": 7,
        "manufacturer_communications_summary": 7,
        "manufacturer_communications": 7,
    }
    assert len({source.key for source in BULK_SOURCES}) == len(BULK_SOURCES)
    assert len({source.url for source in BULK_SOURCES}) == len(BULK_SOURCES)
    assert len(CSSI_SOURCES) == 56
    assert len({source.key for source in CSSI_SOURCES}) == len(CSSI_SOURCES)
