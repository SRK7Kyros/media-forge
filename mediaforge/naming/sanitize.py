"""Windows-safe path components.

Jellyfin reads these names verbatim, so the sanitiser has to be conservative:
strip only what the filesystem forbids and leave everything else — accents,
apostrophes, ``!!`` — intact, because those are the characters a metadata search
matches on.
"""

from __future__ import annotations

ILLEGAL = '<>:"/\\|?*'
RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)
MAX_COMPONENT = 180


def sanitize_component(name: str, *, fallback: str = "untitled") -> str:
    """One safe path component: no illegal characters, no trailing dot or space."""
    cleaned = "".join(" " if ch in ILLEGAL else ch for ch in name)
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 32)
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.split(".")[0].strip().upper() in RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > MAX_COMPONENT:
        cleaned = cleaned[:MAX_COMPONENT].rstrip(" .")
    return cleaned


def tidy(title: str) -> str:
    """Collapse the separators left behind after removing a token."""
    cleaned = " ".join(title.split())
    while "- -" in cleaned:
        cleaned = cleaned.replace("- -", "-")
    cleaned = cleaned.replace(" - - ", " - ").replace(" -  - ", " - ")
    return cleaned.strip(" -")
