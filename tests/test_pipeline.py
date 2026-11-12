"""End-to-end reconciliation against a real NTFS tree: links, NFOs, prune, repoint."""

from __future__ import annotations

import os
from pathlib import Path

from src import fsutil
from src.linker import Linker
from src.pipeline import Pipeline, SyncReport
from src.state import LinkRecord, SqliteStateStore

from .conftest import Fixture, build_library, write

SHOWS_MCU = Path("MCU 1.1 - Iron Man (2008)") / "MCU 1.1 - Iron Man (2008) 1080p.mkv"
SHOWS_DIAVOLI = Path("Diavoli (2020)")
ANIME_INAZUMA = Path("Inazuma Eleven (ITA) (2008)")


def _outcome(report: SyncReport, root_id: str):
    return next(entry for entry in report.roots if entry.root_id == root_id)


def test_first_sync_creates_hardlinks_and_nfos(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    report = pipe.sync(trigger="test")
    assert report.ok

    movies = library.clone / "Movies"
    shows = library.clone / "Shows"
    anime = library.clone / "Anime"

    iron_man = movies / "Iron Man (2008)" / "Iron Man (2008).mkv"
    assert iron_man.is_file()
    assert os.path.samefile(iron_man, library.source("shows") / SHOWS_MCU)
    nfo = (movies / "Iron Man (2008)" / "movie.nfo").read_text(encoding="utf-8")
    assert "<sorttitle>MCU 01.01</sorttitle>" in nfo
    assert "<set>Marvel Cinematic Universe</set>" in nfo

    episode = shows / "Diavoli (2020)" / "Season 01" / "Diavoli (2020) - S01E01.mkv"
    assert episode.is_file()
    assert (shows / "Diavoli (2020)" / "Season 01" / "Diavoli (2020) - S01E02.mkv").is_file()
    assert not (shows / "Diavoli (2020)" / "tvshow.nfo").exists()

    one_piece = anime / "One Piece (1999)" / "Season 01" / "One Piece (1999) - S01E701.mp4"
    assert one_piece.is_file()
    assert (anime / "One Piece (1999)" / "tvshow.nfo").is_file()
    episode_nfo = anime / "One Piece (1999)" / "Season 01" / "One Piece (1999) - S01E700.nfo"
    assert "<episode>700</episode>" in episode_nfo.read_text(encoding="utf-8")
    assert store.pending_count() == 0
    assert len(store.recent_runs(10)) == len(report.roots)


def test_second_sync_is_unchanged_and_journals_nothing(pipeline) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    runs_before = len(store.recent_runs(10))

    report = pipe.sync(trigger="second")
    assert all(outcome.unchanged for outcome in report.roots)
    assert len(store.recent_runs(10)) == runs_before


def test_deleting_a_source_file_prunes_the_clone_and_the_row(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("anime") / ANIME_INAZUMA / "Inazuma Eleven (ITA) (2008) - 003.mp4"
    target = (
        library.clone
        / "Anime"
        / "Inazuma Eleven (2008)"
        / "Season 01"
        / "Inazuma Eleven (2008) - S01E03.mp4"
    )
    assert target.exists()

    source.unlink()
    report = pipe.sync(trigger="prune")
    assert report.ok
    assert not target.exists()
    assert store.get_link(fsutil.normalise(source)) is None
    rows = [row for row in store.iter_links() if "Inazuma" in row.target_path]
    assert len(rows) == 1


def test_a_whole_deleted_title_removes_its_clone_directories(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source_dir = library.source("anime") / ANIME_INAZUMA
    for path in source_dir.iterdir():
        path.unlink()
    source_dir.rmdir()

    pipe.sync(trigger="prune")
    assert not (library.clone / "Anime" / "Inazuma Eleven (2008)").exists()
    assert [row for row in store.iter_links() if "Inazuma" in row.target_path] == []


def test_replacing_a_source_file_repoints_the_hardlink(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    source = library.source("shows") / SHOWS_MCU
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    source.unlink()
    source.write_bytes(b"new-and-longer" * 512)

    pipe.sync(trigger="repoint")
    assert target.read_bytes() == source.read_bytes()
    assert os.path.samefile(source, target)
    row = store.get_by_target(fsutil.normalise(target))
    assert row is not None and row.status == "done"


def test_a_foreign_file_at_a_target_is_never_replaced(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    source = library.source("shows") / SHOWS_MCU

    store.delete_link(fsutil.normalise(source))
    target.unlink()
    target.write_bytes(b"not ours")

    report = pipe.sync(trigger="refuse")
    outcome = _outcome(report, "shows")
    assert any("refusing to replace" in error for error in outcome.plan.errors)
    assert target.read_bytes() == b"not ours"


def test_a_deleted_nfo_is_recreated(pipeline, library: Fixture) -> None:
    pipe, _ = pipeline
    pipe.sync(trigger="first")
    nfo_path = library.clone / "Movies" / "Iron Man (2008)" / "movie.nfo"
    nfo_path.unlink()
    pipe.sync(trigger="nfo")
    assert nfo_path.is_file()


def test_a_stale_nfo_is_rewritten_byte_for_byte(pipeline, library: Fixture) -> None:
    pipe, _ = pipeline
    pipe.sync(trigger="first")
    nfo_path = library.clone / "Movies" / "Iron Man (2008)" / "movie.nfo"
    nfo_path.write_text("<movie><title>wrong</title></movie>", encoding="utf-8")
    pipe.sync(trigger="nfo")
    assert "<sorttitle>MCU 01.01</sorttitle>" in nfo_path.read_text(encoding="utf-8")


def test_prune_disabled_leaves_the_clone_alone(tmp_path: Path) -> None:
    library = build_library(tmp_path, MF_PRUNE_ENABLED="false")
    store = SqliteStateStore(library.state / "db.sqlite")
    try:
        pipe = Pipeline(library.settings, store, Linker(), dry_run=False, plugins=library.plugins)
        pipe.sync(trigger="first")
        source = library.source("anime") / ANIME_INAZUMA / "Inazuma Eleven (ITA) (2008) - 003.mp4"
        target = (
            library.clone
            / "Anime"
            / "Inazuma Eleven (2008)"
            / "Season 01"
            / "Inazuma Eleven (2008) - S01E03.mp4"
        )
        source.unlink()
        report = pipe.sync(trigger="prune")
        assert target.exists()
        outcome = _outcome(report, "anime")
        skips = [op for op in outcome.plan.ops if op.kind == "SKIP"]
        assert any(op.detail_dict().get("reason") == "prune disabled" for op in skips)
    finally:
        store.close()


def test_dry_run_writes_nothing_and_creates_no_run(library: Fixture) -> None:
    store = SqliteStateStore(library.state / "db.sqlite")
    try:
        pipe = Pipeline(library.settings, store, Linker(), dry_run=True, plugins=library.plugins)
        report = pipe.sync(trigger="dry")
        assert report.ok
        assert not library.clone.exists() or not any(library.clone.rglob("*.mkv"))
        assert store.recent_runs(5) == []
        assert store.pending_count() == 0
    finally:
        store.close()


def test_collision_prefers_the_higher_resolution(pipeline, library: Fixture) -> None:
    pipe, _ = pipeline
    folder = library.source("shows") / SHOWS_DIAVOLI
    write(folder / "Diavoli (2020) 720p - S01E01.mkv")
    report = pipe.sync(trigger="collision")
    outcome = _outcome(report, "shows")
    targets = [op.target_path for op in outcome.plan.ops if op.kind == "LINK_CREATE"]
    episode = [t for t in targets if t.endswith("S01E01.mkv")]
    assert len(episode) == 1
    assert any("collision" in warning for warning in outcome.plan.warnings)
    assert any(
        op.kind == "SKIP" and op.detail_dict().get("reason") == "collision"
        for op in outcome.plan.ops
    )


def test_interrupted_run_leaves_a_recoverable_pending_row(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    source = library.source("shows") / SHOWS_MCU
    target.unlink()
    stats = source.stat()
    store.put_pending(
        LinkRecord(
            source_path=fsutil.normalise(source),
            source_dev=stats.st_dev,
            source_ino=stats.st_ino,
            source_size=stats.st_size,
            source_mtime_ns=stats.st_mtime_ns,
            target_path=fsutil.normalise(target),
            target_dev=stats.st_dev,
            target_ino=0,
            target_size=0,
            library="movies",
            plugin_id="streaming_community",
            root_id="shows",
            nfo_path=None,
            nfo_sha256=None,
            sort_title=None,
            franchise=None,
            display_order=None,
            created_ts=0,
            verified_ts=None,
            status="pending",
        )
    )
    report = pipe.sync(trigger="recover")
    assert report.ok
    assert target.exists()
    assert store.pending_count() == 0
