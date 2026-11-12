"""Jellyfin-shaped target paths and NFO specs, built from a clean name.

One implementation, two callers (the plugins), so a naming rule can never drift
between them. Paths are relative to the library root; the reconciler joins them
with ``MF_CLONE_ROOT``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from ..models import Library, NfoSpec, TargetPath
from . import franchise, parse, sanitize, seasons
from .franchise import FranchiseMatch


@dataclass(frozen=True, slots=True)
class Layout:
    target: TargetPath
    nfos: tuple[tuple[PurePath, NfoSpec], ...]
    sort_title: str | None
    franchise: str | None
    order: tuple[int, ...] | None


def clone_folder(clean_title: str, year: int | None) -> str:
    """``("Iron Man", 2008)`` → ``"Iron Man (2008)"``."""
    title = sanitize.sanitize_component(sanitize.tidy(clean_title))
    if year is None:
        return title
    return f"{title} ({year})"


def movie_spec(
    match: FranchiseMatch | None, *, sorttitle: bool, set_enabled: bool
) -> NfoSpec | None:
    elements: list[tuple[str, str]] = []
    if match is not None and sorttitle:
        elements.append(("sorttitle", franchise.sort_title(match)))
    if match is not None and set_enabled and match.set_name:
        elements.append(("set", match.set_name))
    if not elements:
        return None
    return NfoSpec(kind="movie", elements=tuple(elements))


def episode_spec(season: int, episode: int) -> NfoSpec:
    return NfoSpec(
        kind="episode",
        elements=(("season", str(season)), ("episode", str(episode))),
    )


def tvshow_spec(
    match: FranchiseMatch | None,
    *,
    display_order: str | None,
    sorttitle: bool,
    empty: bool = False,
) -> NfoSpec | None:
    """``empty`` asks for a ``tvshow.nfo`` that asserts nothing, which is the
    only way to displace a ``displayorder`` an earlier run wrote."""
    elements: list[tuple[str, str]] = []
    if match is not None and sorttitle:
        elements.append(("sorttitle", franchise.sort_title(match)))
    if display_order:
        elements.append(("displayorder", display_order))
    if not elements and not empty:
        return None
    return NfoSpec(kind="tvshow", elements=tuple(elements))


def movie_layout(
    library: Library,
    folder: str,
    ext: str,
    match: FranchiseMatch | None,
    *,
    emit_nfo: bool = True,
    sorttitle: bool = True,
    set_enabled: bool = True,
) -> Layout:
    """``Movies\\Iron Man (2008)\\Iron Man (2008).mkv`` + ``movie.nfo``."""
    component = sanitize.sanitize_component(folder)
    filename = component + ext
    nfos: list[tuple[PurePath, NfoSpec]] = []
    spec = movie_spec(match, sorttitle=sorttitle, set_enabled=set_enabled) if emit_nfo else None
    if spec is not None:
        nfos.append((PurePath(component, "movie.nfo"), spec))
    return Layout(
        target=TargetPath(library=library, relative=PurePath(component, filename)),
        nfos=tuple(nfos),
        sort_title=franchise.sort_title(match) if match and sorttitle else None,
        franchise=match.key if match else None,
        order=match.order if match else None,
    )


def episode_layout(
    library: Library,
    folder: str,
    season: int,
    episode: int,
    ext: str,
    match: FranchiseMatch | None,
    *,
    show_nfo: NfoSpec | None = None,
    emit_episode_nfo: bool = False,
    sorttitle: bool = True,
) -> Layout:
    """``Shows\\The Flash (2014)\\Season 01\\The Flash (2014) - S01E01.mkv``."""
    component = sanitize.sanitize_component(folder)
    season_dir = f"Season {season:02d}"
    filename = f"{component} - S{season:02d}E{episode:02d}{ext}"
    nfos: list[tuple[PurePath, NfoSpec]] = []
    if emit_episode_nfo:
        nfo_name = PurePath(filename).stem + ".nfo"
        nfos.append(
            (PurePath(component, season_dir, nfo_name), episode_spec(season, episode))
        )
    if show_nfo is not None:
        nfos.append((PurePath(component, "tvshow.nfo"), show_nfo))
    return Layout(
        target=TargetPath(library=library, relative=PurePath(component, season_dir, filename)),
        nfos=tuple(nfos),
        sort_title=franchise.sort_title(match) if match and sorttitle else None,
        franchise=match.key if match else None,
        order=match.order if match else None,
    )


__all__ = [
    "Layout",
    "clone_folder",
    "episode_layout",
    "episode_spec",
    "franchise",
    "movie_layout",
    "parse",
    "sanitize",
    "seasons",
    "tvshow_spec",
]
