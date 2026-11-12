"""Debounced Jellyfin rescans, and a hard rule that they can never break a run.

Three independent safety properties:

* an empty API key disables the whole module (``enabled`` is False);
* a failed refresh leaves the dirty marker in place and backs off;
* one call asks for at most one refresh, so a burst of episodes becomes one scan.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from .jellyfin import HttpJellyfinClient, JellyfinError
from .state import SqliteStateStore

logger = logging.getLogger("mediaforge.jellyfin")


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    libraries: tuple[str, ...]
    full: bool
    accepted: bool
    error: str | None = None


class Refresher:
    def __init__(
        self,
        store: SqliteStateStore,
        client: HttpJellyfinClient,
        *,
        library_ids: Mapping[str, str],
        debounce_s: float = 60.0,
        timeout_s: float = 120.0,
        clock=time.time,
    ) -> None:
        self.store = store
        self.client = client
        self.library_ids = dict(library_ids)
        self.debounce_s = debounce_s
        self.timeout_s = timeout_s
        self.clock = clock
        self._retry_after = 0.0

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    def pending(self) -> tuple[str, ...]:
        return tuple(library for library, _ in self.store.dirty_entries())

    def tick(self) -> RefreshOutcome | None:
        """Refresh when due. ``None`` means "nothing to do", never an error."""
        if not self.enabled:
            return None
        if self.clock() < self._retry_after:
            return None
        entries = self.store.dirty_entries()
        if not entries:
            return None
        oldest = entries[0][1]
        if self.clock() - oldest < self.debounce_s:
            return None

        libraries = tuple(library for library, _ in entries)
        missing = [lib for lib in libraries if not self.library_ids.get(lib)]
        full = bool(missing)
        try:
            if full:
                logger.info(
                    "refreshing all Jellyfin libraries: no item id for %s "
                    "(run 'mediaforge jellyfin-check' to fill them in)",
                    ", ".join(missing),
                )
                self.client.refresh(None)
            else:
                for library in libraries:
                    self.client.refresh(self.library_ids[library])
                logger.info("refresh requested for %s", ", ".join(libraries))
            idle = self.client.wait_idle(self.timeout_s)
            if not idle:
                logger.warning(
                    "Jellyfin did not report idle within %.0fs; the scan continues server-side",
                    self.timeout_s,
                )
        except JellyfinError as exc:
            self._retry_after = self.clock() + self.debounce_s
            logger.warning("Jellyfin refresh failed, markers kept: %s", exc)
            return RefreshOutcome(libraries, full, False, str(exc))

        self.store.clear_dirty(libraries)
        self._retry_after = 0.0
        return RefreshOutcome(libraries, full, True)
