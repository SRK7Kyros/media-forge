"""Execute a plan: idempotent, journaled, and safe to interrupt.

Order matters and is already encoded in the plan: directories, then links, then
NFOs, then prunes. A crash can leave a row without a file (because the row is
committed *before* the syscall) and never a file without a row.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from . import fsutil, nfo
from .config import Settings
from .linker import Linker
from .models import Op
from .planner import PlanReport
from .state import LinkRecord, SqliteStateStore

WRITE_KINDS = frozenset({"LINK_CREATE", "LINK_REPOINT", "LINK_ADOPT"})


@dataclass(slots=True)
class RunResult:
    run_id: int
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    libraries: set[str] = field(default_factory=set)
    interrupted: bool = False

    @property
    def outcome(self) -> str:
        if self.interrupted:
            return "interrupted"
        return "partial" if self.errors else "ok"


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        store: SqliteStateStore,
        linker: Linker,
        *,
        dry_run: bool = False,
        interrupt: Event | None = None,
        clock=time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.linker = linker
        self.dry_run = dry_run
        self.interrupt = interrupt or Event()
        self.clock = clock

    def execute(self, *, root_id: str, plan: PlanReport, trigger: str) -> RunResult:
        run_id = 0 if self.dry_run else self.store.begin_run(trigger, int(self.clock()))
        result = RunResult(run_id=run_id)
        result.counts.update(plan.counts)
        result.errors.extend(plan.errors)

        for op in plan.ops:
            if self.interrupt.is_set():
                result.interrupted = True
                break
            try:
                detail = self._apply(op)
            except Exception as exc:  # one bad item must not abort the batch
                result.errors.append(f"{op.kind} {op.target_path}: {exc}")
                result.counts["errors"] = result.counts.get("errors", 0) + 1
                if not self.dry_run:
                    self.store.log_attempt(
                        run_id, op, "error", f"{type(exc).__name__}: {exc}", int(self.clock())
                    )
                continue
            if not self.dry_run:
                self.store.log_attempt(run_id, op, "ok", detail, int(self.clock()))
            library = op.detail_dict().get("library")
            if library and op.kind != "SKIP":
                result.libraries.add(library)

        if not self.dry_run:
            if self.settings.jellyfin_refresh:
                for library in sorted(result.libraries):
                    self.store.mark_dirty(library, int(self.clock()))
            self._prune_empty(result)
            self.store.finish_run(result.run_id, result.counts, result.outcome, int(self.clock()))
        return result

    # ── one operation ───────────────────────────────────────────────────────

    def _apply(self, op: Op) -> str:
        if self.dry_run and op.kind not in {"MKDIR", "SKIP"}:
            return f"would {op.kind.lower()}"

        if op.kind == "MKDIR":
            created = False if self.dry_run else fsutil.make_dirs(Path(op.target_path))
            return "created" if created else "exists"

        if op.kind in WRITE_KINDS:
            return self._apply_link(op)

        if op.kind == "NFO_WRITE":
            if op.nfo is None:
                raise RuntimeError("NFO_WRITE without a spec")
            digest, wrote = nfo.write(Path(op.target_path), op.nfo, dry_run=self.dry_run)
            owner = op.detail_dict().get("owner_target")
            if owner and not self.dry_run:
                self.store.set_nfo(owner, digest, int(self.clock()))
            return "written" if wrote else "current"

        if op.kind == "NFO_DELETE":
            self._unlink(Path(op.target_path))
            return "deleted"

        if op.kind == "FILE_PRUNE":
            self._unlink(Path(op.target_path))
            self._forget_pruned(op)
            return "pruned"

        if op.kind == "DIR_PRUNE":
            try:
                os.rmdir(op.target_path)
                return "removed"
            except OSError:
                return "not empty"

        if op.kind == "SKIP":
            return op.detail_dict().get("reason", "skipped")

        raise RuntimeError(f"unknown op {op.kind}")

    def _apply_link(self, op: Op) -> str:
        assert op.source_path is not None
        source = Path(op.source_path)
        target = Path(op.target_path)
        detail = op.detail_dict()
        self.linker.require_linkable(source, self.settings.clone_root)

        if not self.dry_run:
            self.store.put_pending(self._record(source, target, detail))

        outcome = self.linker.ensure(source, target)

        if not self.dry_run:
            inode = self.linker.inode_of_target(target)[1]
            try:
                size = os.stat(str(target)).st_size
            except OSError:  # the source vanished between the link and the stat
                size = 0
            digest = detail.get("nfo_sha256") or None
            self.store.confirm(
                fsutil.normalise(source), inode, size, digest, int(self.clock())
            )
        return outcome

    def _record(self, source: Path, target: Path, detail: dict[str, str]) -> LinkRecord:
        source_info = os.stat(str(source))
        order = detail.get("order") or None
        return LinkRecord(
            source_path=fsutil.normalise(source),
            source_dev=source_info.st_dev,
            source_ino=source_info.st_ino,
            source_size=source_info.st_size,
            source_mtime_ns=source_info.st_mtime_ns,
            target_path=fsutil.normalise(target),
            target_dev=source_info.st_dev,
            target_ino=source_info.st_ino,
            target_size=source_info.st_size,
            library=detail.get("library", "movies"),
            plugin_id=detail.get("plugin_id", ""),
            root_id=detail.get("root_id", ""),
            nfo_path=detail.get("nfo_path") or None,
            nfo_sha256=None,
            sort_title=detail.get("sort_title") or None,
            franchise=detail.get("franchise") or None,
            display_order=order,
            created_ts=int(self.clock()),
            verified_ts=None,
            status="pending",
        )

    def _unlink(self, path: Path) -> None:
        if self.dry_run:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def _forget_pruned(self, op: Op) -> None:
        """Drop the row behind a pruned file, but only while it still names it.

        Links reconcile before prunes, and a relocated item is both a write at its
        new target and a prune of its old one under the *same* source key. Deleting
        by key alone therefore discards the row the same run just wrote.
        """
        if not op.source_path:
            return
        row = self.store.get_link(op.source_path)
        if row is None:
            return
        if fsutil.normalise(row.target_path) != fsutil.normalise(op.target_path):
            return
        self.store.delete_link(op.source_path)

    def _prune_empty(self, result: RunResult) -> None:
        prune_empty_library_dirs(self.settings, result.libraries)


def prune_empty_library_dirs(settings: Settings, libraries: set[str]) -> list[Path]:
    """Remove library directories that no longer hold anything.

    A second sweep after the plan, because a directory can empty out only after
    its own DIR_PRUNE has run. The liveness sweeper reuses it, so a prune outside
    a sync run leaves no hollow ``Season 01`` folders behind either.
    """
    removed: list[Path] = []
    for library in sorted(libraries):
        root = settings.clone_root / settings.library_dirs[library]
        if not root.is_dir():
            continue
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
            if Path(dirpath) == root:
                continue
            try:
                os.rmdir(dirpath)
            except OSError:
                continue
            removed.append(Path(dirpath))
    return removed
