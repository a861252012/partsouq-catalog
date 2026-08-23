from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from partsouq_crawler.nhtsa.config import NhtsaConfig
from partsouq_crawler.nhtsa.models import NhtsaRunLease

HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("NHTSA_HEARTBEAT_INTERVAL_SECONDS", "60"))
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 20.0


def _report_cleanup_failure(label: str, error: BaseException) -> None:
    try:
        print(f"nhtsa {label} cleanup failed: {error}", file=sys.stderr, flush=True)
    except BaseException:
        pass


@contextmanager
def scheduler_heartbeat(label: str) -> Iterator[None]:
    stop = threading.Event()
    failure: list[BaseException] = []

    def emit() -> None:
        try:
            while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                print(f"nhtsa {label}: still working", file=sys.stderr, flush=True)
        except BaseException as error:
            failure.append(error)
            stop.set()

    thread = threading.Thread(target=emit, name=f"nhtsa-{label}-heartbeat", daemon=True)
    thread.start()
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as error:
        primary_error = error
        raise
    finally:
        stop.set()
        thread.join(HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            failure.append(RuntimeError(f"NHTSA {label} heartbeat did not stop before timeout"))
        if primary_error is not None and failure:
            _report_cleanup_failure(f"{label} heartbeat", failure[0])
    if primary_error is None and failure:
        raise failure[0]


@contextmanager
def lease_heartbeat(
    config: NhtsaConfig,
    lease: NhtsaRunLease,
) -> Iterator[Callable[[], None]]:
    stop = threading.Event()
    failure: list[BaseException] = []

    def emit() -> None:
        from partsouq_crawler.nhtsa.repository import (
            HEARTBEAT_DB_TIMEOUT_SECONDS,
            NhtsaMySQLRepository,
        )

        repository: NhtsaMySQLRepository | None = None
        try:
            repository = NhtsaMySQLRepository.create(
                config,
                timeout_seconds=HEARTBEAT_DB_TIMEOUT_SECONDS,
            )
            while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                try:
                    repository.heartbeat(lease)
                except BaseException as error:
                    failure.append(error)
                    stop.set()
                    return
        except BaseException as error:
            failure.append(error)
        finally:
            if repository is not None:
                try:
                    repository.close()
                except BaseException as error:
                    failure.append(error)

    def check() -> None:
        if failure:
            raise failure[0]

    thread = threading.Thread(target=emit, name="nhtsa-db-lease-heartbeat", daemon=True)
    thread.start()
    primary_error: BaseException | None = None
    try:
        yield check
    except BaseException as error:
        primary_error = error
        raise
    finally:
        stop.set()
        thread.join(HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            failure.append(RuntimeError("NHTSA lease heartbeat did not stop before timeout"))
        if primary_error is not None and failure:
            _report_cleanup_failure("lease heartbeat", failure[0])
    if primary_error is None:
        check()
