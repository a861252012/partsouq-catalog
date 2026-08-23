"""PartSouq live HTTP evidence canonicalization and secret-safe replay helpers.

Formal evidence deliberately stores the parser input after deterministic secret
redaction, not request cookies, response headers, browser state, or the raw
token-bearing response.  The live parser result and the sanitized replay result
must be identical before the caller may persist an artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, Tag

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

SANITIZER_VERSION = "partsouq-html-public-v2"
PARSER_CONTRACT_VERSION = "partsouq-catalog-parser-v1"
DEFAULT_EVIDENCE_MAX_BODY_BYTES = 8 * 1024 * 1024
REDACTED_VALUE = "__PARTSOUQ_REDACTED__"
OFFICIAL_HOSTS = frozenset({"partsouq.com", "www.partsouq.com"})
PUBLIC_QUERY_KEYS = frozenset({"c", "model", "vid", "cid", "cname", "uid", "q"})
PARSER_QUERY_KEYS = PUBLIC_QUERY_KEYS | {"ssd"}
SECRET_FIELD_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "cf_clearance",
        "php_sessid",
        "phpsessid",
        "set-cookie",
        "ssd",
    }
)
SECRET_TEXT_PATTERNS = (
    re.compile(
        r"\bssd\s*=\s*(?!__PARTSOUQ_REDACTED(?:_(?:[2-9]|[1-9][0-9]+))?__"
        r"(?:[&\s\"'<>]|$))"
        r"[^&#\s\"'<>]+",
        re.I,
    ),
    re.compile(r"\b(?:cf_clearance|phpsessid|set-cookie|authorization)\b", re.I),
    re.compile(r"__cf_chl_[A-Za-z0-9_-]+", re.I),
)


@dataclass(frozen=True, slots=True)
class CatalogHttpResponse:
    """Successful catalog response metadata exposed to the service layer.

    Headers and cookies are intentionally absent. ``text`` is exactly the
    string supplied to the live parser; only its sanitized equivalent may be
    persisted by the evidence repository.
    """

    final_url: str
    status_code: int
    content_type: str
    raw_body_sha256: str
    text: str
    fetched_at: datetime
    elapsed_ms: int
    attempt: int


@dataclass(frozen=True, slots=True)
class SanitizedBody:
    """Content-addressed parser replay body ready for compressed persistence."""

    body: bytes
    body_sha256: str
    compressed: bytes
    original_bytes: int
    stored_bytes: int
    sanitizer_version: str = SANITIZER_VERSION


@dataclass(frozen=True, slots=True)
class RecordEvidence:
    """Stable identity and payload hashes for one parsed source record."""

    record_type: str
    natural_key_sha256: str
    record_sha256: str
    parent_natural_key_sha256: str | None = None


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON-like data with a stable Unicode and key-order contract."""

    normalized = _normalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def brand_natural_key(name: object) -> dict[str, object]:
    return {"brand": name}


def model_natural_key(brand: object, model: object) -> dict[str, object]:
    return {"brand": brand, "model": model}


def vehicle_natural_key(
    brand: object,
    model: object,
    *,
    name: object,
    model_code: object,
    prod_period: object,
    production_from: object,
    production_to: object,
    engine: object,
    trim_name: object,
    vid: object,
) -> dict[str, object]:
    return {
        "brand": brand,
        "model": model,
        "name": name,
        "model_code": model_code,
        "prod_period": prod_period,
        "production_from": production_from,
        "production_to": production_to,
        "engine": engine,
        "trim_name": trim_name,
        "vid": vid,
    }


def category_natural_key(
    vehicle_key: Mapping[str, object], cid: object, category_name: object
) -> dict[str, object]:
    return {"vehicle": dict(vehicle_key), "cid": cid, "category_name": category_name}


def group_natural_key(
    category_key: Mapping[str, object], group_code: object, uid: object
) -> dict[str, object]:
    return {"category": dict(category_key), "group_code": group_code, "uid": uid}


def part_natural_key(
    group_key: Mapping[str, object], part_number: object, range_str: object
) -> dict[str, object]:
    return {
        "group": dict(group_key),
        "part_number": part_number,
        "range_str": range_str,
    }


def brand_record_evidence(records: Sequence[Mapping[str, object]]) -> list[RecordEvidence]:
    return [
        record_evidence("brand", brand_natural_key(record.get("name")), record)
        for record in records
    ]


