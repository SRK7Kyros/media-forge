"""Link creation with explicit, non-destructive semantics.

A clone entry is a second *name* for the source, never a copy:

* ``symlink`` (default) - a symbolic link. It holds no bytes, so deleting the
  source frees the space immediately; what is left behind is a dangling link
  that :class:`~src.liveness.LivenessSweeper` removes.
* ``hardlink`` - an NTFS hard link. Same inode, no bytes copied either, but the
  data stays on disk until every name is gone.

``repoint`` is the one destructive operation and it only ever replaces a link
this tool already owns.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from . import fsutil

EnsureResult = Literal["created", "reused", "repointed"]
LinkMode = Literal["symlink", "hardlink"]
LINK_MODES: tuple[str, ...] = ("symlink", "hardlink")

WINERROR_PRIVILEGE_NOT_HELD = 1314


class CrossVolumeError(RuntimeError):
    """A hard link was attempted across volumes, which NTFS cannot express."""


class LinkPrivilegeError(PermissionError):
    """Windows refused to create a symbolic link for this account."""


class Linker:
    """The concrete Windows implementation. Callers depend on the shape, not the class."""

    def __init__(self, mode: str = "symlink") -> None:
        if mode not in LINK_MODES:
            raise ValueError(f"unknown link mode {mode!r}; use one of {LINK_MODES}")
        self.mode = mode

    # �� volume rules ��������������������������������������������������������

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
            "NTFS hard links cannot cross volumes."
        )

    def require_linkable(self, src: Path, dst_root: Path) -> None:
        """Hard links cannot cross volumes; symbolic links can."""
        if self.mode == "hardlink":
            self.require_same_volume(src, dst_root)

    # �� one entry ����������������������������������������������������������

    def ensure(self, src: Path, dst: Path) -> EnsureResult:
        """Make ``dst`` a link to ``src``. Idempotent."""
        self.require_linkable(src, dst)
        if fsutil.exists(dst):
            if self.is_link_of(src, dst):
                return "reused"
            fsutil.make_dirs(dst.parent)
            self.unlink(dst)
            self._create(src, dst)
            return "repointed"
        fsutil.make_dirs(dst.parent)
        self._create(src, dst)
        return "created"

    def _create(self, src: Path, dst: Path) -> None:
        if self.mode == "hardlink":
            os.link(str(src), str(dst))
            return
        try:
            os.symlink(str(src), str(dst))
        except OSError as exc:
            if getattr(exc, "winerror", None) == WINERROR_PRIVILEGE_NOT_HELD:
                raise LinkPrivilegeError(
                    f"cannot create the symbolic link {dst}: this account has no "
                    "SeCreateSymbolicLinkPrivilege. Run media-forge from an elevated "
                    "process (the MediaForge task already does), turn on Windows "
                    "Developer Mode, or set MF_LINK_MODE=hardlink."
                ) from exc
            raise

    def is_link_of(self, src: Path, dst: Path) -> bool:
        """True when ``dst`` is a link this tool can vouch for pointing at ``src``."""
        if not fsutil.exists(dst):
            return False
        if self.mode == "symlink":
            return self.points_at(dst, src)
        if not fsutil.same_file(src, dst):
            return False
        return fsutil.hardlink_count(dst) > 1

    def points_at(self, path: Path, expected: Path) -> bool:
        """Whether ``path`` is a symbolic link naming exactly ``expected``.

        Compares the recorded target, so it still answers after the source has
        been deleted - which is precisely when a prune has to decide.
        """
        target = fsutil.link_target(path)
        if target is None:
            return False
        return fsutil.normalise(target) == fsutil.normalise(expected)

    def stat_target(self, path: Path) -> os.stat_result:
        """Identity of the entry itself, not of the file it names."""
        if self.mode == "symlink":
            return os.lstat(str(path))
        return os.stat(str(path))

    def inode_of_target(self, path: Path) -> tuple[int, int]:
        info = self.stat_target(path)
        return info.st_dev, info.st_ino

    def links_of(self, path: Path) -> list[Path]:
        """Every other hard link of ``path``, via ``fsutil hardlink list``.

        Hard-link mode only: a symbolic link has no peer set. An empty list
        means "none found", which is also what a failed query returns - never
        treat it as proof that no other link exists.
        """
        if self.mode != "hardlink":
            return []
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
        """Remove one name. Never removes the file it points at."""
        try:
            os.unlink(str(dst))
        except FileNotFoundError:
            return
