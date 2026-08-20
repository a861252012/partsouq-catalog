"""HTTP 傳輸層的契約測試：cookie 注入、challenge 自動刷新、限流與 404。

覆蓋 SessionManager 在 CloakBrowser cookie 機制下的行為：
- cookie 快照整份套進 requests jar（cf_clearance + PHPSESSID）
- 收到 Cloudflare challenge 時強制刷新 cookie 並重試（刷新成功不
  消耗 attempt 預算，F4）
- no_browser 模式下絕不啟動瀏覽器、直接放棄
- 429 依 Retry-After 退避（尊重上限）、404 拋 NotFoundError
- ensure_fresh 以 cf_clearance 版本訊號偵測 session 已更新
"""

from unittest.mock import Mock

import pytest

from partsouq_catalog.config import CLOAK
from partsouq_catalog.http_client import (
    CHALLENGE_MARKERS,
    ChallengeError,
    NotFoundError,
    SessionManager,
    _cf_value,
)


def response(status_code: int, text: str, url: str, headers=None) -> Mock:
    return Mock(status_code=status_code, text=text, url=url, headers=headers or {})


CHALLENGE_RESPONSE = response(403, "Just a moment...", "https://partsouq.example/catalog")
OK_RESPONSE = response(200, "catalog", "https://partsouq.example/catalog")


def cookies(*, cf: str = "clearance-v1", php: str = "phpsessid-v1") -> list[dict]:
    return [
        {"name": "cf_clearance", "value": cf, "domain": "partsouq.com", "path": "/"},
        {"name": "PHPSESSID", "value": php, "domain": "partsouq.com", "path": "/"},
    ]


def test_session_uses_browser_user_agent() -> None:
    manager = SessionManager()

    assert manager.session.headers["User-Agent"] == CLOAK["user_agent"]
    assert "Mozilla" in manager.session.headers["User-Agent"]


def test_session_injects_cookie_snapshot_into_jar() -> None:
    manager = SessionManager(cookies=cookies())

    jar = manager.session.cookies
    assert jar.get("cf_clearance") == "clearance-v1"
    assert jar.get("PHPSESSID") == "phpsessid-v1"


def test_apply_cookies_replaces_entire_jar() -> None:
    manager = SessionManager(cookies=cookies())
    # 刷新結果缺少舊 cookie 時，舊值不得殘留（SOL review P2）
    manager._apply_cookies()

    jar = manager.session.cookies
    assert jar.get("cf_clearance") == "clearance-v1"
    assert jar.get("PHPSESSID") == "phpsessid-v1"


def test_cf_value_extracts_clearance_version() -> None:
    assert _cf_value(cookies(cf="abc123")) == "abc123"
    assert _cf_value([{"name": "PHPSESSID", "value": "x"}]) == ""
    assert _cf_value(None) == ""


def test_challenge_in_no_browser_mode_stops_without_refresh() -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    manager.session.get = Mock(return_value=CHALLENGE_RESPONSE)

    with pytest.raises(ChallengeError):
        manager.get("https://partsouq.example/catalog")

    assert manager.session.get.call_count == 1


def test_no_browser_refresh_returns_false() -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)

    assert manager.refresh() is False


def test_challenge_forces_refresh_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """F4：刷新成功不消耗 attempt 預算 —— 刷新後必有 follow-up 請求。"""
    manager = SessionManager(cookies=cookies(cf="rejected-v0"))
    manager.session.get = Mock(
        side_effect=[
            CHALLENGE_RESPONSE,
            response(
                200,
                "catalog",
                "https://partsouq.example/catalog",
                headers={"cf-mitigated": "pass"},
            ),
        ]
    )
    refreshed = cookies(cf="fresh-v1")
    monkeypatch.setattr(
        "partsouq_catalog.http_client.force_refresh_session",
        lambda rejected_version: refreshed,
    )
    monkeypatch.setattr(
        "partsouq_catalog.http_client.time.sleep", lambda _seconds: None
    )

    assert manager.get("https://partsouq.example/catalog") == "catalog"
    # 被拒的 cf_clearance 版本必須傳給 force_refresh_session（SOL P2）
    assert manager.cookies is refreshed
    assert manager.session.get.call_count == 2


def test_challenge_with_failed_refresh_retries_then_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(cookies=cookies())
    manager.session.get = Mock(return_value=CHALLENGE_RESPONSE)
    monkeypatch.setattr(
        "partsouq_catalog.http_client.force_refresh_session", lambda _v: None
    )
    monkeypatch.setattr(
        "partsouq_catalog.http_client.time.sleep", lambda _seconds: None
    )

    with pytest.raises(ChallengeError, match="challenge"):
        manager.get("https://partsouq.example/catalog")

    # challenge_retries=3：第 4 次 attempt 才達到「3 次連續刷新失敗」門檻
    assert manager.session.get.call_count == 4


