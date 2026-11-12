"""Filesystem primitives: volumes, inodes, atomic writes, hashing.

Nothing here knows about Jellyfin, plugins or state. Every function is a thin,
inspectable wrapper over the standard library so the layers above can be tested
without a real library.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path, PureWindowsPath

CHUNK = 1024 * 1024


def normalise(path: Path | str) -> str:
    """Absolute Windows path with backslashes, used as a database key."""
    return os.path.abspath(str(path)).replace("/", "\\")


def relpath(base: Path, path: Path) -> PureWindowsPath:
    """``path`` relative to ``base``, as a Windows relative path."""
    return PureWindowsPath(os.path.relpath(normalise(path), normalise(base)))


def is_within(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` or lives underneath it."""
    child_s = normalise(child).lower()
    parent_s = normalise(parent).lower().rstrip("\\")
    return child_s == parent_s or child_s.startswith(parent_s + "\\")


def volume_of(path: Path | str) -> int | None:
    """``st_dev`` of ``path`` or of its nearest existing ancestor.

    The clone root usually does not exist yet on a first run, so walking up is
    what makes the cross-volume guard usable *before* anything is created.
    """
    current = Path(os.path.abspath(str(path)))
    while True:
        try:
            return current.stat().st_dev
        except OSError:
            parent = current.parent
            if parent == current:
                return None
            current = parent


def volume_name(path: Path | str) -> str:
    """``D:`` for a path on D:, used only in error messages."""
    drive = os.path.splitdrive(os.path.abspath(str(path)))[0]
    return drive or str(path)


def inode_of(path: Path | str) -> tuple[int, int]:
    """``(st_dev, st_ino)`` — identity of a file across renames."""
    info = os.stat(str(path))
    return info.st_dev, info.st_ino


def hardlink_count(path: Path | str) -> int:
    """``st_nlink``: 1 means the tracked file has no other hard link."""
    return os.stat(str(path)).st_nlink


def exists(path: Path | str) -> bool:
    return os.path.lexists(str(path))


def is_file(path: Path | str) -> bool:
    return os.path.isfile(str(path))


def is_dir(path: Path | str) -> bool:
    return os.path.isdir(str(path))


def same_file(a: Path | str, b: Path | str) -> bool:
    """``os.path.samefile`` without raising when either side is missing."""
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` so a reader sees the old bytes or the new ones, never both.

    The temporary file lives in the destination directory, so ``os.replace`` is
    a same-volume rename. On NTFS that is atomic for a reader already holding a
    handle to the old file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=".mf-tmp-", suffix=".part", dir=str(path.parent), delete=False
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def unique_suffix() -> str:
    """Short random token for scratch names."""
    return uuid.uuid4().hex[:8]


def make_dirs(path: Path) -> bool:
    """Create ``path`` and parents. Returns True when something was created."""
    if path.is_dir():
        return False
    path.mkdir(parents=True, exist_ok=True)
    return True
