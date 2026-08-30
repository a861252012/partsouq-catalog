from __future__ import annotations

import hashlib
import json
import zlib
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from partsouq_catalog.config import CRAWL
from partsouq_catalog.crawler import Crawler
from partsouq_catalog.evidence import (
    PARSER_CONTRACT_VERSION,
    REDACTED_VALUE,
    SANITIZER_VERSION,
    CatalogHttpResponse,
    assert_no_secret_material,
    brand_natural_key,
    canonical_parser_context,
    canonical_sha256,
    category_natural_key,
    dataset_sha256,
    group_natural_key,
    group_record_evidence,
    model_natural_key,
    part_record_evidence,
    public_source_url,
    record_evidence,
    replay_catalog_records,
    restore_sanitized_body,
    sanitize_parser_html,
    vehicle_record_evidence,
)
from partsouq_catalog.parsers import parse_groups, parse_parts, parse_vehicles
from partsouq_catalog.repositories import CrawlRepository


def _unit_html(*, ssd: str = "SSD-A+B") -> str:
    return f"""
    <!doctype html>
    <html>
      <head>
        <script>window.catalogSession = {ssd!r};</script>
        <meta http-equiv="set-cookie" content="PHPSESSID=COOKIE-ONE">
      </head>
      <body>
        <!-- cf_clearance=COOKIE-TWO -->
        <a href="/en/catalog/genuine/unit?uid=10&amp;ssd={ssd}&amp;q=">unit</a>
        <input type="hidden" name="ssd" value="{ssd}">
        <table>
          <thead>
            <tr>
              <th>Number</th><th>Name</th><th>Code</th>
              <th>Quantity</th><th>Range</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td><a href="/en/search/all?q=PN-0001">PN-0001</a></td>
              <td>ENGINE ASSY</td><td>11000</td><td>01</td>
              <td>01.2018 - 12.2019</td>
            </tr>
          </tbody>
        </table>
      </body>
    </html>
    """


def _part_record(index: int, *, name: str | None = None) -> dict[str, object]:
    return {
        "part_number": f"PN-{index:05d}",
        "name": name or f"PART {index:05d}",
        "code": "11000",
        "note": "",
        "quantity": "01",
        "range_str": "01.2018 - 12.2019",
        "part_from": "2018-01",
        "part_to": "2019-12",
    }


def _formal_group_key() -> dict[str, object]:
    return {
        "category": {
            "vehicle": {
                "brand": "TOYOTA",
                "model": "CAMRY",
                "name": "CAMRY",
                "model_code": "AXVA70",
                "prod_period": "01.2018 - 12.2019",
                "production_from": "2018-01",
                "production_to": "2019-12",
                "engine": "A25A-FKS",
                "trim_name": "LE",
                "vid": "100",
            },
            "cid": "1",
            "category_name": "ENGINE/FUEL/TOOL",
        },
        "group_code": "1101",
        "uid": "10",
    }


def _part_evidence(*, name: str | None = None):
    parts, malformed, skipped, skipped_rows = parse_parts(_unit_html(), diagnostics=True)
    assert (malformed, skipped, skipped_rows) == (0, 0, [])
    part = {**parts[0], "name": name or parts[0]["name"]}
    return part_record_evidence(_formal_group_key(), [part])[0]


def _record_evidence(
    repository: CrawlRepository,
    *,
    public_url: str,
    sanitized_body=None,
    parsed_records=None,
    replayed_records=None,
    accepted_records=None,
) -> int:
    body = sanitized_body or sanitize_parser_html(_unit_html())
    parsed = list(parsed_records if parsed_records is not None else [_part_evidence()])
    return repository.record_http_evidence(
        17,
        23,
        page_type="unit",
        public_url=public_url,
        raw_body_sha256="a" * 64,
        status_code=200,
        content_type="text/html",
        fetched_at=datetime(2026, 8, 22, 12, 0, 0),
        elapsed_ms=25,
        attempt=1,
        sanitized_body=body,
        parser_name="parse_parts",
        parser_version=PARSER_CONTRACT_VERSION,
        parser_context={"group_key": _formal_group_key()},
        parsed_records=parsed,
        replayed_records=list(replayed_records if replayed_records is not None else parsed),
        accepted_records=list(
            accepted_records if accepted_records is not None else [(101, parsed[0])]
        ),
    )


def _record_set_sha256(records) -> str:
    return dataset_sha256(records)


def _repository_verification_fixture():
    run_started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=10)
    fetched_at = run_started_at + timedelta(seconds=1)
    vehicle_key = _formal_group_key()["category"]["vehicle"]
    category_key = category_natural_key(vehicle_key, "1", "ENGINE/FUEL/TOOL")
    group_key = group_natural_key(category_key, "1101", "10")
    part = parse_parts(_unit_html(), diagnostics=True)[0][0]
    records = [
        record_evidence("brand", brand_natural_key("TOYOTA"), {"name": "TOYOTA"}),
        record_evidence(
            "model",
            model_natural_key("TOYOTA", "CAMRY"),
            {"brand": "TOYOTA", "name": "CAMRY"},
            parent_natural_key=brand_natural_key("TOYOTA"),
        ),
        record_evidence(
            "vehicle",
            vehicle_key,
            {"brand": "TOYOTA", "model": "CAMRY", **vehicle_key},
            parent_natural_key=model_natural_key("TOYOTA", "CAMRY"),
        ),
        record_evidence(
            "category",
            category_key,
            {"vehicle": vehicle_key, "cid": "1", "category_name": "ENGINE/FUEL/TOOL"},
            parent_natural_key=vehicle_key,
        ),
        record_evidence(
            "group",
            group_key,
            {"category": category_key, "group_code": "1101", "uid": "10"},
            parent_natural_key=category_key,
        ),
        part_record_evidence(group_key, [part])[0],
    ]
    accepted = [records[-1]]
    body = sanitize_parser_html(_unit_html())
    parser_context = {"group_key": group_key}
    parser_context_json = canonical_parser_context("parse_parts", parser_context)
    public_url = "https://partsouq.com/en/catalog/genuine/unit?uid=10"
    artifacts = [
        {
            "id": 1,
            "scheduled_job_run_id": 23,
            "capture_kind": "live_http",
            "page_type": "unit",
            "public_source_url": public_url,
            "source_url_sha256": hashlib.sha256(public_url.encode()).hexdigest(),
            "raw_body_sha256": "a" * 64,
            "body_sha256": body.body_sha256,
            "sanitizer_version": body.sanitizer_version,
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "challenge_detected": 0,
            "fetched_at": fetched_at,
            "elapsed_ms": 25,
            "attempt": 1,
            "parser_name": "parse_parts",
            "parser_version": PARSER_CONTRACT_VERSION,
            "parser_context_json": parser_context_json.decode(),
            "parser_context_sha256": hashlib.sha256(parser_context_json).hexdigest(),
            "malformed_row_count": 0,
            "skipped_record_count": 0,
            "parsed_record_count": len(records),
            "parsed_records_sha256": _record_set_sha256(records),
            "accepted_record_count": 1,
            "accepted_records_sha256": _record_set_sha256(accepted),
            "verified_at": fetched_at,
            "evidence_job_name": "catalog",
            "evidence_trigger_mode": "daemon",
            "evidence_job_status": "running",
            "evidence_job_exit_code": None,
            "evidence_job_started_at": run_started_at,
            "evidence_job_finished_at": None,
        }
    ]
    bodies = [
        {
            "body_sha256": body.body_sha256,
            "compression": "zlib",
            "body_blob": body.compressed,
            "original_bytes": body.original_bytes,
            "stored_bytes": body.stored_bytes,
            "sanitizer_version": body.sanitizer_version,
        }
    ]
    record_rows = [
        {
            "artifact_id": 1,
            "record_type": record.record_type,
            "natural_key_sha256": record.natural_key_sha256,
            "parent_natural_key_sha256": record.parent_natural_key_sha256,
            "record_sha256": record.record_sha256,
            "accepted": int(index == len(records) - 1),
            "part_id": 101 if index == len(records) - 1 else None,
        }
        for index, record in enumerate(records)
    ]
    source_rows = [
        {
            "id": 101,
            **part,
            "group_code": "1101",
            "uid": "10",
            "cid": "1",
            "category_name": "ENGINE/FUEL/TOOL",
            "vehicle_name": vehicle_key["name"],
            "model_code": vehicle_key["model_code"],
            "prod_period": vehicle_key["prod_period"],
            "production_from": vehicle_key["production_from"],
            "production_to": vehicle_key["production_to"],
            "engine": vehicle_key["engine"],
            "trim_name": vehicle_key["trim_name"],
            "vid": vehicle_key["vid"],
            "model_name": "CAMRY",
            "brand_name": "TOYOTA",
            "source_url": public_url,
            "evidence_record_sha256": records[-1].record_sha256,
        }
    ]
    database = mock.MagicMock()

    def execute(sql, _params=()):
        cursor = mock.MagicMock()
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT COUNT(*) AS row_count FROM partsouq_http_artifacts"):
            cursor.fetchone.return_value = {"row_count": 0}
        elif normalized.startswith("SELECT (SELECT COUNT(*) FROM partsouq_http_artifacts"):
            referenced_body_hashes = {artifact["body_sha256"] for artifact in artifacts}
            cursor.fetchone.return_value = {
                "artifact_count": len(artifacts),
                "original_bytes": sum(
                    int(body_row["original_bytes"])
                    for body_row in bodies
                    if body_row["body_sha256"] in referenced_body_hashes
                ),
            }
        elif normalized.startswith("SELECT artifact.id, artifact.scheduled_job_run_id"):
            cursor.fetchall.return_value = artifacts
        elif normalized.startswith("SELECT body.body_sha256"):
            cursor.fetchall.return_value = bodies
        elif normalized.startswith("SELECT records.artifact_id"):
            cursor.fetchall.return_value = record_rows
        elif normalized.startswith("SELECT part.id, part.part_number"):
            cursor.fetchall.return_value = source_rows
        else:
            raise AssertionError(f"unexpected evidence query: {normalized}")
        return cursor

    database._execute.side_effect = execute
    return {
        "repository": CrawlRepository(database, "evidence-verify-test"),
        "run_started_at": run_started_at,
        "artifacts": artifacts,
        "bodies": bodies,
        "records": records,
        "record_rows": record_rows,
        "source_rows": source_rows,
        "body": body,
    }


