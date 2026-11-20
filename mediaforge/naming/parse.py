"""Parsing the real folder and file name shapes of this library.

Every pattern here was derived from files that exist on disk, not invented:

    MCU 3.01 - Captain America - Civil War (2016)
    MCU 3.01 - Captain America - Civil War (2016) 1080p.mkv
    The Flash (2014) 720p - S01E01.mkv
    One Piece (ITA) (1999) - 700.mp4
    Inazuma Eleven (ITA) (2008) - 001.mp4
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath

YEAR_RE = re.compile(r"^(?P<base>.*?)\s*\((?P<year>(?:19|20)\d{2})\)\s*$")
EPISODE_RE = re.compile(r"S(?P<season>\d{1,2})\s*E(?P<episode>\d{1,4})", re.IGNORECASE)
RESOLUTION_RE = re.compile(r"(?<![0-9])(?P<res>\d{3,4}[pi])(?![0-9])", re.IGNORECASE)
TRAILING_INT_RE = re.compile(r"^(?P<base>.*?)\s*-\s*(?P<num>\d{1,4})\s*$")
ITA_RE = re.compile(r"\s*\(\s*ITA\s*\)", re.IGNORECASE)
MOVIE_TOKEN_RE = re.compile(r"\b(?:Movie|Film)\b", re.IGNORECASE)
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9}


@dataclass(frozen=True, slots=True)
class TitleYear:
    base: str
    year: int | None


@dataclass(frozen=True, slots=True)
class EpisodeRef:
    season: int
    episode: int
    resolution: str | None


def stem(path: PurePath | str) -> str:
    return PurePath(str(path)).stem


def split_year(name: str) -> TitleYear:
    """``"The Flash (2014)"`` → ``("The Flash", 2014)``."""
    match = YEAR_RE.match(name.strip())
    if not match:
        return TitleYear(base=name.strip(), year=None)
    return TitleYear(base=match.group("base").strip(), year=int(match.group("year")))


def parse_resolution(name: str) -> str | None:
    match = RESOLUTION_RE.search(name)
    return None if match is None else match.group("res").lower()


def parse_season_episode(filename: str) -> EpisodeRef | None:
    """``"The Flash (2014) 1080p - S01E01.mkv"`` → ``EpisodeRef(1, 1, "1080p")``."""
    match = EPISODE_RE.search(PurePath(filename).stem)
    if match is None:
        return None
    return EpisodeRef(
        season=int(match.group("season")),
        episode=int(match.group("episode")),
        resolution=parse_resolution(filename),
    )


def parse_absolute_episode(filename: str, folder_base: str) -> int | None:
    """``"One Piece (ITA) (1999) - 700.mp4"`` → ``700``.

    Any ``SxxEyy`` name is rejected outright: those carry a season, and a bare
    integer suffix on such a name is a different thing entirely.
    """
    if parse_season_episode(filename) is not None:
        return None
    body = PurePath(filename).stem
    if folder_base and body.lower().startswith(folder_base.lower()):
        body = body[len(folder_base) :]
    match = TRAILING_INT_RE.match(body.strip())
    if match is None:
        return None
    return int(match.group("num"))


def clean_show_title(name: str) -> str:
    """Drop the downloader's ``(ITA)`` marker; it is a source tag, not a title."""
    return " ".join(ITA_RE.sub(" ", name).split()).strip(" -")


def clean_movie_title(title: str) -> str:
    """Remove a standalone ``Movie``/``Film`` token when promoting a single file."""
    without = MOVIE_TOKEN_RE.sub(" ", title)
    without = " ".join(without.split())
    return without.strip(" -")


def roman_to_int(token: str) -> int | None:
    return ROMAN.get(token.strip().upper())
