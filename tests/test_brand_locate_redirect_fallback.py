"""品牌 locate 入口被 301 正規化時的 pick 層後備測試。

背景：站方把 locate?c=Toyota 永久 301 到品牌著陸頁
https://partsouq.com/catalog/toyota（實證 2026-09-02）。著陸頁沒有
型號清單（0 個 pick 錨點、parse_brand_index 解析 0 筆），品牌層重試
會無限失敗。同一份型號清單在 pick?c={brand}（不帶 model）可取得，
解析契約原樣複用；其餘轉址維持 fail-closed。
"""

from unittest import mock

import pytest

from partsouq_catalog.config import CRAWL
from partsouq_catalog.crawler import Crawler, _is_brand_landing_redirect
from partsouq_catalog.http_client import RobotsPolicyError

LOCATE_URL = "https://partsouq.com/en/catalog/genuine/locate?c=Toyota"
PICK_INDEX_URL = "https://partsouq.com/en/catalog/genuine/pick?c=Toyota"
LANDING_URL = "https://partsouq.com/catalog/toyota"

PICK_INDEX_HTML = (
    '<a href="/en/catalog/genuine/pick?c=Toyota&model=TACOMA&ssd=aaa">TACOMA</a>'
    '<a href="/en/catalog/genuine/pick?c=Toyota&model=COROLLA&ssd=bbb">COROLLA</a>'
)


def _make_crawler(monkeypatch) -> Crawler:
    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    return Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)


def test_is_brand_landing_redirect_accepts_single_segment_paths() -> None:
    """同域 /catalog/<單段>、無 query 與 fragment 才算品牌著陸頁。"""

    assert _is_brand_landing_redirect("https://partsouq.com/catalog/toyota")
    assert _is_brand_landing_redirect("/catalog/toyota")
    assert not _is_brand_landing_redirect("")
    assert not _is_brand_landing_redirect("https://partsouq.com/catalog/toyota/models")
    assert not _is_brand_landing_redirect("https://partsouq.com/catalog/?c=Toyota")
    assert not _is_brand_landing_redirect("https://evil.example.com/catalog/toyota")
    assert not _is_brand_landing_redirect("https://partsouq.com/en/catalog/genuine/locate")


def test_crawl_brand_falls_back_to_pick_index(monkeypatch) -> None:
    """locate 被 301 到品牌著陸頁時改抓 pick 層型號清單。"""

    instance = _make_crawler(monkeypatch)
    fetched: list[str] = []

    def _fetch(url):
        fetched.append(url)
        if url == LOCATE_URL:
            raise RobotsPolicyError(
                f"catalog redirect refused at {url} -> {LANDING_URL}",
                redirect_location=LANDING_URL,
            )
        return PICK_INDEX_HTML, None

    instance._fetch = mock.MagicMock(side_effect=_fetch)
    instance.crawl.is_done = mock.MagicMock(return_value=False)
    instance.crawl_model = mock.MagicMock(return_value=(0, True))

    failures = instance.crawl_brand("Toyota")

    assert failures == 0
    assert fetched == [LOCATE_URL, PICK_INDEX_URL]
    instance.crawl_model.assert_any_call("Toyota", mock.ANY, mock.ANY)
    instance.close()


def test_crawl_brand_refuses_non_landing_redirect(monkeypatch) -> None:
    """非品牌著陸頁的轉址維持 fail-closed，不改抓 pick 層。"""

    instance = _make_crawler(monkeypatch)

    def _fetch(url):
        raise RobotsPolicyError(
            f"catalog redirect refused at {url} -> https://partsouq.com/en/somepage",
            redirect_location="https://partsouq.com/en/somepage",
        )

    instance._fetch = mock.MagicMock(side_effect=_fetch)

    with pytest.raises(RobotsPolicyError):
        instance.crawl_brand("Toyota")
    assert instance._fetch.call_count == 1
    instance.close()
