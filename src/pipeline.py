"""One reconcile pass across every configured source root.

This is the only place where the layers meet. It is deliberately linear:

    scan -> detect -> select plugin -> plan -> reconcile

and it is *idempotent by construction*: a run where nothing changed produces an
empty operation list, which means no journal row, no Jellyfin refresh and no
filesystem write. That is what makes a 15-second poll cheap and safe.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil, registry
from .config import Settings
from .linker import Linker
from .models import PlannedItem, ScanResult
from .naming import parse
from .planner import Planner, PlanReport
from .plugin_api import DetectContext, PlanContext
from .plugin_loader import LoadedPlugin, load_plugins, safe_plan
from .reconciler import Reconciler, RunResult
from .scan import ScanError, Scanner
from .state import SqliteStateStore

logger = logging.getLogger("mediaforge.pipeline")


@dataclass(slots=True)
class RootOutcome:
    """What happened to one source root during one pass."""

    root_id: str
    plugin_id: str | None = None
    confidence: float = 0.0
    candidates: tuple[registry.Candidate, ...] = ()
    plan: PlanReport | None = None
    result: RunResult | None = None
    unchanged: bool = False
    error: str | None = None

    @property
    def scanned(self) -> int:
        return len(self.plan.items) if self.plan else 0

    @property
    def op_count(self) -> int:
        return len(self.plan.ops) if self.plan else 0

    @property
    def libraries(self) -> set[str]:
        return set(self.result.libraries) if self.result else set()


@dataclass(slots=True)
class SyncReport:
    roots: list[RootOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def libraries(self) -> set[str]:
        found: set[str] = set()
        for outcome in self.roots:
            found |= outcome.libraries
        return found

    @property
    def ok(self) -> bool:
        return not self.errors and all(not outcome.error for outcome in self.roots)


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        store: SqliteStateStore,
        linker: Linker,
        *,
        dry_run: bool | None = None,
        interrupt=None,
        clock=time.time,
        plugins: tuple[LoadedPlugin, ...] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.linker = linker
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.interrupt = interrupt
        self.clock = clock
        self.scanner = Scanner(settings.exclude_globs)
        self.planner = Planner(settings)
        self._plugin_dir = _resolve_plugin_dir(settings)
        self._plugins = plugins if plugins is not None else load_plugins(self._plugin_dir)

    def _remembered_selection(self, root_id: str) -> registry.Selection | None:
        """Fall back to the plugin recorded for this root, if it is still installed.

        Detection is evidence-based, and an *empty* root is ambiguous: a finished
        cleanup and an unmounted drive look identical. The remembered choice
        resolves that ambiguity in favour of the plugin the operator already ran
        here (so removing every episode still prunes the clone), never in favour
        of a plugin that merely scored highest once.
        """
        remembered = self.store.get_plugin_choice(root_id)
        if remembered is None:
            return None
        plugin_id, _confidence = remembered
        for loaded in self._plugins:
            if loaded.ok and loaded.plugin_id == plugin_id:
                return registry.Selection(
                    plugin_id=plugin_id,
                    plugin=loaded.module,
                    confidence=0.0,
                    reason=f"remembered from a previous run ({plugin_id})",
                    candidates=(),
                    overridden=False,
                )
        return None

    @property
    def plugins(self) -> tuple[LoadedPlugin, ...]:
        return self._plugins

    # ── detection only ──────────────────────────────────────────────────────

    def inspect(self) -> tuple[RootOutcome, ...]:
        """Score every plugin against every root without touching the clone tree."""
        return inspect_sources(self.settings, self._plugins)

    # ── one full pass ───────────────────────────────────────────────────────

    def sync(
        self,
        *,
        root_ids: tuple[str, ...] | None = None,
        trigger: str = "manual",
        scans: Mapping[str, ScanResult] | None = None,
    ) -> SyncReport:
        report = SyncReport()
        wanted = set(root_ids) if root_ids else None

        for source in self.settings.sources:
            if wanted is not None and source.root_id not in wanted:
                continue
            precomputed = scans.get(source.root_id) if scans else None
            outcome = self._run_root(
                source.root_id, source.path, trigger, scan=precomputed
            )
            report.roots.append(outcome)
            if outcome.error:
                report.errors.append(f"{outcome.root_id}: {outcome.error}")

        return report

    def _run_root(
        self,
        root_id: str,
        root_path: Path,
        trigger: str,
        *,
        scan: ScanResult | None = None,
    ) -> RootOutcome:
        outcome = RootOutcome(root_id)
        if scan is None:
            try:
                scan = self.scanner.enumerate_root(
                    next(s for s in self.settings.sources if s.root_id == root_id)
                )
            except ScanError as exc:
                outcome.error = str(exc)
                return outcome

        self.store.upsert_root(root_id, fsutil.normalise(root_path), enabled=True)
        detect_ctx: DetectContext = self.scanner.detect_context(scan)

        try:
            selection = registry.select(
                root_id,
                self._plugins,
                detect_ctx,
                override=self.settings.plugin_overrides.get(root_id),
                min_confidence=self.settings.plugin_min_confidence,
            )
        except registry.NoPluginError as exc:
            remembered = self._remembered_selection(root_id)
            if remembered is None:
                outcome.error = str(exc)
                logger.error("root %s: %s", root_id, exc)
                return outcome
            selection = remembered
            logger.warning(
                "root %s: detection failed (%s); reusing the remembered plugin %s. "
                "This also lets an emptied root prune its old links.",
                root_id,
                str(exc).splitlines()[0],
                selection.plugin_id,
            )

        outcome.plugin_id = selection.plugin_id
        outcome.confidence = selection.confidence
        self.store.set_plugin_choice(
            root_id,
            selection.plugin_id,
            selection.confidence,
            selection.reason,
            int(self.clock()),
        )

        plan_ctx = PlanContext(
            root_id=root_id,
            root_path=root_path,
            files=scan.files,
            dirs=scan.dirs,
            options=self.settings.raw,
        )
        raw_items, plan_error = safe_plan(selection.plugin, plan_ctx)
        if plan_error:
            outcome.error = f"plan() failed: {plan_error}"
            logger.error("root %s: %s", root_id, outcome.error)
            return outcome

        items, type_errors = _validate_items(raw_items)
        previous = {
            row.source_path: row for row in self.store.iter_links(root_id=root_id)
        }
        plan = self.planner.build(
            root_id=root_id, scan=scan, items=items, previous=previous
        )
        plan.errors.extend(type_errors)
        outcome.plan = plan

        if not plan.ops and not plan.errors:
            outcome.unchanged = True
            self.store.record_scan(
                root_id, scan.signature, selection.plugin_id, int(self.clock())
            )
            return outcome

        reconciler = Reconciler(
            self.settings,
            self.store,
            self.linker,
            dry_run=self.dry_run,
            interrupt=self.interrupt,
            clock=self.clock,
        )
        outcome.result = reconciler.execute(root_id=root_id, plan=plan, trigger=trigger)
        self.store.record_scan(root_id, scan.signature, selection.plugin_id, int(self.clock()))
        return outcome


def _resolve_plugin_dir(settings: Settings) -> Path:
    """``MF_PLUGIN_DIR`` is either absolute or relative to the checkout root."""
    configured = settings.plugin_dir
    if configured.is_absolute():
        return configured
    return settings.repo_root / configured


def plugin_dir(settings: Settings) -> Path:
    return _resolve_plugin_dir(settings)


def inspect_sources(
    settings: Settings,
    plugins: tuple[LoadedPlugin, ...],
    *,
    scanner: Scanner | None = None,
) -> tuple[RootOutcome, ...]:
    """Detection scores for every root, with no database and no writes."""
    walker = scanner or Scanner(settings.exclude_globs)
    outcomes: list[RootOutcome] = []
    for source in settings.sources:
        try:
            scan = walker.enumerate_root(source)
        except ScanError as exc:
            outcomes.append(RootOutcome(source.root_id, error=str(exc)))
            continue
        ctx = walker.detect_context(scan)
        try:
            selection = registry.select(
                source.root_id,
                plugins,
                ctx,
                override=settings.plugin_overrides.get(source.root_id),
                min_confidence=settings.plugin_min_confidence,
            )
        except registry.NoPluginError as exc:
            outcomes.append(RootOutcome(source.root_id, error=str(exc)))
            continue
        outcomes.append(
            RootOutcome(
                source.root_id,
                plugin_id=selection.plugin_id,
                confidence=selection.confidence,
                candidates=selection.candidates,
            )
        )
    return tuple(outcomes)


def _validate_items(raw: tuple[object, ...]) -> tuple[tuple[PlannedItem, ...], list[str]]:
    """Reject anything a plugin yields that is not a ``PlannedItem``.

    A plugin is arbitrary code in a directory; a bad yield must become a clear
    error, not an ``AttributeError`` ten frames deep in the planner.
    """
    items: list[PlannedItem] = []
    errors: list[str] = []
    for entry in raw:
        if isinstance(entry, PlannedItem):
            items.append(entry)
        else:
            errors.append(
                f"plan() yielded {type(entry).__name__}, expected PlannedItem"
            )
    return tuple(items), errors


__all__ = ["Pipeline", "RootOutcome", "SyncReport", "inspect_sources", "plugin_dir"]


# ``parse`` is imported for plugins that want the shared helpers; re-exported so a
# plugin can ``from src.pipeline import parse`` without a second import path.
__all__ += ["parse"]
