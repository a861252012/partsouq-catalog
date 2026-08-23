from __future__ import annotations

import os
from dataclasses import dataclass

VNCS_BASE_HOST = "vncs.moenv.gov.tw"
DEFAULT_BASE_URL = f"https://{VNCS_BASE_HOST}/VNCSEXLRPT.aspx"


@dataclass(frozen=True, slots=True)
class VncsConfig:
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3308
    mysql_database: str = "partsouq_catalog"
    mysql_user: str = "partsouq"
    mysql_password: str = "partsouq-local"
    base_url: str = DEFAULT_BASE_URL
    user_agent: str = "vncs-official-data-sync/0.1"
    # 政府站台禮節節流：兩次 HTTP 請求之間至少間隔 1 秒。
    rate_limit_seconds: float = 1.0
    request_timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls, **overrides: object) -> VncsConfig:
        values: dict[str, object] = {
            "mysql_host": os.getenv("PARTSOUQ_DB_HOST", "127.0.0.1"),
            "mysql_port": int(os.getenv("PARTSOUQ_DB_PORT", "3308")),
            "mysql_database": os.getenv("PARTSOUQ_DB_NAME", "partsouq_catalog"),
            "mysql_user": os.getenv("PARTSOUQ_DB_USER", "partsouq"),
            "mysql_password": os.getenv("PARTSOUQ_DB_PASSWORD", "partsouq-local"),
            "base_url": os.getenv("VNCS_BASE_URL", DEFAULT_BASE_URL),
            "user_agent": os.getenv("VNCS_USER_AGENT", "vncs-official-data-sync/0.1"),
            "rate_limit_seconds": float(os.getenv("VNCS_RATE_LIMIT_SECONDS", "1")),
            "request_timeout_seconds": float(os.getenv("VNCS_REQUEST_TIMEOUT_SECONDS", "60")),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        config = cls(
            mysql_host=str(values["mysql_host"]),
            mysql_port=int(str(values["mysql_port"])),
            mysql_database=str(values["mysql_database"]),
            mysql_user=str(values["mysql_user"]),
            mysql_password=str(values["mysql_password"]),
            base_url=str(values["base_url"]),
            user_agent=str(values["user_agent"]),
            rate_limit_seconds=float(str(values["rate_limit_seconds"])),
            request_timeout_seconds=float(str(values["request_timeout_seconds"])),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.mysql_host or not self.mysql_database or not self.mysql_user:
            raise ValueError("VNCS MySQL host, database, and user are required")
        if not 1 <= self.mysql_port <= 65535:
            raise ValueError("VNCS MySQL port must be between 1 and 65535")
        allowed_prefixes = (
            f"https://{VNCS_BASE_HOST}/",
            f"http://{VNCS_BASE_HOST}/",
        )
        if not self.base_url.startswith(allowed_prefixes):
            raise ValueError(f"VNCS base URL must stay on {VNCS_BASE_HOST}")
        if self.request_timeout_seconds <= 0:
            raise ValueError("VNCS request timeout must be positive")
        if self.rate_limit_seconds < 1:
            raise ValueError("VNCS rate limit must be at least 1 second")
        if not self.user_agent.strip():
            raise ValueError("VNCS user agent must not be empty")

    def public_dict(self) -> dict[str, object]:
        return {
            "mysql_host": self.mysql_host,
            "mysql_port": self.mysql_port,
            "mysql_database": self.mysql_database,
            "mysql_user": self.mysql_user,
            "base_url": self.base_url,
            "rate_limit_seconds": self.rate_limit_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
        }