def test_public_source_url_is_stable_and_drops_session_material() -> None:
    first = public_source_url(
        "HTTPS://www.partsouq.com:443/en/catalog/genuine/unit"
        "?ssd=FIRST-SECRET&uid=9&vid=2&c=toyota&q=&token=SECOND-SECRET#private"
    )
    second = public_source_url(
        "https://partsouq.com/en/catalog/genuine/unit?q=&c=toyota&vid=2&uid=9&ssd=DIFFERENT-SECRET"
    )

    assert first == second
    assert first == ("https://partsouq.com/en/catalog/genuine/unit?c=toyota&q=&uid=9&vid=2")
    assert "SECRET" not in first
    assert "ssd=" not in first.lower()
    assert "token=" not in first.lower()
    assert "#" not in first


@pytest.mark.parametrize(
    "url",
    [
        "http://partsouq.com/en/catalog/genuine/unit?uid=1",
        "https://example.test/en/catalog/genuine/unit?uid=1",
        "https://user:password@partsouq.com/en/catalog/genuine/unit?uid=1",
        "https://partsouq.com:8443/en/catalog/genuine/unit?uid=1",
        "https://partsouq.com/en/search/all?q=PN-0001",
    ],
)
def test_public_source_url_rejects_non_catalog_authorities_and_paths(url: str) -> None:
    with pytest.raises(ValueError):
        public_source_url(url)


@pytest.mark.parametrize(
    "unit_url",
    (
        "/en/catalog/genuine/unit?c=TOYOTA&ssd=SECRET&vid=SITE-VID-1&cid=1&uid=10001",
        "//partsouq.com/en/catalog/genuine/unit?c=TOYOTA&ssd=SECRET&vid=SITE-VID-1&cid=1&uid=10001",
        "http://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&ssd=SECRET&vid=SITE-VID-1&cid=1&uid=10001",
    ),
)
def test_formal_group_evidence_canonicalizes_parser_url_variants(unit_url: str) -> None:
    vehicle_key = {
        "brand": "TOYOTA",
        "model": "CAMRY",
        "name": "CAMRY",
        "model_code": "AXVA70",
        "prod_period": "01.2018 - 12.2020",
        "production_from": "2018-01",
        "production_to": "2020-12",
        "engine": None,
        "trim_name": None,
        "vid": "SITE-VID-1",
    }
    html = f'<a href="{unit_url}">1101: PARTIAL ENGINE ASSEMBLY</a>'
    groups, malformed, skipped, image_only = parse_groups(
        html,
        "TOYOTA",
        diagnostics=True,
        expected_vid="SITE-VID-1",
    )
    assert (malformed, skipped, image_only) == (0, 0, 0)

    live_records = group_record_evidence(vehicle_key, groups)
    replayed, replay_malformed, replay_skipped = replay_catalog_records(
        sanitize_parser_html(html).body,
        parser_name="parse_groups",
        parser_version=PARSER_CONTRACT_VERSION,
        context={
            "brand": "TOYOTA",
            "vehicle_key": vehicle_key,
            "default_cid": "1",
            "expected_vid": "SITE-VID-1",
        },
    )

    assert (replay_malformed, replay_skipped) == (0, 0)
    assert replayed == live_records


def test_sanitized_replay_is_deterministic_secret_free_and_parser_equivalent() -> None:
    live_html = _unit_html()

    first = sanitize_parser_html(live_html)
    second = sanitize_parser_html(live_html)

    assert first == second
    assert first.body_sha256 == hashlib.sha256(first.body).hexdigest()
    assert restore_sanitized_body("zlib", first.compressed) == first.body
    assert first.original_bytes == len(first.body)
    assert first.stored_bytes == len(first.compressed)
    assert first.stored_bytes < first.original_bytes
    assert REDACTED_VALUE.encode() in first.body
    for secret in (b"SSD-A+B", b"COOKIE-ONE", b"COOKIE-TWO"):
        assert secret not in first.body
    assert_no_secret_material(first.body)
    assert parse_parts(live_html, diagnostics=True) == parse_parts(
        first.body.decode("utf-8"), diagnostics=True
    )


def test_restore_sanitized_body_rejects_body_over_configured_limit() -> None:
    compressed = zlib.compress(b"x" * 257)

    with pytest.raises(ValueError, match="exceeds configured limit"):
        restore_sanitized_body("zlib", compressed, max_bytes=256)


def test_restore_sanitized_body_rejects_trailing_compressed_data() -> None:
    compressed = zlib.compress(b"parser input") + b"unexpected trailing bytes"

    with pytest.raises(ValueError, match="invalid or oversized"):
        restore_sanitized_body("zlib", compressed)


def test_restore_sanitized_body_rejects_declared_size_mismatch() -> None:
    compressed = zlib.compress(b"parser input")

    with pytest.raises(ValueError, match="size mismatch"):
        restore_sanitized_body("zlib", compressed, expected_size=13)


def test_restore_sanitized_body_rejects_invalid_zlib_as_validation_error() -> None:
    with pytest.raises(ValueError, match="invalid evidence replay body"):
        restore_sanitized_body("zlib", b"not-zlib")


def test_sanitized_replay_preserves_part_search_query_but_redacts_ssd() -> None:
    sanitized = sanitize_parser_html(_unit_html(ssd="opaque%2Btoken"))
    body = sanitized.body.decode("utf-8")

    assert "/en/search/all?q=PN-0001" in body
    assert "opaque" not in body
    assert f"ssd={REDACTED_VALUE}" in body or f"ssd={REDACTED_VALUE.replace('_', '%5F')}" in body
    parts, malformed, skipped_nameless, skipped_rows = parse_parts(body, diagnostics=True)
    assert malformed == 0
    assert skipped_nameless == 0
    assert skipped_rows == []
    assert [part["part_number"] for part in parts] == ["PN-0001"]


def test_sanitized_replay_does_not_leak_secret_url_fragments_or_attributes() -> None:
    html = (
        '<a href="/en/catalog/genuine/unit?uid=1#ssd=FRAGMENT-SECRET">unit</a>'
        '<div data-token="ATTRIBUTE-SECRET" data-ssd="DATA-SSD-SECRET" '
        "onclick=\"location.href='/en/catalog/genuine/unit?ssd=EVENT-SECRET'\">content</div>"
        '<input type="hidden" name="sessionToken" value="INPUT-SECRET">'
    )

    sanitized = sanitize_parser_html(html).body.decode("utf-8")

    assert "FRAGMENT-SECRET" not in sanitized
    assert "ATTRIBUTE-SECRET" not in sanitized
    assert "DATA-SSD-SECRET" not in sanitized
    assert "EVENT-SECRET" not in sanitized
    assert "INPUT-SECRET" not in sanitized


def test_sanitized_replay_redacts_secret_from_open_graph_url() -> None:
    html = (
        '<meta property="og:url" content="https://partsouq.com/en/catalog/genuine/pick?'
        'c=Toyota&amp;model=1000&amp;ssd=OPEN-GRAPH-SECRET">'
    )

    sanitized = sanitize_parser_html(html).body.decode("utf-8")

    assert "OPEN-GRAPH-SECRET" not in sanitized
    assert "og:url" not in sanitized


