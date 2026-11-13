"""Turn plugin output into an ordered, idempotent list of operations.

The plan is a *diff*: what the plugin wants, what the clone tree currently is,
and what the database remembers. Those three disagree in specific, enumerable
ways, and each disagreement has exactly one operation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil, nfo
from .config import Settings
from .models import MediaFile, Op, PlannedItem, ScanResult
from .naming import parse
from .state import LinkRecord

KIND_ORDER = {
    "MKDIR": 0,
    "LINK_CREATE": 1,
    "LINK_REPOINT": 1,
    "LINK_ADOPT": 1,
    "NFO_WRITE": 2,
    "NFO_DELETE": 2,
    "SKIP": 3,
    "FILE_PRUNE": 4,
    "DIR_PRUNE": 5,
}
FOLDER_NFOS = ("movie.nfo", "tvshow.nfo")


@dataclass(slots=True)
class PlanReport:
    ops: tuple[Op, ...]
    items: tuple[PlannedItem, ...]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def writes(self) -> tuple[Op, ...]:
        return tuple(op for op in self.ops if op.kind not in {"SKIP", "DIR_PRUNE", "MKDIR"})


class Planner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ── paths ───────────────────────────────────────────────────────────────

    def library_root(self, library: str) -> Path:
        return self.settings.clone_root / self.settings.library_dirs[library]

    def target_path(self, item: PlannedItem) -> Path:
        return self.library_root(item.target.library) / item.target.relative

    def nfo_path(self, item: PlannedItem, relative) -> Path:
        return self.library_root(item.target.library) / relative

    # ── collision ranking ───────────────────────────────────────────────────

    def _resolution_rank(self, name: str) -> int:
        resolution = parse.parse_resolution(name)
        preference = self.settings.resolution_preference
        if resolution is None:
            return len(preference)
        try:
            return preference.index(resolution)
        except ValueError:
            return len(preference)

    # ── planning ────────────────────────────────────────────────────────────

    def build(
        self,
        *,
        root_id: str,
        scan: ScanResult,
        items: tuple[PlannedItem, ...],
        previous: Mapping[str, LinkRecord],
    ) -> PlanReport:
        report = PlanReport(ops=(), items=items)
        known_inodes = {(media.dev, media.ino): media for media in scan.files}

        chosen, chosen_ops = self._resolve_collisions(items, report)
        ops: list[Op] = list(chosen_ops)

        planned_targets: set[str] = set()
        planned_dirs: set[str] = set()
        moved_from: set[str] = set()
        dirs_to_make: set[Path] = set()
        nfo_writes: dict[str, tuple[Path, str]] = {}
        rows_by_source = {fsutil.normalise(path): row for path, row in previous.items()}

        for target_key, item in chosen.items():
            target = Path(target_key)
            planned_targets.add(target_key)
            ancestor = target.parent
            while fsutil.is_within(ancestor, self.settings.clone_root):
                planned_dirs.add(fsutil.normalise(ancestor))
                if fsutil.normalise(ancestor) == fsutil.normalise(self.settings.clone_root):
                    break
                ancestor = ancestor.parent
            row = rows_by_source.get(fsutil.normalise(item.source.path))
            if not fsutil.is_dir(target.parent):
                dirs_to_make.add(target.parent)

            kind, reason = self._link_kind(item, target, row, known_inodes)
            if kind is None:
                if reason:
                    report.errors.append(reason)
                    ops.append(
                        Op("SKIP", target_key, str(item.source.path), (("reason", reason),))
                    )
            else:
                ops.append(
                    Op(
                        kind,
                        target_key,
                        str(item.source.path),
                        self._link_detail(item, root_id),
                    )
                )
                counter = {
                    "LINK_CREATE": "created",
                    "LINK_REPOINT": "repointed",
                    "LINK_ADOPT": "adopted",
                }[kind]
                report.counts[counter] = report.counts.get(counter, 0) + 1

            if row is not None and fsutil.normalise(row.target_path) != target_key:
                moved_key = fsutil.normalise(row.target_path)
                moved_from.add(moved_key)
                ops.append(self._prune_op(Path(moved_key), "target moved", row, report))
                planned_dirs.add(fsutil.normalise(Path(moved_key).parent))

            for relative, spec in item.nfos:
                path = self.nfo_path(item, relative)
                expected = nfo.digest(spec)
                path_key = fsutil.normalise(path)
                if path_key in nfo_writes:
                    continue  # one show-level NFO is shared by every episode in it
                if not fsutil.is_dir(path.parent):
                    dirs_to_make.add(path.parent)
                if self._nfo_current(path, expected):
                    continue
                nfo_writes[path_key] = (path, expected)
                ops.append(
                    Op(
                        "NFO_WRITE",
                        path_key,
                        None,
                        (
                            ("nfo_sha256", expected),
                            ("relative", str(relative)),
                            ("library", item.target.library),
                            ("owner_target", target_key),
                        ),
                        nfo=spec,
                    )
                )

        ops.extend(
            self._prune_ops(planned_targets, planned_dirs, rows_by_source, moved_from, report)
        )
        ops.extend(
            Op("MKDIR", fsutil.normalise(path), None, ())
            for path in sorted(dirs_to_make, key=lambda p: (len(p.parts), str(p).lower()))
        )

        report.counts.setdefault("scanned", len(scan.files))
        report.counts["nfo_written"] = sum(1 for op in ops if op.kind == "NFO_WRITE")
        report.ops = tuple(sorted(ops, key=_sort_key))
        return report

    def _resolve_collisions(
        self, items: tuple[PlannedItem, ...], report: PlanReport
    ) -> tuple[dict[str, PlannedItem], list[Op]]:
        groups: dict[str, list[PlannedItem]] = {}
        for item in items:
            groups.setdefault(fsutil.normalise(self.target_path(item)), []).append(item)
        chosen: dict[str, PlannedItem] = {}
        ops: list[Op] = []
        for target, group in groups.items():
            ordered = sorted(
                group,
                key=lambda i: (
                    self._resolution_rank(i.source.path.name),
                    str(i.source.path).lower(),
                ),
            )
            chosen[target] = ordered[0]
            for loser in ordered[1:]:
                report.counts["superseded"] = report.counts.get("superseded", 0) + 1
                report.warnings.append(
                    f"collision at {target}: kept {ordered[0].source.path.name}, "
                    f"skipped {loser.source.path.name}"
                )
                ops.append(
                    Op("SKIP", target, str(loser.source.path), (("reason", "collision"),))
                )
        return chosen, ops

    def _links_source(self, target: Path, source: Path) -> bool:
        """Whether ``target`` is already the link this tool would create."""
        if self.settings.link_mode == "symlink":
            if not fsutil.is_symlink(target):
                return False
            link = fsutil.link_target(target)
            return link is not None and fsutil.normalise(link) == fsutil.normalise(source)
        return fsutil.same_file(source, target) and fsutil.hardlink_count(target) > 1

    def _link_kind(
        self,
        item: PlannedItem,
        target: Path,
        row: LinkRecord | None,
        known_inodes: Mapping[tuple[int, int], MediaFile],
    ) -> tuple[str | None, str | None]:
        if not fsutil.exists(target):
            return "LINK_CREATE", None
        if self._links_source(target, item.source.path):
            # Already a link this tool can vouch for. An untracked one is adopted
            # (a rebuilt database must not orphan a healthy tree); a tracked one
            # needs no operation at all.
            return ("LINK_ADOPT" if row is None else None), None
        same_row = (
            row is not None and fsutil.normalise(row.target_path) == fsutil.normalise(target)
        )
        if fsutil.is_symlink(target) and not same_row:
            return (
                None,
                f"{target} is a symbolic link this tool did not create; "
                "refusing to replace it",
            )
        if same_row:
            return "LINK_REPOINT", None
        try:
            dev, ino = fsutil.inode_of(target)
        except OSError as exc:
            return None, f"cannot stat {target}: {exc}"
        if (dev, ino) in known_inodes:
            return "LINK_ADOPT", None
        return (
            None,
            f"{target} exists and is not a link of any current source; "
            "refusing to replace a file this tool did not create",
        )

    def _link_detail(self, item: PlannedItem, root_id: str) -> tuple[tuple[str, str], ...]:
        source = item.source
        return (
            ("source_dev", str(source.dev)),
            ("source_ino", str(source.ino)),
            ("source_size", str(source.size)),
            ("source_mtime_ns", str(source.mtime_ns)),
            ("library", item.target.library),
            ("plugin_id", item.plugin_id),
            ("root_id", root_id),
            ("sort_title", item.sort_title or ""),
            ("franchise", item.franchise or ""),
            ("order", ".".join(str(part) for part in item.order) if item.order else ""),
            ("target_relative", str(item.target.relative)),
            (
                "nfo_path",
                fsutil.normalise(self.nfo_path(item, item.nfos[0][0]))
                if item.nfos
                else "",
            ),
        )

    def _nfo_current(self, path: Path, expected: str) -> bool:
        """Hash the file, never the row.

        The row records what this tool last wrote, which is exactly the wrong
        question: a human (or another tool) can edit ``movie.nfo`` in place, and
        then the row still matches while the file no longer does.
        """
        if not fsutil.exists(path):
            return False
        return fsutil.sha256_file(path) == expected

    def _prune_ops(
        self,
        planned_targets: set[str],
        planned_dirs: set[str],
        rows: Mapping[str, LinkRecord],
        moved_from: set[str],
        report: PlanReport,
    ) -> list[Op]:
        ops: list[Op] = []
        pruned_dirs: set[Path] = set()
        planned_nfos: set[str] = set()

        def prune_file(path: Path, reason: str, library: str) -> None:
            key = fsutil.normalise(path)
            if key in planned_targets or key in planned_nfos:
                return
            if not fsutil.exists(path):
                return
            planned_nfos.add(key)
            ops.append(
                Op(
                    "FILE_PRUNE",
                    key,
                    None,
                    (("reason", reason), ("expected_ino", ""), ("library", library)),
                )
            )

        for row in rows.values():
            target_key = fsutil.normalise(row.target_path)
            if target_key in planned_targets:
                continue
            # A row whose target moved was already pruned (and counted) where the
            # move was planned. Emitting it again here unlinks the same file twice
            # and reports two prunes for one episode.
            if target_key not in moved_from:
                reason = (
                    "source gone" if not fsutil.exists(row.source_path) else "not in plan"
                )
                ops.append(self._prune_op(Path(target_key), reason, row, report))
            prune_file(
                Path(target_key).with_suffix(".nfo"), "episode pruned", row.library
            )
            parent = Path(target_key).parent
            pruned_dirs.add(parent)
            self._folder_nfo_prunes(parent, planned_dirs, planned_nfos, row.library, ops)
        ops.extend(self._dir_prune_ops(pruned_dirs))
        return ops

    def _folder_nfo_prunes(
        self,
        start: Path,
        planned_dirs: set[str],
        planned_nfos: set[str],
        library: str,
        ops: list[Op],
    ) -> None:
        """``tvshow.nfo``/``movie.nfo`` sit on the *title* folder, not the season.

        A pruned season must therefore look at every ancestor up to the library
        root, and stop as soon as an ancestor still holds a planned target (a
        season that keeps episodes keeps its show NFO).
        """
        library_root = fsutil.normalise(self.library_root(library))
        clone_root = fsutil.normalise(self.settings.clone_root)
        current = start
        while True:
            key = fsutil.normalise(current)
            if key in (library_root, clone_root):
                return
            if not fsutil.is_within(current, self.settings.clone_root):
                return
            if key in planned_dirs:
                return  # this folder still receives links
            for name in FOLDER_NFOS:
                sibling = current / name
                sibling_key = fsutil.normalise(sibling)
                if sibling_key in planned_nfos or not fsutil.exists(sibling):
                    continue
                planned_nfos.add(sibling_key)
                ops.append(
                    Op(
                        "FILE_PRUNE",
                        sibling_key,
                        None,
                        (
                            ("reason", "folder pruned"),
                            ("expected_ino", ""),
                            ("library", library),
                        ),
                    )
                )
            current = current.parent
            if current == current.parent:
                return

    def _prune_op(
        self, path: Path, reason: str, row: LinkRecord, report: PlanReport
    ) -> Op:
        target_key = fsutil.normalise(path)
        if not self.settings.prune_enabled:
            return Op(
                "SKIP",
                target_key,
                row.source_path,
                (("reason", "prune disabled"), ("library", row.library)),
            )
        if path.suffix.lower() not in self.settings.managed_extensions:
            return Op(
                "SKIP",
                target_key,
                row.source_path,
                (("reason", "extension not managed"), ("library", row.library)),
            )
        if not fsutil.exists(path):
            return Op(
                "SKIP",
                target_key,
                row.source_path,
                (("reason", "already absent"), ("library", row.library)),
            )
        if self.settings.prune_require_known_inode:
            if self.settings.link_mode == "symlink":
                if not fsutil.entry_owned_by(path, row.source_path, row.target_ino):
                    reason = (
                        "link target changed"
                        if fsutil.is_symlink(path)
                        else "not a link this tool created"
                    )
                    return Op(
                        "SKIP",
                        target_key,
                        row.source_path,
                        (("reason", reason), ("library", row.library)),
                    )
            else:
                try:
                    _, ino = fsutil.inode_of(path)
                except OSError as exc:
                    return Op(
                        "SKIP",
                        target_key,
                        row.source_path,
                        (("reason", str(exc)), ("library", row.library)),
                    )
                if ino != row.target_ino:
                    report.errors.append(
                        f"{path} inode changed since it was recorded (expected {row.target_ino}); "
                        "not pruning a file whose provenance no longer matches"
                    )
                    return Op(
                        "SKIP",
                        target_key,
                        row.source_path,
                        (("reason", "inode changed"), ("library", row.library)),
                    )
        report.counts["pruned"] = report.counts.get("pruned", 0) + 1
        return Op(
            "FILE_PRUNE",
            target_key,
            row.source_path,
            (
                ("reason", reason),
                ("expected_ino", str(row.target_ino)),
                ("library", row.library),
            ),
        )

    def _dir_prune_ops(self, dirs: set[Path]) -> list[Op]:
        candidates: set[Path] = set()
        roots = {fsutil.normalise(self.library_root(lib)) for lib in self.settings.library_dirs}
        for directory in dirs:
            current = directory
            while True:
                key = fsutil.normalise(current)
                if key in roots or key == fsutil.normalise(self.settings.clone_root):
                    break
                if not fsutil.is_within(current, self.settings.clone_root):
                    break
                candidates.add(current)
                parent = current.parent
                if parent == current:
                    break
                current = parent
        return [
            Op("DIR_PRUNE", fsutil.normalise(path), None, ())
            for path in sorted(candidates, key=lambda p: (-len(p.parts), str(p).lower()))
        ]


def _sort_key(op: Op) -> tuple[int, int, str]:
    depth = len(Path(op.target_path).parts)
    if op.kind == "DIR_PRUNE":
        return (KIND_ORDER[op.kind], -depth, op.target_path.lower())
    return (KIND_ORDER[op.kind], depth, op.target_path.lower())
