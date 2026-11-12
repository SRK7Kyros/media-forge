"""Enumerating a source root with ``stat`` only — never an ``open``.

One walk produces everything the rest of the pipeline needs: the file list, a
cheap structural sample for plugin detection, and a signature that is stable
across runs and changes when a file is added, removed, resized or touched.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat as stat_module
from pathlib import Path

from . import fsutil
from .config import SourceRoot
from .models import VIDEO_EXTENSIONS, DirectorySample, MediaFile, ScanResult
from .plugin_api import DetectContext

REPARSE_FLAG = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SAMPLE_NAMES = 6


class ScanError(RuntimeError):
    """A source root could not be read at all."""


def _is_reparse_point(path: Path) -> bool:
    """Junctions and symlinks are skipped: following one can leave the volume."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_FLAG)


class Scanner:
    def __init__(self, exclude_globs: tuple[str, ...] = ()) -> None:
        self._globs = tuple(exclude_globs)

    def _skip(self, name: str, relpath: str) -> bool:
        if name.startswith("."):
            return True
        return any(
            fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relpath, pattern)
            for pattern in self._globs
        )

    def enumerate_root(self, root: SourceRoot) -> ScanResult:
        root_path = root.path
        if not root_path.is_dir():
            raise ScanError(f"Source '{root.root_id}' is not a directory: {root_path}")

        files: list[MediaFile] = []
        dirs: list[DirectorySample] = []
        skipped: list[str] = []

        for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
            current = Path(dirpath)
            rel_dir = fsutil.relpath(root_path, current)
            keep: list[str] = []
            for name in sorted(dirnames):
                full = current / name
                rel = str(fsutil.relpath(root_path, full))
                if self._skip(name, rel):
                    skipped.append(rel)
                    continue
                if _is_reparse_point(full):
                    skipped.append(rel + " (reparse point)")
                    continue
                keep.append(name)
            dirnames[:] = keep

            videos = 0
            sidecars: list[str] = []
            samples: list[str] = []
            for name in sorted(filenames):
                full = current / name
                if name.startswith("."):
                    skip_rel = str(fsutil.relpath(root_path, full))
                    skipped.append(skip_rel)
                    continue
                if full.suffix.lower() not in VIDEO_EXTENSIONS:
                    sidecars.append(name)
                    continue
                try:
                    info = full.stat()
                except OSError:
                    skipped.append(str(fsutil.relpath(root_path, full)) + " (vanished)")
                    continue
                files.append(
                    MediaFile(
                        path=full,
                        relpath=fsutil.relpath(root_path, full),
                        size=info.st_size,
                        mtime_ns=info.st_mtime_ns,
                        dev=info.st_dev,
                        ino=info.st_ino,
                        root_id=root.root_id,
                    )
                )
                videos += 1
                if len(samples) < SAMPLE_NAMES:
                    samples.append(name)

            if str(rel_dir) != "." and len(rel_dir.parts) == 1:
                dirs.append(
                    DirectorySample(
                        name=current.name,
                        relpath=rel_dir,
                        video_count=videos,
                        sidecar_names=tuple(sidecars),
                        sample_names=tuple(samples),
                    )
                )

        return ScanResult(
            root_id=root.root_id,
            root_path=root_path,
            files=tuple(files),
            dirs=tuple(dirs),
            signature=signature_of(files),
            skipped=tuple(skipped),
        )

    def detect_context(self, scan: ScanResult) -> DetectContext:
        histogram: dict[str, int] = {}
        for media in scan.files:
            key = media.path.suffix.lower()
            histogram[key] = histogram.get(key, 0) + 1
        return DetectContext(
            root_id=scan.root_id,
            root_path=scan.root_path,
            dirs=scan.dirs,
            total_files=len(scan.files),
            extension_histogram=histogram,
        )


def signature_of(files: tuple[MediaFile, ...]) -> str:
    """sha256 over ``relpath|size|mtime_ns`` sorted — cheap and order-independent."""
    digest = hashlib.sha256()
    for line in sorted(f"{f.relpath.as_posix()}|{f.size}|{f.mtime_ns}" for f in files):
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