def test_sanitized_replay_preserves_distinct_vehicle_candidate_identities() -> None:
    html = """
    <table>
      <tr><th class="n_name">Name</th><th class="__model">Model</th></tr>
      <tr>
        <td><a href="/en/catalog/genuine/vehicle?c=Toyota&amp;ssd=SECRET-A&amp;vid=1">A</a></td>
        <td>MODEL-A</td>
      </tr>
      <tr>
        <td><a href="/en/catalog/genuine/vehicle?c=Toyota&amp;ssd=SECRET-B&amp;vid=1">B</a></td>
        <td>MODEL-B</td>
      </tr>
    </table>
    """
    live, live_malformed = parse_vehicles(html, "Toyota", diagnostics=True)

    sanitized = sanitize_parser_html(html)
    replayed, replay_malformed, replay_skipped = replay_catalog_records(
        sanitized.body,
        parser_name="parse_vehicles",
        parser_version=PARSER_CONTRACT_VERSION,
        context={"brand": "Toyota", "model": "1000"},
    )

    assert (live_malformed, replay_malformed, replay_skipped) == (0, 0, 0)
    assert replayed == vehicle_record_evidence("Toyota", "1000", live)
    assert b"SECRET-A" not in sanitized.body
    assert b"SECRET-B" not in sanitized.body
    assert REDACTED_VALUE.encode() in sanitized.body
    assert b"__PARTSOUQ_REDACTED_2__" in sanitized.body


def test_sanitized_replay_preserves_blank_and_missing_ssd_diagnostics() -> None:
    html = """
    <table>
      <tr><th class="n_name">Name</th><th class="__model">Model</th></tr>
      <tr>
        <td><a href="/en/catalog/genuine/vehicle?c=Toyota&amp;ssd=&amp;vid=1">A</a></td>
        <td>MODEL-A</td>
      </tr>
      <tr>
        <td><a href="/en/catalog/genuine/vehicle?c=Toyota&amp;vid=1">B</a></td>
        <td>MODEL-B</td>
      </tr>
      <tr>
        <td><a href="/en/catalog/genuine/vehicle?c=Toyota&amp;ssd=SECRET-C&amp;vid=2">C</a></td>
        <td>MODEL-C</td>
      </tr>
    </table>
    """
    live, live_malformed = parse_vehicles(html, "Toyota", diagnostics=True)

    sanitized = sanitize_parser_html(html)
    replayed, replay_malformed, replay_skipped = replay_catalog_records(
        sanitized.body,
        parser_name="parse_vehicles",
        parser_version=PARSER_CONTRACT_VERSION,
        context={"brand": "Toyota", "model": "1000"},
    )

    assert (live_malformed, replay_malformed, replay_skipped) == (1, 1, 0)
    assert replayed == vehicle_record_evidence("Toyota", "1000", live)
    assert b"SECRET-C" not in sanitized.body
    assert REDACTED_VALUE.encode() in sanitized.body
    assert b"__PARTSOUQ_REDACTED_2__" not in sanitized.body


def test_sanitizer_fails_closed_on_unstructured_ssd_text() -> None:
    with pytest.raises(ValueError, match="secret material"):
        sanitize_parser_html("<p>diagnostic ssd=TEXT-SECRET</p>")


def test_durable_replay_dispatch_matches_parser_and_detects_body_mutation() -> None:
    sanitized = sanitize_parser_html(_unit_html())
    context = {"group_key": _formal_group_key()}
    records, malformed, skipped = replay_catalog_records(
        sanitized.body,
        parser_name="parse_parts",
        parser_version=PARSER_CONTRACT_VERSION,
        context=context,
    )
    parsed, live_malformed, live_skipped, skipped_rows = parse_parts(
        sanitized.body.decode("utf-8"), diagnostics=True
    )

    assert (malformed, skipped) == (live_malformed, live_skipped) == (0, 0)
    assert records == [
        *part_record_evidence(_formal_group_key(), parsed),
        *part_record_evidence(
            _formal_group_key(),
            skipped_rows,
            record_type="quarantine_part",
        ),
    ]

    mutated_body = sanitized.body.replace(b"ENGINE ASSY", b"MUTATED ASSY")
    mutated_records, _, _ = replay_catalog_records(
        mutated_body,
        parser_name="parse_parts",
        parser_version=PARSER_CONTRACT_VERSION,
        context=context,
    )
    assert mutated_records != records


def test_durable_replay_rejects_parser_version_and_secret_context_mutations() -> None:
    sanitized = sanitize_parser_html(_unit_html())

    with pytest.raises(ValueError, match="unsupported evidence parser version"):
        replay_catalog_records(
            sanitized.body,
            parser_name="parse_parts",
            parser_version="mutated-parser-version",
            context={"group_key": _formal_group_key()},
        )

    secret_context = {"group_key": _formal_group_key(), "ssd": "DO-NOT-PERSIST"}
    with pytest.raises(ValueError, match="already be public and secret-free"):
        replay_catalog_records(
            sanitized.body,
            parser_name="parse_parts",
            parser_version=PARSER_CONTRACT_VERSION,
            context=secret_context,
        )
    with pytest.raises(ValueError, match="already be public and secret-free"):
        canonical_parser_context("parse_parts", secret_context)
    assert b"ssd" not in canonical_parser_context("parse_parts", {"group_key": _formal_group_key()})


def test_non_unit_replay_preserves_skipped_diagnostics_without_quarantine() -> None:
    vehicle_key = {
        **_formal_group_key()["category"]["vehicle"],
        "brand": "toyota",
        "vid": "7",
    }
    source_url = "https://partsouq.com/en/catalog/genuine/vehicle?c=toyota&vid=7"
    html = (
        '<a href="https://partsouq.com/en/catalog/genuine/vehicle'
        '?c=toyota&amp;ssd=live-token&amp;vid=7&amp;cid=2&amp;cname=BODY">BODY</a>'
        '<a href="https://partsouq.com/en/catalog/genuine/vehicle'
        '?c=honda&amp;ssd=foreign-token&amp;vid=8&amp;cid=3&amp;cname=ELECTRICAL">'
        "ELECTRICAL</a>"
    )
    sanitized = sanitize_parser_html(html)
    context = {
        "brand": "toyota",
        "vehicle_key": vehicle_key,
        "expected_vid": "7",
        "source_url": source_url,
    }
    records, malformed, skipped = replay_catalog_records(
        sanitized.body,
        parser_name="parse_category_links",
        parser_version=PARSER_CONTRACT_VERSION,
        context=context,
    )

    assert malformed == 0
    assert skipped == 1
    assert [record.record_type for record in records] == ["category", "category"]
    assert not any(record.record_type == "quarantine_part" for record in records)

    database = mock.MagicMock()
    database._execute.side_effect = RuntimeError("reached database boundary")
    with pytest.raises(RuntimeError, match="reached database boundary"):
        CrawlRepository(database, "non-unit-skipped-test").record_http_evidence(
            17,
            23,
            page_type="vehicle",
            public_url=source_url,
            raw_body_sha256="a" * 64,
            status_code=200,
            content_type="text/html",
            fetched_at=datetime(2026, 8, 22, 12, 0, 0),
            elapsed_ms=25,
            attempt=1,
            sanitized_body=sanitized,
            parser_name="parse_category_links",
            parser_version=PARSER_CONTRACT_VERSION,
            parser_context=context,
            parsed_records=records,
            replayed_records=records,
            accepted_records=[],
            skipped_record_count=skipped,
        )


def test_catalog_http_response_envelope_cannot_carry_headers_or_cookies() -> None:
    response = CatalogHttpResponse(
        final_url="https://partsouq.com/en/catalog/genuine/unit?ssd=ephemeral&uid=1",
        status_code=200,
        content_type="text/html",
        raw_body_sha256=hashlib.sha256(_unit_html().encode()).hexdigest(),
        text=_unit_html(),
        fetched_at=datetime(2026, 8, 22, 12, 0, 0),
        elapsed_ms=125,
        attempt=1,
    )

    envelope = asdict(response)
    assert "headers" not in envelope
    assert "request_headers" not in envelope
    assert "response_headers" not in envelope
    assert "cookies" not in envelope
    assert "cookie" not in envelope


def test_record_hash_is_order_independent_and_omits_secret_fields() -> None:
    first = _part_record(1)
    second = {
        **_part_record(2),
        "ssd": "DO-NOT-HASH",
        "cookie": "PHPSESSID=DO-NOT-HASH",
        "source_url": ("https://partsouq.com/en/catalog/genuine/unit?uid=2&ssd=DO-NOT-HASH&vid=7"),
    }

    first_evidence = record_evidence("part", {"id": 1}, first)
    second_evidence = record_evidence("part", {"id": 2}, second)
    public_second = {
        key: value for key, value in second.items() if key not in {"ssd", "cookie", "source_url"}
    }
    public_second["source_url"] = public_source_url(str(second["source_url"]))

    assert dataset_sha256([first_evidence, second_evidence]) == dataset_sha256(
        [second_evidence, first_evidence]
    )
    assert second_evidence == record_evidence("part", {"id": 2}, public_second)


