from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class CrawlLogger:
    def __init__(self, *, json_mode: bool = False) -> None:
        self.json_mode = json_mode
        self.logger = logging.getLogger("partsouq_crawler")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    def event(self, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        if self.json_mode:
            self.logger.info(json.dumps(payload, ensure_ascii=False, default=str))
            return
        details = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        self.logger.info("%s%s", event, f" {details}" if details else "")
