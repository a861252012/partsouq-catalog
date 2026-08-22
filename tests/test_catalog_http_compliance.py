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
    CATALOG_USER_AGENT,
    CHALLENGE_MARKERS,
    ROBOTS_BODY_MAX_BYTES,
    ROBOTS_CACHE_TTL_SECONDS,
    ChallengeError,
    NotFoundError,
    RobotsPolicyError,
    SessionManager,
    _cf_value,
)


def response(status_code: int, text: str, url: str, headers=None) -> Mock:
    result = Mock(
        status_code=status_code,
        text=text,
        content=text.encode(),
        url=url,
        headers=headers or {},
    )
    result.iter_content.return_value = iter([text.encode()])
    return result


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
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)

    assert manager.get("https://partsouq.example/catalog") == "catalog"
    # 被拒的 cf_clearance 版本必須傳給 force_refresh_session（SOL P2）
    assert manager.cookies is refreshed
    assert manager.session.get.call_count == 2


def test_challenge_with_failed_refresh_retries_then_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(cookies=cookies())
    manager.session.get = Mock(return_value=CHALLENGE_RESPONSE)
    monkeypatch.setattr("partsouq_catalog.http_client.force_refresh_session", lambda _v: None)
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)

    with pytest.raises(ChallengeError, match="challenge"):
        manager.get("https://partsouq.example/catalog")

    # challenge_retries=3：第 4 次 attempt 才達到「3 次連續刷新失敗」門檻
    assert manager.session.get.call_count == 4


def test_refresh_backoff_emits_heartbeat_between_sleep_chunks(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager(cookies=cookies())
    slept: list[float] = []
    monkeypatch.setattr("partsouq_catalog.http_client.session_backoff_remaining", lambda: 130.0)
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", slept.append)
    caplog.set_level("WARNING", logger="http")

    manager._sleep_with_backoff(0)

    assert slept == [60.0, 60.0, 15.0]
    assert [record.getMessage() for record in caplog.records] == [
        "cookie refresh backoff; 135s remaining",
        "cookie refresh backoff; 75s remaining",
        "cookie refresh backoff; 15s remaining",
    ]


def test_challenge_after_fresh_browser_session_is_rejected_stops_without_relaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(cookies=cookies())
    manager.session.get = Mock(return_value=CHALLENGE_RESPONSE)
    refresh = Mock(return_value=cookies(cf="fresh-v1"))
    reject = Mock()
    monkeypatch.setattr("partsouq_catalog.http_client.force_refresh_session", refresh)
    monkeypatch.setattr("partsouq_catalog.http_client.reject_session", reject)
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)

    with pytest.raises(ChallengeError, match="fresh browser session still challenged"):
        manager.get("https://partsouq.example/catalog")

    assert manager.session.get.call_count == 2
    refresh.assert_called_once()
    reject.assert_called_once_with("fresh-v1")


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
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda s: slept.append(float(s)))

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
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda s: slept.append(float(s)))

    assert manager.get("https://partsouq.example/catalog") == "catalog"
    # 過去的日期 → 低於 15 秒下限 → 以 15 秒重試（F4 修復：不會拋錯）
    assert slept == [15.0]


def test_500_is_retried_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    manager.session.get = Mock(return_value=response(500, "boom", "https://partsouq.example/x"))
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)

    with pytest.raises(Exception, match="http 500"):
        manager.get("https://partsouq.example/x")

    assert manager.session.get.call_count == 5  # max_retries


def test_connection_error_resets_pool_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SessionManager(cookies=cookies(), no_browser=True)
    import requests

    manager.session.get = Mock(
        side_effect=[requests.exceptions.ConnectionError("CLOSE_WAIT"), OK_RESPONSE]
    )
    monkeypatch.setattr("partsouq_catalog.http_client.time.sleep", lambda _seconds: None)

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


