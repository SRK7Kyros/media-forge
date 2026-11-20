"""Watch-order resolution, driven by ``franchise_rules.toml``.

The number a rule produces is *ordering data*, never a rename: the folder keeps
its clean title and the order goes into ``<sorttitle>``. That is the whole
mechanism decision 3 asked for, and it is why ``MCU 3.01`` and ``MCU 4.1`` — the
library's real inconsistency — both sort correctly without touching a filename.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .parse import roman_to_int


@dataclass(frozen=True, slots=True)
class Rule:
    key: str
    sort_key: str
    set_name: str | None
    match: re.Pattern[str] | None
    order_mode: str
    strip: bool
    table: dict[str, int]


@dataclass(frozen=True, slots=True)
class FranchiseMatch:
    key: str
    sort_key: str
    set_name: str | None
    order: tuple[int, ...]
    clean_title: str
    strip_prefix: bool


def rules_path() -> Path:
    return Path(__file__).with_name("franchise_rules.toml")


@lru_cache(maxsize=4)
def load_rules(path: Path | None = None) -> tuple[Rule, ...]:
    target = path or rules_path()
    with open(target, "rb") as handle:
        document = tomllib.load(handle)
    rules: list[Rule] = []
    for entry in document.get("rule", []):
        pattern = entry.get("match")
        rules.append(
            Rule(
                key=entry["key"],
                sort_key=entry.get("sort_key", entry["key"]),
                set_name=entry.get("set_name"),
                match=re.compile(pattern) if pattern else None,
                order_mode=entry.get("order", "table"),
                strip=bool(entry.get("strip", False)),
                table={str(k): int(v) for k, v in (entry.get("table") or {}).items()},
            )
        )
    return tuple(rules)


def _order_from_rule(rule: Rule, match: re.Match[str] | None) -> tuple[int, ...] | None:
    if rule.order_mode == "dotted":
        if match is None:
            return None
        parts = match.group("order").split(".")
        try:
            return tuple(int(part) for part in parts)
        except ValueError:
            return None
    if rule.order_mode == "roman":
        if match is None:
            return None
        value = roman_to_int(match.group("roman"))
        return None if value is None else (value,)
    if rule.order_mode == "int":
        if match is None:
            return None
        try:
            return (int(match.group("num")),)
        except (IndexError, ValueError):
            return None
    return None


def resolve(base: str, rules: tuple[Rule, ...] | None = None) -> FranchiseMatch | None:
    """Resolve a year-stripped folder name to its franchise and watch order."""
    for rule in rules if rules is not None else load_rules():
        if rule.order_mode == "table":
            if base not in rule.table:
                continue
            return FranchiseMatch(
                key=rule.key,
                sort_key=rule.sort_key,
                set_name=rule.set_name,
                order=(rule.table[base],),
                clean_title=base,
                strip_prefix=False,
            )
        match = rule.match.match(base) if rule.match else None
        if match is None:
            continue
        order = _order_from_rule(rule, match)
        if order is None:
            continue
        title = base
        if rule.strip:
            groups = match.groupdict()
            if groups.get("title"):
                title = groups["title"].strip()
        return FranchiseMatch(
            key=rule.key,
            sort_key=rule.sort_key,
            set_name=rule.set_name,
            order=order,
            clean_title=title,
            strip_prefix=rule.strip,
        )
    return None


def sort_title(match: FranchiseMatch) -> str:
    """``MCU 01.01`` for a saga entry, ``ROCKY 06`` for a plain sequence."""
    joined = ".".join(f"{part:02d}" for part in match.order)
    return f"{match.sort_key} {joined}"