def test_parser_output_hash_includes_quarantined_source_rows() -> None:
    parsed = [_part_record(1)]
    first_quarantine = [
        {
            "part_number": "PN-NAMELESS-1",
            "code": "11000",
            "quantity": "01",
            "range_str": "",
            "note": "",
        }
    ]
    changed_quarantine = [{**first_quarantine[0], "quantity": "02"}]

    parts = part_record_evidence(_formal_group_key(), parsed)
    first = part_record_evidence(
        _formal_group_key(), first_quarantine, record_type="quarantine_part"
    )
    changed = part_record_evidence(
        _formal_group_key(), changed_quarantine, record_type="quarantine_part"
    )
    without_quarantine = dataset_sha256(parts)
    with_quarantine = dataset_sha256([*parts, *first])
    mutated_quarantine = dataset_sha256([*parts, *changed])

    assert with_quarantine != without_quarantine
    assert mutated_quarantine != with_quarantine


def test_dataset_hash_detects_9999_coverage_and_record_mutations() -> None:
    complete = [
        record_evidence(
            "part",
            {"part_number": f"PN-{index:05d}", "range_str": ""},
            _part_record(index),
        )
        for index in range(10_000)
    ]
    reordered = list(reversed(complete))
    missing_one = complete[:-1]
    changed = [
        *complete[:-1],
        record_evidence(
            "part",
            {"part_number": "PN-09999", "range_str": ""},
            _part_record(9_999, name="MUTATED PART NAME"),
        ),
    ]

    expected = dataset_sha256(complete)
    assert dataset_sha256(reordered) == expected
    assert dataset_sha256(missing_one) != expected
    assert dataset_sha256(changed) != expected


def test_secret_detector_rejects_cookie_and_authorization_material() -> None:
    for value in (
        "Set-Cookie: PHPSESSID=secret",
        "Cookie: cf_clearance=secret",
        "Authorization: Bearer secret",
        "https://partsouq.com/en/catalog/genuine/unit?ssd=secret&uid=1",
    ):
        with pytest.raises(ValueError, match="secret material"):
            assert_no_secret_material(value)


@pytest.mark.parametrize(
    "value",
    [
        "ssd%3DENCODED-SECRET",
        "cf%5Fclearance%3DENCODED-SECRET",
        "PHPSESSID%3DENCODED-SECRET",
        "Set-Cookie%3A%20PHPSESSID%3DENCODED-SECRET",
        "Authorization%3A%20Bearer%20ENCODED-SECRET",
    ],
)
def test_secret_detector_rejects_percent_encoded_secret_material(value: str) -> None:
    with pytest.raises(ValueError, match="secret material"):
        assert_no_secret_material(value)


def test_repository_rejects_unsanitized_source_url_before_database_write() -> None:
    database = mock.MagicMock()
    repository = CrawlRepository(database, "evidence-test")

    with pytest.raises(ValueError, match="canonical and secret-free"):
        _record_evidence(
            repository,
            public_url=("https://partsouq.com/en/catalog/genuine/unit?uid=1&ssd=DO-NOT-PERSIST"),
        )

    database._execute.assert_not_called()
    database._executemany.assert_not_called()


def test_repository_rejects_sanitized_body_hash_mutation_before_database_write() -> None:
    database = mock.MagicMock()
    repository = CrawlRepository(database, "evidence-test")
    body = sanitize_parser_html(_unit_html())

    with pytest.raises(ValueError, match="body hash mismatch"):
        _record_evidence(
            repository,
            public_url="https://partsouq.com/en/catalog/genuine/unit?uid=1",
            sanitized_body=replace(body, body_sha256="0" * 64),
        )

    database._execute.assert_not_called()
    database._executemany.assert_not_called()


def test_repository_rejects_unsupported_sanitizer_before_database_write() -> None:
    database = mock.MagicMock()
    repository = CrawlRepository(database, "evidence-test")
    body = sanitize_parser_html(_unit_html())

    with pytest.raises(ValueError, match="unsupported PartSouq evidence sanitizer version"):
        _record_evidence(
            repository,
            public_url="https://partsouq.com/en/catalog/genuine/unit?uid=1",
            sanitized_body=replace(body, sanitizer_version="partsouq-html-public-v1"),
        )

    database._execute.assert_not_called()
    database._executemany.assert_not_called()


def test_repository_rejects_parser_replay_mutation_before_database_write() -> None:
    database = mock.MagicMock()
    repository = CrawlRepository(database, "evidence-test")
    live = _part_evidence()
    mutated_replay = _part_evidence(name="MUTATED REPLAY NAME")

    with pytest.raises(RuntimeError, match="replay does not match"):
        _record_evidence(
            repository,
            public_url="https://partsouq.com/en/catalog/genuine/unit?uid=1",
            parsed_records=[live],
            replayed_records=[mutated_replay],
            accepted_records=[(101, live)],
        )

    database._execute.assert_not_called()
    database._executemany.assert_not_called()


def test_repository_rejects_accepted_record_outside_exact_parser_result() -> None:
    database = mock.MagicMock()
    repository = CrawlRepository(database, "evidence-test")
    parsed = _part_evidence()
    mutated_accepted = _part_evidence(name="MUTATED ACCEPTED NAME")

    with pytest.raises(ValueError, match="exact parsed-record subset"):
        _record_evidence(
            repository,
            public_url="https://partsouq.com/en/catalog/genuine/unit?uid=1",
            parsed_records=[parsed],
            replayed_records=[parsed],
            accepted_records=[(101, mutated_accepted)],
        )

    database._execute.assert_not_called()
    database._executemany.assert_not_called()


def test_repository_supersedes_only_the_same_parser_context_slot() -> None:
    database = mock.MagicMock()

    def execute(sql, _params=()):
        cursor = mock.MagicMock()
        normalized = " ".join(sql.split())
        if "FROM crawl_runs AS cr" in normalized:
            cursor.fetchone.return_value = {
                "started_at": datetime(2026, 8, 22, 11, 0, 0),
                "status": "running",
                "dataset_kind": "bounded",
                "target_parts": 10_000,
                "scheduled_job_run_id": 23,
                "scheduled_job_name": "catalog",
                "scheduled_trigger_mode": "daemon",
                "scheduled_job_status": "running",
            }
        elif normalized.startswith("INSERT INTO partsouq_http_artifacts"):
            cursor.lastrowid = 31
        elif normalized.startswith("SELECT (SELECT COUNT(*) FROM partsouq_http_artifacts"):
            cursor.fetchone.return_value = {"artifact_count": 1, "original_bytes": 1}
        return cursor

    database._execute.side_effect = execute
    _record_evidence(
        CrawlRepository(database, "evidence-supersede-test"),
        public_url="https://partsouq.com/en/catalog/genuine/unit?uid=10",
    )

    supersede_call = next(
        call
        for call in database._execute.call_args_list
        if "verification_status = 'superseded'" in call.args[0]
    )
    normalized = " ".join(supersede_call.args[0].split())
    assert "parser_name = %s" in normalized
    assert "parser_context_sha256 = %s" in normalized


def test_repository_verifier_replays_the_stored_cas_body() -> None:
    fixture = _repository_verification_fixture()
    repository = fixture["repository"]
    records = fixture["records"]
    with mock.patch(
        "partsouq_catalog.repositories.replay_catalog_records",
        return_value=(records, 0, 0),
    ) as replay:
        summary = repository._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )

    assert summary.accepted_record_count == 1
    replay.assert_called_once()
    assert replay.call_args.args[0] == fixture["body"].body
    assert replay.call_args.kwargs == {
        "parser_name": "parse_parts",
        "parser_version": PARSER_CONTRACT_VERSION,
        "context": {"group_key": _formal_group_key()},
    }


def test_completed_evidence_rebuilds_from_immutable_bounded_snapshot() -> None:
    fixture = _repository_verification_fixture()
    original_execute = fixture["repository"].db._execute.side_effect

    def execute(sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT part.id, part.part_number"):
            cursor = mock.MagicMock()
            cursor.fetchall.return_value = []
            return cursor
        if normalized.startswith("SELECT bounded_part.part_id AS id"):
            cursor = mock.MagicMock()
            cursor.fetchall.return_value = fixture["source_rows"]
            return cursor
        return original_execute(sql, params)

    fixture["repository"].db._execute.side_effect = execute
    with mock.patch(
        "partsouq_catalog.repositories.replay_catalog_records",
        return_value=(fixture["records"], 0, 0),
    ):
        summary = fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
            use_bounded_snapshot=True,
        )

    assert summary.accepted_record_count == 1


