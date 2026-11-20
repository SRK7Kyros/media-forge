"""Frozen value types shared by every layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal

Library = Literal["movies", "shows", "anime"]

LIBRARY_DIRS: dict[str, str] = {
    "movies": "Movies",
    "shows": "Shows",
    "anime": "Anime",
}

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".ts", ".webm"})

OpKind = Literal[
    "MKDIR",
    "LINK_CREATE",
    "LINK_REPOINT",
    "LINK_ADOPT",
    "NFO_WRITE",
    "NFO_DELETE",
    "FILE_PRUNE",
    "DIR_PRUNE",
    "SKIP",
]


@dataclass(frozen=True, slots=True)
class MediaFile:
    path: Path
    relpath: PurePath
    size: int
    mtime_ns: int
    dev: int
    ino: int
    root_id: str


@dataclass(frozen=True, slots=True)
class DirectorySample:
    name: str
    relpath: PurePath
    video_count: int
    sidecar_names: tuple[str, ...]
    sample_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScanResult:
    root_id: str
    root_path: Path
    files: tuple[MediaFile, ...]
    dirs: tuple[DirectorySample, ...]
    signature: str
    skipped: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TargetPath:
    library: Library
    relative: PurePath


@dataclass(frozen=True, slots=True)
class NfoSpec:
    kind: Literal["movie", "episode", "tvshow"]
    elements: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PlannedItem:
    """One source file's destination, plus every NFO it implies.

    ``nfos`` is a tuple because a single episode can justify two files: its own
    ``.nfo`` and the show-level ``tvshow.nfo``. Paths are relative to the
    library root, so the plan stays independent of the clone root.
    """

    source: MediaFile
    target: TargetPath
    nfos: tuple[tuple[PurePath, NfoSpec], ...]
    sort_title: str | None
    franchise: str | None
    order: tuple[int, ...] | None
    plugin_id: str


@dataclass(frozen=True, slots=True)
class Op:
    kind: OpKind
    target_path: str
    source_path: str | None = None
    detail: tuple[tuple[str, str], ...] = ()
    nfo: NfoSpec | None = None

    def detail_dict(self) -> dict[str, str]:
        return dict(self.detail)
