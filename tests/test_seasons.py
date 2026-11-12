"""The anime season map: snapshot data, boundary arithmetic, plugin placement."""

from __future__ import annotations

from pathlib import Path, PurePath

import pytest

from src.models import MediaFile
from src.naming import seasons
from src.plugin_api import PlanContext
from src.plugin_loader import load_plugins

from .conftest import PLUGIN_DIR

MAP_FILE = PLUGIN_DIR / "anime_seasons.json"
SHOW = Path("One Piece (ITA) (1999)")


def _show() -> seasons.ShowSeasons:
    entry = seasons.find(seasons.load(MAP_FILE), "One Piece", 1999)
    assert entry is not None
    return entry


def _entries(shows: object) -> tuple[seasons.ShowSeasons, ...]:
    return seasons.parse({"shows": shows})


def _spans(*pairs: tuple[int, int]) -> dict[str, object]:
    return {"title": "X", "mode": "absolute", "seasons": [list(pair) for pair in pairs]}


def _media(number: int) -> MediaFile:
    name = f"One Piece (ITA) (1999) - {number}.mp4"
    return MediaFile(
        path=SHOW / name,
        relpath=PurePath(SHOW.name, name),
        size=4096,
        mtime_ns=0,
        dev=1,
        ino=number,
        root_id="anime",
    )


@pytest.mark.parametrize(
    ("absolute", "expected"),
    [
        (1, (1, 1)),
        (61, (1, 61)),
        (62, (2, 62)),
        (692, (16, 692)),
        (693, (17, 693)),
        (700, (17, 700)),
        (748, (17, 748)),
        (749, (18, 749)),
        (803, (18, 803)),
        (804, (19, 804)),
        (850, (19, 850)),
        (1180, (23, 1180)),
        (1181, None),
        (0, None),
    ],
)
def test_one_piece_places_absolute_numbers_in_arc_seasons(
    absolute: int, expected: tuple[int, int] | None
) -> None:
    assert _show().resolve(absolute) == expected


def test_the_shipped_map_partitions_the_whole_run() -> None:
    show = _show()
    assert [span.season for span in show.seasons] == list(range(1, 24))
    assert show.episode_count == 1180
    assert all(show.resolve(number) is not None for number in range(1, 1181))


def test_per_season_mode_restarts_at_one_inside_every_season() -> None:
    entry = _entries([{"title": "X", "mode": "per_season", "seasons": [[1, 61], [2, 16]]}])[0]
    assert entry.resolve(61) == (1, 61)
    assert entry.resolve(62) == (2, 1)
    assert entry.resolve(77) == (2, 16)
    assert entry.resolve(78) is None


def test_a_known_year_must_agree_and_an_unknown_one_may() -> None:
    entries = seasons.load(MAP_FILE)
    assert seasons.find(entries, "one piece", 1999) is not None
    assert seasons.find(entries, "One Piece (ITA)", 1999) is None
    assert seasons.find(entries, "One Piece", 2023) is None
    assert seasons.find(entries, "Nana", 2006) is None


def test_a_missing_map_file_maps_nothing(tmp_path: Path) -> None:
    assert seasons.load(tmp_path / "absent.json") == ()


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"shows": {}},
        {"shows": [{"title": "", "mode": "absolute", "seasons": [[1, 1]]}]},
        {"shows": [{"title": "X", "mode": "aired", "seasons": [[1, 1]]}]},
        {"shows": [{"title": "X", "mode": "absolute", "seasons": []}]},
        {"shows": [{"title": "X", "mode": "absolute", "seasons": [[0, 12]]}]},
        {"shows": [{"title": "X", "mode": "absolute", "seasons": [[1, 0]]}]},
        {"shows": [{"title": "X", "mode": "absolute", "seasons": [[1, 12], [1, 3]]}]},
        {"shows": [{"title": "X", "mode": "absolute", "seasons": [[3, 12], [2, 3]]}]},
        {"shows": [{"title": "X", "mode": "absolute", "seasons": [[1, "12"]]}]},
        {"shows": [{"title": "X", "mode": "absolute", "seasons": [[1, 12, 3]]}]},
        {"shows": [_spans((1, 12)), _spans((1, 12))]},
    ],
)
def test_a_contradictory_map_is_refused(document: object) -> None:
    with pytest.raises(seasons.SeasonMapError):
        seasons.parse(document)


def test_a_broken_file_is_refused_with_the_file_name(tmp_path: Path) -> None:
    path = tmp_path / "anime_seasons.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(seasons.SeasonMapError, match="anime_seasons.json"):
        seasons.load(path)


def _plan(options: dict[str, str]):
    plugin = next(entry for entry in load_plugins(PLUGIN_DIR) if entry.plugin_id == "animeunity")
    assert plugin.ok, plugin.error
    ctx = PlanContext(
        root_id="anime",
        root_path=Path("D:/Shows/animeunity"),
        files=(_media(700), _media(701)),
        dirs=(),
        options=options,
    )
    return list(plugin.module.plan(ctx))


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, "Season 17/One Piece (1999) - S17E700.mp4"),
        ({"MF_ANIME_SINGLE_SEASON": "true"}, "Season 01/One Piece (1999) - S01E700.mp4"),
        ({"MF_NFO_ENABLED": "false"}, "Season 17/One Piece (1999) - S17E700.mp4"),
    ],
)
def test_the_plugin_places_mapped_shows_in_seasons(
    options: dict[str, str], expected: str
) -> None:
    items = _plan(options)
    assert items[0].target.relative == PurePath("One Piece (1999)") / PurePath(expected)


def test_the_plugin_drops_the_absolute_display_order_only_for_mapped_shows() -> None:
    mapped = _plan({})[0]
    assert {path.name for path, _ in mapped.nfos} == {
        "One Piece (1999) - S17E700.nfo",
        "tvshow.nfo",
    }
    assert "displayorder" not in dict(mapped.nfos[1][1].elements)

    flat = _plan({"MF_ANIME_SINGLE_SEASON": "true"})[0]
    assert dict(flat.nfos[1][1].elements)["displayorder"] == "absolute"


def test_a_mapped_show_keeps_its_tvshow_nfo_when_sorttitle_is_off() -> None:
    mapped = _plan({"MF_NFO_SORTTITLE": "false"})[0]
    assert "tvshow.nfo" in {path.name for path, _ in mapped.nfos}