def test_completed_evidence_rejects_snapshot_source_url_mutation() -> None:
    fixture = _repository_verification_fixture()
    fixture["source_rows"][0]["source_url"] = "https://partsouq.com/en/catalog/genuine/unit?uid=11"
    original_execute = fixture["repository"].db._execute.side_effect

    def execute(sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT bounded_part.part_id AS id"):
            cursor = mock.MagicMock()
            cursor.fetchall.return_value = fixture["source_rows"]
            return cursor
        return original_execute(sql, params)

    fixture["repository"].db._execute.side_effect = execute
    with pytest.raises(RuntimeError, match="evidence source URL mismatch"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
            use_bounded_snapshot=True,
        )


def test_completed_evidence_rejects_snapshot_record_digest_mutation() -> None:
    fixture = _repository_verification_fixture()
    fixture["source_rows"][0]["evidence_record_sha256"] = "f" * 64
    original_execute = fixture["repository"].db._execute.side_effect

    def execute(sql, params=()):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT bounded_part.part_id AS id"):
            cursor = mock.MagicMock()
            cursor.fetchall.return_value = fixture["source_rows"]
            return cursor
        return original_execute(sql, params)

    fixture["repository"].db._execute.side_effect = execute
    with pytest.raises(RuntimeError, match="snapshot evidence digest mismatch"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
            use_bounded_snapshot=True,
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    (
        ("brand_name", "LEXUS"),
        ("model_name", "TACOMA"),
        ("production_to", "2005-12"),
    ),
)
def test_repository_verifier_rejects_rows_outside_declared_scope(
    field_name: str,
    field_value: str,
) -> None:
    fixture = _repository_verification_fixture()
    fixture["source_rows"][0][field_name] = field_value

    with pytest.raises(RuntimeError, match="declared model scope mismatch"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
            scope_brand="TOYOTA",
            scope_model="CAMRY",
            scope_vehicle_year_floor=2006,
        )


def test_scope_metadata_changes_manifest_while_legacy_null_scope_keeps_list_hash() -> None:
    fixture = _repository_verification_fixture()
    fixed_started_at = datetime(2026, 8, 22, 11, 0, 0)
    fixed_fetched_at = datetime(2026, 8, 22, 11, 0, 1)
    fixture["run_started_at"] = fixed_started_at
    hierarchy_records = fixture["records"][:-1]
    part_records = fixture["records"][-1:]
    first_artifact = fixture["artifacts"][0]
    first_artifact.update(
        fetched_at=fixed_fetched_at,
        verified_at=fixed_fetched_at,
        evidence_job_started_at=fixed_started_at,
        parsed_record_count=len(hierarchy_records),
        parsed_records_sha256=_record_set_sha256(hierarchy_records),
        accepted_record_count=0,
        accepted_records_sha256=_record_set_sha256([]),
    )
    # Keep the accepted part's stored source URL aligned with the artifact
    # while using a distinct, allowed catalog context for manifest ordering.
    second_url = "https://partsouq.com/en/catalog/genuine/unit?c=TOYOTA&uid=10"
    fixture["source_rows"][0]["source_url"] = second_url
    second_artifact = {
        **first_artifact,
        "id": 2,
        "public_source_url": second_url,
        "source_url_sha256": hashlib.sha256(second_url.encode()).hexdigest(),
        "raw_body_sha256": "b" * 64,
        "fetched_at": fixed_fetched_at + timedelta(seconds=1),
        "verified_at": fixed_fetched_at + timedelta(seconds=1),
        "elapsed_ms": 30,
        "attempt": 2,
        "parsed_record_count": len(part_records),
        "parsed_records_sha256": _record_set_sha256(part_records),
        "accepted_record_count": 1,
        "accepted_records_sha256": _record_set_sha256(part_records),
    }
    fixture["artifacts"].append(second_artifact)
    fixture["record_rows"][-1]["artifact_id"] = 2
    captured_manifests: list[object] = []

    def capture_manifest(value: object) -> str:
        captured_manifests.append(value)
        return canonical_sha256(value)

    replay_results = iter(
        [
            (hierarchy_records, 0, 0),
            (part_records, 0, 0),
        ]
        * 3
    )
    with (
        mock.patch(
            "partsouq_catalog.repositories.replay_catalog_records",
            side_effect=lambda *_args, **_kwargs: next(replay_results),
        ),
        mock.patch(
            "partsouq_catalog.repositories.canonical_sha256",
            side_effect=capture_manifest,
        ),
    ):
        legacy = fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )
        scoped = fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
            scope_brand="TOYOTA",
            scope_model="CAMRY",
            scope_vehicle_year_floor=2006,
        )
        moved_floor = fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
            scope_brand="TOYOTA",
            scope_model="CAMRY",
            scope_vehicle_year_floor=2007,
        )

    manifest_inputs = [
        value
        for value in captured_manifests
        if (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and "capture_kind" in value[0]
        )
        or (isinstance(value, dict) and "scope" in value and "artifacts" in value)
    ]
    legacy_manifest = [
        {
            "capture_kind": "live_http",
            "scheduled_job_run_id": 23,
            "page_type": "unit",
            "source_url_sha256": (
                "4e5cd1dca803f26bade15e23305fefc208661c44d1f8b327bd9310bf05d6c366"
            ),
            "raw_body_sha256": "a" * 64,
            "body_sha256": "ee8a62862b20ce97f74e2bdba935d141361fee3c543a75ebb9a0214c3edca72e",
            "sanitizer_version": "partsouq-html-public-v2",
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "fetched_at": "2026-08-22T11:00:01.000000",
            "elapsed_ms": 25,
            "attempt": 1,
            "parser_name": "parse_parts",
            "parser_version": "partsouq-catalog-parser-v1",
            "parser_context_sha256": (
                "6e67f898abf81eea4fa0d62d3ffd867110e8d53c19a7a87cebcc93ebd6988b8a"
            ),
            "parsed_record_count": 5,
            "parsed_records_sha256": (
                "ecad426511cbe19a83ac239b5b1e1693a90e194d863985ab09e22331dafc1cf4"
            ),
            "accepted_record_count": 0,
            "accepted_records_sha256": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
        },
        {
            "capture_kind": "live_http",
            "scheduled_job_run_id": 23,
            "page_type": "unit",
            "source_url_sha256": (
                "e846fa4348a66b7e0057001e0495afc534fc58712a958ec426b27604c084e48c"
            ),
            "raw_body_sha256": "b" * 64,
            "body_sha256": "ee8a62862b20ce97f74e2bdba935d141361fee3c543a75ebb9a0214c3edca72e",
            "sanitizer_version": "partsouq-html-public-v2",
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "fetched_at": "2026-08-22T11:00:02.000000",
            "elapsed_ms": 30,
            "attempt": 2,
            "parser_name": "parse_parts",
            "parser_version": "partsouq-catalog-parser-v1",
            "parser_context_sha256": (
                "6e67f898abf81eea4fa0d62d3ffd867110e8d53c19a7a87cebcc93ebd6988b8a"
            ),
            "parsed_record_count": 1,
            "parsed_records_sha256": (
                "0d814063349732da13235a872b708fd5f1de2969cd81f1c25e309e0667db8d66"
            ),
            "accepted_record_count": 1,
            "accepted_records_sha256": (
                "0d814063349732da13235a872b708fd5f1de2969cd81f1c25e309e0667db8d66"
            ),
        },
    ]
    assert manifest_inputs[0] == legacy_manifest
    assert canonical_sha256(legacy_manifest) == (
        "66c51d5f7863473856ec6d9b7190c3f64ea7d8732da70a0bbce1569b938d2ef4"
    )
    assert (
        legacy.manifest_sha256 == "66c51d5f7863473856ec6d9b7190c3f64ea7d8732da70a0bbce1569b938d2ef4"
    )
    assert legacy.manifest_sha256 == canonical_sha256(legacy_manifest)
    assert manifest_inputs[1] == {
        "scope": {
            "brand": "toyota",
            "model": "camry",
            "vehicle_year_floor": 2006,
        },
        "artifacts": legacy_manifest,
    }
    assert len({legacy.manifest_sha256, scoped.manifest_sha256, moved_floor.manifest_sha256}) == 3


def test_verify_run_evidence_seals_the_persisted_scope() -> None:
    database = mock.MagicMock()
    run = {
        "started_at": datetime.now(UTC).replace(tzinfo=None),
        "status": "running",
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "scope_brand": "TOYOTA",
        "scope_model": "TACOMA",
        "scope_vehicle_year_floor": 2006,
        "scheduled_job_run_id": 23,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "running",
        "scheduled_crawl_count": 1,
    }
    select_cursor = mock.MagicMock()
    select_cursor.fetchone.return_value = run
    update_cursor = mock.MagicMock()
    database._execute.side_effect = (select_cursor, update_cursor)
    repository = CrawlRepository(database, "scope-seal")
    summary = mock.MagicMock(
        manifest_sha256="a" * 64,
        dataset_sha256="b" * 64,
        artifact_count=7,
        accepted_record_count=10_000,
        original_bytes=1_024,
        stored_bytes=512,
    )

    with mock.patch.object(repository, "_calculate_run_evidence", return_value=summary) as seal:
        repository.verify_run_evidence(17)

    select_query = database._execute.call_args_list[0].args[0]
    assert "cr.scope_brand, cr.scope_model, cr.scope_vehicle_year_floor" in select_query
    seal.assert_called_once_with(
        17,
        23,
        run["started_at"],
        10_000,
        scope_brand="TOYOTA",
        scope_model="TACOMA",
        scope_vehicle_year_floor=2006,
    )


