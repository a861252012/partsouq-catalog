"""品牌來源測試：/en/brands-16.html 總覽頁優先、首頁側欄後備。

背景：站方首頁側欄只列浮動子集（2026-09 實測 16 個且缺 Toyota/Kia），
full crawl 只依賴首頁會永遠爬不到完整品牌面。總覽頁失效時必須退回
首頁維持 fail-closed，不得讓整趟 run 直接死在品牌清單上。
"""

from unittest import mock

from partsouq_catalog.config import CRAWL, SITE
from partsouq_catalog.crawler import Crawler
from partsouq_catalog.evidence import public_source_url
from partsouq_catalog.parsers import parse_brands

BRANDS_PAGE_HTML = (
    '<li><a href="/en/brands-16.html"><span class="glyphicon glyphicon-th-list">'
    "</span>Brands</a></li>"
    '<li class="nav-header">Genuine Catalogs</li>'
    '<li><a href="/en/catalog/genuine/locate?c=Toyota"><i class="icon-ps-TOYOTA"></i>Toyota</a></li>'
    '<li><a href="/en/catalog/genuine/locate?c=Lexus"><i class="icon-ps-LEXUS"></i>Lexus</a></li>'
    '<li><a href="/en/catalog/genuine/locate?c=Kia"><i class="icon-ps-KIA"></i>Kia</a></li>'
    '<li><a href="/en/catalog/genuine/locate?c=Ram"><i class="icon-ps-RAM"></i>Ram</a></li>'
    '<li><a href="/en/catalog/genuine/locate?c=Lexus"><i class="icon-ps-LEXUS"></i></a></li>'
)

GENUINE_PAGE_HTML = (
    '<li><a href="/en/catalog/genuine/locate?c=Lexus">Lexus</a></li>'
    '<li><a href="/en/catalog/genuine/locate?c=Nissan">Nissan</a></li>'
)


def test_parse_brands_reads_brands_overview_page() -> None:
    """總覽頁的品牌錨（含圖標無文字的重複錨）解析成去重後的品牌清單。"""

    brands, malformed = parse_brands(BRANDS_PAGE_HTML, diagnostics=True)

    assert malformed == 0
    assert [b["name"] for b in brands] == ["Toyota", "Lexus", "Kia", "Ram"]


def test_public_source_url_accepts_brands_overview_path() -> None:
    """總覽頁 URL 必須能進證據鏈（replay 輸入證據的正規化來源位址）。"""

    url = "https://partsouq.com/en/brands-16.html"

    assert public_source_url(url) == url


def _make_crawler(monkeypatch) -> Crawler:
    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    return instance


def test_brands_prefers_overview_page(monkeypatch) -> None:
    """總覽頁可解析時不應請求首頁。"""
    instance = _make_crawler(monkeypatch)
    instance.http.ensure_fresh = mock.MagicMock()
    instance.http.get_response = mock.MagicMock(
        side_effect=lambda url: mock.MagicMock(
            text=BRANDS_PAGE_HTML if url == SITE["brands"] else GENUINE_PAGE_HTML
        )
    )

    brands = instance._brands()

    assert [b["name"] for b in brands] == ["Toyota", "Lexus", "Kia", "Ram"]
    instance.http.get_response.assert_called_once_with(SITE["brands"])
    instance.close()


def test_brands_falls_back_to_genuine_index(monkeypatch) -> None:
    """總覽頁請求失敗（挑戰／404）時退回首頁側欄，不讓品牌清單直接死。"""
    instance = _make_crawler(monkeypatch)
    instance.http.ensure_fresh = mock.MagicMock()
    responses = {
        SITE["brands"]: RuntimeError("http 403 challenge at /en/brands-16.html"),
        SITE["genuine"]: GENUINE_PAGE_HTML,
    }

    def _get(url):
        payload = responses[url]
        if isinstance(payload, Exception):
            raise payload
        return mock.MagicMock(text=payload)

    instance.http.get_response = mock.MagicMock(side_effect=_get)

    brands = instance._brands()

    assert [b["name"] for b in brands] == ["Lexus", "Nissan"]
    instance.http.get_response.assert_any_call(SITE["brands"])
    instance.http.get_response.assert_any_call(SITE["genuine"])
    instance.close()


def test_brands_refuses_when_both_sources_fail(monkeypatch) -> None:
    """兩個來源都失敗時維持 fail-closed：拋最後一個錯誤，不回空清單。"""
    instance = _make_crawler(monkeypatch)
    instance.http.ensure_fresh = mock.MagicMock()
    instance.http.get_response = mock.MagicMock(side_effect=RuntimeError("challenge"))

    try:
        instance._brands()
    except RuntimeError as exc:
        assert "challenge" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when both brand sources fail")
    instance.close()
