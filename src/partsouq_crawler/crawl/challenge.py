from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChallengeDecision:
    challenged: bool
    reason: str | None = None


CHALLENGE_MARKERS = (
    b"just a moment",
    b"enable javascript and cookies to continue",
    b"/cdn-cgi/challenge-platform/",
    b"managed challenge",
    b"cf-chl-",
)


def detect_challenge(status: int, headers: dict[str, str], body: bytes) -> ChallengeDecision:
    lowered_headers = {key.lower(): value.lower() for key, value in headers.items()}
    lowered_body = body[:1_000_000].lower()
    if lowered_headers.get("cf-mitigated") == "challenge":
        return ChallengeDecision(True, "cloudflare_challenge")
    if any(marker in lowered_body for marker in CHALLENGE_MARKERS):
        reason = (
            "cloudflare_challenge"
            if "cloudflare" in lowered_headers.get("server", "")
            else "access_denied"
        )
        return ChallengeDecision(True, reason)
    if status == 403 and (
        "cloudflare" in lowered_headers.get("server", "") or b"access denied" in lowered_body
    ):
        return ChallengeDecision(True, "access_denied")
    return ChallengeDecision(False)
