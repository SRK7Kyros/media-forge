"""The scanner must be cheap, stable and honest about what it skips."""

from __future__ import annotations

import os
from pathlib import Path

from mediaforge.config import SourceRoot
from mediaforge.models import MediaFile
from mediaforge.scan import Scanner, signature_of

from .conftest import write


def _source(path: Path, root_id: str = "shows") -> SourceRoot:
    return SourceRoot(root_id, path)


def test_walks_every_video_and_samples_its_directory(tmp_path: Path) -> None:
    write(tmp_path / "Show (2020)" / "Show (2020) 1080p - S01E01.mkv")
    write(tmp_path / "Show (2020)" / "Show (2020) 1080p - S01E02.mkv")
    write(tmp_path / "Show (2020)" / "Show.json", b"{}")

    scan = Scanner().enumerate_root(_source(tmp_path))
    assert len(scan.files) == 2
    sample = scan.dirs[0]
    assert sample.name == "Show (2020)"
    assert sample.video_count == 2
    assert sample.sidecar_names == ("Show.json",)
    assert len(sample.sample_names) == 2


def test_hidden_directories_and_files_are_skipped(tmp_path: Path) -> None:
    write(tmp_path / ".state" / "index.json", b"{}")
    write(tmp_path / ".hidden.mkv")
    write(tmp_path / "Visible (2020)" / "Visible (2020).mkv")

    scan = Scanner().enumerate_root(_source(tmp_path))
    assert len(scan.files) == 1
    assert scan.files[0].relpath.name == "Visible (2020).mkv"
    assert any(".state" in skipped for skipped in scan.skipped)


def test_exclude_globs_remove_a_directory(tmp_path: Path) -> None:
    write(tmp_path / ".state" / "a.json", b"{}")
    write(tmp_path / "temp" / "half.mkv")
    write(tmp_path / "keep" / "keep.mkv")

    scan = Scanner(("temp",)).enumerate_root(_source(tmp_path))
    assert [media.relpath.as_posix() for media in scan.files] == ["keep/keep.mkv"]


def test_signature_is_stable_and_order_independent() -> None:
    def media(name: str, size: int, mtime: int) -> MediaFile:
        return MediaFile(
            path=Path(name),
            relpath=Path(name),
            size=size,
            mtime_ns=mtime,
            dev=1,
            ino=1,
            root_id="shows",
        )

    one = (media("a.mkv", 1, 10), media("b.mkv", 2, 20))
    two = (media("b.mkv", 2, 20), media("a.mkv", 1, 10))
    assert signature_of(one) == signature_of(two)


def test_signature_changes_on_size_mtime_and_membership() -> None:
    base = [MediaFile(Path("a"), Path("a"), 1, 10, 1, 1, "shows")]
    bigger = [MediaFile(Path("a"), Path("a"), 2, 10, 1, 1, "shows")]
    touched = [MediaFile(Path("a"), Path("a"), 1, 11, 1, 1, "shows")]
    assert len({signature_of(tuple(x)) for x in (base, bigger, touched)}) == 3


def test_detect_context_counts_extensions(tmp_path: Path) -> None:
    write(tmp_path / "A (2020)" / "a.mkv")
    write(tmp_path / "A (2020)" / "b.mp4")
    scanner = Scanner()
    scan = scanner.enumerate_root(_source(tmp_path))
    ctx = scanner.detect_context(scan)
    assert ctx.extension_histogram == {".mkv": 1, ".mp4": 1}
    assert ctx.total_files == 2


def test_a_missing_root_is_an_error_not_an_empty_scan(tmp_path: Path) -> None:
    import pytest

    from mediaforge.scan import ScanError

    with pytest.raises(ScanError):
        Scanner().enumerate_root(_source(tmp_path / "does-not-exist"))


def test_junction_loops_are_not_followed(tmp_path: Path) -> None:
    """A reparse point is skipped; if the host cannot make one, the walk still holds."""
    real = tmp_path / "real"
    write(real / "video.mkv")
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    scan = Scanner().enumerate_root(_source(tmp_path))
    assert [media.relpath.as_posix() for media in scan.files] == ["real/video.mkv"]
