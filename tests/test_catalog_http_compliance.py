from unittest.mock import Mock

import pytest

from partsouq_catalog.http_client import (
    CATALOG_USER_AGENT,
    ChallengeError,
    RobotsPolicyError,
    SessionManager,
)


def response(status_code: int, text: str, url: str) -> Mock:
    return Mock(status_code=status_code, text=text, url=url, headers={})


def test_catalog_uses_identifiable_non_browser_user_agent() -> None:
    manager = SessionManager()

    assert manager.session.headers["User-Agent"] == CATALOG_USER_AGENT
    assert "github.com/a861252012" in CATALOG_USER_AGENT
    assert "Mozilla" not in CATALOG_USER_AGENT
    assert "Chrome" not in CATALOG_USER_AGENT


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


@pytest.mark.parametrize(
    "robots_text",
    [
        "User-agent: unrelated-bot\nDisallow: /\n",
        "User-agent: partsouq-catalog-crawler\nCrawl-delay: 10\n",
        "User-agent: *\nDisallow /en/catalog/\n",
        "User-agent: partsouq-catalog-crawler\nDisallow: /en/catalog/$bad\n",
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


def test_transport_ignores_environment_proxy_and_discards_cookies() -> None:
    manager = SessionManager()

    def fake_get(*_args, **_kwargs) -> Mock:
        assert not manager.session.cookies
        manager.session.cookies.set("session", "must-not-persist")
        return response(200, "ok", "https://partsouq.example/status")

    manager.session.cookies.set("injected", "must-not-send")
    manager.session.get = Mock(side_effect=fake_get)

    assert manager.get("https://partsouq.example/status") == "ok"
    assert manager.session.trust_env is False
    assert manager.session.proxies == {}
    assert not manager.session.cookies
