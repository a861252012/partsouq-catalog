import gzip

import pytest

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


@pytest.mark.parametrize("rules_order", ["allow-first", "disallow-first"])
def test_robots_uses_most_specific_rule_independent_of_order(rules_order: str) -> None:
    rule_lines = ["Allow: /en/catalog", "Disallow: /en/catalog/private"]
    if rules_order == "disallow-first":
        rule_lines.reverse()
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        f"User-agent: *\n{'\n'.join(rule_lines)}\n".encode(),
    )

    assert not rules.allows("crawler", "https://partsouq.com/en/catalog/private")


@pytest.mark.parametrize("user_agent", ["*", "crawler"])
def test_robots_merges_duplicate_user_agent_groups(user_agent: str) -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        f"""User-agent: {user_agent}
Allow: /
User-agent: {user_agent}
Disallow: /en/catalog/private
""".encode(),
    )

    assert not rules.allows("crawler/1", "https://partsouq.com/en/catalog/private")


def test_robots_same_length_allow_wins() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"User-agent: *\nDisallow: /en/catalog\nAllow: /en/catalog\n",
    )

    assert rules.allows("crawler", "https://partsouq.com/en/catalog")


def test_robots_matches_exact_user_agent_case_insensitively_and_merges_groups() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"""User-agent: crawler
Disallow: /
User-agent: PARTSOUQ-CATALOG-CRAWLER
Allow: /en/catalog
User-agent: partsouq-catalog-crawler
Disallow: /en/catalog/private
User-agent: *
Disallow: /
""",
    )

    assert rules.allows("partsouq-catalog-crawler/1", "https://partsouq.com/en/catalog")
    assert not rules.allows(
        "partsouq-catalog-crawler/1",
        "https://partsouq.com/en/catalog/private",
    )


def test_robots_user_agent_substring_does_not_override_wildcard() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"""User-agent: catalog-crawler
Disallow: /
User-agent: *
Allow: /
""",
    )

    assert rules.allows("partsouq-catalog-crawler/1", "https://partsouq.com/en/catalog")


@pytest.mark.parametrize("directive", ["Allow", "Disallow"])
def test_robots_empty_access_directive_does_not_block(directive: str) -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        f"User-agent: *\n{directive}:\n".encode(),
    )

    assert rules.allows("crawler", "https://partsouq.com/en/catalog")


@pytest.mark.parametrize(
    ("invalid_rule", "valid_rule", "expected"),
    [
        ("Allow: *secret", "Disallow: /", False),
        ("Disallow: *secret", "Allow: /", True),
    ],
)
def test_robots_ignores_non_slash_path_pattern(
    invalid_rule: str,
    valid_rule: str,
    expected: bool,
) -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        f"User-agent: *\n{invalid_rule}\n{valid_rule}\n".encode(),
    )

    assert rules.allows("crawler", "https://partsouq.com/secret") is expected


def test_robots_other_record_does_not_split_user_agent_group() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"""User-agent: crawler
Crawl-delay: 10
User-agent: second-crawler
Disallow: /private
""",
    )

    assert not rules.allows("crawler", "https://partsouq.com/private")


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("/en/catalog/*/private$", "/en/catalog/car/private", False),
        ("/en/catalog/*/private$", "/en/catalog/car/private/child", True),
        ("/files/item-%2A", "/files/item-*", False),
        ("/en/%63atalog", "/en/catalog", False),
        ("/en/a%2Fb", "/en/a%2Fb", False),
        ("/en/a%2Fb", "/en/a/b", True),
        ("/", "?query=1", False),
        ("/query?url=https%3A%2F%2Ffoo.bar", "/query?url=https://foo.bar", False),
    ],
)
def test_robots_wildcard_anchor_and_percent_normalization(
    pattern: str,
    url: str,
    expected: bool,
) -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        f"User-agent: *\nDisallow: {pattern}\n".encode(),
    )

    assert rules.allows("crawler", f"https://partsouq.com{url}") is expected


def test_robots_unrelated_wildcard_rule_does_not_block() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"User-agent: *\nDisallow: /private/*\nAllow: /en/catalog\n",
    )

    assert rules.allows("crawler", "https://partsouq.com/en/catalog/genuine")


def test_robots_specificity_counts_percent_encoded_octets() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"User-agent: *\nAllow: /*%E3%83%84\nDisallow: /abcdef/\n",
    )

    assert not rules.allows("crawler", "https://partsouq.com/abcdef/%E3%83%84")


def test_robots_pattern_characters_in_comments_do_not_block() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"User-agent: *\nDisallow: /cart # literal * and $ in comment\n",
    )

    assert rules.allows("crawler", "https://partsouq.com/en/catalog/genuine")
    assert not rules.allows("crawler", "https://partsouq.com/cart")


def test_robots_pattern_for_unrelated_agent_does_not_block() -> None:
    rules = parse_robots(
        "https://partsouq.com/robots.txt",
        b"""User-agent: unrelated-bot
Disallow: /private/*

User-agent: *
Disallow: /cart
""",
    )

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
