"""A deleted source frees its bytes at once; the clone entry follows within a tick."""

from __future__ import annotations

import os
from pathlib import Path

from src import fsutil
from src.linker import Linker
from src.liveness import LivenessSweeper
from src.state import SqliteStateStore

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
