"""Filesystem events, as *hints* only.

Three layers, and the order matters:

1. events decide **when to look** (this module);
2. the stat signature decides **what is true** (:mod:`mediaforge.scan` together
   with :class:`mediaforge.watcher.PollingWatcher`);
3. anything an event cannot answer - an overflow, a burst too large to keep, a
   root that never emits (SMB/NFS) - collapses into "scan anyway", which is the
   periodic full pass in :mod:`mediaforge.daemon`.

A source never asserts that nothing changed. If it cannot answer, it says so by
emitting :attr:`EventKind.OVERFLOW` or by leaving the root out of ``watched`` so
the daemon polls it instead.

The backend is `watchdog` >= 4, one dependency instead of four ctypes backends:

===========================  ===============================================
Windows ``ReadDirectoryChangesW``  recursive; one buffer; overflow is silent
Linux ``inotify``                  **not** recursive: one watch per directory
macOS ``FSEvents``                 recursive, coalesced
BSD ``kqueue``                     **not** recursive: one fd per vnode
===========================  ===============================================

Every one of those failure modes is the same event here: overflow means "I can
no longer describe what happened", and the answer to it is a scan.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

#: Queue depth that means "we cannot describe what happened any more". The real
#: overflows (:c:`ERROR_NOTIFY_ENUM_DIR`, ``IN_Q_OVERFLOW``) are not surfaced by
#: watchdog, so this is the honest stand-in: a burst this deep is answered by a
#: scan, never by replaying the events.
OVERFLOW_THRESHOLD = 10_000

#: Filesystems whose trees never emit events. A root on one of these is polled.
NETWORK_FS = frozenset(
    {"nfs", "nfs4", "cifs", "smbfs", "smb3", "afpfs", "fuse.sshfs", "davfs", "9p"}
)

#: ``GetDriveTypeW`` answer for a mapped network drive.
DRIVE_REMOTE = 4


class EventKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED = "moved"
    OVERFLOW = "overflow"


@dataclass(frozen=True, slots=True)
class Event:
    """One hint about one root. Never a claim that the tree is unchanged."""

    kind: EventKind
    root_id: str
    path: str = ""


class EventSourceUnavailable(RuntimeError):
    """No event backend can serve these roots; poll instead."""


@runtime_checkable
class EventSource(Protocol):
    #: Root ids events are delivered for.
    watched: frozenset[str]
    #: Root ids that cannot be watched and must be polled.
    polled: frozenset[str]
    #: Short name for the log line ("watchdog", "poll").
    name: str

    def start(self) -> None: ...

    def wait(self, timeout_s: float) -> tuple[Event, ...]: ...

    def stop(self) -> None: ...


def _windows_drive_type(path: str) -> int | None:
    drive = os.path.splitdrive(path)[0]
    if not drive or not drive.endswith(":"):
        return None
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\"))  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return None


def _posix_mount_fstype(path: str) -> str | None:
    """Best effort: the fstype of the longest mount point containing ``path``."""
    target = os.path.abspath(path)
    best: tuple[int, str] | None = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                left, _, right = line.partition(" - ")
                fields = left.split()
                tail = right.split()
                if len(fields) < 5 or not tail:
                    continue
                mount = fields[4]
                if target == mount or target.startswith(mount.rstrip("/") + "/"):
                    if best is None or len(mount) > best[0]:
                        best = (len(mount), tail[0])
    except OSError:
        return None
    return None if best is None else best[1]


def is_network_path(path: Path | str, *, drive_type=_windows_drive_type) -> bool:
    """True when ``path`` lives on a share whose tree emits no events.

    Mapped Windows drives are reported by ``GetDriveTypeW``; UNC paths are
    recognised without asking. On POSIX the mount table answers, and if it cannot
    be read the answer is "local" - a wrong "local" only costs a useless watch
    registration, while a wrong "remote" would disable events everywhere.
    """
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True
    if os.name == "nt":
        return drive_type(text) == DRIVE_REMOTE
    fstype = _posix_mount_fstype(text)
    return fstype is not None and fstype in NETWORK_FS


class PollingEventSource:
    """The fallback: no events at all, just a wait that honours the timeout."""

    name = "poll"

    def __init__(self, roots: tuple[str, ...], *, sleeper: threading.Event | None = None) -> None:
        self.watched: frozenset[str] = frozenset()
        self.polled = frozenset(roots)
        self._idle = sleeper

    def start(self) -> None:
        return None

    def wait(self, timeout_s: float) -> tuple[Event, ...]:
        if self._idle is not None:
            self._idle.wait(max(0.0, timeout_s))
        else:
            threading.Event().wait(max(0.0, timeout_s))
        return ()

    def stop(self) -> None:
        return None


def _drain(sink: queue.Queue[Event], keep: Event | None = None) -> None:
    sink.queue.clear()
    if keep is not None:
        sink.put(keep)


def _dispatch(raw: object, root_id: str, sink: queue.Queue[Event], threshold: int) -> None:
    """Path -> root id, then one :class:`Event` into the queue.

    Overflow drops the backlog *instead of* the marker: replaying a stale burst
    is worse than rescanning, and a marker that removes itself is an overflow
    nobody hears about.
    """
    kind = _KINDS.get(getattr(raw, "event_type", ""))
    if kind is None:  # "opened"/"closed" exist on Linux and carry no change
        return
    if sink.qsize() >= threshold:
        _drain(sink, Event(EventKind.OVERFLOW, root_id))
        return
    sink.put(Event(kind, root_id, str(getattr(raw, "src_path", ""))))


_KINDS = {
    "created": EventKind.CREATED,
    "modified": EventKind.MODIFIED,
    "deleted": EventKind.DELETED,
    "moved": EventKind.MOVED,
}


def handler_for(root_id: str, sink: queue.Queue[Event], threshold: int):
    """One watchdog handler for one root.

    A factory rather than a class body inside the loop: watchdog hands the
    callback the raw event only, so the binding has to come from the closure -
    and closing over a loop variable would make both handlers report the last
    root (``ruff`` B023).
    """
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            _dispatch(event, root_id, sink, threshold)

    return _Handler()


class WatchdogEventSource:
    """Real OS events via `watchdog` >= 4 (inotify, FSEvents, kqueue, RDCW)."""

    name = "watchdog"

    def __init__(
        self,
        roots: dict[str, Path],
        *,
        observer_factory=None,
        network_check=is_network_path,
        overflow_threshold: int = OVERFLOW_THRESHOLD,
    ) -> None:
        self._roots = dict(roots)
        self._factory = observer_factory
        self._queue: queue.Queue[Event] = queue.Queue()
        self._observer = None
        self._threshold = overflow_threshold
        remote = {rid for rid, path in self._roots.items() if network_check(path)}
        self.polled = frozenset(remote)
        self.watched = frozenset(set(self._roots) - remote)

    def _observer_or_raise(self):
        if self._factory is not None:
            return self._factory()
        try:
            from watchdog.observers import Observer
        except ImportError as exc:  # pragma: no cover - guarded by watchdog_available()
            raise EventSourceUnavailable(
                "watchdog is not installed (pip install 'watchdog>=4'); polling instead"
            ) from exc
        return Observer()

    def start(self) -> None:
        if not self.watched:
            return
        observer = self._observer_or_raise()
        for root_id in sorted(self.watched):
            observer.schedule(
                handler_for(root_id, self._queue, self._threshold),
                str(self._roots[root_id]),
                recursive=True,
            )
        observer.start()
        self._observer = observer

    def wait(self, timeout_s: float) -> tuple[Event, ...]:
        try:
            first = self._queue.get(timeout=max(0.0, timeout_s))
        except queue.Empty:
            return ()
        events = [first]
        while len(events) < 512:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return tuple(events)

    def stop(self) -> None:
        observer, self._observer = self._observer, None
        if observer is None:
            return
        observer.stop()
        observer.join(timeout=10)


def watchdog_available() -> bool:
    try:
        import watchdog  # noqa: F401
    except ImportError:
        return False
    return True


def make_event_source(
    roots: dict[str, Path],
    *,
    mode: str = "events",
    logger=None,
    sleeper: threading.Event | None = None,
) -> EventSource:
    """Pick a source; never raise for an environment that cannot serve events."""
    names = tuple(sorted(roots))
    if mode != "events":
        return PollingEventSource(names, sleeper=sleeper)
    if not watchdog_available():
        if logger is not None:
            logger.warning("watchdog is not installed; falling back to polling")
        return PollingEventSource(names, sleeper=sleeper)
    source = WatchdogEventSource(roots)
    if not source.watched:
        if logger is not None:
            logger.info("every source root is on a network share: polling them")
        return PollingEventSource(names, sleeper=sleeper)
    for root_id in sorted(source.polled):
        if logger is not None:
            logger.info("%s is on a network share: polling it", root_id)
    return source


__all__ = [
    "Event",
    "EventKind",
    "EventSource",
    "EventSourceUnavailable",
    "PollingEventSource",
    "WatchdogEventSource",
    "is_network_path",
    "make_event_source",
    "watchdog_available",
]
