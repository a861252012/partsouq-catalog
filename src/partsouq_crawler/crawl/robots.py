from __future__ import annotations

import re
import string
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class _AccessRule:
    allow: bool
    pattern: str


@dataclass(frozen=True, slots=True)
class _AccessGroup:
    user_agents: tuple[str, ...]
    rules: tuple[_AccessRule, ...]


def _is_valid_robots_pattern(value: str) -> bool:
    """路徑樣板必須以 `/` 起頭；`$` 僅能作為結尾錨點。"""
    if not value or value.startswith("/"):
        return "$" not in value[:-1]
    return False


def _access_groups(text: str) -> tuple[_AccessGroup, ...]:
    groups: list[_AccessGroup] = []
    user_agents: list[str] = []
    rules: list[_AccessRule] = []
    for raw_line in text.splitlines():
        line = raw_line.partition("#")[0].strip()
        directive, separator, value = line.partition(":")
        if not separator:
            continue
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            if user_agents and rules:
                groups.append(_AccessGroup(tuple(user_agents), tuple(rules)))
                user_agents = []
                rules = []
            if value:
                user_agents.append(value)
        elif user_agents and directive in {"allow", "disallow"}:
            if _is_valid_robots_pattern(value):
                rules.append(_AccessRule(directive == "allow", value))
    if user_agents:
        groups.append(_AccessGroup(tuple(user_agents), tuple(rules)))
    return tuple(groups)


def _applicable_rules(text: str, user_agent: str) -> tuple[_AccessRule, ...] | None:
    product_token = user_agent.split("/", 1)[0].lower()
    matches: list[_AccessGroup] = []
    wildcard_groups: list[_AccessGroup] = []
    for group in _access_groups(text):
        if any(agent != "*" and agent.lower() == product_token for agent in group.user_agents):
            matches.append(group)
        elif any(agent == "*" for agent in group.user_agents):
            wildcard_groups.append(group)

    selected = tuple(matches or wildcard_groups)
    rules = tuple(rule for group in selected for rule in group.rules)
    if rules:
        return rules
    return None


_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")
_HEX = frozenset(string.hexdigits)


def _normalize_literal(value: str, *, preserve_slash: bool) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if (
            character == "%"
            and index + 2 < len(value)
            and value[index + 1] in _HEX
            and value[index + 2] in _HEX
        ):
            octet = int(value[index + 1 : index + 3], 16)
            decoded = chr(octet)
            normalized.append(decoded if decoded in _UNRESERVED else f"%{octet:02X}")
            index += 3
            continue
        if character in _UNRESERVED or (character == "/" and preserve_slash):
            normalized.append(character)
        else:
            normalized.extend(f"%{octet:02X}" for octet in character.encode("utf-8"))
        index += 1
    return "".join(normalized)


def _normalize_path_query(value: str, *, pattern: bool) -> str:
    path, separator, query = value.partition("?")
    path_separator = r"[^*]+" if pattern else r".+"
    normalized_path = re.sub(
        path_separator,
        lambda match: _normalize_literal(match[0], preserve_slash=True),
        path,
    )
    if not separator:
        return normalized_path
    query_separator = r"[^=&*]+" if pattern else r"[^=&]+"
    normalized_query = re.sub(
        query_separator,
        lambda match: _normalize_literal(match[0], preserve_slash=False),
        query,
    )
    return f"{normalized_path}?{normalized_query}"


def _normalized_url_path(url: str) -> str:
    parsed = urlsplit(url)
    path_query = parsed.path or "/"
    if parsed.query or "?" in url.partition("#")[0]:
        path_query += f"?{parsed.query}"
    return _normalize_path_query(path_query, pattern=False)


def _normalized_pattern(pattern: str) -> tuple[str, bool]:
    full_match = pattern.endswith("$")
    if full_match:
        pattern = pattern[:-1]
    return _normalize_path_query(pattern, pattern=True), full_match


def _pattern_octet_length(pattern: str) -> int:
    length = 0
    index = 0
    while index < len(pattern):
        if pattern[index] == "*":
            index += 1
            continue
        if pattern[index] == "%":
            index += 3
        else:
            index += 1
        length += 1
    return length


def _matching_rule_specificity(rule: _AccessRule, path: str) -> int | None:
    if not rule.pattern:
        return None
    pattern, full_match = _normalized_pattern(rule.pattern)
    expression = "^" + ".*".join(re.escape(part) for part in pattern.split("*"))
    if full_match:
        expression += "$"
    if re.match(expression, path) is None:
        return None
    return _pattern_octet_length(pattern)


def has_applicable_access_rules(text: str, user_agent: str) -> bool:
    """確認指定 UA 具有明確的 Allow／Disallow 指令。"""
    return _applicable_rules(text, user_agent) is not None


def is_allowed(text: str, user_agent: str, url: str) -> bool:
    rules = _applicable_rules(text, user_agent)
    if rules is None:
        return True
    path = _normalized_url_path(url)
    matches = [
        (specificity, rule.allow)
        for rule in rules
        if (specificity := _matching_rule_specificity(rule, path)) is not None
    ]
    if not matches:
        return True
    most_specific = max(specificity for specificity, _allow in matches)
    return any(allow for specificity, allow in matches if specificity == most_specific)


@dataclass(frozen=True, slots=True)
class RobotsRules:
    url: str
    text: str
    sitemaps: tuple[str, ...]

    def allows(self, user_agent: str, url: str) -> bool:
        return is_allowed(self.text, user_agent, url)


def parse_robots(url: str, body: bytes, charset: str = "utf-8") -> RobotsRules:
    text = body.decode(charset, errors="replace")
    sitemaps = tuple(
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.lower().startswith("sitemap:") and line.split(":", 1)[1].strip()
    )
    return RobotsRules(url=url, text=text, sitemaps=sitemaps)
