"""瀏覽器後備的 off-unit 轉址處理：落點不是 unit 頁時必須轉成 NotFound。"""

from unittest.mock import Mock

import pytest

from partsouq_catalog.cloak import NonUnitPageError
from partsouq_catalog.http_client import (
    NotFoundError,
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


def test_browser_fallback_off_unit_redirect_raises_not_found(
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

    with pytest.raises(NotFoundError):
        manager.get_response(url)
