from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("NHTSA_HEARTBEAT_INTERVAL_SECONDS", "60"))


@contextmanager
def scheduler_heartbeat(label: str) -> Iterator[None]:
    stop = threading.Event()

    def emit() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            print(f"nhtsa {label}: still working", file=sys.stderr, flush=True)

    thread = threading.Thread(target=emit, name=f"nhtsa-{label}-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
