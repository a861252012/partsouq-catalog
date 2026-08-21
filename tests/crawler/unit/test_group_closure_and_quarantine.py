"""SOL review P1 修正的單元測試。

1. group closure 改「uid → code 集合」：同 uid 的 code 變體消失必須偵測到。
2. 無名稱純料號列：不得標 done（terminal receipt），必須 quarantine +
   標 partial，讓下次排程重抓。
"""

from unittest import mock

import pytest

from partsouq_catalog.config import CRAWL
from partsouq_catalog.crawler import Crawler, _group_closure_mismatches


def _group() -> dict:
    return {
        "category_name": "ENGINE/FUEL/TOOL",
        "cid": "1",
        "group_code": "1101",
        "group_name": "PARTIAL ENGINE ASSEMBLY",
        "uid": "10001",
        "url": "/en/catalog/genuine/unit?uid=10001",
    }


def _parts_html(rows: list[tuple[str, str, str]]) -> str:
    """rows: (part_number, name, code)。其餘欄位固定：Note 空、Qty 01、Range 空。"""
    body = "".join(
        "<tr>"
        f'<td><a href="/en/search/all?q={part_number}">{part_number}</a></td>'
        f"<td>{name}</td><td>{code}</td><td></td><td>01</td><td></td>"
        "</tr>"
        for part_number, name, code in rows
    )
    return f"<table><tbody>{body}</tbody></table>"


@pytest.fixture
def sample_crawler(monkeypatch):
    monkeypatch.setitem(CRAWL, "limit_parts", 1000)
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=4)
    instance.run_id = 17
    instance.vehicles = mock.MagicMock()
    instance.vehicles.upsert_category.return_value = 31
    instance.vehicles.upsert_group.return_value = 41
    instance.parts = mock.MagicMock()
    instance.parts.upsert_parts.side_effect = lambda _group, rows, _run, **_kwargs: len(rows)
    instance.crawl = mock.MagicMock()
    instance.crawl.run_key = "2026-08-fixture"
    instance.crawl.previous_row_count.return_value = 0
    instance._get = mock.MagicMock(return_value="<html>unit</html>")
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# group closure：uid → code 集合
# ---------------------------------------------------------------------------


def test_closure_detects_missing_code_variant_for_same_uid() -> None:
    """SOL review 範例：DB 有 (0902, UID-X)、(0903, UID-X)，頁面只剩 0902。
    舊的 uid-only closure 會放行；新邏輯必須偵測到 0903 消失。"""
    known = {"UID-X": {"0902", "0903"}}
    parsed = [
        {"cid": "1", "uid": "UID-X", "group_code": "0902", "url": "url"},
    ]
    missing_uids, missing_codes, downgraded = _group_closure_mismatches(known, parsed, "1")
    assert missing_uids == []
    assert missing_codes == ["0903@UID-X"]
    assert downgraded == []


def test_closure_detects_missing_uid() -> None:
    known = {"UID-A": {"0901"}, "UID-B": {"0902"}}
    parsed = [{"cid": "1", "uid": "UID-A", "group_code": "0901", "url": "url"}]
    missing_uids, missing_codes, downgraded = _group_closure_mismatches(known, parsed, "1")
    assert missing_uids == ["UID-B"]
    assert missing_codes == []
    assert downgraded == []


def test_closure_accepts_text_to_image_downgrade_as_warning() -> None:
    """文字 code 這月只以圖片-only（code 空）出現：group 仍在，不算消失，
    但必須告警（downgraded），不能靜默當成完整文字資料。"""
    known = {"UID-X": {"0902"}}
    parsed = [{"cid": "1", "uid": "UID-X", "group_code": "", "url": "url"}]
    missing_uids, missing_codes, downgraded = _group_closure_mismatches(known, parsed, "1")
    assert missing_uids == []
    assert missing_codes == []
    assert downgraded == ["0902@UID-X"]


def test_closure_accepts_image_only_known_row_when_uid_present() -> None:
    """已知圖片-only（code 空）列：uid 以任何形式出現即滿足。"""
    known = {"UID-IMG": {""}}
    parsed = [{"cid": "1", "uid": "UID-IMG", "group_code": "0901", "url": "url"}]
    missing_uids, missing_codes, downgraded = _group_closure_mismatches(known, parsed, "1")
    assert missing_uids == []
    assert missing_codes == []
    assert downgraded == []


def test_closure_ignores_foreign_cid() -> None:
    known = {"UID-A": {"0901"}}
    parsed = [
        {"cid": "2", "uid": "OTHER", "group_code": "9999", "url": "url"},
    ]
    missing_uids, missing_codes, downgraded = _group_closure_mismatches(known, parsed, "1")
    assert missing_uids == ["UID-A"]
    assert missing_codes == []
    assert downgraded == []


# ---------------------------------------------------------------------------
# 無名稱純料號列：quarantine + partial，不得標 done
# ---------------------------------------------------------------------------


def test_all_nameless_page_quarantines_and_marks_partial(sample_crawler) -> None:
    sample_crawler._get.return_value = _parts_html([("IMG10001", "", "B10")])
    fetched = {}

    truncated = sample_crawler.crawl_group("TOYOTA", 7, _group(), fetched=fetched)

    assert truncated is False
    sample_crawler.parts.quarantine_parts.assert_called_once()
    args = sample_crawler.parts.quarantine_parts.call_args.args
    assert args[0] == 41
    assert args[1] == "2026-08-fixture"
    assert args[2] == [
        {"part_number": "IMG10001", "code": "B10", "quantity": "01", "range_str": "", "note": ""}
    ]
    sample_crawler.crawl.mark_group_fetched.assert_called_once_with(
        41, "2026-08-fixture", status="partial", row_count=0
    )
    assert fetched == {("1", "1101", "10001"): 0}


def test_mixed_page_quarantines_nameless_and_marks_partial(sample_crawler) -> None:
    sample_crawler._get.return_value = _parts_html(
        [("P00001", "ENGINE BOLT", "11000"), ("IMG20002", "", "C20")]
    )
    fetched = {}

    truncated = sample_crawler.crawl_group("TOYOTA", 7, _group(), fetched=fetched)

    assert truncated is False
    sample_crawler.parts.quarantine_parts.assert_called_once()
    args = sample_crawler.parts.quarantine_parts.call_args.args
    assert args[2] == [
        {"part_number": "IMG20002", "code": "C20", "quantity": "01", "range_str": "", "note": ""}
    ]
    sample_crawler.crawl.mark_group_fetched.assert_called_once_with(
        41, "2026-08-fixture", status="partial", row_count=1
    )
    assert fetched == {("1", "1101", "10001"): 1}


def test_all_valid_page_marks_done_without_quarantine(sample_crawler) -> None:
    sample_crawler._get.return_value = _parts_html([("P00001", "ENGINE BOLT", "11000")])
    fetched = {}

    truncated = sample_crawler.crawl_group("TOYOTA", 7, _group(), fetched=fetched)

    assert truncated is False
    sample_crawler.parts.quarantine_parts.assert_not_called()
    sample_crawler.crawl.mark_group_fetched.assert_called_once_with(
        41, "2026-08-fixture", status="done", row_count=1
    )
    assert fetched == {("1", "1101", "10001"): 1}
