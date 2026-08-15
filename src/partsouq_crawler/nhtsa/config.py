from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = (
    Path(os.getenv("PARTSOUQ_HOME", str(Path(__file__).resolve().parents[3])))
    .expanduser()
    .resolve()
)


@dataclass(frozen=True, slots=True)
class NhtsaConfig:
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3308
    mysql_database: str = "partsouq_catalog"
    mysql_user: str = "partsouq"
    mysql_password: str = "partsouq-local"
    raw_dir: Path = PROJECT_DIR / "output/nhtsa/raw"
    user_agent: str = "nhtsa-official-data-sync/0.1"
    request_timeout_seconds: float = 120.0
    api_delay_seconds: float = 0.2

    @classmethod
    def from_env(cls, **overrides: object) -> NhtsaConfig:
        values: dict[str, object] = {
            "mysql_host": os.getenv("PARTSOUQ_DB_HOST", "127.0.0.1"),
            "mysql_port": int(os.getenv("PARTSOUQ_DB_PORT", "3308")),
            "mysql_database": os.getenv("PARTSOUQ_DB_NAME", "partsouq_catalog"),
            "mysql_user": os.getenv("PARTSOUQ_DB_USER", "partsouq"),
            "mysql_password": os.getenv("PARTSOUQ_DB_PASSWORD", "partsouq-local"),
            "raw_dir": Path(os.getenv("NHTSA_RAW_DIR", str(PROJECT_DIR / "output/nhtsa/raw"))),
            "user_agent": os.getenv("NHTSA_USER_AGENT", "nhtsa-official-data-sync/0.1"),
            "request_timeout_seconds": float(os.getenv("NHTSA_REQUEST_TIMEOUT_SECONDS", "120")),
            "api_delay_seconds": float(os.getenv("NHTSA_API_DELAY_SECONDS", "0.2")),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        config = cls(
            mysql_host=str(values["mysql_host"]),
            mysql_port=int(str(values["mysql_port"])),
            mysql_database=str(values["mysql_database"]),
            mysql_user=str(values["mysql_user"]),
            mysql_password=str(values["mysql_password"]),
            raw_dir=Path(str(values["raw_dir"])),
            user_agent=str(values["user_agent"]),
            request_timeout_seconds=float(str(values["request_timeout_seconds"])),
            api_delay_seconds=float(str(values["api_delay_seconds"])),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.mysql_host or not self.mysql_database or not self.mysql_user:
            raise ValueError("NHTSA MySQL host, database, and user are required")
        if not 1 <= self.mysql_port <= 65535:
            raise ValueError("NHTSA MySQL port must be between 1 and 65535")
        if self.request_timeout_seconds <= 0:
            raise ValueError("NHTSA request timeout must be positive")
        if self.api_delay_seconds < 0:
            raise ValueError("NHTSA API delay must not be negative")
        if not self.user_agent.strip():
            raise ValueError("NHTSA user agent must not be empty")

    def public_dict(self) -> dict[str, object]:
        return {
            "mysql_host": self.mysql_host,
            "mysql_port": self.mysql_port,
            "mysql_database": self.mysql_database,
            "mysql_user": self.mysql_user,
            "raw_dir": str(self.raw_dir),
            "request_timeout_seconds": self.request_timeout_seconds,
            "api_delay_seconds": self.api_delay_seconds,
        }
