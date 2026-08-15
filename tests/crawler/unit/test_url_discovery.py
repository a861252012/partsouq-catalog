from partsouq_crawler.crawl.discovery import discover_links, is_in_scope, normalize_url


def test_url_normalization_preserves_ssd_and_query_order() -> None:
    url = "HTTPS://PartSouq.com:443/en/catalog/genuine/parts?ssd=a%2Bb&uid=2&vid=1#tab"
    normalized = normalize_url(url)
    assert normalized == "https://partsouq.com/en/catalog/genuine/parts?ssd=a%2Bb&uid=2&vid=1"


def test_different_query_order_is_not_deduplicated() -> None:
    first = normalize_url("https://partsouq.com/en/catalog/genuine?ssd=x&uid=1")
    second = normalize_url("https://partsouq.com/en/catalog/genuine?uid=1&ssd=x")
    assert first != second


def test_default_port_removed_but_non_default_kept() -> None:
    assert normalize_url("http://EXAMPLE.com:80/a") == "http://example.com/a"
    assert normalize_url("http://EXAMPLE.com:8080/a") == "http://example.com:8080/a"


def test_html_link_discovery_all_supported_attributes() -> None:
    body = b"""
    <html><head><meta http-equiv="refresh" content="0; URL=/meta"></head><body>
      <a href="/href">a</a><div data-href="/data-href"></div>
      <div data-url="/data-url"></div><form action="/form"></form>
      <select><option value="/option">x</option></select>
      <script>window.location = '/location';</script>
    </body></html>
    """
    links = discover_links(body, "https://partsouq.com/en/catalog/genuine")
    expected = {
        f"https://partsouq.com/{name}"
        for name in ("href", "data-href", "data-url", "form", "option", "meta", "location")
    }
    assert links == expected


def test_discovery_ignores_fragment_javascript_and_mailto() -> None:
    body = b'<a href="#x"></a><a href="javascript:void(0)"></a><a href="mailto:a@b.c"></a>'
    assert discover_links(body, "https://partsouq.com/en/catalog/genuine") == set()


def test_partsouq_scope_allows_catalog_search_and_sitemap() -> None:
    seed = "https://partsouq.com/en/catalog/genuine"
    assert is_in_scope("https://partsouq.com/en/catalog/genuine/parts?ssd=x", seed)
    assert is_in_scope("https://partsouq.com/en/search/all?q=1", seed)
    assert is_in_scope("https://partsouq.com/catalog.xml.gz", seed)


def test_partsouq_scope_rejects_cart_assets_and_other_hosts() -> None:
    seed = "https://partsouq.com/en/catalog/genuine"
    assert not is_in_scope("https://partsouq.com/cart", seed)
    assert not is_in_scope("https://partsouq.com/app.js", seed)
    assert not is_in_scope("https://example.com/en/catalog/genuine", seed)


def test_local_fake_scope_accepts_same_host_paths() -> None:
    seed = "http://127.0.0.1:8000/"
    assert is_in_scope("http://127.0.0.1:8000/arbitrary/1", seed)
    assert not is_in_scope("http://127.0.0.1:8001/arbitrary/1", seed)
