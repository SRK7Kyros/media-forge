"""Removing clone entries whose source has disappeared, on their own clock.

A symbolic link is not a reference that keeps bytes alive: the moment the source
is deleted the space is already free. What remains is a dangling link, which
Jellyfin reports as an error, so the entry is removed here rather than waiting
for the reconciler's next settled scan.

This is deliberately not a second planner. It answers one question - "does the
file this row points at still exist?" - and it only ever removes an entry it can
prove it created.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from .config import Settings
from .linker import Linker
from .models import Op
from .reconciler import prune_empty_library_dirs
from .state import LinkRecord, SqliteStateStore

FOLDER_NFOS = ("movie.nfo", "tvshow.nfo")


@dataclass(slots=True)
class SweepResult:
    pruned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    libraries: set[str] = field(default_factory=set)

    @property
    def changed(self) -> bool:
        return bool(self.pruned)


class LivenessSweeper:
    """Prunes a clone entry once its source has been gone for the grace window.

    The grace window is what keeps a delete-and-recreate (a downloader replacing
    a file) from flapping the mirror: the source must be missing on more than one
    observation before the entry is removed.
    """

    def __init__(
        self,
        settings: Settings,
        store: SqliteStateStore,
        linker: Linker,
        *,
        dry_run: bool = False,
        clock=time.monotonic,
        wall=time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.linker = linker
        self.dry_run = dry_run
        self._clock = clock
        self._wall = wall
        self._missing_since: dict[str, float] = {}
        self._run_id: int | None = None

    def sweep(self) -> SweepResult:
        result = SweepResult()
        now = self._clock()
        self._run_id = None
        seen: set[str] = set()
        for row in self.store.iter_links():
            key = fsutil.normalise(row.source_path)
            seen.add(key)
            if fsutil.exists(row.source_path):
                self._missing_since.pop(key, None)
                continue
            first_seen = self._missing_since.setdefault(key, now)
            if now - first_seen < self.settings.liveness_grace_s:
                continue
            self._prune(row, result)
            self._missing_since.pop(key, None)

        for key in [key for key in self._missing_since if key not in seen]:
            del self._missing_since[key]

        if self._run_id is not None:
            self.store.finish_run(
                self._run_id,
                {"pruned": len(result.pruned), "errors": len(result.errors)},
                "partial" if result.errors else "ok",
                int(self._wall()),
            )
        if result.pruned and not self.dry_run:
            self._prune_folder_nfos(result.libraries)
            prune_empty_library_dirs(self.settings, result.libraries)
        return result

    def _prune_folder_nfos(self, libraries: set[str]) -> None:
        """Remove ``movie.nfo``/``tvshow.nfo`` from a title folder that emptied out.

        An episode row owns the episode NFO, never the folder NFO, so the last
        episode of a deleted series leaves its ``tvshow.nfo`` behind and Jellyfin
        keeps advertising a show with no files. Only folders with no media of
        their own and no surviving child are touched.
        """
        if not libraries:
            return
        for library in sorted(libraries):
            root = self.settings.clone_root / self.settings.library_dirs[library]
            if not root.is_dir():
                continue
            keeps: set[str] = set()
            for dirpath, dirnames, filenames in os.walk(root, topdown=False):
                # Any file that is not the folder's own NFO means the folder is not empty.
                leftovers = any(name not in FOLDER_NFOS for name in filenames)
                has_kept_child = any(
                    fsutil.normalise(Path(dirpath) / name) in keeps for name in dirnames
                )
                if leftovers or has_kept_child:
                    keeps.add(fsutil.normalise(dirpath))
                    continue
                for name in FOLDER_NFOS:
                    candidate = Path(dirpath) / name
                    if fsutil.exists(candidate):
                        self._unlink(candidate)

    def _prune(self, row: LinkRecord, result: SweepResult) -> None:
        target = Path(row.target_path)
        if fsutil.exists(target):
            if not self._owns(row, target):
                result.errors.append(
                    f"{target} is no longer this tool's link for {row.source_path}; "
                    "not pruning it"
                )
                return
            if not self.dry_run and not self._unlink(target):
                result.errors.append(f"{target}: could not be removed")
                return

        if row.nfo_path and fsutil.exists(row.nfo_path) and not self.dry_run:
            self._unlink(Path(row.nfo_path))

        if not self.dry_run:
            self.store.delete_link(row.source_path)
            self.store.mark_dirty(row.library, int(self._wall()))
            self._journal(row, target)
        result.pruned.append(fsutil.normalise(target))
        result.libraries.add(row.library)

    def _owns(self, row: LinkRecord, target: Path) -> bool:
        return fsutil.entry_owned_by(target, row.source_path, row.target_ino)

    def _unlink(self, path: Path) -> bool:
        try:
            os.unlink(str(path))
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def _journal(self, row: LinkRecord, target: Path) -> None:
        now = int(self._wall())
        if self._run_id is None:
            self._run_id = self.store.begin_run("liveness", now)
        op = Op(
            "FILE_PRUNE",
            fsutil.normalise(target),
            row.source_path,
            (("reason", "source gone"), ("library", row.library)),
        )
        self.store.log_attempt(self._run_id, op, "ok", "pruned", now)
