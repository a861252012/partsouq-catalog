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

    groups, malformed, skipped = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 0
    assert skipped == 0
    assert [(g["cid"], g["uid"]) for g in groups] == [("1", "100"), ("1", "101")]


def test_groups_same_identity_with_different_name_is_malformed() -> None:
    html = _unit_link("1", "100", "1000", "ENGINE BLOCK") + _unit_link(
        "1", "100", "1000", "DIFFERENT NAME"
    )

    groups, malformed, _skipped = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert malformed == 1
    assert len(groups) == 1


def test_groups_count_foreign_context_in_skipped_not_malformed() -> None:
    html = _unit_link("1", "100", "1000", "ENGINE BLOCK") + (
        '<a href="https://partsouq.com/en/catalog/genuine/unit'
        '?c=honda&amp;ssd=token1&amp;vid=8&amp;cid=1&amp;uid=200&amp;q=">1000: OTHER</a>'
    )

    groups, malformed, skipped = parse_groups(
        html, "toyota", diagnostics=True, expected_vid="7", expected_cid="1"
    )

    assert [g["uid"] for g in groups] == ["100"]
    assert malformed == 0
    assert skipped == 1


def test_parts_rejects_image_row_without_product_name() -> None:
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Note"],
        ["IMG10001", "", "B10", "02", "CHECK FIT"],
    )

    parts, malformed = parse_parts(html)

    assert malformed == 1
    assert parts == []


def test_parts_image_row_with_range_but_no_name_is_malformed() -> None:
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Range"],
        ["IMG20002", "", "C20", "01", "01.2018 - 12.2019"],
    )

    parts, malformed = parse_parts(html)

    assert malformed == 1
    assert parts == []


def test_parts_image_row_with_unexpected_text_is_malformed() -> None:
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Extra"],
        ["IMG30003", "", "E30", "01", "SURPRISE"],
    )

    parts, malformed = parse_parts(html)

    assert malformed == 1
    assert parts == []


def test_parts_image_row_without_code_is_malformed() -> None:
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Note"],
        ["IMG40004", "", "", "01", "CHECK FIT"],
    )

    parts, malformed = parse_parts(html)

    assert malformed == 1
    assert parts == []
