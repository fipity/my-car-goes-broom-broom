"""robots.txt per RFC 9309, which the stdlib parser doesn't follow.

urllib.robotparser applies the *first* matching rule and has no `*`/`$` wildcards. Cars24's
file starts its `*` group with `Allow: /`, so the stdlib would allow `/api/` too. Here the
longest matching rule wins, Allow wins a tie, and wildcards work as Google documents them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(rx + ("$" if anchored else ""))


def _norm(path: str) -> str:
    # Compare decoded forms so %2F-style differences don't matter.
    return unquote(path)


class Robots:
    def __init__(self, groups: list[_Group], allow_all: bool = False, disallow_all: bool = False):
        self._groups = groups
        self.allow_all = allow_all
        self.disallow_all = disallow_all

    # -- construction --------------------------------------------------------------------
    @classmethod
    def parse(cls, text: str) -> Robots:
        groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (s.strip() for s in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if current is None:
                continue  # rules before any user-agent line are ignored
            if key in ("allow", "disallow"):
                if value:  # an empty Disallow means "allow everything": no rule needed
                    current.rules.append((key == "allow", _norm(value)))
            elif key == "crawl-delay":
                try:
                    current.crawl_delay = float(value)
                except ValueError:
                    pass
        return cls(groups)

    @classmethod
    def allowing_all(cls) -> Robots:
        return cls([], allow_all=True)

    @classmethod
    def disallowing_all(cls) -> Robots:
        return cls([], disallow_all=True)

    # -- queries -------------------------------------------------------------------------
    def _groups_for(self, user_agent: str) -> list[_Group]:
        # RFC 9309: match the product token ("Mozilla" in "Mozilla/5.0 (...) Chrome/..."),
        # not any substring, or a "User-agent: Safari" group would apply to a Chrome UA.
        m = re.match(r"[A-Za-z_-]+", user_agent.strip())
        token = m.group(0).lower() if m else ""
        specific = [g for g in self._groups if token and token in g.agents]
        if specific:
            return specific
        return [g for g in self._groups if "*" in g.agents]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.allow_all:
            return True
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        target = _norm((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        if target == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None  # (pattern length, allow)
        for group in self._groups_for(user_agent):
            for allow, pattern in group.rules:
                if _compile(pattern).match(target):
                    cand = (len(pattern), allow)
                    # longer pattern wins; on equal length Allow wins
                    if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                        best = cand
        return True if best is None else best[1]

    def crawl_delay(self, user_agent: str) -> float | None:
        delays = [g.crawl_delay for g in self._groups_for(user_agent) if g.crawl_delay is not None]
        return max(delays) if delays else None
