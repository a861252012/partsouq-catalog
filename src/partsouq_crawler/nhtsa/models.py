from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class NhtsaRunLease:
    id: int
    token: str
    scheduled_job_run_id: int


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


def verified_stored_artifact_path(
    artifact: Mapping[str, object] | None,
    *,
    parser_name: str,
    parser_version: str,
) -> Path | None:
    metadata = _stored_artifact_metadata(
        artifact,
        parser_name=parser_name,
        parser_version=parser_version,
    )
    if metadata is None:
        return None
    path, expected_size, expected_sha256 = metadata
    try:
        with path.open("rb") as raw_file:
            file_stat = os.fstat(raw_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != expected_size:
                return None
            actual_sha256 = hashlib.file_digest(raw_file, "sha256").hexdigest()
    except OSError:
        return None
    return path if actual_sha256 == expected_sha256 else None


def read_verified_stored_artifact(
    artifact: Mapping[str, object] | None,
    *,
    parser_name: str,
    parser_version: str,
) -> bytes | None:
    metadata = _stored_artifact_metadata(
        artifact,
        parser_name=parser_name,
        parser_version=parser_version,
    )
    if metadata is None:
        return None
    path, expected_size, expected_sha256 = metadata
    try:
        with path.open("rb") as raw_file:
            file_stat = os.fstat(raw_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != expected_size:
                return None
            body = raw_file.read()
    except OSError:
        return None
    if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
        return None
    return body


def _stored_artifact_metadata(
    artifact: Mapping[str, object] | None,
    *,
    parser_name: str,
    parser_version: str,
) -> tuple[Path, int, str] | None:
    if artifact is None:
        return None
    try:
        if (
            artifact.get("status") != "imported"
            or artifact.get("verified_at") is None
            or int(str(artifact.get("rejected_rows"))) != 0
            or artifact.get("parser_name") != parser_name
            or artifact.get("parser_version") != parser_version
        ):
            return None
        stored_path = artifact.get("stored_path")
        if not isinstance(stored_path, (str, os.PathLike)) or not str(stored_path):
            return None
        byte_count = int(str(artifact.get("byte_count")))
        sha256 = str(artifact.get("sha256") or "")
    except (TypeError, ValueError):
        return None
    if (
        byte_count <= 0
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        return None
    return Path(stored_path), byte_count, sha256
