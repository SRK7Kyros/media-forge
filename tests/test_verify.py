"""verify: the only command that can honestly say pruning is safe."""

from __future__ import annotations

import os

from src.linker import Linker
from src.state import SqliteStateStore
from src.verify import Verifier, summarise

from .conftest import Fixture, requires_symlinks, write

pytestmark = requires_symlinks


def _verifier(library: Fixture, store: SqliteStateStore) -> Verifier:
    return Verifier(library.settings, store, Linker())


def test_a_fresh_clone_is_clean(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    report = _verifier(library, store).run()
    assert report.ok, summarise(report)
    assert report.orphan_files == []
    assert report.tracked > 0


def test_a_missing_target_is_reported(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    (library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv").unlink()
    report = _verifier(library, store).run()
    assert not report.ok
    assert report.missing_target


def test_a_copy_instead_of_a_link_is_not_linked(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    target = library.clone / "Movies" / "Iron Man (2008)" / "Iron Man (2008).mkv"
    os.unlink(target)
    write(target, b"a copy, not a link")
    report = _verifier(library, store).run()
    assert report.not_linked


def test_an_untracked_file_is_an_orphan(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    write(library.clone / "Movies" / "Stray (1999)" / "Stray (1999).mkv")
    report = _verifier(library, store).run()
    assert any("Stray (1999).mkv" in path for path in report.orphan_files)


def test_orphan_scan_can_be_skipped(pipeline, library: Fixture) -> None:
    pipe, store = pipeline
    pipe.sync(trigger="first")
    write(library.clone / "Movies" / "Stray (1999)" / "Stray (1999).mkv")
    report = _verifier(library, store).run(include_orphans=False)
    assert report.ok
    assert report.orphan_files == []
