from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _env_page_size(name: str, default: int) -> int:
    value = _env_int(name, default)
    return value if value in {10, 25, 30, 50, 100, 200} else default


@dataclass(frozen=True, slots=True)
class AdminConfig:
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3308
    mysql_user: str = "partsouq"
    mysql_password: str = "partsouq-local"
    mysql_database: str = "partsouq_catalog"
    bind_host: str = "127.0.0.1"
    bind_port: int = 8086
    secret_key: str = ""
    username: str = ""
    password: str = ""
    secure_cookie: bool = False
    default_actor: str = "local-admin"
    page_size: int = 30

    @classmethod
    def from_env(cls) -> AdminConfig:
        return cls(
            mysql_host=os.getenv("PARTSOUQ_DB_HOST", "127.0.0.1"),
            mysql_port=_env_int("PARTSOUQ_DB_PORT", 3308),
            mysql_user=os.getenv("PARTSOUQ_DB_USER", "partsouq"),
            mysql_password=os.getenv(
                "PARTSOUQ_DB_PASSWORD",
                "partsouq-local",
            ),
            mysql_database=os.getenv("PARTSOUQ_DB_NAME", "partsouq_catalog"),
            bind_host=os.getenv("PARTSOUQ_STATION_ADMIN_HOST", "127.0.0.1"),
            bind_port=_env_int("PARTSOUQ_STATION_ADMIN_PORT", 8086),
            secret_key=os.getenv(
                "PARTSOUQ_STATION_ADMIN_SECRET_KEY",
                os.getenv("PARTSOUQ_ADMIN_TOKEN", ""),
            ),
            username=os.getenv("PARTSOUQ_STATION_ADMIN_USERNAME", ""),
            password=os.getenv("PARTSOUQ_STATION_ADMIN_PASSWORD", ""),
            secure_cookie=os.getenv("PARTSOUQ_STATION_ADMIN_SECURE_COOKIE", "0") == "1",
            default_actor=os.getenv("PARTSOUQ_STATION_ADMIN_ACTOR", "local-admin"),
            page_size=_env_page_size("PARTSOUQ_STATION_ADMIN_PAGE_SIZE", 30),
        )

    def resolved_secret_key(self) -> str:
        return self.secret_key or secrets.token_hex(32)

    @property
    def auth_required(self) -> bool:
        return bool(self.username and self.password)

    def validate_server_mode(self) -> None:
        if bool(self.username) != bool(self.password):
            raise ValueError(
                "PARTSOUQ_STATION_ADMIN_USERNAME and "
                "PARTSOUQ_STATION_ADMIN_PASSWORD must be set together"
            )
        if self.auth_required and not self.secret_key:
            raise ValueError("authenticated admin requires PARTSOUQ_STATION_ADMIN_SECRET_KEY")
        if self.bind_host in {"127.0.0.1", "localhost", "::1"}:
            return
        if not self.auth_required:
            raise ValueError(
                "non-loopback admin requires "
                "PARTSOUQ_STATION_ADMIN_USERNAME/PARTSOUQ_STATION_ADMIN_PASSWORD"
            )
        if not self.secure_cookie:
            raise ValueError("non-loopback admin requires PARTSOUQ_STATION_ADMIN_SECURE_COOKIE=1")
