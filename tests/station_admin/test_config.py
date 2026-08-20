from partsouq_station_admin.config import AdminConfig


def test_station_admin_uses_shared_database_environment(monkeypatch) -> None:
    monkeypatch.setenv("PARTSOUQ_DB_HOST", "shared-mysql")
    monkeypatch.setenv("PARTSOUQ_DB_PORT", "3306")
    monkeypatch.setenv("PARTSOUQ_DB_USER", "shared-user")
    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "shared-password")
    monkeypatch.setenv("PARTSOUQ_DB_NAME", "shared-database")
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_PAGE_SIZE", "200")

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


def test_invalid_default_page_size_falls_back_to_thirty(monkeypatch) -> None:
    monkeypatch.setenv("PARTSOUQ_STATION_ADMIN_PAGE_SIZE", "15")

    assert AdminConfig.from_env().page_size == 30