def test_repository_verifier_detects_coherent_cas_body_replacement() -> None:
    fixture = _repository_verification_fixture()
    mutated_body = sanitize_parser_html(_unit_html().replace("ENGINE ASSY", "MUTATED ASSY"))
    fixture["bodies"][:] = [
        {
            "body_sha256": mutated_body.body_sha256,
            "compression": "zlib",
            "body_blob": mutated_body.compressed,
            "original_bytes": mutated_body.original_bytes,
            "stored_bytes": mutated_body.stored_bytes,
            "sanitizer_version": mutated_body.sanitizer_version,
        }
    ]
    fixture["artifacts"][0]["body_sha256"] = mutated_body.body_sha256
    mutated_records = [*fixture["records"][:-1], _part_evidence(name="MUTATED ASSY")]

    with (
        mock.patch(
            "partsouq_catalog.repositories.replay_catalog_records",
            return_value=(mutated_records, 0, 0),
        ),
        pytest.raises(RuntimeError, match="parser evidence mismatch"),
    ):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )


def test_repository_verifier_rejects_mixed_sanitizer_version() -> None:
    fixture = _repository_verification_fixture()
    fixture["artifacts"][0]["sanitizer_version"] = "partsouq-html-public-v1"

    with pytest.raises(RuntimeError, match="is not verified live HTTP"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )


def test_repository_verifier_rejects_cross_scheduler_artifact() -> None:
    fixture = _repository_verification_fixture()
    fixture["artifacts"][0]["scheduled_job_run_id"] = 24

    with pytest.raises(RuntimeError, match="is not verified live HTTP"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )


def test_repository_verifier_accepts_failed_prior_scheduler_attempt() -> None:
    fixture = _repository_verification_fixture()
    artifact = fixture["artifacts"][0]
    artifact["scheduled_job_run_id"] = 22
    artifact["evidence_job_status"] = "failed"
    artifact["evidence_job_exit_code"] = 125
    artifact["evidence_job_finished_at"] = artifact["fetched_at"] + timedelta(seconds=1)

    with mock.patch(
        "partsouq_catalog.repositories.replay_catalog_records",
        return_value=(fixture["records"], 0, 0),
    ):
        summary = fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )

    assert summary.accepted_record_count == 1


def test_repository_recovery_verifier_accepts_failed_current_scheduler_attempt() -> None:
    fixture = _repository_verification_fixture()
    artifact = fixture["artifacts"][0]
    artifact["evidence_job_status"] = "failed"
    artifact["evidence_job_exit_code"] = 125
    artifact["evidence_job_finished_at"] = artifact["fetched_at"] + timedelta(seconds=1)

    with pytest.raises(RuntimeError, match="is not verified live HTTP"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )

    with mock.patch(
        "partsouq_catalog.repositories.replay_catalog_records",
        return_value=(fixture["records"], 0, 0),
    ):
        summary = fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
            allow_failed_current_scheduler=True,
        )

    assert summary.accepted_record_count == 1


@pytest.mark.parametrize(
    "window_mutation",
    ["missing_finished_at", "before_attempt", "after_attempt"],
)
def test_repository_verifier_rejects_prior_attempt_outside_time_window(
    window_mutation: str,
) -> None:
    fixture = _repository_verification_fixture()
    artifact = fixture["artifacts"][0]
    artifact["scheduled_job_run_id"] = 22
    artifact["evidence_job_status"] = "failed"
    artifact["evidence_job_exit_code"] = 125
    artifact["evidence_job_finished_at"] = artifact["fetched_at"] + timedelta(seconds=1)
    if window_mutation == "missing_finished_at":
        artifact["evidence_job_finished_at"] = None
    elif window_mutation == "before_attempt":
        artifact["evidence_job_started_at"] = artifact["fetched_at"] + timedelta(seconds=1)
    else:
        artifact["evidence_job_finished_at"] = artifact["fetched_at"] - timedelta(minutes=6)

    with pytest.raises(RuntimeError, match="is outside the run window"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )


@pytest.mark.parametrize(
    ("status", "exit_code", "finished_at", "trigger_mode"),
    [
        ("running", None, None, "daemon"),
        ("completed", 0, "finished", "daemon"),
        ("failed", 0, "finished", "daemon"),
        ("failed", 125, "finished", "manual"),
    ],
)
def test_repository_verifier_rejects_unproven_prior_scheduler_attempt(
    status: str,
    exit_code: int | None,
    finished_at: str | None,
    trigger_mode: str,
) -> None:
    fixture = _repository_verification_fixture()
    artifact = fixture["artifacts"][0]
    artifact["scheduled_job_run_id"] = 22
    artifact["evidence_job_status"] = status
    artifact["evidence_job_exit_code"] = exit_code
    artifact["evidence_trigger_mode"] = trigger_mode
    artifact["evidence_job_finished_at"] = (
        artifact["fetched_at"] + timedelta(seconds=1) if finished_at else None
    )

    with pytest.raises(RuntimeError, match="is not verified live HTTP"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )


def test_repository_verifier_normalizes_invalid_nested_parser_context() -> None:
    fixture = _repository_verification_fixture()
    fixture["artifacts"][0]["parser_context_json"] = json.dumps({"group_key": "not-an-object"})

    with pytest.raises(RuntimeError, match="parser context is invalid"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )


def test_repository_verifier_rejects_context_hash_and_fixture_mutations() -> None:
    context_fixture = _repository_verification_fixture()
    context_fixture["artifacts"][0]["parser_context_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="parser context hash mismatch"):
        context_fixture["repository"]._calculate_run_evidence(
            17,
            23,
            context_fixture["run_started_at"],
            1,
        )

    source_fixture = _repository_verification_fixture()
    source_fixture["artifacts"][0]["capture_kind"] = "fixture"
    with pytest.raises(RuntimeError, match="is not verified live HTTP"):
        source_fixture["repository"]._calculate_run_evidence(
            17,
            23,
            source_fixture["run_started_at"],
            1,
        )


def test_repository_verifier_rejects_cross_run_part_membership() -> None:
    fixture = _repository_verification_fixture()
    fixture["record_rows"][-1]["part_id"] = 202

    with pytest.raises(RuntimeError, match="HTTP evidence coverage mismatch"):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            1,
        )


def test_repository_verifier_rejects_exact_9999_of_10000_coverage() -> None:
    fixture = _repository_verification_fixture()
    parent_rows = fixture["record_rows"][:5]
    group_key = _formal_group_key()
    part_records = [
        record_evidence(
            "part",
            {"group": group_key, "part_number": f"PN-{index:05d}", "range_str": ""},
            {"group": group_key, "part_number": f"PN-{index:05d}"},
            parent_natural_key=group_key,
        )
        for index in range(9_999)
    ]
    fixture["record_rows"][:] = [
        *parent_rows,
        *[
            {
                "artifact_id": 1,
                "record_type": record.record_type,
                "natural_key_sha256": record.natural_key_sha256,
                "parent_natural_key_sha256": record.parent_natural_key_sha256,
                "record_sha256": record.record_sha256,
                "accepted": 1,
                "part_id": index + 1,
            }
            for index, record in enumerate(part_records)
        ],
    ]
    fixture["source_rows"][:] = [{"id": index + 1} for index in range(10_000)]

    with pytest.raises(
        RuntimeError,
        match=r"source=10000, accepted=9999, target=10000",
    ):
        fixture["repository"]._calculate_run_evidence(
            17,
            23,
            fixture["run_started_at"],
            10_000,
        )


def test_evidence_schema_supports_exact_root_url_and_mutation_identity() -> None:
    for relative_path in ("db/catalog.sql", "migrations/catalog/017_partsouq_http_evidence.sql"):
        schema = (Path(__file__).parents[1] / relative_path).read_text()
        assert "public_source_url = 'https://partsouq.com/en/catalog/genuine'" in schema
        identity_start = schema.index("UNIQUE KEY uq_partsouq_artifact_identity")
        identity_end = schema.index(")", identity_start)
        identity = schema[identity_start:identity_end]
        assert "scheduled_job_run_id" in identity
        assert "body_sha256" in identity
        assert "parser_context_sha256" in identity

    migration = (
        Path(__file__).parents[1] / "migrations/catalog/020_artifact_sanitizer_version.sql"
    ).read_text()
    catalog = (Path(__file__).parents[1] / "db/catalog.sql").read_text()
    for schema in (migration, catalog):
        assert "sanitizer_version VARCHAR(64) NOT NULL" in schema
        assert "chk_partsouq_artifact_sanitizer" in schema
    assert SANITIZER_VERSION in migration


