"""Hardlink creation with explicit, non-destructive semantics.

A clone is only ever a second name for the same inode. ``repoint`` is the one
destructive operation and it only ever replaces a file this tool already owns.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from . import fsutil

EnsureResult = Literal["created", "reused", "repointed"]


class CrossVolumeError(RuntimeError):
    """A hardlink was attempted across volumes, which NTFS cannot express."""


class Linker:
    """The concrete Windows implementation. Callers depend on the shape, not the class."""

    def same_volume(self, a: Path, b: Path) -> bool:
        left = fsutil.volume_of(a)
        right = fsutil.volume_of(b)
        return left is not None and left == right

    def require_same_volume(self, src: Path, dst_root: Path) -> None:
        if self.same_volume(src, dst_root):
            return
        raise CrossVolumeError(
            f"Clone root {dst_root} is on volume {fsutil.volume_name(dst_root)} but source "
            f"{src} is on volume {fsutil.volume_name(src)}. "
            "NTFS hardlinks cannot cross volumes."
        )

    def ensure(self, src: Path, dst: Path) -> EnsureResult:
        """Make ``dst`` a hardlink of ``src``. Idempotent."""
        self.require_same_volume(src, dst)
        if fsutil.exists(dst):
            if fsutil.same_file(src, dst):
                return "reused"
            fsutil.make_dirs(dst.parent)
            self.unlink(dst)
            os.link(str(src), str(dst))
            return "repointed"
        fsutil.make_dirs(dst.parent)
        os.link(str(src), str(dst))
        return "created"

    def is_link_of(self, src: Path, dst: Path) -> bool:
        """True when ``dst`` is a second name for exactly ``src``'s inode."""
        if not (fsutil.exists(src) and fsutil.exists(dst)):
            return False
        if not fsutil.same_file(src, dst):
            return False
        return fsutil.hardlink_count(dst) > 1

    def links_of(self, path: Path) -> list[Path]:
        """Every other hard link of ``path``, via ``fsutil hardlink list``.

        An empty list means "none found", which is also what a failed query
        returns: never treat it as proof that no other link exists.
        """
        try:
            result = subprocess.run(
                ["fsutil", "hardlink", "list", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        drive = os.path.splitdrive(str(path))[0]
        found: list[Path] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            absolute = drive + line if line.startswith("\\") else line
            candidate = Path(os.path.abspath(absolute))
            if not fsutil.same_file(candidate, path):
                continue
            if fsutil.normalise(candidate) != fsutil.normalise(path):
                found.append(candidate)
        return found

    def unlink(self, dst: Path) -> None:
        """Remove one name. Never removes another link in the set."""
        try:
            os.unlink(str(dst))
        except FileNotFoundError:
            return
