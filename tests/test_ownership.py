"""Which entries a prune may remove: link target first, recorded inode second."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediaforge import fsutil

from .conftest import requires_symlinks, write


def test_a_symbolic_link_naming_the_source_is_owned(tmp_path: Path) -> None:
    source = write(tmp_path / "source.mkv", b"bytes")
    target = tmp_path / "clone.mkv"
    try:
        os.symlink(str(source), str(target))
    except OSError:
        pytest.skip("this account cannot create symbolic links")

    assert fsutil.entry_owned_by(target, source, None)


def test_a_verbatim_link_target_is_the_same_key_as_a_plain_path() -> None:
    """Windows returns a created link's target as ``\\\\?\\D:\\...``.

    That prefix is an NT namespace detail, not part of the path: without this
    the link never compares equal to the source it was made from and every
    entry in a symbolic-link tree reads as "not a link of its source".
    """
    assert fsutil.normalise("\\\\?\\D:\\Shows\\x\\y.mp4") == fsutil.normalise(
        "D:\\Shows\\x\\y.mp4"
    )
    assert fsutil.normalise("\\\\?\\UNC\\srv\\share\\y.mp4") == fsutil.normalise(
        "\\\\srv\\share\\y.mp4"
    )


@requires_symlinks
def test_a_symbolic_link_naming_somewhere_else_is_not_owned(tmp_path: Path) -> None:
    source = write(tmp_path / "source.mkv", b"bytes")
    other = write(tmp_path / "other.mkv", b"bytes")
    target = tmp_path / "clone.mkv"
    os.symlink(str(other), str(target))

    assert not fsutil.entry_owned_by(target, source, None)


def test_a_legacy_hardlink_is_owned_by_its_recorded_inode(tmp_path: Path) -> None:
    """The tree was hard links before 2026-11-13; that inode still proves it."""
    source = write(tmp_path / "source.mkv", b"bytes")
    target = tmp_path / "clone.mkv"
    os.link(str(source), str(target))
    recorded = fsutil.inode_of(target)[1]

    source.unlink()

    assert fsutil.entry_owned_by(target, source, recorded)


def test_a_hardlink_whose_inode_changed_is_not_owned(tmp_path: Path) -> None:
    source = write(tmp_path / "source.mkv", b"bytes")
    target = tmp_path / "clone.mkv"
    os.link(str(source), str(target))
    recorded = fsutil.inode_of(target)[1]

    target.unlink()
    target.write_bytes(b"a file someone else put here")

    assert not fsutil.entry_owned_by(target, source, recorded)


def test_a_plain_copy_is_owned_only_by_a_matching_inode(tmp_path: Path) -> None:
    """An unrecorded copy is not ours; a recorded inode is the only claim."""
    source = write(tmp_path / "source.mkv", b"bytes")
    target = write(tmp_path / "clone.mkv", b"bytes")

    assert not fsutil.entry_owned_by(target, source, None)
    assert not fsutil.entry_owned_by(target, source, fsutil.inode_of(target)[1] + 1)
    assert fsutil.entry_owned_by(target, source, fsutil.inode_of(target)[1])

