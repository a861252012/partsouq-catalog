from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueueItem:
    id: int
    run_id: int
    requested_url: str
    depth: int
    attempts: int
    page_type_hint: str | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    attempt: int
    redirect_chain: tuple[str, ...] = ()

    @property
    def content_type(self) -> str | None:
        value = self.headers.get("Content-Type") or self.headers.get("content-type")
        return value.split(";", 1)[0].strip() if value else None

    @property
    def charset(self) -> str | None:
        value = self.headers.get("Content-Type") or self.headers.get("content-type", "")
        for part in value.split(";")[1:]:
            key, separator, raw = part.strip().partition("=")
            if separator and key.lower() == "charset":
                return raw.strip('"')
        return None
