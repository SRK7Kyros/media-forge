"""Proving that the clone tree still is what the database says it is.

``verify`` answers three questions, in this order of importance:

1. Is every tracked item still a hard link of its source?
2. Is there a file in the clone tree that this tool did not create?
3. Is every piece of recorded metadata still on disk?

Question 2 is the reason this command exists at all: it is the only check that
can tell a human "yes, pruning is safe" instead of "probably".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from .config import Settings
from .linker import Linker
from .state import LinkRecord, SqliteStateStore

logger = logging.getLogger("mediaforge.verify")

FOLDER_NFOS = frozenset({"movie.nfo", "tvshow.nfo"})


@dataclass(slots=True)
class VerifyReport:
    tracked: int = 0
    healthy: int = 0
    missing_source: list[LinkRecord] = field(default_factory=list)
    missing_target: list[LinkRecord] = field(default_factory=list)
    not_linked: list[LinkRecord] = field(default_factory=list)
    inode_changed: list[LinkRecord] = field(default_factory=list)
    missing_nfo: list[LinkRecord] = field(default_factory=list)
    pending: list[LinkRecord] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    scanned_clone_files: int = 0

    @property
    def problems(self) -> int:
        return (
            len(self.missing_source)
            + len(self.missing_target)
            + len(self.not_linked)
            + len(self.inode_changed)
            + len(self.missing_nfo)
            + len(self.orphan_files)
        )

    @property
    def ok(self) -> bool:
        return self.problems == 0


class Verifier:
    def __init__(self, settings: Settings, store: SqliteStateStore, linker: Linker) -> None:
        self.settings = settings
        self.store = store
        self.linker = linker

    def run(self, *, include_orphans: bool = True) -> VerifyReport:
        report = VerifyReport()
        tracked_targets: set[str] = set()
        tracked_dirs: set[str] = set()
        stems_by_dir: dict[str, set[str]] = {}

        for row in self.store.iter_links():
            report.tracked += 1
            target_key = fsutil.normalise(row.target_path)
            tracked_targets.add(target_key)
            parent = Path(row.target_path).parent
            tracked_dirs.add(fsutil.normalise(parent))
            stems_by_dir.setdefault(fsutil.normalise(parent), set()).add(
                Path(row.target_path).stem.lower()
            )
            ancestor = parent
            while fsutil.is_within(ancestor, self.settings.clone_root):
                tracked_dirs.add(fsutil.normalise(ancestor))
                if fsutil.normalise(ancestor) == fsutil.normalise(self.settings.clone_root):
                    break
                ancestor = ancestor.parent
            if row.nfo_path:
                tracked_targets.add(fsutil.normalise(row.nfo_path))
            if row.status != "done":
                report.pending.append(row)
            healthy = self._check(row, report)
            if healthy:
                report.healthy += 1

        if include_orphans:
            report.orphan_files = self._orphans(
                tracked_targets, tracked_dirs, stems_by_dir, report
            )
        return report

    def _check(self, row: LinkRecord, report: VerifyReport) -> bool:
        source = Path(row.source_path)
        target = Path(row.target_path)
        healthy = True

        if not fsutil.exists(source):
            report.missing_source.append(row)
            healthy = False
        if not fsutil.exists(target):
            report.missing_target.append(row)
            healthy = False
        if fsutil.exists(source) and fsutil.exists(target):
            if not fsutil.same_file(source, target):
                report.not_linked.append(row)
                healthy = False
            elif fsutil.hardlink_count(target) <= 1:
                report.not_linked.append(row)
                healthy = False
            else:
                try:
                    _, inode = fsutil.inode_of(target)
                except OSError:
                    inode = None
                if inode is not None and inode != row.target_ino:
                    report.inode_changed.append(row)
                    healthy = False

        if row.nfo_path and not fsutil.exists(row.nfo_path):
            report.missing_nfo.append(row)
            healthy = False
        return healthy

    def _orphans(
        self,
        tracked: set[str],
        tracked_dirs: set[str],
        stems_by_dir: dict[str, set[str]],
        report: VerifyReport,
    ) -> list[str]:
        root = self.settings.clone_root
        if not root.is_dir():
            return []
        orphans: list[str] = []
        for dirpath, _dirnames, filenames in _walk_skipping_junk(root):
            dir_key = fsutil.normalise(dirpath)
            sibling_stems = stems_by_dir.get(dir_key, set())
            for name in filenames:
                full = Path(dirpath) / name
                suffix = full.suffix.lower()
                if (
                    suffix not in self.settings.managed_extensions
                    and name.lower() not in FOLDER_NFOS
                ):
                    continue
                report.scanned_clone_files += 1
                key = fsutil.normalise(full)
                if key in tracked:
                    continue
                if name.lower() in FOLDER_NFOS and dir_key in tracked_dirs:
                    continue  # movie.nfo / tvshow.nfo belongs to the folder, not a row
                if suffix == ".nfo" and full.stem.lower() in sibling_stems:
                    continue  # the episode NFO next to a tracked episode
                orphans.append(key)
        return sorted(orphans)


def _walk_skipping_junk(root: Path):
    """``os.walk`` without descending into directories this tool never writes."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        yield dirpath, dirnames, sorted(filenames)


def summarise(report: VerifyReport, *, limit: int = 20) -> list[str]:
    """Turn a report into printable lines, capped so a broken tree stays readable."""
    lines = [
        f"tracked {report.tracked}, healthy {report.healthy}, "
        f"clone files scanned {report.scanned_clone_files}"
    ]
    if report.pending:
        lines.append(f"pending rows (a run was interrupted): {len(report.pending)}")
    buckets = (
        ("source missing", report.missing_source),
        ("target missing", report.missing_target),
        ("target is not a hard link of its source", report.not_linked),
        ("target inode changed", report.inode_changed),
        ("recorded NFO missing", report.missing_nfo),
    )
    for label, rows in buckets:
        if not rows:
            continue
        lines.append(f"{label}: {len(rows)}")
        for row in rows[:limit]:
            lines.append(f"  {row.target_path}  <-  {row.source_path}")
        if len(rows) > limit:
            lines.append(f"  ... and {len(rows) - limit} more")
    if report.orphan_files:
        lines.append(f"files in the clone tree with no database row: {len(report.orphan_files)}")
        for path in report.orphan_files[:limit]:
            lines.append(f"  {path}")
        if len(report.orphan_files) > limit:
            lines.append(f"  ... and {len(report.orphan_files) - limit} more")
    if report.ok:
        lines.append("verdict: clean")
    else:
        lines.append(f"verdict: {report.problems} problem(s)")
    return lines
