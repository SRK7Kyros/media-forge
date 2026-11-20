"""State store: migrations, provenance, dirty markers, the single-instance lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from mediaforge.models import Op
from mediaforge.state import SCHEMA_VERSION, LinkRecord, SingleInstanceLock, SqliteStateStore


def _record(source: str, target: str, **overrides: object) -> LinkRecord:
    values: dict[str, object] = {
        "source_path": source,
        "source_dev": 1,
        "source_ino": 2,
        "source_size": 3,
        "source_mtime_ns": 4,
        "target_path": target,
        "target_dev": 1,
        "target_ino": 2,
        "target_size": 3,
        "library": "movies",
        "plugin_id": "test",
        "root_id": "movies",
        "nfo_path": None,
        "nfo_sha256": None,
        "sort_title": None,
        "franchise": None,
        "display_order": None,
        "created_ts": 10,
        "verified_ts": None,
        "status": "pending",
    }
    values.update(overrides)
    return LinkRecord(**values)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> SqliteStateStore:
    handle = SqliteStateStore(tmp_path / "state.db")
    for root_id in ("movies", "shows", "anime"):
        handle.upsert_root(root_id, rf"D:\{root_id}")
    yield handle
    handle.close()


def test_migrations_run_once_and_record_the_version(store: SqliteStateStore) -> None:
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    store.migrate()  # idempotent
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_root_and_plugin_choice_round_trip(store: SqliteStateStore) -> None:
    store.upsert_root("shows", r"D:\Shows\streaming-community")
    store.set_plugin_choice("shows", "streaming_community", 1.0, "because", 100)
    store.record_scan("shows", "sig-1", "streaming_community", 101)
    assert store.get_plugin_choice("shows") == ("streaming_community", 1.0)
    assert store.last_signature("shows") == "sig-1"
    assert "shows" in [row["root_id"] for row in store.roots()]


def test_pending_then_confirm_is_visible_as_done(store: SqliteStateStore) -> None:
    record = _record(r"D:\a.mkv", r"D:\clone\a.mkv")
    store.put_pending(record)
    assert store.pending_count() == 1
    assert store.get_link(record.source_path) is not None
    store.confirm(record.source_path, target_ino=99, target_size=123, nfo_sha256="abc", ts=200)
    stored = store.get_link(record.source_path)
    assert stored is not None
    assert (stored.status, stored.target_ino, stored.nfo_sha256) == ("done", 99, "abc")
    assert store.pending_count() == 0


def test_a_second_source_cannot_claim_the_same_target(store: SqliteStateStore) -> None:
    store.put_pending(_record(r"D:\a.mkv", r"D:\clone\same.mkv"))
    store.put_pending(_record(r"D:\b.mkv", r"D:\clone\same.mkv"))
    assert store.get_link(r"D:\a.mkv") is None
    assert store.get_by_target(r"D:\clone\same.mkv") is not None


def test_find_by_inode_covers_both_sides(store: SqliteStateStore) -> None:
    store.put_pending(_record(r"D:\a.mkv", r"D:\clone\a.mkv", source_ino=7, target_ino=7))
    assert len(store.find_by_inode(1, 7)) == 1


def test_nfo_hash_is_updated_by_target(store: SqliteStateStore) -> None:
    store.put_pending(_record(r"D:\a.mkv", r"D:\clone\a.mkv"))
    store.set_nfo(r"D:\clone\a.mkv", "deadbeef", ts=5)
    stored = store.get_by_target(r"D:\clone\a.mkv")
    assert stored is not None and stored.nfo_sha256 == "deadbeef"


def test_dirty_markers_are_read_without_being_consumed(store: SqliteStateStore) -> None:
    store.mark_dirty("movies", 10)
    store.mark_dirty("shows", 20)
    store.mark_dirty("movies", 30)  # already queued, keeps the first timestamp
    assert store.dirty_entries() == [("movies", 10), ("shows", 20)]
    store.clear_dirty(["movies"])
    assert store.dirty_entries() == [("shows", 20)]


def test_runs_and_attempts_are_journaled(store: SqliteStateStore) -> None:
    run_id = store.begin_run("manual", 1)
    op = Op("SKIP", r"D:\clone\x", r"D:\src\x", (("reason", "because"),))
    store.log_attempt(run_id, op, "ok", "why", 2)
    store.finish_run(run_id, {"created": 1, "errors": 0}, "ok", 3)
    recent = store.recent_runs(1)
    assert recent[0]["run_id"] == run_id
    assert recent[0]["created"] == 1
    assert recent[0]["outcome"] == "ok"


def test_counts_by_library_groups_by_status(store: SqliteStateStore) -> None:
    store.put_pending(_record(r"D:\a.mkv", r"D:\clone\a.mkv"))
    store.put_pending(_record(r"D:\b.mkv", r"D:\clone\b.mkv", library="anime"))
    store.confirm(r"D:\a.mkv", 2, 3, None, 1)
    counts = store.counts_by_library()
    assert counts["movies"]["done"] == 1
    assert counts["anime"]["pending"] == 1


def test_single_instance_lock_is_exclusive(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "x.lock")
    second = SingleInstanceLock(tmp_path / "x.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()
