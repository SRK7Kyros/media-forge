"""Naming: the shapes are taken from the real library, so these are contracts."""

from __future__ import annotations

from pathlib import PurePath

import pytest

from src.naming import clone_folder, episode_layout, franchise, movie_layout, parse, sanitize


def test_split_year_reads_the_trailing_year() -> None:
    assert parse.split_year("Diavoli (2020)") == parse.TitleYear("Diavoli", 2020)
    assert parse.split_year("No year here") == parse.TitleYear("No year here", None)


def test_split_year_keeps_an_ita_marker_in_the_base() -> None:
    parsed = parse.split_year("One Piece (ITA) (1999)")
    assert parsed.base == "One Piece (ITA)"
    assert parsed.year == 1999


def test_clean_show_title_drops_only_the_ita_marker() -> None:
    assert parse.clean_show_title("One Piece (ITA)") == "One Piece"
    assert parse.clean_show_title("Orion no Kokuin") == "Orion no Kokuin"


def test_clean_movie_title_removes_the_hint_token() -> None:
    assert parse.clean_movie_title("Haikyu!! Movie - The Dumpster Battle") == (
        "Haikyu!! - The Dumpster Battle"
    )


def test_season_episode_and_resolution() -> None:
    ref = parse.parse_season_episode("Diavoli (2020) 1080p - S01E07.mkv")
    assert ref is not None
    assert (ref.season, ref.episode, ref.resolution) == (1, 7, "1080p")


def test_season_episode_accepts_lowercase_and_spaces() -> None:
    ref = parse.parse_season_episode("Show s2 e10 720p.mkv")
    assert ref is not None
    assert (ref.season, ref.episode, ref.resolution) == (2, 10, "720p")


def test_resolution_is_not_read_from_a_year() -> None:
    assert parse.parse_resolution("Movie (2008).mkv") is None


def test_absolute_episode_uses_the_folder_prefix() -> None:
    number = parse.parse_absolute_episode(
        "One Piece (ITA) (1999) - 700.mp4", "One Piece (ITA) (1999)"
    )
    assert number == 700


def test_absolute_episode_rejects_any_season_episode_name() -> None:
    assert parse.parse_absolute_episode("Show - S01E04.mp4", "Show") is None


def test_absolute_episode_rejects_a_name_without_a_number() -> None:
    assert parse.parse_absolute_episode("Show - special.mp4", "Show") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Iron Man", "Iron Man"),
        ("Thor: Ragnarok", "Thor Ragnarok"),
        ("What?", "What"),
        ("  spaced   out  ", "spaced out"),
        ("trailing. ", "trailing"),
        ("CON", "_CON"),
        ("", "untitled"),
    ],
)
def test_sanitize_component(raw: str, expected: str) -> None:
    assert sanitize.sanitize_component(raw) == expected


def test_sanitize_strips_control_characters() -> None:
    assert sanitize.sanitize_component("bad\x00name\x1f") == "badname"


def test_tidy_collapses_the_gap_left_by_a_removed_prefix() -> None:
    assert sanitize.tidy("Iron Man - - The - Movie") == "Iron Man - The - Movie"


def test_mcu_dotted_order_is_two_ints_not_a_float() -> None:
    match = franchise.resolve("MCU 4.1 - The Falcon and the Winter Soldier")
    assert match is not None
    assert match.order == (4, 1)
    assert match.clean_title == "The Falcon and the Winter Soldier"
    assert franchise.sort_title(match) == "MCU 04.01"


def test_mcu_double_digit_order_sorts_after_single_digit() -> None:
    early = franchise.resolve("MCU 3.01 - Captain America - Civil War")
    late = franchise.resolve("MCU 3.10 - Avengers - Endgame")
    assert early is not None and late is not None
    assert franchise.sort_title(early) == "MCU 03.01"
    assert franchise.sort_title(late) == "MCU 03.10"
    assert franchise.sort_title(early) < franchise.sort_title(late)


def test_star_wars_uses_roman_numerals_and_keeps_its_name() -> None:
    match = franchise.resolve("Star Wars - Episodio IV - Una nuova speranza")
    assert match is not None
    assert match.order == (4,)
    assert match.clean_title == "Star Wars - Episodio IV - Una nuova speranza"


def test_table_rules_cover_plain_sequences() -> None:
    rocky = franchise.resolve("Rocky II")
    padrino = franchise.resolve("Il padrino - Parte III")
    assert rocky is not None and rocky.order == (2,)
    assert padrino is not None and padrino.order == (3,)


def test_unknown_title_is_not_a_franchise() -> None:
    assert franchise.resolve("Diavoli") is None


def test_clone_folder_keeps_accents_and_appends_the_year() -> None:
    assert clone_folder("L'incredibile Hulk", 2008) == "L'incredibile Hulk (2008)"
    assert clone_folder("Guardiani della Galassia", None) == "Guardiani della Galassia"


def test_movie_layout_matches_the_jellyfin_convention() -> None:
    match = franchise.resolve("MCU 1.1 - Iron Man")
    layout = movie_layout("movies", "Iron Man (2008)", ".mkv", match)
    assert layout.target.library == "movies"
    assert layout.target.relative == PurePath("Iron Man (2008)", "Iron Man (2008).mkv")
    assert layout.nfos[0][0] == PurePath("Iron Man (2008)", "movie.nfo")
    elements = dict(layout.nfos[0][1].elements)
    assert elements["sorttitle"] == "MCU 01.01"
    assert elements["set"] == "Marvel Cinematic Universe"


def test_movie_layout_without_a_franchise_emits_no_nfo() -> None:
    layout = movie_layout("movies", "Diavoli (2020)", ".mkv", None)
    assert layout.nfos == ()


def test_episode_layout_pins_season_and_episode_for_absolute_anime() -> None:
    show = episode_layout(
        "anime",
        "One Piece (1999)",
        1,
        700,
        ".mp4",
        None,
    )
    assert show.target.relative == PurePath(
        "One Piece (1999)", "Season 01", "One Piece (1999) - S01E700.mp4"
    )


def test_episode_layout_can_emit_the_episode_nfo() -> None:
    layout = episode_layout(
        "anime",
        "One Piece (1999)",
        1,
        700,
        ".mp4",
        None,
        emit_episode_nfo=True,
    )
    relative, spec = layout.nfos[0]
    assert relative == PurePath("One Piece (1999)", "Season 01", "One Piece (1999) - S01E700.nfo")
    assert dict(spec.elements) == {"season": "1", "episode": "700"}
