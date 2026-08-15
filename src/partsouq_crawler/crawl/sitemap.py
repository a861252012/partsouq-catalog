from __future__ import annotations

import gzip
from dataclasses import dataclass

from lxml import etree


@dataclass(frozen=True, slots=True)
class SitemapResult:
    urls: tuple[str, ...]
    nested_sitemaps: tuple[str, ...]


def parse_sitemap(body: bytes, *, compressed: bool = False) -> SitemapResult:
    if compressed or body.startswith(b"\x1f\x8b"):
        body = gzip.decompress(body)
    root = etree.fromstring(body)
    name = etree.QName(root).localname.lower()
    locations = tuple(
        str(value).strip()
        for value in root.xpath("//*[local-name()='loc']/text()")
        if str(value).strip()
    )
    if name == "sitemapindex":
        return SitemapResult((), locations)
    if name == "urlset":
        return SitemapResult(locations, ())
    raise ValueError(f"unsupported sitemap root: {name}")