def test_formal_bounded_view_requires_verified_live_evidence_and_exact_coverage() -> None:
    for relative_path in (
        "db/admin.sql",
        "migrations/catalog/019_verified_bounded_catalog_view.sql",
    ):
        schema = (Path(__file__).parents[1] / relative_path).read_text()
        assert "verified_bounded_evidence AS" in schema
        assert "verified_bounded_records AS" in schema
        assert "current_run.evidence_status = 'verified'" in schema
        assert "current_run.evidence_record_count = 10000" in schema
        assert "verified_evidence.live_artifact_count = verified_evidence.artifact_count" in schema
        assert "verified_evidence.page_type_count = 6" in schema
        assert "verified_records.accepted_part_count = current_run.evidence_record_count" in schema
        assert "SELECT DISTINCT crawl_run_id" in schema
        assert "artifact.crawl_run_id = active_snapshot.crawl_run_id" in schema
        assert "record.crawl_run_id = active_record_snapshot.crawl_run_id" in schema
        assert "FORCE INDEX (idx_partsouq_artifact_run_status)" in schema
        assert "FORCE INDEX (idx_partsouq_record_run_accepted)" in schema
        assert "evidence_job.trigger_mode = 'daemon'" in schema
        assert "artifact.scheduled_job_run_id = evidence_run.scheduled_job_run_id" in schema
        assert "evidence_job.status = 'completed'" in schema
        assert "artifact.scheduled_job_run_id <> evidence_run.scheduled_job_run_id" in schema
        assert "evidence_job.status = 'failed'" in schema
        assert "evidence_job.exit_code <> 0" in schema
        assert "artifact.fetched_at >= evidence_run.started_at" in schema
        assert "artifact.fetched_at >= evidence_job.started_at" in schema
        assert "artifact.fetched_at <= evidence_job.finished_at + INTERVAL 5 MINUTE" in schema
        assert "artifact.http_status = 200" in schema
        assert "artifact.challenge_detected = 0" in schema
        assert "LOWER(artifact.content_type) LIKE 'text/html%'" in schema
        assert "artifact.malformed_row_count = 0" in schema


def test_bounded_publish_checks_sealed_evidence_before_snapshot_mutation() -> None:
    database = mock.MagicMock()
    run = {
        "run_key": "bounded-evidence-test",
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "status": "running",
        "started_at": datetime(2026, 8, 22, 12, 0, 0),
        "scheduled_job_run_id": 23,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "running",
        "scheduled_crawl_count": 1,
        "evidence_status": "verified",
        "evidence_manifest_sha256": "a" * 64,
        "evidence_dataset_sha256": "b" * 64,
        "evidence_artifact_count": 1,
        "evidence_record_count": 10_000,
        "evidence_original_bytes": 1,
        "evidence_stored_bytes": 1,
        "evidence_verified_at": datetime(2026, 8, 22, 12, 1, 0),
    }

    def execute(sql, _params=()):
        cursor = mock.MagicMock()
        normalized = " ".join(sql.split())
        if "FROM crawl_runs AS cr" in normalized:
            cursor.fetchone.return_value = run
            return cursor
        if "FROM crawl_state" in normalized:
            cursor.fetchall.return_value = []
            return cursor
        if normalized == "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s":
            cursor.fetchone.return_value = {"row_count": 10_000}
            return cursor
        raise AssertionError(f"snapshot mutated before evidence gate: {normalized}")

    database._execute.side_effect = execute
    repository = CrawlRepository(database, "bounded-evidence-test")
    with (
        mock.patch.object(
            repository,
            "_assert_verified_run_evidence",
            side_effect=RuntimeError("manifest mutation"),
        ) as evidence_gate,
        mock.patch.object(repository, "_assert_bounded_group_receipts") as receipt_gate,
        pytest.raises(RuntimeError, match="manifest mutation"),
    ):
        repository.publish_bounded_parts(17, 10_000)

    evidence_gate.assert_called_once_with(17, run, 10_000)
    receipt_gate.assert_not_called()
    executed_sql = [" ".join(call.args[0].split()) for call in database._execute.call_args_list]
    assert not any(sql.startswith("DELETE FROM bounded_parts") for sql in executed_sql)


def test_bounded_publish_revalidates_the_persisted_snapshot_evidence_binding() -> None:
    database = mock.MagicMock()
    run = {
        "run_key": "bounded-snapshot-binding",
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "status": "running",
        "started_at": datetime(2026, 8, 22, 12, 0, 0),
        "scope_brand": "toyota",
        "scope_model": "tacoma",
        "scope_vehicle_year_floor": 2006,
        "scheduled_job_run_id": 23,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "running",
        "scheduled_crawl_count": 1,
        "evidence_status": "verified",
        "evidence_manifest_sha256": "a" * 64,
        "evidence_dataset_sha256": "b" * 64,
        "evidence_artifact_count": 1,
        "evidence_record_count": 10_000,
        "evidence_original_bytes": 1,
        "evidence_stored_bytes": 1,
        "evidence_verified_at": datetime(2026, 8, 22, 12, 1, 0),
    }

    def execute(sql: str, _params=()):
        cursor = mock.MagicMock()
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT cr.run_key"):
            cursor.fetchone.return_value = run
        elif normalized.startswith("SELECT id FROM crawl_state"):
            cursor.fetchall.return_value = []
        elif (
            normalized == "SELECT COUNT(*) AS row_count FROM parts WHERE seen_run_id = %s"
            or normalized.startswith("SELECT COUNT(*) AS row_count FROM parts AS part")
        ):
            cursor.fetchone.return_value = {"row_count": 10_000}
        elif normalized.startswith(
            "SELECT scope_brand, scope_model, scope_vehicle_year_floor "
            "FROM catalog_desired_bounded_scope"
        ):
            cursor.fetchone.return_value = {
                "scope_brand": "toyota",
                "scope_model": "tacoma",
                "scope_vehicle_year_floor": 2006,
            }
        elif normalized == "DELETE FROM bounded_parts":
            pass
        elif normalized.startswith("INSERT INTO bounded_parts"):
            cursor.rowcount = 10_000
        elif normalized.startswith("SELECT COUNT(*) AS row_count, COUNT(DISTINCT crawl_run_id)"):
            cursor.fetchone.return_value = {
                "row_count": 10_000,
                "run_count": 1,
                "min_run_id": 17,
                "max_run_id": 17,
            }
        elif normalized.startswith("SELECT COUNT(*) AS invalid_rows FROM bounded_parts"):
            cursor.fetchone.return_value = {"invalid_rows": 0}
        else:
            raise AssertionError(f"unexpected bounded publish query: {normalized}")
        return cursor

    database._execute.side_effect = execute
    repository = CrawlRepository(database, "bounded-snapshot-binding")
    with (
        mock.patch.object(repository, "_assert_verified_run_evidence") as evidence_gate,
        mock.patch.object(repository, "_assert_bounded_group_receipts") as receipt_gate,
    ):
        assert repository.publish_bounded_parts(17, 10_000) == 10_000

    evidence_gate.assert_has_calls(
        [
            mock.call(17, run, 10_000),
            mock.call(17, run, 10_000, use_bounded_snapshot=True),
        ]
    )
    assert evidence_gate.call_count == 2
    receipt_gate.assert_called_once_with(17, 10_000)
    insert_sql = next(
        " ".join(call.args[0].split())
        for call in database._execute.call_args_list
        if "INSERT INTO bounded_parts" in call.args[0]
    )
    assert "evidence_record_sha256" in insert_sql
    assert "evidence_record.record_sha256" in insert_sql
    assert "JOIN partsouq_artifact_records AS evidence_record" in insert_sql


def test_formal_bounded_crawler_seals_evidence_before_publish(monkeypatch) -> None:
    monkeypatch.setattr("partsouq_catalog.crawler.catalog_writer_admission", nullcontext)
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 10_000)
    monkeypatch.setitem(CRAWL, "bounded_run_key", "")
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 77)
    monkeypatch.setitem(CRAWL, "min_brands", 1)
    for key in ("start_brand", "limit_brands", "limit_models", "limit_vehicles", "limit_groups"):
        monkeypatch.setitem(CRAWL, key, "" if key == "start_brand" else 0)

    crawler = Crawler(mock.MagicMock(), mock.MagicMock(), workers=4)
    crawler.crawl = mock.MagicMock()
    crawler.crawl.start_run.return_value = 17
    crawler.crawl.run_status.return_value = "running"
    crawler.crawl.count_run_parts.return_value = 10_000
    crawler.crawl.discard_invalid_bounded_membership.return_value = 0
    crawler.crawl.resumable_bounded_run_key.return_value = None
    crawler.crawl.purge_legacy_vehicle_state.return_value = 0
    crawler.crawl.count_failures.return_value = 0
    crawler.crawl.count_quarantined.return_value = 0
    try:
        crawler.run()
    finally:
        crawler.close()

    crawler.crawl.verify_run_evidence.assert_called_once_with(17)
    crawler.crawl.publish_bounded_parts.assert_called_once_with(17, 10_000)
    assert crawler.crawl.method_calls.index(mock.call.verify_run_evidence(17)) < (
        crawler.crawl.method_calls.index(mock.call.publish_bounded_parts(17, 10_000))
    )


