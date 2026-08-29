"""requests 層對 unit 頁 3xx 轉址的處理：
- off-unit（非挑戰）→ NotFoundError（crawl_group 標 not_found，terminal）
- 挑戰轉址 → ChallengeError，進入既有刷新/瀏覽器後備重試路徑
- 非 unit 頁（索引等）的轉址 → 維持 fail-closed（RobotsPolicyError）"""

from unittest.mock import Mock

import pytest

from partsouq_catalog.http_client import (
    ChallengeError,
    NotFoundError,
    RobotsPolicyError,
    SessionManager,
)


def _resp(status_code: int, headers: dict, url: str) -> Mock:
    r = Mock(
        status_code=status_code,
        text="",
        content=b"",
        url=url,
        headers=headers,
    )
    r.iter_content.return_value = iter([b""])
    return r


def test_off_unit_3xx_redirect_is_not_found() -> None:
    url = "https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1&vid=0"
    manager = SessionManager()
    manager._ensure_catalog_allowed = lambda _u: None
    manager.session.get = Mock(
        return_value=_resp(
            302,
            {"Location": "https://partsouq.com/en/catalog/genuine/locate?c=Toyota"},
            url,
        )
    )
    with pytest.raises(NotFoundError):
        manager.get_response(url)


def test_unit_3xx_to_same_uid_stays_fail_closed() -> None:
    """同 unit（同 uid）的站內正規化轉址不賦予 gone 語意：維持
    RobotsPolicyError（不跟隨、不猜測）。"""
    url = "https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1&vid=0"
    manager = SessionManager()
    manager._ensure_catalog_allowed = lambda _u: None
    manager.session.get = Mock(
        return_value=_resp(
            302,
            {"Location": "/en/catalog/genuine/unit?c=Toyota&uid=1&vid=7"},
            url,
        )
    )
    with pytest.raises(RobotsPolicyError, match="catalog redirect refused"):
        manager.get_response(url)


def test_challenge_3xx_redirect_enters_challenge_retry_path() -> None:
    """挑戰轉址必須走 ChallengeError（重試路徑），不是靜默失敗。
    no_browser 模式下重新整理被禁用 → 直接以 ChallengeError 放棄。"""
    url = "https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1&vid=0"
    manager = SessionManager(no_browser=True)
    manager._ensure_catalog_allowed = lambda _u: None
    manager.session.get = Mock(
        return_value=_resp(
            302,
            {"Location": "https://partsouq.com/cdn-cgi/challenge-platform/..."},
            url,
        )
    )
    with pytest.raises(ChallengeError, match="challenge"):
        manager.get_response(url)


def test_challenge_3xx_redirect_triggers_browser_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挑戰轉址在刷新無效後，應由瀏覽器後備接手抓回同一 unit 頁。"""
    url = "https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1&vid=0"
    manager = SessionManager()
    manager._ensure_catalog_allowed = lambda _u: None
    manager.session.get = Mock(
        return_value=_resp(
            302,
            {"Location": "https://partsouq.com/cdn-cgi/challenge-platform/..."},
            url,
        )
    )
    monkeypatch.setattr("partsouq_catalog.http_client.force_refresh_session", lambda _v: None)
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)
    recovered = "<html>unit page via browser</html>"
    monkeypatch.setattr("partsouq_catalog.http_client.fetch_page", lambda _u: recovered)

    result = manager.get_response(url)

    assert result.status_code == 200
    assert result.text == recovered


def test_non_unit_catalog_3xx_redirect_stays_fail_closed() -> None:
    """非 unit 頁（索引等）的轉址維持 fail-closed：不賦予 gone 語意。"""
    url = "https://partsouq.com/en/catalog/genuine"
    manager = SessionManager()
    manager._ensure_catalog_allowed = lambda _u: None
    manager.session.get = Mock(
        return_value=_resp(302, {"Location": "https://partsouq.com/en/elsewhere"}, url)
    )
    with pytest.raises(RobotsPolicyError, match="catalog redirect refused"):
        manager.get_response(url)
