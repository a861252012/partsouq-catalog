import pytest

from partsouq_station_admin.config import AdminConfig


def test_station_admin_uses_shared_database_environment(monkeypatch) -> None:
    monkeypatch.setenv("PARTSOUQ_DB_HOST", "shared-mysql")
    monkeypatch.setenv("PARTSOUQ_DB_PORT", "3306")
    monkeypatch.setenv("PARTSOUQ_DB_USER", "shared-user")
    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "shared-password")
    monkeypatch.setenv("PARTSOUQ_DB_NAME", "shared-database")
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_PAGE_SIZE", "200")
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_REQUIRE_AUTH", "1")

    config = AdminConfig.from_env()

    assert (
        config.mysql_host,
        config.mysql_port,
        config.mysql_user,
        config.mysql_password,
        config.mysql_database,
    ) == (
        "shared-mysql",
        3306,
        "shared-user",
        "shared-password",
        "shared-database",
    )
    assert config.page_size == 200
    assert config.require_auth is True


def test_invalid_default_page_size_falls_back_to_thirty(monkeypatch) -> None:
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_PAGE_SIZE", "15")

    assert AdminConfig.from_env().page_size == 30


def test_station_admin_secret_never_falls_back_to_shared_api_token(monkeypatch) -> None:
    monkeypatch.delenv("PARTSOUQ_STATION_ADMIN_SECRET_KEY", raising=False)
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "shared-api-token")
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_PASSWORD", "password")

    config = AdminConfig.from_env()

    assert config.secret_key == ""
    with pytest.raises(ValueError, match="STATION_ADMIN_SECRET_KEY"):
        config.validate_server_mode()


def test_station_admin_allowed_hosts_are_explicitly_configurable(monkeypatch) -> None:
    monkeypatch.setenv(
        "PARTSOUQ_STATION_ADMIN_ALLOWED_HOSTS",
        " admin.partsouq.localhost,127.0.0.1 ",
    )

    assert AdminConfig.from_env().allowed_hosts == (
        "admin.partsouq.localhost",
        "127.0.0.1",
    )


def test_station_admin_rejects_empty_host_allowlist() -> None:
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        AdminConfig(allowed_hosts=()).validate_server_mode()


def test_station_admin_deployment_requires_auth_even_on_loopback() -> None:
    with pytest.raises(ValueError, match="deployment requires"):
        AdminConfig(bind_host="127.0.0.1", require_auth=True).validate_server_mode()

    AdminConfig(
        bind_host="127.0.0.1",
        require_auth=True,
        secret_key="station-secret",
        username="admin",
        password="password",
    ).validate_server_mode()