def test_transport_ignores_environment_proxy() -> None:
    manager = SessionManager()

    assert manager.session.trust_env is False
    assert manager.session.proxies == {}


def test_robots_fetch_uses_identifiable_crawler_user_agent() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        side_effect=[
            response(200, "User-agent: *\nDisallow:\n", "https://partsouq.com/robots.txt"),
            response(200, "catalog", "https://partsouq.com/en/catalog/genuine"),
        ]
    )

    assert manager.get("https://partsouq.com/en/catalog/genuine") == "catalog"
    robots_call = manager.session.get.call_args_list[0]
    assert robots_call.kwargs["headers"] == {"User-Agent": CATALOG_USER_AGENT}
    assert robots_call.kwargs["stream"] is True
    assert "Mozilla" not in CATALOG_USER_AGENT
    assert "github.com/a861252012" in CATALOG_USER_AGENT


def test_catalog_fetches_robots_before_first_allowed_request() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        side_effect=[
            response(
                200,
                "User-agent: *\nDisallow: /private\n",
                "https://partsouq.com/robots.txt",
            ),
            response(200, "catalog", "https://partsouq.com/en/catalog/genuine"),
        ]
    )

    assert manager.get("https://partsouq.com/en/catalog/genuine") == "catalog"
    assert [call.args[0] for call in manager.session.get.call_args_list] == [
        "https://partsouq.com/robots.txt",
        "https://partsouq.com/en/catalog/genuine",
    ]
    assert all(
        call.kwargs["allow_redirects"] is False for call in manager.session.get.call_args_list
    )


def test_catalog_reuses_robots_but_checks_each_url() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        side_effect=[
            response(200, "User-agent: *\nDisallow:\n", "https://partsouq.com/robots.txt"),
            response(200, "one", "https://partsouq.com/en/catalog/genuine"),
            response(200, "two", "https://partsouq.com/en/catalog/genuine/locate"),
        ]
    )

    assert manager.get("https://partsouq.com/en/catalog/genuine") == "one"
    assert manager.get("https://partsouq.com/en/catalog/genuine/locate") == "two"
    assert manager.session.get.call_count == 3


