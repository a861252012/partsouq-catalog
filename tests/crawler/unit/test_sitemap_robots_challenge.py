import gzip

from partsouq_crawler.crawl.challenge import detect_challenge
from partsouq_crawler.crawl.robots import parse_robots
from partsouq_crawler.crawl.sitemap import parse_sitemap


def test_sitemap_index() -> None:
    body = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://x/a.xml</loc></sitemap><sitemap><loc>https://x/b.xml.gz</loc></sitemap></sitemapindex>'
    result = parse_sitemap(body)
    assert result.urls == ()
    assert result.nested_sitemaps == ("https://x/a.xml", "https://x/b.xml.gz")


def test_sitemap_urlset() -> None:
    body = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x/a</loc></url><url><loc>https://x/b</loc></url></urlset>'
    result = parse_sitemap(body)
    assert result.urls == ("https://x/a", "https://x/b")
    assert result.nested_sitemaps == ()


def test_gzip_sitemap() -> None:
    body = gzip.compress(b"<urlset><url><loc>https://x/a</loc></url></urlset>")
    assert parse_sitemap(body).urls == ("https://x/a",)


def test_unsupported_sitemap_root_fails() -> None:
    try:
        parse_sitemap(b"<rss></rss>")
    except ValueError as error:
        assert "unsupported sitemap root" in str(error)
    else:
        raise AssertionError("unsupported sitemap was accepted")


def test_robots_parses_sitemap_and_disallow() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"User-agent: *\nDisallow: /cart\nSitemap: https://partsouq.com/sitemap.xml\n",
    )
    assert rules.sitemaps == ("https://partsouq.com/sitemap.xml",)
    assert rules.allows("crawler", "https://partsouq.com/en/catalog/genuine")
    assert not rules.allows("crawler", "https://partsouq.com/cart")


def test_cloudflare_header_detection() -> None:
    result = detect_challenge(403, {"cf-mitigated": "challenge"}, b"")
    assert result.challenged
    assert result.reason == "cloudflare_challenge"


def test_cloudflare_html_detection() -> None:
    result = detect_challenge(
        403,
        {"server": "cloudflare"},
        b"<title>Just a moment...</title>Enable JavaScript and cookies to continue",
    )
    assert result.challenged


def test_access_denied_detection() -> None:
    result = detect_challenge(403, {}, b"Access Denied")
    assert result.challenged
    assert result.reason == "access_denied"


def test_normal_403_is_not_automatically_cloudflare() -> None:
    assert not detect_challenge(403, {"server": "nginx"}, b"Forbidden").challenged
