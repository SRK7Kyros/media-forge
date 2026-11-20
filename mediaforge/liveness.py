"""Removing clone entries whose source has disappeared, on their own clock.

A symbolic link is not a reference that keeps bytes alive: the moment the source
is deleted the space is already free. What remains is a dangling link, which
Jellyfin reports as an error, so the entry is removed here rather than waiting
for the reconciler's next settled scan.

Two passes answer the same question from both sides:

* the **row pass** runs every liveness tick and asks each row whether its source
  still exists;
* the **tree pass** runs on a slower clock and walks the clone tree, so a
  dangling link is removed even when the row that would have explained it is
  gone - a restored database, a run killed between link and row, a row deleted
  by hand. Without it, "the source disappeared" only heals while the database
  remembers it.

Pruning is suspended for any source root whose path is not readable right now:
an unplugged drive reads exactly like "every file was deleted", and that
difference must never be guessed in the destructive direction.

This is deliberately not a second planner. It answers one question - "is this
still real?" - and it only ever removes an entry it can prove it created.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
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
    paused_roots: set[str] = field(default_factory=set)

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
        self._orphan_missing_since: dict[str, float] = {}
        self._next_tree_sweep = 0.0
        self._run_id: int | None = None

    def sweep(self) -> SweepResult:
        result = SweepResult()
        now = self._clock()
        self._run_id = None
        available = self._available_roots(result)
        seen: set[str] = set()
        for row in self.store.iter_links():
            key = fsutil.normalise(row.source_path)
            seen.add(key)
            if not available.get(row.root_id, True):
                self._missing_since.pop(key, None)
                continue
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

        walked = self._tree_sweep(now, available, result)

        if self._run_id is not None:
            self.store.finish_run(
                self._run_id,
                {"pruned": len(result.pruned), "errors": len(result.errors)},
                "partial" if result.errors else "ok",
                int(self._wall()),
            )
        if (result.pruned or walked) and not self.dry_run:
            libraries = result.libraries | walked
            self._prune_folder_nfos(libraries)
            prune_empty_library_dirs(self.settings, libraries)
        return result

    def _available_roots(self, result: SweepResult) -> dict[str, bool]:
        """Whether each source root can be read right now.

        ``os.path.isdir`` on the root - not on a file inside it - is the whole
        guard: a drive letter that is not ready, a renamed folder or a dropped
        share makes every source path read as missing at once, and acting on
        that reading would empty the clone tree and the media server with it.
        """
        available: dict[str, bool] = {}
        for source in self.settings.sources:
            present = fsutil.is_dir(source.path)
            available[source.root_id] = present
            if not present:
                result.paused_roots.add(source.root_id)
        return available

    def _tree_sweep(
        self, now: float, available: Mapping[str, bool], result: SweepResult
    ) -> set[str]:
        """Walk the clone tree for dangling links no database row explains.

        A readlink per entry costs about 0.1 s for a library of 1 100 links, so
        this runs on its own clock rather than on every liveness tick. It is the
        only pass that can heal a disappearance the database has forgotten, and
        the only one that can clear a ``movie.nfo`` left in a title folder whose
        rows were pruned by an earlier process that then died.
        """
        interval = self.settings.orphan_scan_interval_s
        if interval <= 0 or now < self._next_tree_sweep:
            return set()
        self._next_tree_sweep = now + interval
        roots = [
            source.path
            for source in self.settings.sources
            if available.get(source.root_id, True)
        ]
        walked: set[str] = set()
        seen: set[str] = set()
        for library in sorted(self.settings.library_dirs):
            base = self.settings.clone_root / self.settings.library_dirs[library]
            if not base.is_dir():
                continue
            walked.add(library)
            for dirpath, _dirnames, filenames in os.walk(base):
                for name in filenames:
                    path = Path(dirpath) / name
                    if not fsutil.is_symlink(path):
                        continue
                    key = fsutil.normalise(path)
                    seen.add(key)
                    if os.path.exists(str(path)):
                        self._orphan_missing_since.pop(key, None)
                        continue
                    target = fsutil.link_target(path)
                    if target is None:
                        continue
                    if not any(fsutil.is_within(target, root) for root in roots):
                        continue
                    first_seen = self._orphan_missing_since.setdefault(key, now)
                    if now - first_seen < self.settings.liveness_grace_s:
                        continue
                    self._prune_orphan(path, library, target, result)
                    self._orphan_missing_since.pop(key, None)

        for key in [key for key in self._orphan_missing_since if key not in seen]:
            del self._orphan_missing_since[key]
        return walked

    def _prune_orphan(
        self, path: Path, library: str, target: Path, result: SweepResult
    ) -> None:
        """Remove a dangling link that no row accounts for.

        The proof of provenance is the link's own target: nothing but this tool
        writes symbolic links into the clone tree, and a target under a
        configured source root names exactly the kind of entry it creates.
        """
        if not self.dry_run and not self._unlink(path):
            result.errors.append(f"{path}: could not be removed")
            return
        if not self.dry_run:
            self.store.mark_dirty(library, int(self._wall()))
            self._journal_orphan(path, target, library)
        result.pruned.append(fsutil.normalise(path))
        result.libraries.add(library)

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

    def _journal_orphan(self, target: Path, source: Path, library: str) -> None:
        now = int(self._wall())
        if self._run_id is None:
            self._run_id = self.store.begin_run("liveness", now)
        op = Op(
            "FILE_PRUNE",
            fsutil.normalise(target),
            str(source),
            (("reason", "source gone (no row)"), ("library", library)),
        )
        self.store.log_attempt(self._run_id, op, "ok", "pruned", now)
