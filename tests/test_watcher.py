"""Polling semantics: settle, minimum gap, startup grace, periodic full scans."""

from __future__ import annotations

from mediaforge.watcher import PollingWatcher


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _watcher(clock: Clock, **overrides) -> PollingWatcher:
    options = {
        "settle_s": 10.0,
        "min_gap_s": 5.0,
        "full_interval_s": 1000.0,
        "startup_grace_s": 0.0,
        "monotonic": clock,
    }
    options.update(overrides)
    watcher = PollingWatcher(("shows",), **options)
    watcher.start()
    return watcher


def test_the_first_signature_only_establishes_a_baseline() -> None:
    clock = Clock()
    watcher = _watcher(clock)
    assert watcher.poll({"shows": "a"}) == []


def test_a_change_fires_only_after_the_settle_window() -> None:
    clock = Clock()
    watcher = _watcher(clock)
    watcher.poll({"shows": "a"})

    clock.advance(1)
    assert watcher.poll({"shows": "b"}) == []  # armed
    clock.advance(5)
    assert watcher.poll({"shows": "b"}) == []  # 5 < settle
    clock.advance(6)
    assert watcher.poll({"shows": "b"}) == ["shows"]  # 11 >= settle


def test_a_still_changing_signature_keeps_re_arming() -> None:
    clock = Clock()
    watcher = _watcher(clock)
    watcher.poll({"shows": "a"})
    for signature in ("b", "c", "d"):
        clock.advance(5)
        assert watcher.poll({"shows": signature}) == []
    clock.advance(11)
    assert watcher.poll({"shows": "d"}) == ["shows"]


def test_the_minimum_gap_is_respected() -> None:
    clock = Clock()
    watcher = _watcher(clock, settle_s=2.0, min_gap_s=50.0)
    watcher.poll({"shows": "a"})
    clock.advance(1)
    watcher.poll({"shows": "b"})  # armed
    clock.advance(3)
    assert watcher.poll({"shows": "b"}) == ["shows"]  # last run at 1004

    clock.advance(1)
    watcher.poll({"shows": "c"})  # armed at 1005
    clock.advance(3)
    assert watcher.poll({"shows": "c"}) == []  # settled, but only 4s since the last run
    clock.advance(48)
    assert watcher.poll({"shows": "c"}) == ["shows"]  # 52s >= the gap


def test_startup_grace_suppresses_the_first_burst() -> None:
    clock = Clock()
    watcher = _watcher(clock, startup_grace_s=60.0)
    watcher.poll({"shows": "a"})
    clock.advance(5)
    watcher.poll({"shows": "b"})  # ignored while in grace
    clock.advance(70)
    watcher.poll({"shows": "b"})
    clock.advance(11)
    assert watcher.poll({"shows": "b"}) == []  # never armed during grace
    clock.advance(1)
    assert watcher.poll({"shows": "c"}) == []  # arming works after the grace
    clock.advance(11)
    assert watcher.poll({"shows": "c"}) == ["shows"]


def test_the_full_scan_interval_fires_even_without_a_change() -> None:
    clock = Clock()
    watcher = _watcher(clock, full_interval_s=30.0)
    watcher.poll({"shows": "a"})
    clock.advance(29)
    assert watcher.poll({"shows": "a"}) == []
    clock.advance(2)
    assert watcher.poll({"shows": "a"}) == ["shows"]


def test_mark_ran_pushes_the_minimum_gap_forward() -> None:
    clock = Clock()
    watcher = _watcher(clock, settle_s=2.0, min_gap_s=50.0)
    watcher.poll({"shows": "a"})
    clock.advance(1)
    watcher.poll({"shows": "b"})
    clock.advance(3)
    assert watcher.poll({"shows": "b"}) == ["shows"]

    clock.advance(46)  # a manual run at t=1050
    watcher.mark_ran("shows")
    clock.advance(1)
    watcher.poll({"shows": "c"})  # armed at 1051
    clock.advance(3)
    assert watcher.poll({"shows": "c"}) == []  # 4s since the manual run
    clock.advance(48)
    assert watcher.poll({"shows": "c"}) == ["shows"]  # 52s since the manual run
