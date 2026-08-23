import pytest

from partsouq_catalog.parsers import has_empty_parts_table, parse_parts


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


def test_parse_live_six_column_unit_table_by_header() -> None:
    html = _unit_table(
        ["Number", "Name", "Code", "Quantity", "Unified", "Note"],
        ["SYN10001", "SYNTHETIC BRACKET", "B10", "02", "U-A", "CHECK FIT"],
    )

    parts, malformed = parse_parts(html)

    assert malformed == 0
    assert parts == [
        {
            "part_number": "SYN10001",
            "name": "SYNTHETIC BRACKET",
            "code": "B10",
            "note": "Unified: U-A; Note: CHECK FIT",
            "quantity": "02",
            "range_str": "",
            "part_from": None,
            "part_to": None,
        }
    ]


def test_parse_live_seven_column_metadata_does_not_become_range() -> None:
    html = _unit_table(
        [
            "Number",
            "Name",
            "Code",
            "Quantity",
            "Unified",
            "Filter Note",
            "Specification",
        ],
        [
            "SYN20002",
            "SYNTHETIC FILTER",
            "F20",
            "01",
            "U-B",
            "ALT PACKAGE",
            "01.2018 - 12.2019",
        ],
    )

    parts, malformed = parse_parts(html)

    assert malformed == 0
    assert parts[0]["quantity"] == "01"
    assert parts[0]["note"] == (
        "Unified: U-B; Filter Note: ALT PACKAGE; Specification: 01.2018 - 12.2019"
    )
    assert parts[0]["range_str"] == ""
    assert parts[0]["part_from"] is None
    assert parts[0]["part_to"] is None


@pytest.mark.parametrize("range_header", ["Range", "Prod period"])
def test_parse_legacy_six_column_range_header(range_header: str) -> None:
    html = _unit_table(
        ["Number", "Name", "Code", "Note", "Quantity", range_header],
        ["SYN30003", "SYNTHETIC SEAL", "S30", "LEGACY NOTE", "04", "01.2018 - 12.2019"],
    )

    parts, malformed = parse_parts(html)

    assert malformed == 0
    assert parts[0]["note"] == "LEGACY NOTE"
    assert parts[0]["quantity"] == "04"
    assert parts[0]["range_str"] == "01.2018 - 12.2019"
    assert parts[0]["part_from"] == "2018-01"
    assert parts[0]["part_to"] == "2019-12"


@pytest.mark.parametrize(
    ("headers", "values"),
    [
        (
            ["Number", "Name", "Code", "Unified", "Note"],
            ["SYN40004", "SYNTHETIC ITEM", "X40", "U-C", "NOTE"],
        ),
        (
            ["Number", "Name", "Code", "Quantity", "Unified", "Note"],
            ["SYN50005", "SYNTHETIC ITEM", "X50", "01", "U-D"],
        ),
    ],
)
def test_parse_header_shape_mismatch_is_malformed(headers: list[str], values: list[str]) -> None:
    assert parse_parts(_unit_table(headers, values)) == ([], 1)


def test_parse_empty_parts_table_shell_is_legitimate_empty() -> None:
    """實測案例（2026-08-23，TOYOTA1000 KP30 BODY STRIPE unit，uid=4128）：
    站方渲染完整零件表殼（Number|Name|Code 表頭）但零資料列 —— HTTP 200、
    版型正常，屬合法「此組無零件」。parse_parts 回空且 0 malformed；
    has_empty_parts_table 讓 crawler receipt done/0 而非誤判版型變更。"""
    html = (
        "<table><tr><td>Brand</td><td>Name</td><td>Model</td>"
        "<td>Options</td><td>Prod Period</td></tr>"
        "<tr><td>TOYOTA</td><td>TOYOTA1000 KP3#</td><td>KP30-</td>"
        "<td>Driver's Position: RIGHT-HAND DR</td>"
        "<td>04.1969 - 02.1978</td></tr></table>"
        '<table class="glow pop-vin"><thead><tr>'
        "<th>Number</th><th>Name</th><th>Code</th>"
        "</tr></thead></table>"
    )

    parts, malformed = parse_parts(html)

    assert parts == []
    assert malformed == 0
    assert has_empty_parts_table(html) is True


def test_has_empty_parts_table_is_false_for_populated_or_missing_tables() -> None:
    populated = _unit_table(
        ["Number", "Name", "Code"],
        ["SYN60006", "SYNTHETIC LEVER", "C60"],
    )
    assert has_empty_parts_table(populated) is False
    # 完全沒有零件表（反爬變體、空白頁）→ False，交由 guard 拒絕。
    assert has_empty_parts_table("<html><body>challenge page</body></html>") is False