def model_record_evidence(
    brand: str, records: Sequence[Mapping[str, object]]
) -> list[RecordEvidence]:
    parent = brand_natural_key(brand)
    return [
        record_evidence(
            "model",
            model_natural_key(brand, record.get("name")),
            {"brand": brand, **record},
            parent_natural_key=parent,
        )
        for record in records
    ]


def vehicle_record_natural_key(
    brand: str, model: str, vehicle: Mapping[str, object]
) -> dict[str, object]:
    return vehicle_natural_key(
        brand,
        model,
        name=vehicle.get("name"),
        model_code=vehicle.get("model_code"),
        prod_period=vehicle.get("prod_period"),
        production_from=vehicle.get("production_from"),
        production_to=vehicle.get("production_to"),
        engine=vehicle.get("engine"),
        trim_name=vehicle.get("grade"),
        vid=vehicle.get("vid"),
    )


def vehicle_record_evidence(
    brand: str,
    model: str,
    records: Sequence[Mapping[str, object]],
) -> list[RecordEvidence]:
    parent = model_natural_key(brand, model)
    return [
        record_evidence(
            "vehicle",
            vehicle_record_natural_key(brand, model, record),
            {"brand": brand, "model": model, **record},
            parent_natural_key=parent,
        )
        for record in records
    ]


def category_record_natural_key(
    vehicle_key: Mapping[str, object], category: Mapping[str, object]
) -> dict[str, object]:
    from .parsers import CATEGORY_NAMES

    cid = str(category.get("cid") or "")
    normalized_name = CATEGORY_NAMES.get(cid, str(category.get("category_name") or ""))
    return category_natural_key(vehicle_key, cid, normalized_name)


def category_record_evidence(
    vehicle_key: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
) -> list[RecordEvidence]:
    from .parsers import CATEGORY_NAMES

    evidence = []
    for record in records:
        cid = str(record.get("cid") or "")
        normalized_name = CATEGORY_NAMES.get(cid, str(record.get("category_name") or ""))
        evidence.append(
            record_evidence(
                "category",
                category_natural_key(vehicle_key, cid, normalized_name),
                {
                    **record,
                    "vehicle": dict(vehicle_key),
                    "category_name": normalized_name,
                    "source_category_name": record.get("category_name"),
                },
                parent_natural_key=vehicle_key,
            )
        )
    return evidence


def group_record_evidence(
    vehicle_key: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
) -> list[RecordEvidence]:
    evidence = []
    for record in records:
        category_key = category_record_natural_key(vehicle_key, record)
        evidence.append(
            record_evidence(
                "group",
                group_natural_key(
                    category_key,
                    record.get("group_code"),
                    record.get("uid"),
                ),
                {"category": category_key, **record},
                parent_natural_key=category_key,
            )
        )
    return evidence


def part_record_evidence(
    group_key: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    *,
    record_type: str = "part",
) -> list[RecordEvidence]:
    return [
        record_evidence(
            record_type,
            part_natural_key(
                group_key,
                record.get("part_number"),
                record.get("range_str"),
            ),
            {"group": dict(group_key), **record},
            parent_natural_key=group_key,
        )
        for record in records
    ]


def canonical_parser_context(parser_name: str, context: Mapping[str, object]) -> bytes:
    """Return a canonical secret-free parser context for durable replay."""

    public_context = _validated_parser_context(parser_name, context)
    encoded = canonical_json_bytes(public_context)
    assert_no_secret_material(encoded)
    return encoded


