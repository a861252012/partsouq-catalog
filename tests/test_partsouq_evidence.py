from __future__ import annotations

import hashlib

import pytest

from partsouq_catalog.evidence import (
    REDACTED_VALUE,
    assert_no_secret_material,
    canonical_json_bytes,
    dataset_sha256,
    public_source_url,
    record_evidence,
    restore_sanitized_body,
    sanitize_parser_html,
)
from partsouq_catalog.parsers import parse_parts


def test_public_source_url_discards_ephemeral_and_unknown_query_values() -> None:
    url = (
        "https://www.partsouq.com/en/catalog/genuine/unit?"
        "ssd=secret-token&uid=123&cid=45&future_session=also-secret&vid=67"
    )

    assert public_source_url(url) == (
        "https://partsouq.com/en/catalog/genuine/unit?cid=45&uid=123&vid=67"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://partsouq.com/en/catalog/genuine",
        "https://evil.example/en/catalog/genuine",
        "https://user:password@partsouq.com/en/catalog/genuine",
        "https://partsouq.com/en/search/all?q=123",
    ],
)
def test_public_source_url_rejects_non_catalog_origins(url: str) -> None:
    with pytest.raises(ValueError):
        public_source_url(url)


def test_record_evidence_canonicalizes_parser_relative_catalog_url() -> None:
    relative = record_evidence(
        "brand",
        {"name": "TOYOTA"},
        {"name": "TOYOTA", "url": "/en/catalog/genuine/locate?c=TOYOTA"},
    )
    absolute = record_evidence(
        "brand",
        {"name": "TOYOTA"},
        {"name": "TOYOTA", "url": "https://partsouq.com/en/catalog/genuine/locate?c=TOYOTA"},
    )

    assert relative.record_sha256 == absolute.record_sha256


def test_sanitized_body_is_secret_free_and_replays_parser_identically() -> None:
    html = """
    <html><head>
      <meta http-equiv="set-cookie" content="PHPSESSID=session-value">
      <script>window.__cf_chl_token = "cloudflare-secret";</script>
    </head><body>
      <!-- cf_clearance=comment-secret -->
      <input type="hidden" name="ssd" value="hidden-secret">
      <a href="/en/catalog/genuine/unit?uid=123&amp;ssd=url-secret">unit</a>
      <table>
        <thead><tr>
          <th>Number</th><th>Name</th><th>Code</th><th>Quantity</th>
          <th>Unified</th><th>Note</th>
        </tr></thead>
        <tbody><tr>
          <td><a href="/en/search/all?q=ABC123">ABC123</a></td>
          <td>ENGINE BRACKET</td><td>E10</td><td>01</td><td>U-A</td><td>FIT</td>
        </tr></tbody>
      </table>
    </body></html>
    """

    live_records, live_malformed, live_skipped, live_skipped_rows = parse_parts(
        html, diagnostics=True
    )
    sanitized = sanitize_parser_html(html)
    replay_html = restore_sanitized_body("zlib", sanitized.compressed).decode()
    replay_records, replay_malformed, replay_skipped, replay_skipped_rows = parse_parts(
        replay_html, diagnostics=True
    )

    assert (
        live_records,
        live_malformed,
        live_skipped,
        live_skipped_rows,
    ) == (
        replay_records,
        replay_malformed,
        replay_skipped,
        replay_skipped_rows,
    )
    assert sanitized.body_sha256 == hashlib.sha256(sanitized.body).hexdigest()
    assert sanitized.original_bytes == len(sanitized.body)
    assert sanitized.stored_bytes == len(sanitized.compressed)
    assert REDACTED_VALUE.encode() in sanitized.body
    for secret in (
        b"session-value",
        b"cloudflare-secret",
        b"comment-secret",
        b"hidden-secret",
        b"url-secret",
    ):
        assert secret not in sanitized.body
    assert_no_secret_material(sanitized.body)


def test_restore_rejects_unknown_compression() -> None:
    with pytest.raises(ValueError, match="unsupported evidence compression"):
        restore_sanitized_body("gzip", b"not-used")


def test_canonical_hashes_are_unicode_normalized_order_independent_and_sensitive() -> None:
    composed = {"name": "CAFÉ", "number": "ABC123"}
    decomposed_reordered = {"number": "ABC123", "name": "CAFE\u0301"}
    first = record_evidence("part", {"number": "ABC123"}, composed)
    second = record_evidence("part", {"number": "ABC123"}, decomposed_reordered)
    changed = record_evidence("part", {"number": "ABC123"}, {**composed, "name": "OTHER"})

    assert canonical_json_bytes(composed) == canonical_json_bytes(decomposed_reordered)
    assert first == second
    assert first.record_sha256 != changed.record_sha256
    assert dataset_sha256([first, changed]) == dataset_sha256([changed, first])


def test_record_hash_never_includes_secret_fields_or_source_url_tokens() -> None:
    record = {
        "number": "ABC123",
        "source_url": ("https://partsouq.com/en/catalog/genuine/unit?uid=123&ssd=token-value"),
        "cookie": "cookie-value",
    }

    evidence = record_evidence("part", {"number": "ABC123"}, record)

    assert (
        evidence.record_sha256
        == record_evidence(
            "part",
            {"number": "ABC123"},
            {
                "number": "ABC123",
                "source_url": "https://partsouq.com/en/catalog/genuine/unit?uid=123",
            },
        ).record_sha256
    )
