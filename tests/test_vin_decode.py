"""VIN 解碼解析的純函式單元測試（不需瀏覽器 / DB）。"""

from __future__ import annotations

from partsouq_catalog.http_client import _parse_vin_decode_result


def test_parses_unit_links_with_html_entities() -> None:
    # 站方 href 實際使用 &amp; 轉義
    html = """
    <a href="/en/catalog/genuine/unit?c=bmw&amp;cid=1&amp;uid=225099&amp;vid=0&amp;ssd=abc">Engine</a>
    <a href="/en/catalog/genuine/unit?c=bmw&amp;cid=3&amp;uid=225100&amp;vid=2&amp;ssd=abc">Body</a>
    """
    result = _parse_vin_decode_result(html, "bmw")
    assert result.raw_html_len > 0
    assert len(result.units) == 2
    by_uid = {u.uid: u for u in result.units}
    assert by_uid["225099"].cid == "1"
    assert by_uid["225099"].vid == "0"
    assert by_uid["225099"].ssd == "abc"
    assert by_uid["225099"].brand == "bmw"
    assert by_uid["225099"].url.startswith("https://partsouq.com/en/catalog/genuine/unit?")
    assert "uid=225099" in by_uid["225099"].url


def test_parses_vehicle_links() -> None:
    html = """
    <a href="/en/catalog/genuine/vehicle?c=toyota&amp;ssd=xyz&amp;vid=77">Corolla</a>
    """
    result = _parse_vin_decode_result(html, "toyota")
    assert result.units == []
    assert len(result.vehicle_links) == 1
    assert result.vehicle_links[0].startswith("https://partsouq.com/en/catalog/genuine/vehicle?")
    assert "vid=77" in result.vehicle_links[0]


def test_ignores_links_without_uid() -> None:
    html = """
    <a href="/en/catalog/genuine/unit?c=bmw&amp;cid=1">no uid here</a>
    <a href="/en/catalog/genuine/pick?c=bmw&amp;model=3">pick</a>
    """
    result = _parse_vin_decode_result(html, "bmw")
    assert result.units == []
    # pick 連結不算 vehicle 也不算 unit
    assert result.vehicle_links == []
