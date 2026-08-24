"""vehicle_year_window 政策：只收錄生產期間與最近 N 個日曆年重疊的車款。

界線以執行當天動態計算（2026 跑 = 2006 年起，2028 跑 = 2008 年起），
不寫死年份；生產結束年不明的車款一律照爬。
"""

from datetime import date
from unittest import mock

import pytest

from partsouq_catalog.config import CRAWL
from partsouq_catalog.crawler import (
    Crawler,
    _vehicle_production_end_year,
    _vehicle_year_window_floor,
)


def test_production_end_year_reads_year_prefix() -> None:
    assert _vehicle_production_end_year({"production_to": "1978-12"}) == 1978
    assert _vehicle_production_end_year({"production_to": "2005-01"}) == 2005


def test_production_end_year_none_without_closed_end() -> None:
    # 只有起始年（仍在產線上）或完全沒有年份 → 不視為老車。
    assert _vehicle_production_end_year({"production_to": ""}) is None
    assert _vehicle_production_end_year({}) is None
    assert _vehicle_production_end_year({"production_to": "garbage"}) is None


def test_year_floor_is_zero_when_policy_off() -> None:
    with mock.patch.dict(CRAWL, {"vehicle_year_window": 0}):
        assert _vehicle_year_window_floor() == 0


def test_year_floor_follows_execution_year_dynamically() -> None:
    # 不凍結時鐘：以「今天」相對推導，任何年份執行都必須成立。
    with mock.patch.dict(CRAWL, {"vehicle_year_window": 20}):
        assert _vehicle_year_window_floor() == date.today().year - 20
    with mock.patch.dict(CRAWL, {"vehicle_year_window": 10}):
        assert _vehicle_year_window_floor() == date.today().year - 10


def _window_config(monkeypatch: pytest.MonkeyPatch, window: int) -> None:
    monkeypatch.setitem(CRAWL, "vehicle_year_window", window)
    monkeypatch.setitem(CRAWL, "limit_parts", 0)
    monkeypatch.setitem(CRAWL, "bounded_parts", 0)
    monkeypatch.setitem(CRAWL, "limit_vehicles", 0)
    monkeypatch.setitem(CRAWL, "limit_groups", 0)
    monkeypatch.setitem(CRAWL, "min_brands", 1)
    monkeypatch.setitem(CRAWL, "start_brand", "")
    monkeypatch.setitem(CRAWL, "scheduled_job_run_id", 0)


def _vehicle(name: str, production_to: str) -> dict[str, object]:
    return {
        "name": name,
        "model_code": "",
        "prod_period": "",
        "production_from": "",
        "production_to": production_to,
        "vid": f"VID-{name}",
        "ssd": f"SSD-{name}",
    }


def _make_crawler() -> Crawler:
    instance = Crawler(mock.MagicMock(), mock.MagicMock(), workers=1)
    instance.brands = mock.MagicMock()
    instance.brands.upsert_model.return_value = 31
    instance.crawl = mock.MagicMock()
    instance.crawl.is_done.return_value = False
    instance._fetch = mock.MagicMock(return_value=("<html>pick</html>", object()))
    instance._capture_http_evidence = mock.MagicMock()
    return instance


def test_old_vehicle_skipped_without_marking_done(monkeypatch) -> None:
    _window_config(monkeypatch, window=20)
    this_year = date.today().year
    vehicles = [
        _vehicle("ANCIENT-1", f"{this_year - 30}-06"),
        _vehicle("BOUNDARY-IN", f"{this_year - 20}-12"),
        _vehicle("RECENT", f"{this_year - 3}-01"),
        _vehicle("OPEN-ENDED", ""),
        _vehicle("UNKNOWN-YEAR", "not-a-date"),
    ]
    instance = _make_crawler()
    instance.crawl_vehicle = mock.MagicMock()
    try:
        with mock.patch(
            "partsouq_catalog.crawler.parse_vehicles",
            return_value=(vehicles, 0),
        ):
            failed, worked = instance.crawl_model(
                "TOYOTA", 17, {"name": "CAMRY", "ssd": "MODEL-SSD", "url": "/pick"}
            )
    finally:
        instance.close()

    dispatched = [call.args[2]["name"] for call in instance.crawl_vehicle.call_args_list]
    # 邊界含在內：生產結束年 == 下限（剛好 20 年）必須照爬。
    assert dispatched == ["BOUNDARY-IN", "RECENT", "OPEN-ENDED", "UNKNOWN-YEAR"]
    assert (failed, worked) == (0, True)
    # 政策是動態的：被跳過的車不得留下永久 done 標記。resume key 經過
    # 雜湊，車名不會出現在呼叫參數裡，必須比對雜湊前的完整 key。
    ancient_resume_key = instance._vehicle_key(17, vehicles[0])
    for call in instance.crawl.mark_done.call_args_list:
        assert call.args[1] != ancient_resume_key, call.args


def test_all_old_vehicles_skip_without_parts_work(monkeypatch) -> None:
    _window_config(monkeypatch, window=20)
    this_year = date.today().year
    vehicles = [
        _vehicle("OLD-A", f"{this_year - 25}-01"),
        _vehicle("OLD-B", f"{this_year - 40}-12"),
    ]
    instance = _make_crawler()
    instance.crawl_vehicle = mock.MagicMock()
    try:
        with mock.patch(
            "partsouq_catalog.crawler.parse_vehicles",
            return_value=(vehicles, 0),
        ):
            failed, worked = instance.crawl_model(
                "TOYOTA", 17, {"name": "1000", "ssd": "MODEL-SSD", "url": "/pick"}
            )
    finally:
        instance.close()

    instance.crawl_vehicle.assert_not_called()
    assert (failed, worked) == (0, False)