def test_completed_evidence_audit_rechecks_sealed_run_without_writing() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "started_at": datetime(2026, 8, 22, 12, 0, 0),
        "finished_at": datetime(2026, 8, 22, 13, 0, 0),
        "status": "bounded_success",
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "scheduled_job_run_id": 23,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "completed",
        "scheduled_job_exit_code": 0,
        "scheduled_crawl_count": 1,
        "evidence_status": "verified",
        "evidence_manifest_sha256": "a" * 64,
        "evidence_dataset_sha256": "b" * 64,
        "evidence_artifact_count": 250,
        "evidence_record_count": 10_000,
        "evidence_original_bytes": 1_000_000,
        "evidence_stored_bytes": 250_000,
        "evidence_verified_at": datetime(2026, 8, 22, 12, 59, 0),
    }
    database._execute.return_value = cursor
    repository = CrawlRepository(database, "evidence-audit-test")

    with mock.patch.object(repository, "_assert_verified_run_evidence") as replay_gate:
        result = repository.audit_run_evidence(17)

    replay_gate.assert_called_once_with(
        17,
        cursor.fetchone.return_value,
        10_000,
        allow_failed_current_scheduler=False,
        use_bounded_snapshot=True,
    )
    assert result == {
        "run_id": 17,
        "expected_parts": 10_000,
        "artifact_count": 250,
        "record_count": 10_000,
        "manifest_sha256": "a" * 64,
        "dataset_sha256": "b" * 64,
        "verified": True,
    }
    assert database._execute.call_count == 1


def test_interrupted_recovery_evidence_audit_allows_running_scheduler() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "started_at": datetime(2026, 8, 22, 12, 0, 0),
        "finished_at": datetime(2026, 8, 22, 13, 0, 0),
        "status": "bounded_success",
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "scheduled_job_run_id": 23,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "running",
        "scheduled_job_exit_code": None,
        "scheduled_crawl_count": 1,
        "evidence_status": "verified",
        "evidence_manifest_sha256": "a" * 64,
        "evidence_dataset_sha256": "b" * 64,
        "evidence_artifact_count": 250,
        "evidence_record_count": 10_000,
        "evidence_original_bytes": 1_000_000,
        "evidence_stored_bytes": 250_000,
        "evidence_verified_at": datetime(2026, 8, 22, 12, 59, 0),
    }
    database._execute.return_value = cursor
    repository = CrawlRepository(database, "evidence-recovery-test")

    with mock.patch.object(repository, "_assert_verified_run_evidence") as replay_gate:
        result = repository.audit_run_evidence(
            17,
            10_000,
            allow_running_scheduler=True,
        )

    replay_gate.assert_called_once_with(
        17,
        cursor.fetchone.return_value,
        10_000,
        allow_failed_current_scheduler=False,
        use_bounded_snapshot=True,
    )
    assert result["verified"] is True


def test_interrupted_recovery_evidence_audit_allows_failed_scheduler() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "started_at": datetime(2026, 8, 22, 12, 0, 0),
        "finished_at": datetime(2026, 8, 22, 13, 0, 0),
        "status": "bounded_success",
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "scheduled_job_run_id": 23,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "failed",
        "scheduled_job_exit_code": 125,
        "scheduled_job_finished_at": datetime(2026, 8, 22, 13, 0, 1),
        "scheduled_crawl_count": 1,
        "evidence_status": "verified",
        "evidence_manifest_sha256": "a" * 64,
        "evidence_dataset_sha256": "b" * 64,
        "evidence_artifact_count": 250,
        "evidence_record_count": 10_000,
        "evidence_original_bytes": 1_000_000,
        "evidence_stored_bytes": 250_000,
        "evidence_verified_at": datetime(2026, 8, 22, 12, 59, 0),
    }
    database._execute.return_value = cursor
    repository = CrawlRepository(database, "evidence-failed-recovery-test")

    with pytest.raises(RuntimeError, match="no completed scheduler provenance"):
        repository.audit_run_evidence(17, 10_000)

    with mock.patch.object(repository, "_assert_verified_run_evidence") as replay_gate:
        result = repository.audit_run_evidence(
            17,
            10_000,
            allow_failed_scheduler=True,
        )

    replay_gate.assert_called_once_with(
        17,
        cursor.fetchone.return_value,
        10_000,
        allow_failed_current_scheduler=True,
        use_bounded_snapshot=True,
    )
    assert result["verified"] is True


def test_completed_evidence_audit_rejects_running_scheduler_by_default() -> None:
    database = mock.MagicMock()
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = {
        "started_at": datetime(2026, 8, 22, 12, 0, 0),
        "finished_at": datetime(2026, 8, 22, 13, 0, 0),
        "status": "bounded_success",
        "dataset_kind": "bounded",
        "target_parts": 10_000,
        "scheduled_job_run_id": 23,
        "scheduled_job_name": "catalog",
        "scheduled_trigger_mode": "daemon",
        "scheduled_job_status": "running",
        "scheduled_job_exit_code": None,
        "scheduled_crawl_count": 1,
    }
    database._execute.return_value = cursor
    repository = CrawlRepository(database, "evidence-audit-test")

    with pytest.raises(RuntimeError, match="no completed scheduler provenance"):
        repository.audit_run_evidence(17)


def test_evidence_cli_returns_success_only_after_repository_audit(monkeypatch, capsys) -> None:
    from partsouq_catalog import config as config_module
    from partsouq_catalog import db as db_module
    from partsouq_catalog import evidence as evidence_module
    from partsouq_catalog import repositories as repositories_module

    database = mock.MagicMock()
    database_factory = mock.MagicMock()
    database_factory.return_value.connect.return_value = database
    repository = mock.MagicMock()
    repository.audit_run_evidence.return_value = {
        "run_id": 17,
        "expected_parts": 10_000,
        "verified": True,
    }
    repository_factory = mock.MagicMock(return_value=repository)
    monkeypatch.setitem(config_module.DB_CONFIG, "database", "partsouq_catalog")
    monkeypatch.setattr(db_module, "Database", database_factory)
    monkeypatch.setattr(repositories_module, "CrawlRepository", repository_factory)
    monkeypatch.setattr("sys.argv", ["partsouq-evidence", "--run-id", "17"])

    assert evidence_module.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "database": "partsouq_catalog",
        "run_id": 17,
        "expected_parts": 10_000,
        "verified": True,
    }
    repository.audit_run_evidence.assert_called_once_with(17, 10_000)
    database.rollback.assert_called_once_with()
    database.close.assert_called_once_with()


def test_evidence_cli_returns_failure_when_repository_audit_rejects_run(
    monkeypatch, capsys
) -> None:
    from partsouq_catalog import config as config_module
    from partsouq_catalog import db as db_module
    from partsouq_catalog import evidence as evidence_module
    from partsouq_catalog import repositories as repositories_module

    database = mock.MagicMock()
    database_factory = mock.MagicMock()
    database_factory.return_value.connect.return_value = database
    repository = mock.MagicMock()
    repository.audit_run_evidence.side_effect = RuntimeError("manifest mutation")
    monkeypatch.setitem(config_module.DB_CONFIG, "database", "partsouq_catalog")
    monkeypatch.setattr(db_module, "Database", database_factory)
    monkeypatch.setattr(
        repositories_module,
        "CrawlRepository",
        mock.MagicMock(return_value=repository),
    )
    monkeypatch.setattr("sys.argv", ["partsouq-evidence", "--run-id", "17"])

    assert evidence_module.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "database": "partsouq_catalog",
        "run_id": 17,
        "verified": False,
        "error": "manifest mutation",
    }
    database.rollback.assert_called_once_with()
    database.close.assert_called_once_with()


def test_evidence_cli_rejects_non_production_database_before_connect(monkeypatch, capsys) -> None:
    from partsouq_catalog import config as config_module
    from partsouq_catalog import db as db_module
    from partsouq_catalog import evidence as evidence_module

    database_factory = mock.MagicMock()
    monkeypatch.setitem(config_module.DB_CONFIG, "database", "partsouq_staging")
    monkeypatch.setattr(db_module, "Database", database_factory)
    monkeypatch.setattr("sys.argv", ["partsouq-evidence", "--run-id", "17"])

    assert evidence_module.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "database": "partsouq_staging",
        "run_id": 17,
        "verified": False,
        "error": "formal evidence audit requires database partsouq_catalog",
    }
    database_factory.assert_not_called()
