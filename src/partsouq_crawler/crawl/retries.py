from __future__ import annotations

import email.utils
import random
from datetime import UTC, datetime

RETRYABLE_STATUS = {500, 502, 503, 504}


def retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(retry_after)
                if not isinstance(parsed, datetime):
                    raise ValueError("invalid Retry-After date")
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return max(0.0, float((parsed - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError):
                pass
    base: float = min(60.0, float(2 ** max(0, attempt - 1)))
    jitter: float = float(random.uniform(0.0, base * 0.25))
    return base + jitter
