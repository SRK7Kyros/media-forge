"""Plugin: the streaming-community downloader's library tree.

Recognised by three independent signals that all hold on the real tree: a JSON
sidecar beside every title, ``(<year>)`` at the end of most folder names, and
either one ``<Folder> <res>.mkv`` movie file or ``<Folder> <res> - SxxEyy.mkv``
episodes. The order prefix (``MCU 4.2 - ``) is read, then dropped from the name
and carried in the NFO instead.
"""

from __future__ import annotations

import re
from collections import defaultdict

from src import naming
from src.models import PlannedItem
from src.naming import franchise, parse
from src.plugin_api import DetectContext, Detected, PlanContext, read_bool

id = "streaming_community"
name = "streaming-community downloader"
version = "1"
priority = 10
default_library = "shows"

YEAR_TAIL = re.compile(r"\(\d{4}\)\s*$")
EPISODE_NAME = re.compile(r"S\d{1,2}\s*E\d{1,4}", re.IGNORECASE)
ORDER_PREFIX = re.compile(r"^MCU \d+\.\d+ - ")


def detect(ctx: DetectContext) -> Detected:
    dirs = [d for d in ctx.dirs if d.video_count or d.sidecar_names]
    if not dirs:
        return Detected(id, 0.0, ("no populated folders",))

    score = 0.0
    reasons: list[str] = []
    total = max(1, ctx.total_files)

    if ctx.extension_histogram.get(".mkv", 0) / total >= 0.95:
        score += 0.15
        reasons.append("95%+ of videos are .mkv")

    dated = sum(1 for d in dirs if YEAR_TAIL.search(d.name))
    if dated >= 0.6 * len(dirs):
        score += 0.20
        reasons.append("most folders end with (YYYY)")

    with_json = sum(
        1 for d in dirs if any(n.lower().endswith(".json") for n in d.sidecar_names)
    )
    if with_json >= 0.5 * len(dirs):
        score += 0.50
        reasons.append("JSON sidecar beside the videos")

    if any(EPISODE_NAME.search(n) for d in dirs for n in d.sample_names):
        score += 0.15
        reasons.append("SxxEyy episode names")

    if any(ORDER_PREFIX.match(d.name) for d in dirs):
        score += 0.10
        reasons.append("numbered order prefixes")

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


def plan(ctx: PlanContext):
    sorttitle = read_bool(ctx.options, "MF_NFO_SORTTITLE", True)
    set_enabled = read_bool(ctx.options, "MF_NFO_SET", True)
    emit_nfo = read_bool(ctx.options, "MF_NFO_ENABLED", True)

    by_dir: dict[str, list] = defaultdict(list)
    for media in ctx.files:
        parts = media.relpath.parts
        if len(parts) < 2:
            continue  # a loose file in the root has no title to take
        by_dir[parts[0]].append(media)

    for dir_name in sorted(by_dir):
        files = sorted(by_dir[dir_name], key=lambda m: m.path.name)
        title_year = parse.split_year(dir_name)
        match = franchise.resolve(title_year.base)
        clean = match.clean_title if match else title_year.base
        folder = naming.clone_folder(clean, title_year.year)

        episodes = [(m, parse.parse_season_episode(m.path.name)) for m in files]
        shows = [(m, ref) for m, ref in episodes if ref is not None]
        movies = [m for m, ref in episodes if ref is None]

        if shows:
            show_nfo = (
                naming.tvshow_spec(match, display_order=None, sorttitle=sorttitle)
                if emit_nfo
                else None
            )
            for media, ref in shows:
                assert ref is not None
                layout = naming.episode_layout(
                    "shows",
                    folder,
                    ref.season,
                    ref.episode,
                    media.path.suffix.lower(),
                    match,
                    show_nfo=show_nfo,
                    sorttitle=sorttitle,
                )
                yield _item(media, layout)

        for media in movies:
            layout = naming.movie_layout(
                "movies",
                folder,
                media.path.suffix.lower(),
                match,
                emit_nfo=emit_nfo,
                sorttitle=sorttitle,
                set_enabled=set_enabled,
            )
            yield _item(media, layout)
