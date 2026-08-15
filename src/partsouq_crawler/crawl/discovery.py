from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import html

LOCATION_RE = re.compile(
    r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
META_REFRESH_RE = re.compile(r"(?:^|;)\s*url\s*=\s*['\"]?([^'\";]+)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, parts.query, ""))


def discover_links(body: bytes, base_url: str, charset: str = "utf-8") -> set[str]:
    try:
        text = body.decode(charset, errors="replace")
        document = html.fromstring(text, base_url=base_url)
    except (LookupError, ValueError):
        return set()

    raw_links: list[str] = []
    for xpath in ("//@href", "//@data-href", "//@data-url", "//form/@action", "//option/@value"):
        raw_links.extend(str(value) for value in document.xpath(xpath))
    for content in document.xpath(
        "//meta[translate(@http-equiv, 'REFSH', 'refsh')='refresh']/@content"
    ):
        match = META_REFRESH_RE.search(str(content))
        if match:
            raw_links.append(match.group(1).strip())
    raw_links.extend(LOCATION_RE.findall(text))
    return _resolve_links(raw_links, base_url)


def _resolve_links(values: Iterable[str], base_url: str) -> set[str]:
    links: set[str] = set()
    for value in values:
        value = value.strip()
        if not value or value.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        resolved = urljoin(base_url, value)
        if urlsplit(resolved).scheme in {"http", "https"}:
            links.add(normalize_url(resolved))
    return links


def is_in_scope(url: str, seed_url: str) -> bool:
    target = urlsplit(url)
    seed = urlsplit(seed_url)
    if target.hostname != seed.hostname or target.port != seed.port:
        return False
    if seed.hostname not in {"partsouq.com", "www.partsouq.com"}:
        return target.scheme in {"http", "https"}
    if target.path in {"/robots.txt", "/sitemap.xml"} or target.path.endswith((".xml", ".xml.gz")):
        return True
    return target.path.startswith("/en/catalog/genuine") or target.path == "/en/search/all"
