"""Plugin: the animeunity tree (Italian dub).

Two shapes, both present on disk: a folder of ``<Folder> - <n>.mp4`` episodes
with absolute numbering (One Piece starts at 700) and a single-file folder whose
name carries ``Movie`` (Haikyu). Episode integers are never assumed contiguous:
``Inazuma Eleven (ITA) (2008)`` has 126 files and a hole before 127.

A show listed in ``anime_seasons.json`` is laid out in its provider's seasons
instead of one flat ``Season 01``; see :mod:`mediaforge.naming.seasons`.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path

from mediaforge import naming
from mediaforge.models import PlannedItem
from mediaforge.naming import parse
from mediaforge.naming import seasons as seasonmap
from mediaforge.plugin_api import DetectContext, Detected, PlanContext, read_bool

id = "animeunity"
name = "animeunity (Italian dub)"
version = "2"
priority = 10
default_library = "anime"

logger = logging.getLogger(__name__)

#: The map is data, not config: it ships in the same directory as the plugin.
SEASON_MAP = Path(__file__).with_name("anime_seasons.json")

YEAR_TAIL = re.compile(r"\(\d{4}\)\s*$")
NUMBERED = re.compile(r"^(?P<base>.+?)\s+-\s+(?P<num>\d{1,4})$")
ABSOLUTE_HINT = re.compile(r"\s-\s(\d{3,4})$")


def _numbered(name: str, folder: str) -> re.Match[str] | None:
    stem = re.sub(r"\.(?:mp4|mkv|avi|m4v|ts|webm)$", "", name, flags=re.IGNORECASE)
    if not stem.lower().startswith(folder.lower()):
        return None
    return NUMBERED.match(stem)


def detect(ctx: DetectContext) -> Detected:
    dirs = [d for d in ctx.dirs if d.video_count]
    if not dirs:
        return Detected(id, 0.0, ("no video folders",))

    score = 0.0
    reasons: list[str] = []
    total = max(1, ctx.total_files)

    if ctx.extension_histogram.get(".mp4", 0) / total >= 0.90:
        score += 0.45
        reasons.append("90%+ of videos are .mp4")

    matching = sum(
        1
        for d in dirs
        if d.sample_names
        and all(_numbered(n, d.name) is not None for n in d.sample_names)
    )
    if matching >= 5 and matching >= 0.8 * len(dirs):
        score += 0.25
        reasons.append("every sampled name is '<Folder> - <n>.mp4'")

    if any(ABSOLUTE_HINT.search(n) for d in dirs for n in d.sample_names):
        score += 0.15
        reasons.append("three-digit episode numbers")

    if any(
        d.video_count == 1
        and any(n.lower().endswith("- 1.mp4") for n in d.sample_names)
        for d in dirs
    ):
        score += 0.15
        reasons.append("single-file folder numbered 1")

    return Detected(id, min(1.0, score), tuple(reasons))


def _item(source, layout: naming.Layout) -> PlannedItem:
    return PlannedItem(
        source=source,
        target=layout.target,
        nfos=layout.nfos,
        sort_title=layout.sort_title,
        franchise=layout.franchise,
        order=layout.order,
        plugin_id=id,
    )


def _place(show: seasonmap.ShowSeasons | None, number: int) -> tuple[int, int]:
    """Season and episode for one absolute number; flat ``Season 01`` when unmapped."""
    if show is None:
        return 1, number
    resolved = show.resolve(number)
    if resolved is not None:
        return resolved
    logger.warning(
        "%s: episode %d is past the season map, which covers 1-%d; keeping it in Season 01",
        show.title,
        number,
        show.episode_count,
    )
    return 1, number


def plan(ctx: PlanContext):
    sorttitle = read_bool(ctx.options, "MF_NFO_SORTTITLE", True)
    emit_nfo = read_bool(ctx.options, "MF_NFO_ENABLED", True)
    map_entries = (
        ()
        if read_bool(ctx.options, "MF_ANIME_SINGLE_SEASON", False)
        else seasonmap.load(SEASON_MAP)
    )
    hints = re.compile(
        ctx.options.get("MF_ANIME_MOVIE_HINTS", r"\b(Movie|Film)\b"), re.IGNORECASE
    )

    by_dir: dict[str, list] = defaultdict(list)
    for media in ctx.files:
        parts = media.relpath.parts
        if len(parts) < 2:
            continue
        by_dir[parts[0]].append(media)

    for dir_name in sorted(by_dir):
        files = sorted(by_dir[dir_name], key=lambda m: m.path.name)
        title_year = parse.split_year(dir_name)
        show_title = parse.clean_show_title(title_year.base)

        if len(files) == 1 and hints.search(show_title):
            folder = naming.clone_folder(parse.clean_movie_title(show_title), title_year.year)
            layout = naming.movie_layout(
                "movies",
                folder,
                files[0].path.suffix.lower(),
                None,
                emit_nfo=emit_nfo,
                sorttitle=sorttitle,
                set_enabled=read_bool(ctx.options, "MF_NFO_SET", True),
            )
            yield _item(files[0], layout)
            continue

        folder = naming.clone_folder(show_title, title_year.year)
        show = seasonmap.find(map_entries, show_title, title_year.year)
        show_nfo = None
        if emit_nfo:
            show_nfo = naming.tvshow_spec(
                None,
                display_order=None if show is not None else "absolute",
                sorttitle=sorttitle,
                empty=show is not None,
            )
        for media in files:
            number = parse.parse_absolute_episode(media.path.name, title_year.base)
            if number is None:
                continue
            season, episode = _place(show, number)
            layout = naming.episode_layout(
                "anime",
                folder,
                season,
                episode,
                media.path.suffix.lower(),
                None,
                show_nfo=show_nfo,
                emit_episode_nfo=emit_nfo,
                sorttitle=sorttitle,
            )
            yield _item(media, layout)
