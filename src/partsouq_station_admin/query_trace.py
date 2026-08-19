from __future__ import annotations

import re
from dataclasses import dataclass, field

_COMMENT_RE = re.compile(r"^\s*/\*\s*admin:[^*]+\*/\s*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def sql_fingerprint(sql: str) -> str:
    """Return a stable fingerprint without values or the per-call query tag."""
    without_tag = _COMMENT_RE.sub("", sql)
    return _WHITESPACE_RE.sub(" ", without_tag).strip().lower()


@dataclass(frozen=True, slots=True)
class QueryEvent:
    tag: str
    fingerprint: str
    elapsed_ms: float
    row_count: int


@dataclass(slots=True)
class QueryTrace:
    events: list[QueryEvent] = field(default_factory=list)

    def record(self, *, tag: str, sql: str, elapsed_ms: float, row_count: int) -> None:
        self.events.append(
            QueryEvent(
                tag=tag,
                fingerprint=sql_fingerprint(sql),
                elapsed_ms=elapsed_ms,
                row_count=row_count,
            )
        )

    @property
    def count(self) -> int:
        return len(self.events)

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(event.fingerprint for event in self.events)

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(event.tag for event in self.events)
