"""The refresher: disabled by default, debounced, and failure-tolerant."""

from __future__ import annotations

from pathlib import Path

from mediaforge.jellyfin import JellyfinError
from mediaforge.jellyfin_refresh import Refresher
from mediaforge.state import SqliteStateStore


class FakeClient:
    def __init__(self, *, enabled: bool = True, fail: bool = False, idle: bool = True) -> None:
        self.enabled = enabled
        self.fail = fail
        self.idle = idle
        self.calls: list[str | None] = []

    def refresh(self, library: str | None = None) -> bool:
        if self.fail:
            raise JellyfinError("nope")
        self.calls.append(library)
        return True

    def wait_idle(self, timeout_s: float) -> bool:
        return self.idle


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _refresher(client: FakeClient, store: SqliteStateStore, clock: Clock, **kwargs) -> Refresher:
    options = {"library_ids": {"movies": "abc"}, "debounce_s": 60.0, "timeout_s": 5.0}
    options.update(kwargs)
    return Refresher(store, client, clock=clock, **options)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> SqliteStateStore:
    return SqliteStateStore(tmp_path / "state.db")


def test_no_api_key_means_no_refresh(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        clock = Clock()
        store.mark_dirty("movies", 0)
        client = FakeClient(enabled=False)
        assert _refresher(client, store, clock).tick() is None
        assert client.calls == []
        assert store.dirty_entries()
    finally:
        store.close()


def test_a_burst_waits_for_the_debounce_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        clock = Clock()
        store.mark_dirty("movies", 990)
        refresher = _refresher(FakeClient(), store, clock)
        assert refresher.tick() is None
        clock.now = 1060
        outcome = refresher.tick()
        assert outcome is not None and outcome.accepted
        assert store.dirty_entries() == []
    finally:
        store.close()


def test_a_successful_refresh_uses_the_mapped_library_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        clock = Clock()
        store.mark_dirty("movies", 0)
        client = FakeClient()
        outcome = _refresher(client, store, clock).tick()
        assert outcome is not None
        assert client.calls == ["abc"]
        assert not outcome.full
    finally:
        store.close()


def test_an_unmapped_library_falls_back_to_a_full_refresh(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        clock = Clock()
        store.mark_dirty("anime", 0)
        client = FakeClient()
        outcome = _refresher(client, store, clock, library_ids={"movies": "abc"}).tick()
        assert outcome is not None and outcome.full
        assert client.calls == [None]
    finally:
        store.close()


def test_a_failure_keeps_the_markers_and_backs_off(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        clock = Clock()
        store.mark_dirty("movies", 0)
        client = FakeClient(fail=True)
        refresher = _refresher(client, store, clock)
        first = refresher.tick()
        assert first is not None and not first.accepted
        assert store.dirty_entries()
        clock.now += 10
        assert refresher.tick() is None  # backing off
        clock.now += 60
        assert refresher.tick() is not None
    finally:
        store.close()


def test_nothing_dirty_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        client = FakeClient()
        assert _refresher(client, store, Clock()).tick() is None
        assert client.calls == []
    finally:
        store.close()
