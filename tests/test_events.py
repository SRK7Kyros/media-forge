"""Events are hints: the layer that may say "look now", never "nothing changed".

The tests that matter here are the failure shapes, because they are the ones a
real filesystem produces: a burst too large to keep (overflow), a root on a
share that never emits anything (polled instead of watched), and an event kind
that carries no change at all.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediaforge.events import (
    OVERFLOW_THRESHOLD,
    Event,
    EventKind,
    PollingEventSource,
    WatchdogEventSource,
    is_network_path,
    make_event_source,
    watchdog_available,
)


class FakeObserver:
    """The minimum of ``Observer`` that :class:`WatchdogEventSource` uses."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[object, str, bool]] = []
        self.started = False
        self.stopped = False

    def schedule(self, handler, path, recursive=False):  # noqa: ANN001 - watchdog's shape
        self.scheduled.append((handler, path, recursive))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout=None) -> None:  # noqa: ANN001
        return None


def _event(event_type: str, path: str = "") -> SimpleNamespace:
    return SimpleNamespace(event_type=event_type, src_path=path)


def _source(tmp_path: Path, **overrides) -> tuple[WatchdogEventSource, FakeObserver]:
    observer = FakeObserver()
    options = {"observer_factory": lambda: observer, "network_check": lambda _p: False}
    options.update(overrides)
    return WatchdogEventSource({"shows": tmp_path}, **options), observer


# ── the source selection ─────────────────────────────────────────────────────


def test_poll_mode_watches_nothing_and_polls_everything() -> None:
    source = make_event_source({"shows": Path("x")}, mode="poll")
    assert isinstance(source, PollingEventSource)
    assert source.watched == frozenset()
    assert source.polled == frozenset({"shows"})


def test_a_missing_watchdog_falls_back_to_polling(monkeypatch) -> None:
    monkeypatch.setattr("mediaforge.events.watchdog_available", lambda: False)
    source = make_event_source({"shows": Path("x")}, mode="events")
    assert isinstance(source, PollingEventSource)


def test_a_root_on_a_share_is_polled_not_watched(tmp_path: Path) -> None:
    observer = FakeObserver()
    source = WatchdogEventSource(
        {"shows": tmp_path / "local", "anime": tmp_path / "share"},
        observer_factory=lambda: observer,
        network_check=lambda path: Path(path).name == "share",
    )
    assert source.watched == frozenset({"shows"})
    assert source.polled == frozenset({"anime"})

    source.start()
    assert [entry[1] for entry in observer.scheduled] == [str(tmp_path / "local")]
    assert observer.scheduled[0][2] is True  # recursive: a tree, not a directory
    source.stop()
    assert observer.stopped is True


def test_every_root_on_a_share_means_polling(monkeypatch, tmp_path: Path) -> None:
    """Nothing to watch is the same decision as an unusable backend: poll."""

    class AllRemote:
        name = "watchdog"
        watched = frozenset()
        polled = frozenset({"shows"})

        def start(self) -> None:
            return None

        def wait(self, timeout_s: float):  # noqa: ANN201
            return ()

        def stop(self) -> None:
            return None

    monkeypatch.setattr(
        "mediaforge.events.WatchdogEventSource", lambda _roots: AllRemote()
    )
    source = make_event_source({"shows": tmp_path}, mode="events")
    assert isinstance(source, PollingEventSource)


def test_unc_paths_are_network_paths() -> None:
    assert is_network_path(r"\\nas\media\shows") is True
    assert is_network_path("//nas/media/shows") is True


def test_a_mapped_remote_drive_is_a_network_path(tmp_path: Path) -> None:
    def drive_type(_path: str) -> int:
        return 4  # DRIVE_REMOTE

    def local(_path: str) -> int:
        return 3  # DRIVE_FIXED

    assert is_network_path(tmp_path, drive_type=drive_type) is True
    assert is_network_path(tmp_path, drive_type=local) is False


# ── the handler chain ────────────────────────────────────────────────────────


def test_an_event_reaches_the_queue_with_its_root(tmp_path: Path) -> None:
    source, observer = _source(tmp_path)
    source.start()
    handler = observer.scheduled[0][0]
    handler.on_any_event(_event("created", str(tmp_path / "a.mkv")))
    events = source.wait(0.1)
    assert events == (Event(EventKind.CREATED, "shows", str(tmp_path / "a.mkv")),)


@pytest.mark.parametrize("event_type", ["opened", "closed"])
def test_event_kinds_that_carry_no_change_are_dropped(tmp_path: Path, event_type: str) -> None:
    source, observer = _source(tmp_path)
    source.start()
    observer.scheduled[0][0].on_any_event(_event(event_type, str(tmp_path / "a.mkv")))
    assert source.wait(0.01) == ()


def test_each_root_gets_its_own_handler(tmp_path: Path) -> None:
    """A closure over the loop variable would make both handlers report the last root."""
    observer = FakeObserver()
    source = WatchdogEventSource(
        {"shows": tmp_path / "s", "anime": tmp_path / "a"},
        observer_factory=lambda: observer,
        network_check=lambda _path: False,
    )
    source.start()
    by_path = {entry[1]: entry[0] for entry in observer.scheduled}
    by_path[str(tmp_path / "s")].on_any_event(_event("created", "x"))
    by_path[str(tmp_path / "a")].on_any_event(_event("created", "y"))
    assert {event.root_id for event in source.wait(0.1)} == {"shows", "anime"}


def test_a_burst_beyond_the_threshold_becomes_one_overflow(tmp_path: Path) -> None:
    """Replaying a stale burst is worse than rescanning, so the backlog is dropped."""
    source, observer = _source(tmp_path, overflow_threshold=2)
    source.start()
    handler = observer.scheduled[0][0]
    for index in range(5):
        handler.on_any_event(_event("created", f"f{index}"))
    events = source.wait(0.1)
    assert events == (Event(EventKind.OVERFLOW, "shows"),)
    assert len(events) < 5
    assert OVERFLOW_THRESHOLD > 2  # the production default is deliberately large


def test_wait_honours_its_timeout(tmp_path: Path) -> None:
    source, _observer = _source(tmp_path)
    source.start()
    started = time.monotonic()
    assert source.wait(0.05) == ()
    assert time.monotonic() - started >= 0.04


# ── the real backend, end to end ─────────────────────────────────────────────


@pytest.mark.skipif(not watchdog_available(), reason="watchdog is not installed")
def test_a_real_observer_reports_a_new_file(tmp_path: Path) -> None:
    """No fake anywhere: the OS backend, the handler, the queue."""
    root = tmp_path / "root"
    root.mkdir()
    source = WatchdogEventSource({"shows": root})
    assert source.watched == frozenset({"shows"})
    source.start()
    try:
        (root / "a.mkv").write_bytes(b"x" * 16)
        deadline = time.monotonic() + 20
        seen: list[Event] = []
        while time.monotonic() < deadline and not any(e.path.endswith("a.mkv") for e in seen):
            seen.extend(source.wait(0.5))
    finally:
        source.stop()
    assert any(e.root_id == "shows" and e.path.endswith("a.mkv") for e in seen)
