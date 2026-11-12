"""Signature polling.

A kernel watcher is deliberately not used. ``D:\\Gerbera\\reindex-watch.ps1``
exists because ``FileSystemWatcher`` silently missed a change on this host, and
``ReadDirectoryChangesW`` drops events on burst overflow without an error — a
failure mode that is indistinguishable from "nothing happened". A stat walk over
~1700 entries costs milliseconds and cannot lie.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import MediaFile


@dataclass(slots=True)
class RootWatch:
    signature: str | None = None
    armed_at: float | None = None
    last_run: float = 0.0
    previous_items: dict[str, tuple[int, int]] = field(default_factory=dict)


class PollingWatcher:
    def __init__(
        self,
        root_ids: tuple[str, ...],
        *,
        settle_s: float = 20.0,
        min_gap_s: float = 30.0,
        full_interval_s: float = 21600.0,
        startup_grace_s: float = 60.0,
        monotonic=time.monotonic,
    ) -> None:
        self.root_ids = root_ids
        self.settle_s = settle_s
        self.min_gap_s = min_gap_s
        self.full_interval_s = full_interval_s
        self.startup_grace_s = startup_grace_s
        self._monotonic = monotonic
        self._states: dict[str, RootWatch] = {rid: RootWatch() for rid in root_ids}
        self._started_at: float | None = None
        self._last_full = 0.0

    def start(self) -> None:
        now = self._monotonic()
        self._started_at = now
        self._last_full = now

    def poll(self, signatures: Mapping[str, str]) -> list[str]:
        """Root ids whose change has settled and which may be reconciled now."""
        if self._started_at is None:
            self.start()
        now = self._monotonic()
        in_grace = now - self._started_at < self.startup_grace_s
        full_due = now - self._last_full >= self.full_interval_s
        ready: list[str] = []

        for root_id in self.root_ids:
            state = self._states[root_id]
            signature = signatures.get(root_id)
            if state.signature is None:
                state.signature = signature
                continue
            if signature != state.signature:
                state.signature = signature
                if not in_grace:
                    state.armed_at = now
                continue

            if full_due:
                ready.append(root_id)
                state.armed_at = None
                state.last_run = now
                continue
            if state.armed_at is None:
                continue
            if now - state.armed_at < self.settle_s:
                continue
            if now - state.last_run < self.min_gap_s:
                continue
            ready.append(root_id)
            state.armed_at = None
            state.last_run = now

        if full_due:
            self._last_full = now
        return ready

    def mark_ran(self, root_id: str) -> None:
        self._states[root_id].last_run = self._monotonic()

    def settle(
        self, root_id: str, files: tuple[MediaFile, ...], now_wall: float | None = None
    ) -> tuple[MediaFile, ...]:
        """Files that were unchanged since the previous poll *and* are old enough.

        Two conditions, not one: the age rule alone would accept a file that is
        being appended to slowly, and the stability rule alone would accept a
        file written once, seconds ago, whose writer is about to rename it.
        """
        now = time.time() if now_wall is None else now_wall
        state = self._states[root_id]
        current = {str(media.relpath): (media.size, media.mtime_ns) for media in files}
        settled: list[MediaFile] = []
        for media in files:
            key = str(media.relpath)
            stable = state.previous_items.get(key) == current[key]
            old_enough = (now - media.mtime_ns / 1_000_000_000) >= self.settle_s
            if stable and old_enough:
                settled.append(media)
        state.previous_items = current
        return tuple(settled)
