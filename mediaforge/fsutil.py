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
    """Absolute Windows path with backslashes, used as a database key.

    ``os.readlink`` on Windows returns the target of a link this tool created as
    a *verbatim* path (``\\\\?\\D:\\...``), and a verbatim path never compares
    equal to the plain one even though both name the same file. The prefix is
    stripped here rather than at every comparison, so a link target and a source
    path are interchangeable keys.
    """
    text = str(path)
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\") :]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\") :]
    return os.path.abspath(text).replace("/", "\\")


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
    """True for a real file *and* for a dangling symbolic link.

    ``os.path.lexists`` is deliberate: a link whose target was deleted still
    occupies a name in the clone tree and must be prunable.
    """
    return os.path.lexists(str(path))


def is_symlink(path: Path | str) -> bool:
    return os.path.islink(str(path))


def link_target(path: Path | str) -> Path | None:
    """The absolute path a symbolic link names, or None when it is not a link.

    Reads the link itself, never the file behind it, so it still answers for a
    dangling link whose source has been deleted.
    """
    try:
        raw = os.readlink(str(path))
    except OSError:
        return None
    base = os.path.dirname(os.path.abspath(str(path)))
    return Path(os.path.abspath(os.path.join(base, raw)))


def entry_owned_by(path: Path | str, source: Path | str, recorded_ino: int | None) -> bool:
    """Whether ``path`` is an entry this tool created for ``source``.

    Two shapes are legitimate:

    * a symbolic link naming ``source`` exactly - the current mode;
    * a *legacy* hard link whose inode is the one recorded when the row was
      written. The tree was made of hard links before the switch to symbolic
      links (2026-11-13) and a hard link keeps its inode, so that inode is
      still proof the tool created it - and the only way an orphan from the
      hard-link era can ever be reclaimed.

    A symbolic link naming anywhere else is never ours; a plain file is ours
    only when its inode matches what was recorded.
    """
    if not exists(path):
        return False
    target = link_target(path)
    if target is not None:
        return normalise(target) == normalise(source)
    if recorded_ino is None:
        return False
    try:
        return inode_of(path)[1] == recorded_ino
    except OSError:
        return False


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


def symlink_supported(directory: Path | str | None = None) -> bool:
    """Whether this *process* may create a symbolic link right now.

    Windows grants ``SeCreateSymbolicLinkPrivilege`` to an elevated process, and
    to every process when Developer Mode is on. A Medium-integrity shell has
    neither, so the only honest probe is to try it once in a scratch directory.
    """
    base = Path(directory) if directory is not None else Path(tempfile.gettempdir())
    token = unique_suffix()
    source = base / f".mf-symlink-probe-{token}.tmp"
    link = base / f".mf-symlink-probe-{token}.lnk"
    try:
        source.write_bytes(b"")
        os.symlink(str(source), str(link))
        return True
    except OSError:
        return False
    finally:
        for path in (link, source):
            try:
                os.unlink(str(path))
            except OSError:
                pass


def make_dirs(path: Path) -> bool:
    """Create ``path`` and parents. Returns True when something was created."""
    if path.is_dir():
        return False
    path.mkdir(parents=True, exist_ok=True)
    return True
