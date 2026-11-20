"""The daemon: decides *when* to run the CLI verbs, and nothing else.

The rule this file exists to enforce:

    the core never runs as a daemon. The daemon owns no state and writes
    nothing - it only decides *when* to run commands you could type yourself.

So it opens no database, takes no lock and imports no reconciler. Every action
is a subprocess (``mediaforge sync``, ``mediaforge sync --full``,
``mediaforge sweep``): a plugin that dies kills one child instead of the
supervisor, and every line in the log names a command you can run by hand.

Three layers decide *when*, and the order matters:

1. events arm a root (:mod:`mediaforge.events`) - a hint, never a truth;
2. the stat signature of a **polled** root arms it
   (:class:`mediaforge.watcher.PollingWatcher`), which is the only signal an
   SMB/NFS share can give;
3. a periodic full pass and an idle sweep run anyway, because both inputs can be
   silent: an overflow, an event the OS never delivered, a share that emits
   nothing at all.

A delete event schedules the sweep one grace window later instead of reacting to
it, which is what keeps a downloader's delete-and-recreate from flapping the
mirror - the decision the old one-second liveness loop made ten times a minute.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from .config import Settings
from .events import Event, EventKind, EventSource, make_event_source
from .exitcodes import EXIT_CONFIG, EXIT_LOCKED, EXIT_OK
from .scan import ScanError, Scanner
from .watcher import PollingWatcher

#: Longest the loop sleeps with nothing to do, so a clock jump or a missed wake-up
#: still gets a tick.
IDLE_WAIT_S = 60.0

#: First retry after the full pass failed; doubles to the maximum while it keeps
#: failing. A per-root failure needs none: the watcher re-arms that root.
RETRY_MIN_S = 30.0
RETRY_MAX_S = 600.0

FOREVER = float("inf")


class Runner(Protocol):
    """Runs one verb and returns its exit code."""

    def __call__(self, argv: tuple[str, ...]) -> int: ...


class SubprocessRunner:
    """Runs the verb against the same checkout and ``.env`` as the daemon."""

    def __init__(
        self,
        settings: Settings,
        *,
        extra_args: tuple[str, ...] = (),
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.extra_args = extra_args
        self.logger = logger

    def __call__(self, argv: tuple[str, ...]) -> int:
        cmd = [
            sys.executable,
            "-m",
            "mediaforge",
            *argv,
            "--root",
            str(self.settings.repo_root),
            *self.extra_args,
        ]
        if argv[0] == "sync":
            # `status` prints the trigger of the last run, so a run started here
            # must not be recorded as "manual" - "who did this" is the first
            # question asked of a journal.
            cmd += ("--trigger", "daemon")
        if self.logger is not None:
            self.logger.info("$ %s", " ".join(cmd[1:]))
        try:
            return subprocess.run(cmd, cwd=str(self.settings.repo_root), check=False).returncode
        except OSError as exc:
            if self.logger is not None:
                self.logger.error("could not start %s: %s", argv[0], exc)
            return 1


@dataclass(slots=True)
class DaemonStats:
    syncs: int = 0
    full_passes: int = 0
    sweeps: int = 0
    failures: int = 0


class Daemon:
    """The loop. It writes nothing; it only decides which verb runs when."""

    def __init__(
        self,
        settings: Settings,
        *,
        runner: Runner,
        logger: logging.Logger,
        stop: threading.Event | None = None,
        event_source: EventSource | None = None,
        watcher: PollingWatcher | None = None,
        clock=time.monotonic,
    ) -> None:
        self.settings = settings
        self.runner = runner
        self.logger = logger
        self.stop = stop or threading.Event()
        self.clock = clock
        self.sources = {source.root_id: source for source in settings.sources}
        self.root_ids = tuple(self.sources)
        self.source = event_source or make_event_source(
            {root_id: source.path for root_id, source in self.sources.items()},
            mode=settings.watch_mode,
            logger=logger,
            sleeper=self.stop,
        )
        self.gate = watcher or PollingWatcher(
            self.root_ids,
            settle_s=settings.settle_s,
            min_gap_s=settings.min_gap_s,
            startup_grace_s=settings.startup_grace_s,
            monotonic=clock,
        )
        self.scanner = Scanner(settings.exclude_globs)
        self.stats = DaemonStats()
        self.next_poll = 0.0
        self.next_sweep = 0.0
        self.sweep_at: float | None = None
        self.next_full = 0.0
        self.full_wanted = True
        self.full_not_before = 0.0
        self.full_backoff = 0.0
        self._failed_scans: set[str] = set()
        #: root id -> (consecutive failures, when the next attempt is allowed).
        #: A root that cannot be planned (a plugin that refuses the shape) would
        #: otherwise be retried on the settle clock forever, spawning a child and
        #: walking the whole tree each time.
        self._retry: dict[str, tuple[int, float]] = {}

    # ── loop ────────────────────────────────────────────────────────────────

    def run(self) -> int:
        if self.settings.watch_mode == "off":
            self.logger.error("MF_WATCH_MODE=off: no daemon is started")
            return EXIT_CONFIG
        self.gate.start()
        self.source.start()
        self._announce()
        try:
            while not self.stop.is_set():
                self.tick()
                if self.stop.is_set():
                    break
                self.absorb(self.source.wait(self.wait_timeout()))
        except KeyboardInterrupt:
            self.logger.info("interrupted")
        finally:
            self.source.stop()
            self.logger.info(
                "daemon stopped (%d sync, %d full, %d sweep, %d failed)",
                self.stats.syncs,
                self.stats.full_passes,
                self.stats.sweeps,
                self.stats.failures,
            )
        return EXIT_OK

    def tick(self) -> None:
        """Do whatever is due, in priority order: full pass, sync, sweep."""
        now = self._now()
        if (self.full_wanted or now >= self.next_full) and now >= self.full_not_before:
            self.full_wanted = False
            self.next_full = now + self._full_interval()
            self._run_full()
            return
        self._rearm_due(now)
        ready = self.gate.poll(self._signatures(now))
        if ready:
            self._run_sync(ready)
        if self._sweep_due(now):
            # An event-scheduled sweep fires *after* the window, so the child
            # must not wait it again. The idle sweep has no earlier observation
            # to compare against, so it applies the window itself - observing
            # twice, one grace apart, is the whole point of the window.
            scheduled = self.sweep_at is not None and now >= self.sweep_at
            self._run_sweep(windowed=not scheduled)

    def absorb(self, events: tuple[Event, ...]) -> None:
        for event in events:
            if event.kind is EventKind.OVERFLOW:
                self.logger.warning(
                    "%s: the event queue overflowed, so nothing in it can be trusted; "
                    "scheduling a full pass and a sweep",
                    event.root_id,
                )
                self.full_wanted = True
                self.full_not_before = self._now()
                # One window, like a delete: a burst of them is the worst moment
                # to prune on, because half the files may be mid-replace.
                self._schedule_sweep(self._now() + self.settings.liveness_grace_s)
                return
            # A root that is backing off sits on its own clock: an event storm
            # must not pull a failing run forward every few seconds.
            if event.root_id not in self._retry:
                self.gate.arm(event.root_id)
            if event.kind in (EventKind.DELETED, EventKind.MOVED):
                self._schedule_sweep(self._now() + self.settings.liveness_grace_s)

    def wait_timeout(self) -> float:
        """How long the event source may block before a timer needs the loop."""
        now = self._now()
        candidates = [IDLE_WAIT_S]
        due = self.gate.next_due(now)
        if due is not None:
            candidates.append(due - now)
        if self.source.polled and self.settings.poll_interval_s > 0:
            candidates.append(self.next_poll - now)
        if self.sweep_at is not None:
            candidates.append(self.sweep_at - now)
        if self.settings.liveness_interval_s > 0:
            candidates.append(self.next_sweep - now)
        if self.full_wanted:
            candidates.append(self.full_not_before - now)
        for _failures, not_before in self._retry.values():
            if not_before != FOREVER:
                candidates.append(not_before - now)
        return max(0.0, min(candidates))

    # ── the three verbs ─────────────────────────────────────────────────────

    def _run_sync(self, ready: list[str]) -> None:
        # One child per root: a shared exit code would make one unreconcilable
        # root look like a failure of every root in the batch, and would back off
        # the healthy ones with it.
        for root_id in ready:
            self.stats.syncs += 1
            if self._run(("sync", "--source", root_id)) == EXIT_OK:
                self.gate.mark_ran(root_id)
                self._retry.pop(root_id, None)
                continue
            # Never claim a run that did not happen, and never retry it on the
            # settle clock: a root that keeps failing is a defect to read.
            failures = self._retry.get(root_id, (0, 0.0))[0] + 1
            delay = min(RETRY_MIN_S * (2 ** (failures - 1)), RETRY_MAX_S)
            self._retry[root_id] = (failures, self._now() + delay)
            self.logger.info(
                "%s: next attempt in %.0fs (%d failed run(s))", root_id, delay, failures
            )

    def _rearm_due(self, now: float) -> None:
        """Re-arm a root whose backoff has expired, once."""
        for root_id, (failures, not_before) in list(self._retry.items()):
            if now >= not_before:
                self.gate.arm(root_id)
                self._retry[root_id] = (failures, FOREVER)

    def _run_full(self) -> None:
        self.stats.full_passes += 1
        if self._run(("sync", "--full")) == EXIT_OK:
            self.full_backoff = 0.0
            # The pass covered every root, so no root needs a second sync just
            # because its signature moved while the pass was running.
            now = self._now()
            for root_id in self.root_ids:
                self.gate.mark_ran(root_id, now)
            return
        delay = RETRY_MIN_S if self.full_backoff <= 0 else min(self.full_backoff * 2, RETRY_MAX_S)
        self.full_backoff = delay
        self.full_wanted = True
        self.full_not_before = self._now() + delay

    def _run_sweep(self, *, windowed: bool) -> None:
        self.stats.sweeps += 1
        if not windowed:
            self.sweep_at = None
        # An idle sweep must not swallow a scheduled one: the delete that asked
        # for it is still waiting, and a sweep that fires before its window is
        # exactly what the window exists to prevent.
        self.next_sweep = self._now() + self.settings.liveness_interval_s
        # A scheduled sweep has already waited the window; an idle one asks the
        # child for the configured window, and the child waits it only when there
        # is something to wait for.
        argv = ("sweep",) if windowed else ("sweep", "--grace", "0")
        self._run(argv)

    def _run(self, argv: tuple[str, ...]) -> int:
        code = self.runner(argv)
        verb = " ".join(argv)
        if code == EXIT_OK:
            return code
        if code == EXIT_LOCKED:
            # Another instance is mid-run: normal, not a defect in this one. It is
            # also not a completed run, so the caller still re-arms or backs off.
            self.logger.info("%s: another mediaforge holds the lock; will retry", verb)
            return code
        self.stats.failures += 1
        self.logger.error("%s: exited %d", verb, code)
        return code

    # ── inputs ──────────────────────────────────────────────────────────────

    def _signatures(self, now: float) -> dict[str, str]:
        """One walk per *polled* root on the polling clock; events carry the rest."""
        polled = self.source.polled
        if not polled or self.settings.poll_interval_s <= 0 or now < self.next_poll:
            return {}
        self.next_poll = now + self.settings.poll_interval_s
        signatures: dict[str, str] = {}
        for root_id in sorted(polled):
            source = self.sources.get(root_id)
            if source is None:
                continue
            try:
                signatures[root_id] = self.scanner.enumerate_root(source).signature
                self._failed_scans.discard(root_id)
            except ScanError as exc:
                if root_id not in self._failed_scans:
                    self.logger.error("scan failed for %s: %s", root_id, exc)
                    self._failed_scans.add(root_id)
        return signatures

    def _sweep_due(self, now: float) -> bool:
        if self.sweep_at is not None and now >= self.sweep_at:
            return True
        return self.settings.liveness_interval_s > 0 and now >= self.next_sweep

    def _schedule_sweep(self, at: float) -> None:
        self.sweep_at = at if self.sweep_at is None else min(self.sweep_at, at)

    def _full_interval(self) -> float:
        interval = self.settings.full_scan_interval_s
        return interval if interval > 0 else FOREVER

    # ── logging ─────────────────────────────────────────────────────────────

    def _announce(self) -> None:
        self.logger.info(
            "daemon: %d root(s) via %s -> %s (settle %.0fs, poll %.0fs, full %.0fs, "
            "idle sweep %.0fs, min gap %.0fs)",
            len(self.root_ids),
            self.source.name,
            self.settings.clone_root,
            self.settings.settle_s,
            self.settings.poll_interval_s,
            self.settings.full_scan_interval_s,
            self.settings.liveness_interval_s,
            self.settings.min_gap_s,
        )
        for root_id in sorted(self.source.polled):
            self.logger.info(
                "%s: polling every %.0fs (this root emits no events)",
                root_id,
                self.settings.poll_interval_s,
            )

    def _now(self) -> float:
        return float(self.clock())


__all__ = ["Daemon", "DaemonStats", "Runner", "SubprocessRunner"]
