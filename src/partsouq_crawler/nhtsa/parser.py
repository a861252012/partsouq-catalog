from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from partsouq_crawler.nhtsa.datasets import BulkSource, DatasetSpec
from partsouq_crawler.nhtsa.models import ArtifactMember, ParsedRecord, RejectedRow

HEADER_SEPARATOR = re.compile(r"[^A-Za-z0-9]+")
CP1252_BYTE_PRESERVING_ERRORS = "nhtsa_cp1252_byte_preserving"


def _preserve_undefined_cp1252(error: UnicodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeDecodeError):
        raise error
    preserved = "".join(chr(value) for value in error.object[error.start : error.end])
    return preserved, error.end


codecs.register_error(CP1252_BYTE_PRESERVING_ERRORS, _preserve_undefined_cp1252)


class NhtsaFormatError(ValueError):
    pass


def normalize_header(value: str) -> str:
    return HEADER_SEPARATOR.sub("_", value.strip()).strip("_").upper()


class BulkArtifactParser:
    def inspect(
        self,
        path: Path,
        source: BulkSource,
        spec: DatasetSpec,
    ) -> ArtifactMember:
        if source.is_zip:
            try:
                with zipfile.ZipFile(path) as archive:
                    bad_member = archive.testzip()
                    if bad_member is not None:
                        raise NhtsaFormatError(f"ZIP CRC failed for {bad_member}")
                    names = [info.filename for info in archive.infolist() if not info.is_dir()]
                    if names != [source.expected_member]:
                        raise NhtsaFormatError(
                            f"expected ZIP member {source.expected_member!r}, found {names!r}"
                        )
                    info = archive.getinfo(source.expected_member)
                    with archive.open(info) as binary:
                        field_names = self._read_field_names(binary, spec)
                    return self._member(
                        info.filename,
                        info.file_size,
                        info.compress_size,
                        info.CRC,
                        field_names,
                    )
            except zipfile.BadZipFile as error:
                raise NhtsaFormatError("invalid ZIP archive") from error

        if path.stat().st_size == 0:
            raise NhtsaFormatError("downloaded file is empty")
        with path.open("rb") as binary:
            field_names = self._read_field_names(binary, spec)
        size = path.stat().st_size
        return self._member(source.expected_member, size, size, None, field_names)

    def iter_records(
        self,
        path: Path,
        source: BulkSource,
        spec: DatasetSpec,
        member: ArtifactMember,
    ) -> Iterator[ParsedRecord | RejectedRow]:
        if source.is_zip:
            with zipfile.ZipFile(path) as archive, archive.open(source.expected_member) as binary:
                yield from self._iter_binary(binary, spec, member)
            return
        with path.open("rb") as binary:
            yield from self._iter_binary(binary, spec, member)

    def _read_field_names(self, binary: Any, spec: DatasetSpec) -> tuple[str, ...]:
        if not spec.has_header:
            return spec.field_names
        text = self._text_wrapper(binary, spec)
        reader = csv.reader(
            text,
            delimiter=spec.delimiter,
            quoting=csv.QUOTE_MINIMAL if spec.has_header else csv.QUOTE_NONE,
        )
        try:
            header = next(reader)
        except StopIteration as error:
            raise NhtsaFormatError("file has no header row") from error
        fields = tuple(normalize_header(value) for value in header)
        if any(not field for field in fields) or len(set(fields)) != len(fields):
            raise NhtsaFormatError("header contains empty or duplicate normalized field names")
        missing = sorted(set(spec.required_fields) - set(fields))
        if missing:
            raise NhtsaFormatError(f"required fields missing: {missing}")
        return fields

    def _member(
        self,
        name: str,
        uncompressed_bytes: int,
        compressed_bytes: int,
        crc32: int | None,
        field_names: tuple[str, ...],
    ) -> ArtifactMember:
        schema = json.dumps(field_names, ensure_ascii=True, separators=(",", ":"))
        return ArtifactMember(
            name=name,
            uncompressed_bytes=uncompressed_bytes,
            compressed_bytes=compressed_bytes,
            crc32=crc32,
            field_names=field_names,
            schema_sha256=hashlib.sha256(schema.encode()).hexdigest(),
        )

    def _iter_binary(
        self,
        binary: Any,
        spec: DatasetSpec,
        member: ArtifactMember,
    ) -> Iterator[ParsedRecord | RejectedRow]:
        text = self._text_wrapper(binary, spec)
        reader = csv.reader(
            text,
            delimiter=spec.delimiter,
            quoting=csv.QUOTE_MINIMAL if spec.has_header else csv.QUOTE_NONE,
        )
        if spec.has_header:
            next(reader)
        for row in reader:
            line_number = reader.line_num
            if not row or not any(value.strip() for value in row):
                continue
            raw_text = spec.delimiter.join(row)
            if len(row) != len(member.field_names):
                yield self._rejected(
                    member.name,
                    line_number,
                    raw_text,
                    "FieldCountError",
                    f"expected {len(member.field_names)} fields, found {len(row)}",
                )
                continue
            payload = dict(zip(member.field_names, row, strict=True))
            try:
                yield self._record(spec, payload, member.name, line_number)
            except (TypeError, ValueError) as error:
                yield self._rejected(
                    member.name,
                    line_number,
                    raw_text,
                    type(error).__name__,
                    str(error),
                )

    def _text_wrapper(self, binary: Any, spec: DatasetSpec) -> io.TextIOWrapper:
        errors = (
            CP1252_BYTE_PRESERVING_ERRORS
            if spec.encoding.lower().replace("-", "") == "cp1252"
            else "strict"
        )
        return io.TextIOWrapper(binary, encoding=spec.encoding, errors=errors, newline="")

    def _record(
        self,
        spec: DatasetSpec,
        payload: dict[str, str],
        member_name: str,
        line_number: int,
    ) -> ParsedRecord:
        identity = [payload[field].strip() for field in spec.identity_fields]
        if not identity or not any(identity):
            raise ValueError("natural key fields are all empty")
        natural_key = "\x1f".join(identity)
        natural_hash = hashlib.sha256(natural_key.encode()).hexdigest()
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        record_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        return ParsedRecord(
            dataset_name=spec.name,
            natural_key_sha256=natural_hash,
            record_sha256=record_hash,
            natural_key_text=natural_key,
            external_id=self._optional(payload, spec.external_id_field),
            make_name=self._optional(payload, spec.make_field),
            model_name=self._optional(payload, spec.model_field),
            model_year=self._model_year(payload, spec.model_year_field),
            campaign_number=self._optional(payload, spec.campaign_field),
            component_name=self._optional(payload, spec.component_field),
            summary_text=self._optional(payload, spec.summary_field),
            payload_json=payload_json,
            member_name=member_name,
            source_line=line_number,
        )

    def _optional(self, payload: dict[str, str], field: str | None) -> str | None:
        if field is None:
            return None
        value = payload[field].strip()
        return value or None

    def _model_year(self, payload: dict[str, str], field: str | None) -> int | None:
        value = self._optional(payload, field)
        if value is None or value == "9999":
            return None
        if not value.isdigit():
            return None
        year = int(value)
        return year if 1 <= year <= 9998 else None

    def _rejected(
        self,
        member_name: str,
        source_line: int,
        raw_text: str,
        error_type: str,
        error_message: str,
    ) -> RejectedRow:
        return RejectedRow(
            member_name=member_name,
            source_line=source_line,
            raw_sha256=hashlib.sha256(raw_text.encode(errors="replace")).hexdigest(),
            error_type=error_type,
            error_message=error_message,
            raw_text=raw_text,
        )
