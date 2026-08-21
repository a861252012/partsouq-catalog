"""parser 語意測試：外來 context 排除、變體 group dedup、必填名稱。

覆蓋 2954268 後續 review 發現的三項語意變更：
- context mismatch 連結不計 malformed，但必須計入 diagnostics 的
  skipped 計數（呼叫端可據此診斷首次爬取漏抓）
- 同一 (cid, group_code) 以不同 uid 出現時兩筆都要保留（變體專屬零件）
- 純圖片零件列若沒有可驗證的文字名稱，一律計 malformed
"""

from partsouq_catalog.parsers import parse_category_links, parse_groups, parse_parts


def _unit_table(headers: list[str], values: list[str]) -> str:
    header_html = "".join(f"<th>{header}</th>" for header in headers)
    value_html = "".join(
        (
            f'<td><a href="/en/search/all?q={value}">{value}</a></td>'
            if index == 0
            else f"<td>{value}</td>"
        )
        for index, value in enumerate(values)
    )
    return (
        f"<table><thead><tr>{header_html}</tr></thead><tbody><tr>{value_html}</tr></tbody></table>"
    )


def _vehicle_link(c: str, vid: str, cid: str, cname: str) -> str:
    return (
        f'<a href="https://partsouq.com/en/catalog/genuine/vehicle'
        f'?c={c}&amp;ssd=token1&amp;vid={vid}&amp;cid={cid}&amp;cname={cname}">{cname}</a>'
    )


def _unit_link(cid: str, uid: str, code: str, name: str, ssd: str = "token1") -> str:
    return (
        f'<a href="https://partsouq.com/en/catalog/genuine/unit'
        f"?c=toyota&amp;ssd={ssd}&amp;vid=7&amp;cid={cid}&amp;uid={uid}&amp;q="
        f'">{code}: {name}</a>'
    )


def test_category_links_count_foreign_context_in_skipped_not_malformed() -> None:
    html = (
        _vehicle_link("toyota", "7", "2", "BODY")
        + _vehicle_link("honda", "8", "3", "ELECTRICAL")
        + _vehicle_link("toyota", "9", "4", "MISC")
    )

    cats, malformed, skipped = parse_category_links(
        html, brand="toyota", diagnostics=True, expected_vid="7"
    )

    assert [c["cid"] for c in cats] == ["2"]
    assert malformed == 0
    assert skipped == 2


def test_category_links_label_link_without_cid_counts_as_skipped() -> None:
    html = _vehicle_link("toyota", "7", "2", "BODY") + (
        '<a href="https://partsouq.com/en/catalog/genuine/vehicle'
        '?c=toyota&amp;ssd=token1&amp;vid=7">Categories</a>'
    )

    cats, malformed, skipped = parse_category_links(
        html, brand="toyota", diagnostics=True, expected_vid="7"
    )

    assert len(cats) == 1
    assert malformed == 0
    assert skipped == 1


def test_groups_keep_variant_groups_with_distinct_uid() -> None:
    html = _unit_link("1", "100", "1000", "ENGINE BLOCK", ssd="token1") + _unit_link(
        "1", "101", "1000", "ENGINE BLOCK", ssd="token2"
    )

    groups, malformed, skipped, image_only = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 0
    assert skipped == 0
    assert image_only == 0
    assert [(g["cid"], g["uid"]) for g in groups] == [("1", "100"), ("1", "101")]


def test_groups_same_identity_with_different_name_is_malformed() -> None:
    html = _unit_link("1", "100", "1000", "ENGINE BLOCK") + _unit_link(
        "1", "100", "1000", "DIFFERENT NAME"
    )

    groups, malformed, _skipped, image_only = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 1
    assert image_only == 0
    assert len(groups) == 1


def test_groups_count_foreign_context_in_skipped_not_malformed() -> None:
    html = _unit_link("1", "100", "1000", "ENGINE BLOCK") + (
        '<a href="https://partsouq.com/en/catalog/genuine/unit'
        '?c=honda&amp;ssd=token1&amp;vid=8&amp;cid=1&amp;uid=200&amp;q=">1000: OTHER</a>'
    )

    groups, malformed, skipped, image_only = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert [g["uid"] for g in groups] == ["100"]
    assert malformed == 0
    assert skipped == 1
    assert image_only == 0


