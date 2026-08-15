from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DownloadedArtifact:
    http_status: int
    response_headers: dict[str, str]
    path: Path | None
    sha256: str | None
    byte_count: int
    reused_artifact_id: int | None = None


@dataclass(frozen=True, slots=True)
class ArtifactMember:
    name: str
    uncompressed_bytes: int
    compressed_bytes: int
    crc32: int | None
    field_names: tuple[str, ...]
    schema_sha256: str


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    dataset_name: str
    natural_key_sha256: str
    record_sha256: str
    natural_key_text: str
    external_id: str | None
    make_name: str | None
    model_name: str | None
    model_year: int | None
    campaign_number: str | None
    component_name: str | None
    summary_text: str | None
    payload_json: str
    member_name: str
    source_line: int


@dataclass(frozen=True, slots=True)
class RejectedRow:
    member_name: str
    source_line: int
    raw_sha256: str
    error_type: str
    error_message: str
    raw_text: str


@dataclass(frozen=True, slots=True)
class ApiDocument:
    member: ArtifactMember
    records: tuple[ParsedRecord, ...]
    rejections: tuple[RejectedRow, ...]
    count: int
    message: str
