"""When to reconcile: settle windows, minimum gaps, startup grace.

This module decides *when*, never *what*. The truth is the stat signature that
:mod:`mediaforge.scan` computes; a root becomes eligible from either input:

* a filesystem event (:mod:`mediaforge.events`) - a hint that something moved,
  worth acting on immediately but never a statement about what is now true;
* a changed signature, which is the only signal a root on an SMB/NFS share can
  give.

A root that keeps changing keeps re-arming, so a download still being written is
not reconciled mid-file. ``startup_grace_s`` suppresses the burst that exists
only because the process just started; the daemon's first pass already covers it.

The periodic full pass deliberately lives in :mod:`mediaforge.daemon`, not here:
the watcher answers "did something change", and the safety net exists precisely
for the case where the answer is a wrong "no".
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(slots=True)
class RootWatch:
    signature: str | None = None
    armed_at: float | None = None
    last_run: float = 0.0


class PollingWatcher:
    def __init__(
        self,
        root_ids: tuple[str, ...],
        *,
        settle_s: float = 20.0,
        min_gap_s: float = 30.0,
        startup_grace_s: float = 60.0,
        monotonic=time.monotonic,
    ) -> None:
        self.root_ids = root_ids
        self.settle_s = settle_s
        self.min_gap_s = min_gap_s
        self.startup_grace_s = startup_grace_s
        self._monotonic = monotonic
        self._states: dict[str, RootWatch] = {rid: RootWatch() for rid in root_ids}
        self._started_at: float | None = None

    def start(self) -> None:
        self._started_at = self._monotonic()

    def arm(self, root_id: str, now: float | None = None) -> None:
        """Make a root eligible: an event, or a signature that changed.

        Called for every event, so a burst keeps pushing the settle window out -
        that is the point, not a leak.
        """
        if self._started_at is None:
            self.start()
        state = self._states.get(root_id)
        if state is None:
            return
        now = self._monotonic() if now is None else now
        if now - self._started_at < self.startup_grace_s:
            return
        state.armed_at = now

    def poll(self, signatures: Mapping[str, str]) -> list[str]:
        """Feed signatures for the roots that must be polled, return the ready ones.

        A first signature only establishes a baseline: it says where the root
        started, not that anything changed. Passing an empty mapping is the way
        to ask "what is ready?" when events are the trigger.
        """
        if self._started_at is None:
            self.start()
        for root_id, signature in signatures.items():
            state = self._states.get(root_id)
            if state is None:
                continue
            previous = state.signature
            state.signature = signature
            if previous is not None and signature != previous:
                self.arm(root_id)
        return self.ready()

    def ready(self, now: float | None = None) -> list[str]:
        """Roots whose change has settled and which may be reconciled now."""
        now = self._monotonic() if now is None else now
        ready: list[str] = []
        for root_id in self.root_ids:
            state = self._states[root_id]
            if state.armed_at is None:
                continue
            if now - state.armed_at < self.settle_s:
                continue
            if now - state.last_run < self.min_gap_s:
                continue
            ready.append(root_id)
            state.armed_at = None
            state.last_run = now
        return ready

    def next_due(self, now: float | None = None) -> float | None:
        """Earliest monotonic time at which :meth:`ready` could return a root.

        The daemon uses it as its wait timeout, so a settle window that expires
        before the next poll tick wakes the loop instead of being rounded up to
        the tick. ``None`` means nothing is armed.
        """
        now = self._monotonic() if now is None else now
        candidates = [
            max(state.armed_at + self.settle_s, state.last_run + self.min_gap_s)
            for state in self._states.values()
            if state.armed_at is not None
        ]
        if not candidates:
            return None
        return min(candidates)

    def mark_ran(self, root_id: str, now: float | None = None) -> None:
        """Record a run the watcher did not start (a full pass covers every root)."""
        state = self._states.get(root_id)
        if state is None:
            return
        state.last_run = self._monotonic() if now is None else now
