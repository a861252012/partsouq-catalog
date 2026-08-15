from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from lxml import html

ROUTE_TYPES = {
    "locate": "locate",
    "pick": "pick",
    "vehicle": "vehicle",
    "groups": "groups",
    "unit": "unit",
    "parts": "parts",
    "diagram": "diagram",
}


def classify_page(url: str, body: bytes, charset: str = "utf-8") -> str:
    parts = urlsplit(url)
    if parts.path.endswith((".xml", ".xml.gz")) or "sitemap" in parts.path:
        return "sitemap"
    if parts.path == "/robots.txt":
        return "robots"
    if parts.path == "/en/search/all":
        return "search"
    route = parts.path.rstrip("/").rsplit("/", 1)[-1]
    if route in ROUTE_TYPES:
        return ROUTE_TYPES[route]

    try:
        text = body.decode(charset, errors="replace")
        document = html.fromstring(text)
    except (LookupError, ValueError):
        return "unknown"
    heading = " ".join(
        str(value) for value in document.xpath("//h1//text() | //h2//text()")
    ).lower()
    headers = " ".join(str(value) for value in document.xpath("//th//text()")).lower()
    labels = f"{heading} {headers}"
    if "part number" in labels or ("callout" in labels and "quantity" in labels):
        return "parts"
    if "diagram" in labels and "model" not in labels:
        return "diagram"
    if any(value in labels for value in ("prod period", "sales code", "chassis", "engine")):
        return "vehicle"
    if parse_qs(parts.query).get("q"):
        return "search"
    if parts.path.rstrip("/") == "/en/catalog/genuine":
        return "genuine_root"
    return "generic"
