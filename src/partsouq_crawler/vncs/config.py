from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VNCS_BASE_HOST = "vncs.moenv.gov.tw"
DEFAULT_BASE_URL = f"https://{VNCS_BASE_HOST}/VNCSEXLRPT.aspx"
# VNCS 的 TLS 鏈經過 TWCA Secure SSL 中繼憑證，該憑證缺少 Subject Key
# Identifier 延伸，OpenSSL 3 無法從公共 CA 庫建鏈（macOS curl 可過是因為
# LibreSSL 較寬鬆）。因此預設信任庫改為 repo 內錨定的政府 CA 憑證，
# 仍維持 CERT_REQUIRED 完整驗證，只是信任來源更收斂。
DEFAULT_TLS_CA_BUNDLE = str(Path(__file__).parent / "certs" / "twca-secure-ssl-intermediate.pem")


@dataclass(frozen=True, slots=True)
class VncsConfig:
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3308
    mysql_database: str = "partsouq_catalog"
    mysql_user: str = "partsouq"
    mysql_password: str = "partsouq-local"
    base_url: str = DEFAULT_BASE_URL
    user_agent: str = "vncs-official-data-sync/0.1"
    # 政府站台禮節節流：兩次 HTTP 請求（含瀏覽器翻頁）之間至少間隔 1 秒。
    rate_limit_seconds: float = 1.0
    request_timeout_seconds: float = 60.0
    tls_ca_bundle: str = DEFAULT_TLS_CA_BUNDLE
    # 瀏覽器路線（Playwright + Infragistics JS API 翻頁）設定。
    browser_headless: bool = True
    browser_timeout_seconds: float = 60.0
    max_pages_per_kind: int | None = None

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
            "tls_ca_bundle": os.getenv("VNCS_TLS_CA_BUNDLE", DEFAULT_TLS_CA_BUNDLE),
            "browser_headless": _env_bool("VNCS_BROWSER_HEADLESS", default=True),
            "browser_timeout_seconds": float(os.getenv("VNCS_BROWSER_TIMEOUT_SECONDS", "60")),
            "max_pages_per_kind": _env_optional_int("VNCS_MAX_PAGES_PER_KIND"),
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
            tls_ca_bundle=str(values["tls_ca_bundle"]),
            browser_headless=bool(values["browser_headless"]),
            browser_timeout_seconds=float(str(values["browser_timeout_seconds"])),
            max_pages_per_kind=_optional_int_value(values["max_pages_per_kind"]),
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
        if not self.tls_ca_bundle or not Path(self.tls_ca_bundle).is_file():
            raise ValueError(f"VNCS TLS CA bundle does not exist: {self.tls_ca_bundle!r}")
        if self.browser_timeout_seconds <= 0:
            raise ValueError("VNCS browser timeout must be positive")
        if self.max_pages_per_kind is not None and self.max_pages_per_kind < 1:
            raise ValueError("VNCS max pages per kind must be at least 1 when provided")

    def public_dict(self) -> dict[str, object]:
        return {
            "mysql_host": self.mysql_host,
            "mysql_port": self.mysql_port,
            "mysql_database": self.mysql_database,
            "mysql_user": self.mysql_user,
            "base_url": self.base_url,
            "rate_limit_seconds": self.rate_limit_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "browser_headless": self.browser_headless,
            "browser_timeout_seconds": self.browser_timeout_seconds,
            "max_pages_per_kind": self.max_pages_per_kind,
        }


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return int(raw.strip())


def _optional_int_value(value: object) -> int | None:
    return None if value is None else int(str(value))