def test_catalog_refetches_robots_after_24_hours() -> None:
    now = [0.0]
    manager = SessionManager(monotonic=lambda: now[0])
    manager.session.get = Mock(
        side_effect=[
            response(200, "User-agent: *\nDisallow:\n", "https://partsouq.com/robots.txt"),
            response(200, "catalog", "https://partsouq.com/en/catalog/genuine"),
            response(200, "cached", "https://partsouq.com/en/catalog/genuine/locate"),
            response(
                200,
                "User-agent: *\nDisallow: /en/catalog/\n",
                "https://partsouq.com/robots.txt",
            ),
        ]
    )

    assert manager.get("https://partsouq.com/en/catalog/genuine") == "catalog"
    now[0] = float(ROBOTS_CACHE_TTL_SECONDS - 1)
    assert manager.get("https://partsouq.com/en/catalog/genuine/locate") == "cached"
    now[0] = float(ROBOTS_CACHE_TTL_SECONDS)
    with pytest.raises(RobotsPolicyError, match="disallows"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert [call.args[0] for call in manager.session.get.call_args_list] == [
        "https://partsouq.com/robots.txt",
        "https://partsouq.com/en/catalog/genuine",
        "https://partsouq.com/en/catalog/genuine/locate",
        "https://partsouq.com/robots.txt",
    ]


@pytest.mark.parametrize("size_delta", [0, 1])
def test_robots_body_size_limit_is_fail_closed(size_delta: int) -> None:
    prefix = "User-agent: *\nDisallow:\n"
    robots_text = prefix + "#" * (ROBOTS_BODY_MAX_BYTES + size_delta - len(prefix))
    robots_response = response(200, robots_text, "https://partsouq.com/robots.txt")
    responses = [robots_response]
    if size_delta == 0:
        responses.append(response(200, "catalog", "https://partsouq.com/en/catalog/genuine"))
    manager = SessionManager()
    manager.session.get = Mock(side_effect=responses)

    if size_delta == 0:
        assert manager.get("https://partsouq.com/en/catalog/genuine") == "catalog"
        assert manager.session.get.call_count == 2
    else:
        with pytest.raises(RobotsPolicyError, match="byte limit"):
            manager.get("https://partsouq.com/en/catalog/genuine")
        assert manager.session.get.call_count == 1
    robots_response.iter_content.assert_called_once_with(chunk_size=64 * 1024)
    robots_response.close.assert_called_once_with()


def test_robots_stream_stops_after_oversized_chunk_and_closes() -> None:
    second_chunk_requested = [False]

    def chunks():
        yield b"x" * (ROBOTS_BODY_MAX_BYTES + 100)
        second_chunk_requested[0] = True
        yield b"unreachable"

    robots_response = response(200, "", "https://partsouq.com/robots.txt")
    robots_response.iter_content.return_value = chunks()
    manager = SessionManager()
    manager.session.get = Mock(return_value=robots_response)

    with pytest.raises(RobotsPolicyError, match="byte limit"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert second_chunk_requested == [False]
    robots_response.close.assert_called_once_with()


def test_disallowed_catalog_url_is_never_requested() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        return_value=response(
            200,
            "User-agent: *\nDisallow: /en/catalog/\n",
            "https://partsouq.com/robots.txt",
        )
    )

    with pytest.raises(RobotsPolicyError, match="disallows"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_count == 1


def test_named_crawler_rule_takes_precedence_over_wildcard() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        return_value=response(
            200,
            """User-agent: partsouq-catalog-crawler
Disallow: /en/catalog/

User-agent: *
Disallow:
""",
            "https://partsouq.com/robots.txt",
        )
    )

    with pytest.raises(RobotsPolicyError, match="disallows"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_count == 1


def test_robots_wildcard_rule_also_binds_browser_ua_traffic() -> None:
    """合規語意統一：實際請求以 browser UA 送出，`*` 規則也必須允許。"""
    manager = SessionManager()
    manager.session.get = Mock(
        return_value=response(
            200,
            """User-agent: partsouq-catalog-crawler
Disallow:

User-agent: *
Disallow: /en/catalog/
""",
            "https://partsouq.com/robots.txt",
        )
    )

    with pytest.raises(RobotsPolicyError, match="disallows"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_count == 1


@pytest.mark.parametrize(
    "robots_text",
    [
        "User-agent: unrelated-bot\nDisallow: /\n",
        "User-agent: partsouq-catalog-crawler\nCrawl-delay: 10\n",
        "User-agent: *\nDisallow /en/catalog/\n",
        "User-agent: partsouq-catalog-crawler\nAllow: *secret\n",
        "User-agent: partsouq-catalog-crawler\nDisallow: *secret\n",
    ],
)
def test_robots_without_explicit_applicable_access_rule_fails_closed(
    robots_text: str,
) -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        return_value=response(200, robots_text, "https://partsouq.com/robots.txt")
    )

    with pytest.raises(RobotsPolicyError, match="no explicit applicable rules"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_count == 1


@pytest.mark.parametrize(
    ("access_rule", "blocked"),
    [
        ("Disallow: /en/catalog/*", True),
        ("Disallow: /en/catalog/$", False),
        ("Disallow: /en/catalog/$bad", False),
        ("Allow: /en/catalog/*", False),
        ("Allow:", False),
        ("Disallow:", False),
    ],
)
def test_catalog_supports_robots_wildcard_and_end_anchor(
    access_rule: str,
    blocked: bool,
) -> None:
    manager = SessionManager()
    responses = [
        response(
            200,
            f"User-agent: partsouq-catalog-crawler\n{access_rule}\n",
            "https://partsouq.com/robots.txt",
        )
    ]
    if not blocked:
        responses.append(response(200, "catalog", "https://partsouq.com/en/catalog/genuine"))
    manager.session.get = Mock(side_effect=responses)

    if blocked:
        with pytest.raises(RobotsPolicyError, match="disallows"):
            manager.get("https://partsouq.com/en/catalog/genuine")
        assert manager.session.get.call_count == 1
    else:
        assert manager.get("https://partsouq.com/en/catalog/genuine") == "catalog"
        assert manager.session.get.call_count == 2


def test_robots_pattern_for_unrelated_agent_does_not_block_catalog() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        side_effect=[
            response(
                200,
                """User-agent: unrelated-bot
Disallow: /private/*

User-agent: *
Disallow:
""",
                "https://partsouq.com/robots.txt",
            ),
            response(200, "catalog", "https://partsouq.com/en/catalog/genuine"),
        ]
    )

    assert manager.get("https://partsouq.com/en/catalog/genuine") == "catalog"
    assert manager.session.get.call_count == 2


@pytest.mark.parametrize(
    ("robots_response", "error"),
    [
        (
            response(503, "unavailable", "https://partsouq.com/robots.txt"),
            RobotsPolicyError,
        ),
        (response(200, "", "https://partsouq.com/robots.txt"), RobotsPolicyError),
        (
            response(
                403,
                "Just a moment...",
                "https://partsouq.com/robots.txt",
            ),
            ChallengeError,
        ),
    ],
)
def test_unusable_robots_fails_closed_without_catalog_request(
    robots_response: Mock,
    error: type[Exception],
) -> None:
    manager = SessionManager()
    manager.session.get = Mock(return_value=robots_response)

    with pytest.raises(error):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_count == 1


def test_robots_redirect_to_another_origin_fails_closed() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        return_value=response(
            200,
            "User-agent: *\nDisallow:\n",
            "https://example.com/robots.txt",
        )
    )

    with pytest.raises(RobotsPolicyError, match="outside catalog origin"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_count == 1


def test_robots_redirect_is_not_followed() -> None:
    manager = SessionManager()
    manager.session.get = Mock(return_value=response(302, "", "https://partsouq.com/robots.txt"))

    with pytest.raises(RobotsPolicyError, match="robots unavailable"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_args.kwargs["allow_redirects"] is False


def test_catalog_redirect_is_not_followed() -> None:
    manager = SessionManager()
    manager.session.get = Mock(
        side_effect=[
            response(200, "User-agent: *\nDisallow:\n", "https://partsouq.com/robots.txt"),
            response(302, "", "https://partsouq.com/en/catalog/genuine"),
        ]
    )

    with pytest.raises(RobotsPolicyError, match="catalog redirect refused"):
        manager.get("https://partsouq.com/en/catalog/genuine")

    assert manager.session.get.call_count == 2
    assert manager.session.get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://partsouq.com/en/catalog/genuine",
        "https://www.partsouq.com/en/catalog/genuine",
        "https://partsouq.com:8443/en/catalog/genuine",
    ],
)
def test_non_formal_catalog_origin_fails_closed_without_request(url: str) -> None:
    manager = SessionManager()
    manager.session.get = Mock()

    with pytest.raises(RobotsPolicyError, match="unsupported catalog origin"):
        manager.get(url)

    manager.session.get.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "https://partsouq.com/%65n/catalog/genuine",
        "https://partsouq.com/en/%63atalog/genuine",
        "https://partsouq.com/foo/../en/catalog/genuine",
        "https://partsouq.com/en\\catalog/genuine",
    ],
)
def test_ambiguous_partsouq_path_fails_closed_without_request(url: str) -> None:
    manager = SessionManager()
    manager.session.get = Mock()

    with pytest.raises(RobotsPolicyError, match="ambiguous PartSouq path"):
        manager.get(url)

    manager.session.get.assert_not_called()
