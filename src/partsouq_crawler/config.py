from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SEED = "https://partsouq.com/en/catalog/genuine"


@dataclass(frozen=True, slots=True)
class CrawlerConfig:
    database: Path = Path("output/partsouq-live.sqlite3")
    concurrency: int = 1
    delay_seconds: float = 5.0
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    max_pages: int = 0
    max_depth: int = 0
    user_agent: str = ""
    robots_policy: str = "require"
    lease_seconds: int = 120
    log_json: bool = False
    transport: str = "http"
    browser_executable: Path | None = None
    browser_headless: bool = False

    @classmethod
    def from_env(cls, **overrides: object) -> CrawlerConfig:
        values: dict[str, object] = {
            "database": Path(os.getenv("PARTSOUQ_DATABASE", "output/partsouq-live.sqlite3")),
            "concurrency": int(os.getenv("PARTSOUQ_CONCURRENCY", "1")),
            "delay_seconds": float(os.getenv("PARTSOUQ_DELAY_SECONDS", "5")),
            "request_timeout_seconds": float(os.getenv("PARTSOUQ_REQUEST_TIMEOUT_SECONDS", "30")),
            "max_retries": int(os.getenv("PARTSOUQ_MAX_RETRIES", "3")),
            "user_agent": os.getenv("PARTSOUQ_USER_AGENT", ""),
            "transport": os.getenv("PARTSOUQ_TRANSPORT", "http"),
            "browser_executable": os.getenv("PARTSOUQ_BROWSER_EXECUTABLE") or None,
            "browser_headless": os.getenv("PARTSOUQ_BROWSER_HEADLESS", "0").lower()
            in {"1", "true", "yes"},
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        config = cls(
            database=Path(str(values["database"])),
            concurrency=int(str(values["concurrency"])),
            delay_seconds=float(str(values["delay_seconds"])),
            request_timeout_seconds=float(str(values["request_timeout_seconds"])),
            max_retries=int(str(values["max_retries"])),
            max_pages=int(str(values.get("max_pages", 0))),
            max_depth=int(str(values.get("max_depth", 0))),
            user_agent=str(values["user_agent"]),
            robots_policy=str(values.get("robots_policy", "require")),
            lease_seconds=int(str(values.get("lease_seconds", 120))),
            log_json=bool(values.get("log_json", False)),
            transport=str(values.get("transport", "http")),
            browser_executable=(
                Path(str(values["browser_executable"]))
                if values.get("browser_executable")
                else None
            ),
            browser_headless=bool(values.get("browser_headless", False)),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.delay_seconds < 0:
            raise ValueError("delay must not be negative")
        if self.request_timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if self.max_pages < 0 or self.max_depth < 0:
            raise ValueError("max-pages and max-depth use 0 for unlimited")
        if self.robots_policy not in {"require", "ignore"}:
            raise ValueError("robots-policy must be require or ignore")
        if self.transport not in {"http", "browser"}:
            raise ValueError("transport must be http or browser")
        if self.transport == "browser" and self.concurrency != 1:
            raise ValueError("browser transport requires concurrency 1")

    def public_dict(self) -> dict[str, object]:
        return {
            "database": str(self.database),
            "concurrency": self.concurrency,
            "delay_seconds": self.delay_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_retries": self.max_retries,
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "robots_policy": self.robots_policy,
            "lease_seconds": self.lease_seconds,
            "transport": self.transport,
            "browser_executable": (
                str(self.browser_executable) if self.browser_executable else None
            ),
            "browser_headless": self.browser_headless,
        }