def replay_catalog_records(
    body: bytes,
    *,
    parser_name: str,
    parser_version: str,
    context: Mapping[str, object],
) -> tuple[list[RecordEvidence], int, int]:
    """Replay one allowlisted catalog parser from persisted sanitized HTML."""

    from .parsers import (
        parse_brand_index,
        parse_brands,
        parse_category_links,
        parse_groups,
        parse_parts,
        parse_vehicles,
    )

    if parser_version != PARSER_CONTRACT_VERSION:
        raise ValueError(f"unsupported evidence parser version: {parser_version}")
    assert_no_secret_material(body)
    html = body.decode("utf-8")
    public_context = _validated_parser_context(parser_name, context)

    if parser_name == "parse_brands":
        records, malformed = parse_brands(html, diagnostics=True)
        return brand_record_evidence(records), malformed, 0
    if parser_name == "parse_brand_index":
        brand = str(public_context["brand"])
        records, malformed = parse_brand_index(html, brand, diagnostics=True)
        return model_record_evidence(brand, records), malformed, 0
    if parser_name == "parse_vehicles":
        brand = str(public_context["brand"])
        model = str(public_context["model"])
        records, malformed = parse_vehicles(html, brand, diagnostics=True)
        return vehicle_record_evidence(brand, model, records), malformed, 0
    if parser_name == "parse_category_links":
        brand = str(public_context["brand"])
        expected_vid = str(public_context["expected_vid"])
        vehicle_key = _context_mapping(public_context, "vehicle_key")
        records, malformed, skipped = parse_category_links(
            html,
            brand=brand,
            diagnostics=True,
            expected_ssd=REDACTED_VALUE,
            expected_vid=expected_vid,
        )
        categories: list[Mapping[str, object]] = [
            {
                "category_name": "ENGINE/FUEL/TOOL",
                "cid": "1",
                "ssd": REDACTED_VALUE,
                "vid": expected_vid,
                "url": str(public_context["source_url"]),
            }
        ]
        categories.extend(record for record in records if record.get("cid") != "1")
        return category_record_evidence(vehicle_key, categories), malformed, skipped
    if parser_name == "parse_groups":
        brand = str(public_context["brand"])
        vehicle_key = _context_mapping(public_context, "vehicle_key")
        default_cid = str(public_context["default_cid"])
        expected_vid = str(public_context["expected_vid"])
        records, malformed, skipped, image_only = parse_groups(
            html,
            brand,
            default_cid=default_cid,
            diagnostics=True,
            expected_ssd=REDACTED_VALUE,
            expected_vid=expected_vid,
            expected_cid=default_cid,
        )
        return group_record_evidence(vehicle_key, records), malformed, skipped + image_only
    if parser_name == "parse_parts":
        group_key = _context_mapping(public_context, "group_key")
        records, malformed, skipped, skipped_rows = parse_parts(html, diagnostics=True)
        return (
            [
                *part_record_evidence(group_key, records),
                *part_record_evidence(
                    group_key,
                    skipped_rows,
                    record_type="quarantine_part",
                ),
            ],
            malformed,
            skipped,
        )
    raise ValueError(f"unsupported evidence parser: {parser_name}")


def public_source_url(url: str) -> str:
    """Return a public PartSouq URL with only stable, non-secret query keys.

    The exact request URL contains the opaque ``ssd`` value and must remain
    ephemeral.  Unknown query parameters are excluded by allowlist so a future
    token name cannot silently enter MySQL, logs, or manifests.
    """

    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or hostname not in OFFICIAL_HOSTS:
        raise ValueError("evidence source URL must use the official HTTPS PartSouq origin")
    if parts.username is not None or parts.password is not None or parts.port not in (None, 443):
        raise ValueError("evidence source URL contains unsupported authority components")
    canonical_path = parts.path.rstrip("/") or "/"
    if not (
        canonical_path == "/en/catalog/genuine" or canonical_path.startswith("/en/catalog/genuine/")
    ):
        raise ValueError("evidence source URL must be a genuine catalog path")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key in PUBLIC_QUERY_KEYS
    ]
    query.sort()
    return urlunsplit(("https", "partsouq.com", canonical_path, urlencode(query), ""))


def sanitize_parser_html(html: str) -> SanitizedBody:
    """Create deterministic, token-free HTML that the parser can replay.

    Scripts, comments, meta tags, and secret hidden inputs are outside every
    catalog parser's input contract and are removed. Catalog URL attributes
    retain their structure but replace each distinct ephemeral ``ssd`` value
    with a document-local sentinel so parser candidate identities remain stable.
    """

    soup = BeautifulSoup(html, "lxml")
    for element in soup.find_all(["script", "noscript"]):
        element.decompose()
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for meta in soup.find_all("meta"):
        meta.decompose()
    ssd_aliases: dict[str, str] = {}
    for element in soup.find_all(True):
        tag: Tag = element
        if tag.name == "input" and _is_secret_field(str(tag.get("name") or "")):
            tag.decompose()
            continue
        for attribute in ("href", "src", "action"):
            raw = tag.get(attribute)
            if isinstance(raw, str):
                tag[attribute] = _sanitize_url_value(raw, ssd_aliases)
        for attribute in list(tag.attrs):
            lowered = attribute.lower()
            if (
                lowered.startswith("on")
                or lowered.endswith("-ssd")
                or _is_secret_field(lowered)
                or lowered.startswith("data-cf-")
            ):
                del tag.attrs[attribute]
    body = str(soup).encode("utf-8")
    assert_no_secret_material(body)
    compressed = zlib.compress(body, level=9)
    return SanitizedBody(
        body=body,
        body_sha256=hashlib.sha256(body).hexdigest(),
        compressed=compressed,
        original_bytes=len(body),
        stored_bytes=len(compressed),
    )


