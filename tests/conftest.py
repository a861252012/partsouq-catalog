from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def serialize_shared_mysql_gates() -> Iterator[None]:
    gate_names = ("NHTSA_TEST_MYSQL", "UNIFIED_TEST_MYSQL", "STATION_ADMIN_E2E")
    if not any(os.getenv(name) == "1" for name in gate_names):
        yield
        return

    default_path = Path(tempfile.gettempdir()) / "partsouq-catalog-mysql-tests.lock"
    lock_path = Path(os.getenv("PARTSOUQ_TEST_LOCK_PATH", str(default_path)))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
