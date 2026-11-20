"""A deleted source frees its bytes at once; the clone entry follows within a tick."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from mediaforge import fsutil
from mediaforge.linker import Linker
from mediaforge.liveness import LivenessSweeper
from mediaforge.state import SqliteStateStore

from .conftest import Fixture, requires_symlinks

pytestmark = requires_symlinks

SHOWS_MCU = Path("MCU 1.1 - Iron Man (2008)") / "MCU 1.1 - Iron Man (2008) 1080p.mkv"
ANIME_INAZUMA = Path("Inazuma Eleven (ITA) (2008)")


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _sweeper(library: Fixture, store: SqliteStateStore, clock: Clock) -> LivenessSweeper:
    return LivenessSweeper(library.settings, store, Linker(), clock=clock)


def test_a_deleted_source_prunes_the_entry_after_the_grace_window(
    pipeline, library: Fixture
) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("shows") / SHOWS_MCU
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    assert os.path.islink(target)

    source.unlink()
    clock = Clock()
    sweeper = _sweeper(library, store, clock)

    assert sweeper.sweep().pruned == []
    clock.advance(library.settings.liveness_grace_s + 0.5)
    result = sweeper.sweep()

    assert result.pruned == [fsutil.normalise(target)]
    assert not fsutil.exists(target)
    assert store.get_link(fsutil.normalise(source)) is None


def test_a_deleted_series_removes_its_folder_and_show_nfo(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source_dir = library.source("anime") / ANIME_INAZUMA
    show_dir = library.clone / "Anime" / "Inazuma Eleven (2008)"
    assert (show_dir / "tvshow.nfo").is_file()

    for path in source_dir.iterdir():
        path.unlink()
    source_dir.rmdir()

    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    sweeper.sweep()
    clock.advance(library.settings.liveness_grace_s + 0.5)
    result = sweeper.sweep()

    assert len(result.pruned) == 2
    assert not show_dir.exists()
    assert [row for row in store.iter_links() if "Inazuma" in row.target_path] == []


def test_a_title_folder_that_still_holds_something_keeps_its_show_nfo(
    pipeline, library: Fixture
) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source_dir = library.source("anime") / ANIME_INAZUMA
    show_dir = library.clone / "Anime" / "Inazuma Eleven (2008)"
    stray = show_dir / "notes.txt"
    stray.write_text("not this tool's file", encoding="utf-8")

    for path in source_dir.iterdir():
        path.unlink()
    source_dir.rmdir()

    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    sweeper.sweep()
    clock.advance(library.settings.liveness_grace_s + 0.5)
    result = sweeper.sweep()

    assert result.pruned
    assert stray.is_file()
    assert (show_dir / "tvshow.nfo").is_file()


def test_a_foreign_file_at_the_target_is_never_pruned(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("shows") / SHOWS_MCU
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"

    target.unlink()
    target.write_bytes(b"a real file someone put here")
    source.unlink()

    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    sweeper.sweep()
    clock.advance(library.settings.liveness_grace_s + 0.5)
    result = sweeper.sweep()

    assert result.pruned == []
    assert result.errors
    assert target.read_bytes() == b"a real file someone put here"


def test_a_dry_run_reports_without_touching_anything(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("shows") / SHOWS_MCU
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    source.unlink()

    clock = Clock()
    sweeper = LivenessSweeper(library.settings, store, Linker(), dry_run=True, clock=clock)
    sweeper.sweep()
    clock.advance(library.settings.liveness_grace_s + 0.5)
    result = sweeper.sweep()

    assert result.pruned == [fsutil.normalise(target)]
    assert fsutil.exists(target)
    assert store.get_link(fsutil.normalise(source)) is not None


def test_an_unreadable_source_root_pauses_pruning_instead_of_emptying_the_clone(
    pipeline, library: Fixture, tmp_path: Path
) -> None:
    """An unplugged drive reads as 'every file deleted'; it must not be acted on."""
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("shows") / SHOWS_MCU
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    source.unlink()

    away = tmp_path / "source-shows-away"
    library.source("shows").rename(away)
    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    try:
        assert sweeper.sweep().paused_roots == {"shows"}
        clock.advance(library.settings.liveness_grace_s + 0.5)
        result = sweeper.sweep()
        assert result.pruned == []
        assert result.paused_roots == {"shows"}
        assert store.get_link(fsutil.normalise(source)) is not None
        assert fsutil.exists(target)
    finally:
        away.rename(library.source("shows"))

    clock.advance(library.settings.liveness_grace_s + 0.5)
    sweeper.sweep()
    clock.advance(library.settings.liveness_grace_s + 0.5)
    assert sweeper.sweep().pruned == [fsutil.normalise(target)]
    assert not fsutil.exists(target)


def test_a_dangling_link_with_no_row_is_still_swept(pipeline, library: Fixture) -> None:
    """The database can forget a link (restore, killed run); the tree must not."""
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("shows") / SHOWS_MCU
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    assert os.path.islink(target)

    source.unlink()
    store.delete_link(fsutil.normalise(source))
    assert store.get_link(fsutil.normalise(source)) is None

    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    sweeper.sweep()
    clock.advance(library.settings.orphan_scan_interval_s + 1.0)
    result = sweeper.sweep()

    assert fsutil.normalise(target) in result.pruned
    assert not fsutil.exists(target)
    assert "movies" in [name for name, _ in store.dirty_entries()]


def test_the_tree_pass_leaves_a_healthy_link_alone(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"

    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    for _ in range(3):
        assert sweeper.sweep().pruned == []
        clock.advance(library.settings.orphan_scan_interval_s + 1.0)

    assert os.path.islink(target)
    assert os.path.exists(str(target))


def test_a_dangling_link_pointing_outside_the_roots_is_not_touched(
    pipeline, library: Fixture, tmp_path: Path
) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    stray = library.clone / "Movies" / "Stray (1999)" / "Stray (1999).mkv"
    stray.parent.mkdir(parents=True)
    os.symlink(str(tmp_path / "somewhere-else.mkv"), str(stray))

    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    sweeper.sweep()
    clock.advance(library.settings.orphan_scan_interval_s + 1.0)
    sweeper.sweep()
    clock.advance(library.settings.orphan_scan_interval_s + 1.0)
    result = sweeper.sweep()

    assert result.pruned == []
    assert fsutil.exists(stray)


def test_the_tree_pass_clears_a_folder_nfo_left_by_an_interrupted_run(
    pipeline, library: Fixture
) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    hollow = library.clone / "Movies" / "Left Behind (1999)"
    hollow.mkdir(parents=True)
    (hollow / "movie.nfo").write_text("<movie/>", encoding="utf-8")

    clock = Clock()
    sweeper = _sweeper(library, store, clock)
    sweeper.sweep()
    clock.advance(library.settings.orphan_scan_interval_s + 1.0)
    sweeper.sweep()

    assert not (hollow / "movie.nfo").exists()
    assert not hollow.exists()


def test_the_tree_pass_can_be_turned_off(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("shows") / SHOWS_MCU
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    source.unlink()
    store.delete_link(fsutil.normalise(source))

    clock = Clock()
    settings = replace(library.settings, orphan_scan_interval_s=0.0)
    sweeper = LivenessSweeper(settings, store, Linker(), clock=clock)
    for _ in range(3):
        sweeper.sweep()
        clock.advance(600.0)

    assert fsutil.exists(target)
