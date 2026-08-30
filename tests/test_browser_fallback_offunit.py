"""瀏覽器後備無法證明 404；任何錯誤落點都必須維持 fail-closed。"""

from unittest.mock import Mock

import pytest

from partsouq_catalog.cloak import NonUnitPageError
from partsouq_catalog.http_client import (
    RobotsPolicyError,
    SessionManager,
)

CHALLENGE_RESPONSE = Mock(
    status_code=403,
    text="Just a moment...",
    content=b"Just a moment...",
    url="https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1",
    headers={},
)
CHALLENGE_RESPONSE.iter_content.return_value = iter([b"Just a moment..."])


def test_browser_fallback_off_unit_redirect_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1&vid=0"
    manager = SessionManager()
    manager._ensure_catalog_allowed = lambda _u: None
    manager.session.get = Mock(return_value=CHALLENGE_RESPONSE)
    monkeypatch.setattr("partsouq_catalog.http_client.force_refresh_session", lambda _v: None)
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)

    def boom(_u: str) -> str:
        raise NonUnitPageError("https://partsouq.com/en/catalog/genuine/locate?c=Toyota")

    monkeypatch.setattr("partsouq_catalog.http_client.fetch_page", boom)

    with pytest.raises(RobotsPolicyError, match="outside requested catalog page"):
        manager.get_response(url)


def test_browser_fallback_wrong_non_unit_page_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://partsouq.com/en/catalog/genuine/locate?c=Toyota"
    manager = SessionManager()
    manager._ensure_catalog_allowed = lambda _u: None
    manager.session.get = Mock(return_value=CHALLENGE_RESPONSE)
    monkeypatch.setattr("partsouq_catalog.http_client.force_refresh_session", lambda _v: None)
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)

    def boom(_u: str) -> str:
        raise NonUnitPageError("https://partsouq.com/en/catalog/genuine?c=Toyota")

    monkeypatch.setattr("partsouq_catalog.http_client.fetch_page", boom)

    with pytest.raises(RobotsPolicyError, match="outside requested catalog page"):
        manager.get_response(url)
