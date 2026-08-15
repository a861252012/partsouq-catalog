from __future__ import annotations

from dataclasses import dataclass
from urllib.robotparser import RobotFileParser


@dataclass(frozen=True, slots=True)
class RobotsRules:
    url: str
    text: str
    sitemaps: tuple[str, ...]

    def allows(self, user_agent: str, url: str) -> bool:
        parser = RobotFileParser()
        parser.set_url(self.url)
        parser.parse(self.text.splitlines())
        return parser.can_fetch(user_agent, url)


def parse_robots(url: str, body: bytes, charset: str = "utf-8") -> RobotsRules:
    text = body.decode(charset, errors="replace")
    sitemaps = tuple(
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.lower().startswith("sitemap:") and line.split(":", 1)[1].strip()
    )
    return RobotsRules(url=url, text=text, sitemaps=sitemaps)