def restore_sanitized_body(
    compression: str,
    body: bytes,
    *,
    expected_size: int | None = None,
    max_bytes: int = DEFAULT_EVIDENCE_MAX_BODY_BYTES,
) -> bytes:
    """Restore and validate a supported replay-body compression format."""

    if compression != "zlib":
        raise ValueError(f"unsupported evidence compression: {compression}")
    if max_bytes <= 0 or expected_size is not None and not 0 < expected_size <= max_bytes:
        raise ValueError("invalid evidence replay size limit")
    decompressor = zlib.decompressobj()
    try:
        restored = decompressor.decompress(body, max_bytes + 1)
    except zlib.error as error:
        raise ValueError("invalid evidence replay body") from error
    if len(restored) > max_bytes or decompressor.unconsumed_tail:
        raise ValueError("evidence replay body exceeds configured limit")
    remaining = max_bytes + 1 - len(restored)
    if remaining > 0:
        try:
            restored += decompressor.flush(remaining)
        except zlib.error as error:
            raise ValueError("invalid evidence replay body") from error
    if (
        len(restored) > max_bytes
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ValueError("invalid or oversized evidence replay body")
    if expected_size is not None and len(restored) != expected_size:
        raise ValueError("evidence replay body size mismatch")
    return restored


def assert_no_secret_material(value: bytes | str) -> None:
    """Fail closed if persisted evidence still contains secret material."""

    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    candidates = [text]
    for _ in range(3):
        decoded = unquote(candidates[-1])
        if decoded == candidates[-1]:
            break
        candidates.append(decoded)
    for candidate in candidates:
        for pattern in SECRET_TEXT_PATTERNS:
            if pattern.search(candidate):
                raise ValueError("sanitized evidence still contains secret material")


def record_evidence(
    record_type: str,
    natural_key: Mapping[str, object],
    record: Mapping[str, object],
    *,
    parent_natural_key: Mapping[str, object] | None = None,
) -> RecordEvidence:
    """Hash one source record without DB ids, timestamps, or request tokens."""

    if not record_type.strip():
        raise ValueError("record_type is required")
    return RecordEvidence(
        record_type=record_type,
        natural_key_sha256=canonical_sha256(_public_record(natural_key)),
        record_sha256=canonical_sha256(_public_record(record)),
        parent_natural_key_sha256=(
            canonical_sha256(_public_record(parent_natural_key))
            if parent_natural_key is not None
            else None
        ),
    )


def dataset_sha256(records: Sequence[RecordEvidence]) -> str:
    """Hash a record set independent of database ids and crawl ordering."""

    lines = sorted(
        f"{record.record_type}:{record.natural_key_sha256}:"
        f"{record.parent_natural_key_sha256 or ''}:{record.record_sha256}"
        for record in records
    )
    return hashlib.sha256("".join(f"{len(line)}:{line}" for line in lines).encode()).hexdigest()


def _normalize_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            normalized[unicodedata.normalize("NFC", str(key))] = _normalize_json(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [_normalize_json(item) for item in value]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _validated_parser_context(
    parser_name: str, context: Mapping[str, object]
) -> dict[str, JsonValue]:
    schemas = {
        "parse_brands": frozenset(),
        "parse_brand_index": frozenset({"brand"}),
        "parse_vehicles": frozenset({"brand", "model"}),
        "parse_category_links": frozenset({"brand", "vehicle_key", "expected_vid", "source_url"}),
        "parse_groups": frozenset({"brand", "vehicle_key", "default_cid", "expected_vid"}),
        "parse_parts": frozenset({"group_key"}),
    }
    expected = schemas.get(parser_name)
    if expected is None:
        raise ValueError(f"unsupported evidence parser: {parser_name}")
    normalized = _normalize_json(context)
    if not isinstance(normalized, dict):
        raise TypeError("parser context must be an object")
    public = _public_record(context)
    if normalized != public:
        raise ValueError("parser context must already be public and secret-free")
    assert_no_secret_material(canonical_json_bytes(public))
    if frozenset(public) != expected:
        raise ValueError(f"invalid {parser_name} evidence context fields")
    if "vehicle_key" in public:
        _validate_vehicle_key(_context_mapping(public, "vehicle_key"))
    if "group_key" in public:
        _validate_group_key(_context_mapping(public, "group_key"))
    if "source_url" in public:
        source_url = str(public["source_url"])
        if public_source_url(source_url) != source_url:
            raise ValueError("parser context source_url must already be public")
    return public


def _context_mapping(context: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = context.get(key)
    if not isinstance(value, Mapping):
        raise TypeError(f"parser context {key} must be an object")
    return value


def _validate_vehicle_key(value: Mapping[str, object]) -> None:
    expected = {
        "brand",
        "model",
        "name",
        "model_code",
        "prod_period",
        "production_from",
        "production_to",
        "engine",
        "trim_name",
        "vid",
    }
    if set(value) != expected:
        raise ValueError("invalid vehicle evidence natural key")


def _validate_group_key(value: Mapping[str, object]) -> None:
    if set(value) != {"category", "group_code", "uid"}:
        raise ValueError("invalid group evidence natural key")
    category = _context_mapping(value, "category")
    if set(category) != {"vehicle", "cid", "category_name"}:
        raise ValueError("invalid category evidence natural key")
    _validate_vehicle_key(_context_mapping(category, "vehicle"))


def _public_record(record: Mapping[str, object]) -> dict[str, JsonValue]:
    public: dict[str, JsonValue] = {}
    for key, value in record.items():
        normalized_key = unicodedata.normalize("NFC", str(key))
        if _is_secret_field(normalized_key):
            continue
        if normalized_key.lower().endswith("url") and isinstance(value, str):
            parts = urlsplit(value)
            record_url = value
            normalized_path = parts.path.rstrip("/") or "/"
            if not parts.scheme and not parts.netloc and parts.path.startswith("/"):
                record_url = urlunsplit(("https", "partsouq.com", normalized_path, parts.query, ""))
            else:
                try:
                    port = parts.port
                except ValueError:
                    port = -1
                hostname = (parts.hostname or "").lower()
                valid_http = parts.scheme.lower() == "http" and port in (None, 80)
                valid_https = parts.scheme.lower() == "https" and port in (None, 443)
                valid_protocol_relative = (
                    not parts.scheme
                    and bool(parts.netloc)
                    and port
                    in (
                        None,
                        443,
                    )
                )
                if (
                    hostname in OFFICIAL_HOSTS
                    and parts.username is None
                    and parts.password is None
                    and (valid_http or valid_https or valid_protocol_relative)
                ):
                    record_url = urlunsplit(
                        ("https", "partsouq.com", normalized_path, parts.query, "")
                    )
            public[normalized_key] = public_source_url(record_url)
        else:
            public[normalized_key] = _normalize_json(value)
    return public


def _sanitize_url_value(value: str, ssd_aliases: dict[str, str]) -> str:
    parts = urlsplit(value)
    query = []
    for key, item in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered not in PARSER_QUERY_KEYS:
            continue
        if lowered == "ssd" and item:
            redacted = ssd_aliases.get(item)
            if redacted is None:
                index = len(ssd_aliases) + 1
                redacted = REDACTED_VALUE if index == 1 else f"__PARTSOUQ_REDACTED_{index}__"
                ssd_aliases[item] = redacted
            item = redacted
        query.append((key, item))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _is_secret_field(name: str) -> bool:
    lowered = name.strip().lower()
    return lowered in SECRET_FIELD_NAMES or any(
        marker in lowered for marker in ("authorization", "cookie", "session", "token")
    )


def main() -> int:
    """Read-only formal evidence verifier for a completed 10,000-row run."""

    parser = argparse.ArgumentParser(description="重算 PartSouq 正式 10,000 筆 live evidence")
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--expected-parts", type=int, default=10_000)
    args = parser.parse_args()

    from .config import DB_CONFIG
    from .db import Database
    from .repositories import CrawlRepository

    database_name = str(DB_CONFIG["database"])
    if database_name != "partsouq_catalog":
        print(
            json.dumps(
                {
                    "database": database_name,
                    "run_id": args.run_id,
                    "verified": False,
                    "error": "formal evidence audit requires database partsouq_catalog",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1

    database = Database().connect()
    try:
        result = CrawlRepository(database, "evidence-audit").audit_run_evidence(
            args.run_id, args.expected_parts
        )
        result["database"] = database_name
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "database": database_name,
                    "run_id": args.run_id,
                    "verified": False,
                    "error": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    finally:
        database.rollback()
        database.close()
