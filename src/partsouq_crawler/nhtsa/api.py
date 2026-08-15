from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

from partsouq_crawler.nhtsa.datasets import DATASET_SPECS, ApiSource, DatasetSpec
from partsouq_crawler.nhtsa.models import (
    ApiDocument,
    ArtifactMember,
    ParsedRecord,
    RejectedRow,
)

VPIC_PATHS = (
    re.compile(r"/api/vehicles/GetAllMakes"),
    re.compile(r"/api/vehicles/GetModelsForMakeId/0"),
    re.compile(r"/api/vehicles/GetAllManufacturers"),
    re.compile(r"/api/vehicles/GetVehicleVariableList"),
    re.compile(r"/api/vehicles/GetVehicleVariableValuesList/[0-9]+"),
)
VPIC_VIN_PATH = re.compile(r"/api/vehicles/DecodeVinValues/([A-HJ-NPR-Z0-9]{17})")
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
CSSI_PATH = re.compile(r"/CSSIStation/state/[A-Z]{2}")


class NhtsaApiPolicyError(ValueError):
    pass


def normalize_vin(value: str) -> str:
    vin = value.strip().upper()
    if not VIN_RE.fullmatch(vin):
        raise ValueError("VIN 必須是 17 碼，且不可包含 I、O、Q")
    return vin


class NhtsaApiPolicy:
    def validate(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            raise NhtsaApiPolicyError("NHTSA API URL must use plain HTTPS on the default port")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("format") != ["json"]:
            raise NhtsaApiPolicyError("NHTSA API requests must explicitly use format=json")
        if parsed.hostname == "vpic.nhtsa.dot.gov":
            vin_match = VPIC_VIN_PATH.fullmatch(parsed.path)
            is_collection = any(pattern.fullmatch(parsed.path) for pattern in VPIC_PATHS)
            if not is_collection and not vin_match:
                raise NhtsaApiPolicyError(f"vPIC endpoint is not allowlisted: {parsed.path}")
            allowed_query = {"format", "page"} if is_collection else {"format"}
            if set(query) - allowed_query:
                raise NhtsaApiPolicyError("vPIC request has non-allowlisted query parameters")
            if "page" in query and (len(query["page"]) != 1 or not query["page"][0].isdigit()):
                raise NhtsaApiPolicyError("vPIC page must be one non-negative integer")
            if "page" in query and parsed.path != "/api/vehicles/GetAllManufacturers":
                raise NhtsaApiPolicyError("vPIC page is only allowed for GetAllManufacturers")
            if vin_match:
                try:
                    normalize_vin(vin_match.group(1))
                except ValueError as error:
                    raise NhtsaApiPolicyError(str(error)) from error
            return
        if parsed.hostname == "api.nhtsa.gov":
            if not CSSI_PATH.fullmatch(parsed.path) or set(query) != {"format"}:
                raise NhtsaApiPolicyError("only state-scoped CSSIStation requests are allowed")
            return
        raise NhtsaApiPolicyError(f"NHTSA API host is not allowlisted: {parsed.hostname}")


class NhtsaApiParser:
    def parse(self, body: bytes, source: ApiSource) -> ApiDocument:
        try:
            document = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("NHTSA API response is not valid UTF-8 JSON") from error
        if not isinstance(document, dict):
            raise ValueError("NHTSA API response root must be an object")
        results = document.get("Results")
        if not isinstance(results, list):
            raise ValueError("NHTSA API response Results must be an array")
        count = document.get("Count")
        if not isinstance(count, int) or count != len(results):
            raise ValueError(
                f"NHTSA API Count mismatch: declared {count!r}, received {len(results)}"
            )
        message = str(document.get("Message") or "")
        spec = DATASET_SPECS[source.dataset_name]
        records: list[ParsedRecord] = []
        rejections: list[RejectedRow] = []
        field_names: set[str] = set()
        for index, raw in enumerate(results, start=1):
            if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
                text = json.dumps(raw, ensure_ascii=False, default=str)
                rejections.append(
                    self._rejected(index, text, "ResultTypeError", "API result must be an object")
                )
                continue
            payload = dict(raw)
            payload.update(dict(source.context))
            field_names.update(payload)
            missing = sorted(set(spec.required_fields) - set(payload))
            if missing:
                text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                rejections.append(
                    self._rejected(
                        index,
                        text,
                        "MissingFieldError",
                        f"required fields missing: {missing}",
                    )
                )
                continue
            try:
                records.append(self._record(spec, payload, source.key, index))
            except (TypeError, ValueError) as error:
                text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
                rejections.append(self._rejected(index, text, type(error).__name__, str(error)))
        fields = tuple(sorted(field_names))
        schema_json = json.dumps(fields, ensure_ascii=True, separators=(",", ":"))
        member = ArtifactMember(
            name="response.json",
            uncompressed_bytes=len(body),
            compressed_bytes=len(body),
            crc32=None,
            field_names=fields,
            schema_sha256=hashlib.sha256(schema_json.encode()).hexdigest(),
        )
        return ApiDocument(member, tuple(records), tuple(rejections), count, message)

    def _record(
        self,
        spec: DatasetSpec,
        payload: Mapping[str, object],
        source_key: str,
        source_line: int,
    ) -> ParsedRecord:
        identity = [self._text(payload.get(field)) for field in spec.identity_fields]
        if not identity or not any(identity):
            raise ValueError("natural key fields are all empty")
        natural_key = "\x1f".join(identity)
        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ParsedRecord(
            dataset_name=spec.name,
            natural_key_sha256=hashlib.sha256(natural_key.encode()).hexdigest(),
            record_sha256=hashlib.sha256(payload_json.encode()).hexdigest(),
            natural_key_text=natural_key,
            external_id=self._optional(payload, spec.external_id_field),
            make_name=self._optional(payload, spec.make_field),
            model_name=self._optional(payload, spec.model_field),
            model_year=self._model_year(payload, spec.model_year_field),
            campaign_number=self._optional(payload, spec.campaign_field),
            component_name=self._optional(payload, spec.component_field),
            summary_text=self._optional(payload, spec.summary_field),
            payload_json=payload_json,
            member_name="response.json",
            source_line=source_line,
        )

    def _text(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return str(value).strip()

    def _optional(self, payload: Mapping[str, object], field: str | None) -> str | None:
        if field is None:
            return None
        value = self._text(payload.get(field))
        return value or None

    def _model_year(self, payload: Mapping[str, object], field: str | None) -> int | None:
        value = self._optional(payload, field)
        if value is None or value == "9999" or not value.isdigit():
            return None
        year = int(value)
        return year if 1 <= year <= 9998 else None

    def _rejected(
        self,
        source_line: int,
        raw_text: str,
        error_type: str,
        error_message: str,
    ) -> RejectedRow:
        return RejectedRow(
            member_name="response.json",
            source_line=source_line,
            raw_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
            error_type=error_type,
            error_message=error_message,
            raw_text=raw_text,
        )