def test_groups_accept_image_only_unit_links_without_text() -> None:
    """圖片-only 連結（diagram 縮圖、無文字）是站方合法版型：接受為
    空 code/name 的 group，不計 malformed（Mitsubishi 部分車型整頁
    都是此類連結）。"""
    html = (
        '<a href="https://partsouq.com/en/catalog/genuine/unit'
        '?c=toyota&amp;ssd=token1&amp;vid=7&amp;cid=1&amp;uid=500&amp;q=">'
        '<img src="/dia.gif"></a>'
    ) + (
        '<a href="https://partsouq.com/en/catalog/genuine/unit'
        '?c=toyota&amp;ssd=token2&amp;vid=7&amp;cid=1&amp;uid=501&amp;q=">'
        '<img src="/dia2.gif"></a>'
    )

    groups, malformed, skipped, image_only = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 0
    assert skipped == 0
    assert image_only == 2
    assert [g["uid"] for g in groups] == ["500", "501"]
    assert all(g["group_code"] == "" for g in groups)


def test_groups_image_then_text_link_upgrades_in_place() -> None:
    """同 uid 的圖片連結與文字連結並存時，只收錄一次並以文字 code/name 升級。"""
    html = (
        '<a href="https://partsouq.com/en/catalog/genuine/unit'
        '?c=toyota&amp;ssd=token1&amp;vid=7&amp;cid=1&amp;uid=600&amp;q=">'
        '<img src="/dia.gif"></a>'
    ) + _unit_link("1", "600", "0901", "ENGINE BLOCK", ssd="token2")

    groups, malformed, skipped, image_only = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 0
    assert len(groups) == 1
    assert groups[0]["uid"] == "600"
    assert groups[0]["group_code"] == "0901"
    assert groups[0]["group_name"] == "ENGINE BLOCK"


def test_groups_two_text_links_same_uid_different_code_both_kept() -> None:
    """同 (cid, uid) 但 code 不同的兩個文字連結是變體專屬資料：都不能
    被 seen_uids 靜默吞掉（HEAD 語意回歸）。"""
    html = _unit_link("1", "700", "0902", "ENGINE BLOCK A", ssd="token1") + _unit_link(
        "1", "700", "0903", "ENGINE BLOCK B", ssd="token2"
    )

    groups, malformed, skipped, image_only = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 0
    assert image_only == 0
    assert sorted((g["group_code"], g["uid"]) for g in groups) == [
        ("0902", "700"),
        ("0903", "700"),
    ]


def test_groups_text_then_image_same_uid_does_not_duplicate() -> None:
    """文字連結先於同 uid 圖片連結時，圖片不重複收錄。"""
    html = _unit_link("1", "800", "0901", "ENGINE BLOCK", ssd="token1") + (
        '<a href="https://partsouq.com/en/catalog/genuine/unit'
        '?c=toyota&amp;ssd=token2&amp;vid=7&amp;cid=1&amp;uid=800&amp;q=">'
        '<img src="/dia.gif"></a>'
    )

    groups, malformed, skipped, image_only = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 0
    assert len(groups) == 1
    assert groups[0]["group_code"] == "0901"


def test_parts_skips_nameless_row_not_malformed() -> None:
    """純料號列（無名稱、無圖片 alt）是站方合法資料：不落庫、不罰整車。"""
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Note"],
        ["IMG10001", "", "B10", "02", "CHECK FIT"],
    )

    parts, malformed, skipped, _skipped_rows = parse_parts(html, diagnostics=True)

    assert malformed == 0
    assert skipped == 1
    assert parts == []
    assert _skipped_rows == [
        {
            "part_number": "IMG10001",
            "code": "B10",
            "quantity": "02",
            "range_str": "",
            "note": "CHECK FIT",
        }
    ]


def test_parts_nameless_row_with_range_is_skipped() -> None:
    """純料號列（無名稱、無圖片 alt）是站方合法資料：不落庫、不罰整車。"""
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Range"],
        ["IMG20002", "", "C20", "01", "01.2018 - 12.2019"],
    )

    parts, malformed, skipped, _skipped_rows = parse_parts(html, diagnostics=True)

    assert malformed == 0
    assert skipped == 1
    assert parts == []
    assert _skipped_rows == [
        {
            "part_number": "IMG20002",
            "code": "C20",
            "quantity": "01",
            "range_str": "01.2018 - 12.2019",
            "note": "",
        }
    ]


def test_parts_nameless_row_with_unexpected_text_is_malformed() -> None:
    """空名稱列若含料號/code/數量/日期/note 以外的文字 = 欄位錯位。"""
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Extra"],
        ["IMG30003", "", "E30", "01", "SURPRISE"],
    )

    parts, malformed, skipped, _skipped_rows = parse_parts(html, diagnostics=True)

    assert malformed == 1
    assert skipped == 0
    assert parts == []


def test_parts_nameless_row_without_code_is_malformed() -> None:
    """空名稱且缺 code = 殘缺列，仍算 malformed。"""
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Note"],
        ["IMG40004", "", "", "01", "CHECK FIT"],
    )

    parts, malformed, skipped, _skipped_rows = parse_parts(html, diagnostics=True)

    assert malformed == 1
    assert skipped == 0
    assert parts == []
