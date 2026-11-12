"""Absolute episode numbers resolved onto a metadata provider's season layout.

The layout is *data*, never a live lookup: the pipeline has no network access and
a rerun has to be deterministic, so a mapped show is a snapshot in
``plugins/anime_seasons.json`` that records the provider id and the date it was
taken. A show without an entry keeps the single-season layout.

An entry stores only ``(season, episode_count)`` because the seasons of a
long-running show partition the absolute numbering: One Piece's seasons 1-16 hold
692 episodes, so absolute 700 belongs to season 17. Whether the file name keeps
the absolute number or restarts at 1 inside the season is the entry's ``mode``,
because providers disagree: TMDB numbers One Piece 693-748 inside season 17, but
numbers a show built out of separate series 1-47 inside each season.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: ``absolute`` keeps the provider's global episode number in the file name
#: (season 17 of One Piece holds 693-748, so the file is ``S17E700``).
#: ``per_season`` restarts at 1 inside every season (``S02E16``).
MODES = frozenset({"absolute", "per_season"})


class SeasonMapError(ValueError):
    """``anime_seasons.json`` is unreadable or self-contradictory."""


@dataclass(frozen=True, slots=True)
class SeasonSpan:
    season: int
    count: int


@dataclass(frozen=True, slots=True)
class ShowSeasons:
    """One show's season layout, as a snapshot of its metadata provider."""

    title: str
    year: int | None
    mode: str
    seasons: tuple[SeasonSpan, ...]

    @property
    def episode_count(self) -> int:
        """Last absolute episode number the map covers."""
        return sum(span.count for span in self.seasons)

    def resolve(self, absolute: int) -> tuple[int, int] | None:
        """``700`` → ``(17, 700)``; ``None`` when no season claims that number."""
        if absolute < 1:
            return None
        offset = 0
        for span in self.seasons:
            if offset < absolute <= offset + span.count:
                episode = absolute if self.mode == "absolute" else absolute - offset
                return span.season, episode
            offset += span.count
        return None


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse(document: object, *, source: str = "<map>") -> tuple[ShowSeasons, ...]:
    """Validate a map document. Unknown top-level keys are ignored."""
    if not isinstance(document, dict):
        raise SeasonMapError(f"{source}: expected an object at the top level")
    shows = document.get("shows")
    if not isinstance(shows, list):
        raise SeasonMapError(f"{source}: 'shows' must be a list")
    entries: list[ShowSeasons] = []
    seen: set[tuple[str, int | None]] = set()
    for index, raw in enumerate(shows):
        entries.append(_entry(raw, f"{source}: shows[{index}]", seen))
    return tuple(entries)


def _entry(raw: object, where: str, seen: set[tuple[str, int | None]]) -> ShowSeasons:
    if not isinstance(raw, dict):
        raise SeasonMapError(f"{where}: expected an object")
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise SeasonMapError(f"{where}: 'title' must be a non-empty string")
    year = raw.get("year")
    if year is not None and not _int(year):
        raise SeasonMapError(f"{where}: 'year' must be an integer or null")
    mode = raw.get("mode")
    if mode not in MODES:
        raise SeasonMapError(f"{where}: 'mode' must be one of {sorted(MODES)}")
    spans = _spans(raw.get("seasons"), where)
    key = (" ".join(title.split()).casefold(), year)
    if key in seen:
        raise SeasonMapError(f"{where}: {title.strip()!r} ({year}) is listed twice")
    seen.add(key)
    return ShowSeasons(title=title.strip(), year=year, mode=mode, seasons=spans)


def _spans(raw: object, where: str) -> tuple[SeasonSpan, ...]:
    if not isinstance(raw, list) or not raw:
        raise SeasonMapError(f"{where}: 'seasons' must be a non-empty list")
    spans: list[SeasonSpan] = []
    seen: set[int] = set()
    for position, pair in enumerate(raw):
        spot = f"{where}: seasons[{position}]"
        if not isinstance(pair, list) or len(pair) != 2 or not all(_int(v) for v in pair):
            raise SeasonMapError(f"{spot} must be [season, episode_count] integers")
        season, count = pair
        if season < 1:
            raise SeasonMapError(f"{spot}: season {season} < 1; specials are never mapped")
        if count < 1:
            raise SeasonMapError(f"{spot}: season {season} holds {count} episodes")
        if season in seen:
            raise SeasonMapError(f"{spot}: season {season} is listed twice")
        if spans and season <= spans[-1].season:
            fallback = spans[-1].season
            raise SeasonMapError(f"{spot}: seasons must ascend; {season} follows {fallback}")
        seen.add(season)
        spans.append(SeasonSpan(season=season, count=count))
    return tuple(spans)


def find(
    entries: tuple[ShowSeasons, ...], title: str, year: int | None
) -> ShowSeasons | None:
    """The entry for a show, ignoring ``(ITA)``-style noise and case.

    An entry with a year only rejects a show whose year is known and different,
    so ``One Piece (2023)`` never inherits the 1999 map.
    """
    wanted = " ".join(title.split()).casefold()
    for entry in entries:
        if " ".join(entry.title.split()).casefold() != wanted:
            continue
        if entry.year is not None and year is not None and entry.year != year:
            continue
        return entry
    return None


def load(path: Path) -> tuple[ShowSeasons, ...]:
    """Read a map file. A missing file simply means "nothing is mapped"."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise SeasonMapError(f"{path}: {exc}") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise SeasonMapError(f"{path}: not valid JSON ({exc})") from exc
    return parse(document, source=path.name)


__all__ = [
    "MODES",
    "SeasonMapError",
    "SeasonSpan",
    "ShowSeasons",
    "find",
    "load",
    "parse",
]
