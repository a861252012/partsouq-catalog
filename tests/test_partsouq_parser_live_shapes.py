import pytest

from partsouq_catalog.parsers import parse_parts


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
