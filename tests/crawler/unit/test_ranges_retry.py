from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from partsouq_crawler.crawl.retries import retry_delay
from partsouq_crawler.parsers.common import (
    is_assembly_name,
    normalize_part_number,
    parse_unambiguous_range,
)


def test_retry_after_seconds() -> None:
    assert retry_delay(1, "7") == 7


def test_retry_after_http_date() -> None:
    future = datetime.now(UTC) + timedelta(seconds=10)
    assert 8 <= retry_delay(1, format_datetime(future)) <= 10


def test_unambiguous_month_range() -> None:
    result = parse_unambiguous_range("2006-05 ～ 2011-12")
    assert (result.start, result.end, result.precision) == ("2006-05", "2011-12", "month")


def test_ambiguous_short_range_is_not_guessed() -> None:
    result = parse_unambiguous_range("0605-")
    assert result.start is None and result.end is None and result.precision == "unknown"


def test_partsouq_production_ranges_are_normalized() -> None:
    cases = (
        ("01.2015 - 03.2018", "2015-01", "2018-03"),
        ("01.2018 -", "2018-01", None),
        ("2010 - 2015", "2010-01", "2015-12"),
        ("2010 -", "2010-01", None),
        ("- 2015", None, "2015-12"),
        ("- 03.2018", None, "2018-03"),
        ("2018", "2018-01", "2018-12"),
    )
    for raw, start, end in cases:
        result = parse_unambiguous_range(raw)
        assert (result.start, result.end, result.confidence) == (start, end, 1.0)


def test_reversed_partsouq_range_is_not_published_as_known() -> None:
    result = parse_unambiguous_range("12.2020 - 01.2018")

    assert result.start is None
    assert result.end is None
    assert result.confidence == 0.0


def test_invalid_or_reversed_generic_month_range_is_not_published_as_known() -> None:
    for raw in ("2020-13 ～ 2021-01", "2020-01 ～ 2021-99", "2021-12 ～ 2020-01"):
        result = parse_unambiguous_range(raw)
        assert (result.start, result.end, result.confidence) == (None, None, 0.0)


def test_part_number_normalization_preserves_raw_leading_zero() -> None:
    raw = "00123-AB"
    assert raw == "00123-AB"
    assert normalize_part_number(raw) == "00123AB"


def test_assembly_inference_is_explicit() -> None:
    assert is_assembly_name("PUMP ASSY") == (True, "name_keyword")
    assert is_assembly_name("PUMP") == (False, None)
