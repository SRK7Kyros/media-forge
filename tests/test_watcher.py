"""The arm/settle gate: when a root becomes eligible, never what is true.

The signature is the truth (``mediaforge.scan``); an event only *arms* a root.
These tests pin the three rules that make the gate safe: a settle window (a file
still being written is not reconciled), a minimum gap (a burst costs one run),
and a startup grace (a restart's event storm is ignored, because the first pass
already covers it).
"""

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
    assert watcher.poll({"shows": "b"}) == []  # armed at 1001
    clock.advance(5)
    assert watcher.poll({"shows": "b"}) == []  # 6 < settle
    clock.advance(6)
    assert watcher.poll({"shows": "b"}) == ["shows"]  # 12 >= settle


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
    assert watcher.poll({"shows": "c"}) == []  # settled, but only 4 s since the last run
    clock.advance(48)
    assert watcher.poll({"shows": "c"}) == ["shows"]  # 52 s >= the gap


def test_startup_grace_suppresses_the_first_burst() -> None:
    clock = Clock()
    watcher = _watcher(clock, startup_grace_s=60.0)
    watcher.poll({"shows": "a"})
    clock.advance(5)
    watcher.poll({"shows": "b"})  # ignored while in the grace window
    clock.advance(70)
    watcher.poll({"shows": "b"})
    clock.advance(11)
    assert watcher.poll({"shows": "b"}) == []  # never armed during grace
    clock.advance(1)
    assert watcher.poll({"shows": "c"}) == []  # arming works after the grace
    clock.advance(11)
    assert watcher.poll({"shows": "c"}) == ["shows"]


def test_an_event_arms_a_root_without_a_signature() -> None:
    """The event path: no walk at all, still a run after the settle window."""
    clock = Clock()
    watcher = _watcher(clock)
    watcher.arm("shows")
    clock.advance(9)
    assert watcher.ready() == []
    clock.advance(2)
    assert watcher.ready() == ["shows"]


def test_an_event_for_an_unknown_root_is_ignored() -> None:
    clock = Clock()
    watcher = _watcher(clock)
    watcher.arm("not-a-root")
    clock.advance(60)
    assert watcher.ready() == []


def test_next_due_names_the_earliest_moment_a_root_can_be_ready() -> None:
    clock = Clock()
    watcher = _watcher(clock, settle_s=10.0, min_gap_s=5.0)
    assert watcher.next_due() is None  # nothing armed, so no deadline exists
    watcher.arm("shows")  # 1000
    assert watcher.next_due() == 1010
    clock.advance(20)
    watcher.mark_ran("shows")  # 1020
    watcher.arm("shows")  # 1020, settle 10 s -> 1030, gap 5 s -> 1025
    assert watcher.next_due() == 1030


def test_mark_ran_holds_a_root_back_for_the_minimum_gap() -> None:
    """A run the gate did not start still counts, or a full pass is repeated."""
    clock = Clock()
    watcher = _watcher(clock, settle_s=1.0, min_gap_s=30.0)
    watcher.mark_ran("shows")
    watcher.arm("shows")
    clock.advance(2)
    assert watcher.ready() == []
    clock.advance(29)
    assert watcher.ready() == ["shows"]


def test_ready_clears_the_arm_so_one_change_costs_one_run() -> None:
    clock = Clock()
    watcher = _watcher(clock, settle_s=1.0, min_gap_s=0.0)
    watcher.arm("shows")
    clock.advance(2)
    assert watcher.ready() == ["shows"]
    clock.advance(1)
    assert watcher.ready() == []