def test_challenge_after_too_many_successful_refreshes_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(cookies=cookies())
    manager.session.get = Mock(return_value=CHALLENGE_RESPONSE)
    monkeypatch.setattr(
        "partsouq_catalog.http_client.force_refresh_session",
        lambda _v: cookies(cf="fresh-v1"),
    )
    monkeypatch.setattr(
        "partsouq_catalog.http_client.time.sleep", lambda _seconds: None
    )

    with pytest.raises(ChallengeError):
        manager.get("https://partsouq.example/catalog")

    # max_refresh_per_request=3：成功刷新 3 次後第 4 次仍被 challenge 就放棄
    assert manager.session.get.call_count == 4


def test_404_raises_not_found_error() -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    manager.session.get = Mock(return_value=response(404, "", "https://partsouq.example/unit"))

    with pytest.raises(NotFoundError):
        manager.get("https://partsouq.example/unit")


def test_429_respects_retry_after_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    manager.session.get = Mock(
        side_effect=[
            response(
                429,
                "",
                "https://partsouq.example/catalog",
                headers={"retry-after": "999999"},
            ),
            OK_RESPONSE,
        ]
    )
    slept: list[float] = []
    monkeypatch.setattr(
        "partsouq_catalog.http_client.time.sleep", lambda s: slept.append(float(s))
    )

    assert manager.get("https://partsouq.example/catalog") == "catalog"
    assert manager.session.get.call_count == 2
    # 巨額 Retry-After 必須被 cap 在 retry_after_cap=300 秒內
    assert slept == [300.0]


def test_429_http_date_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    manager.session.get = Mock(
        side_effect=[
            response(
                429,
                "",
                "https://partsouq.example/catalog",
                headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
            ),
            OK_RESPONSE,
        ]
    )
    slept: list[float] = []
    monkeypatch.setattr(
        "partsouq_catalog.http_client.time.sleep", lambda s: slept.append(float(s))
    )

    assert manager.get("https://partsouq.example/catalog") == "catalog"
    # 過去的日期 → 低於 15 秒下限 → 以 15 秒重試（F4 修復：不會拋錯）
    assert slept == [15.0]


def test_500_is_retried_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    manager.session.get = Mock(return_value=response(500, "boom", "https://partsouq.example/x"))
    monkeypatch.setattr(
        "partsouq_catalog.http_client.time.sleep", lambda _seconds: None
    )

    with pytest.raises(Exception, match="http 500"):
        manager.get("https://partsouq.example/x")

    assert manager.session.get.call_count == 5  # max_retries


def test_connection_error_resets_pool_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    import requests

    manager.session.get = Mock(
        side_effect=[requests.exceptions.ConnectionError("CLOSE_WAIT"), OK_RESPONSE]
    )
    monkeypatch.setattr(
        "partsouq_catalog.http_client.time.sleep", lambda _seconds: None
    )

    assert manager.get("https://partsouq.example/catalog") == "catalog"
    assert manager.session.get.call_count == 2


def test_is_challenge_detects_header_and_markers() -> None:
    assert SessionManager._is_challenge(
        response(200, "whatever", "u", headers={"cf-mitigated": "challenge"}), "whatever"
    )
    assert SessionManager._is_challenge(
        response(200, "whatever", "u", headers={"cf-chl": "1"}), "whatever"
    )
    assert SessionManager._is_challenge(response(200, "ok", "u"), "Turnstile challenge")
    assert not SessionManager._is_challenge(response(200, "ok", "u"), "catalog body")


def test_challenge_markers_cover_turnstile() -> None:
    assert "challenge-platform" in CHALLENGE_MARKERS
    assert "Turnstile" in CHALLENGE_MARKERS
    assert "Managed Challenge" in CHALLENGE_MARKERS


def test_ensure_fresh_reapplies_when_version_changes(monkeypatch: pytest.MonkeyPatch) -> None:

    manager = SessionManager(cookies=cookies(cf="v1"))
    fresh = Mock(return_value=cookies(cf="v2"))
    monkeypatch.setattr("partsouq_catalog.http_client.get_session", fresh)

    manager.ensure_fresh()
    assert _cf_value(manager.cookies) == "v2"
    assert manager.session.cookies.get("cf_clearance") == "v2"

    # 版本沒變（同一次刷新結果被沿用）：不得重複套 jar
    manager.ensure_fresh()
    assert manager.session.cookies.get("cf_clearance") == "v2"
    assert fresh.call_count == 2


def test_ensure_fresh_skipped_in_no_browser_mode(monkeypatch: pytest.MonkeyPatch) -> None:

    manager = SessionManager(cookies=cookies(), no_browser=True)
    fresh = Mock(return_value=cookies(cf="v2"))
    monkeypatch.setattr("partsouq_catalog.http_client.get_session", fresh)

    manager.ensure_fresh()
    assert fresh.call_count == 0