"""SQLite state: provenance, NFO hashes, run journal, single-instance lock.

The tree is authoritative for *existence*; this database is authoritative for
*provenance*. That split is what lets the pruner distinguish "a clone we made"
from "a file somebody dropped into the clone tree", and it is the only thing
that can answer it: ``st_nlink`` cannot say who created a link.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .models import Op

SCHEMA_VERSION = 1

MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE root (
      root_id TEXT PRIMARY KEY,
      path TEXT NOT NULL,
      enabled INTEGER NOT NULL DEFAULT 1,
      last_scan_ts INTEGER,
      last_signature TEXT,
      last_plugin_id TEXT
    );
    CREATE TABLE plugin_choice (
      root_id TEXT PRIMARY KEY REFERENCES root(root_id) ON DELETE CASCADE,
      plugin_id TEXT NOT NULL,
      confidence REAL NOT NULL,
      reason TEXT,
      decided_ts INTEGER NOT NULL
    );
    CREATE TABLE link (
      source_path TEXT PRIMARY KEY,
      source_dev INTEGER NOT NULL,
      source_ino INTEGER NOT NULL,
      source_size INTEGER NOT NULL,
      source_mtime_ns INTEGER NOT NULL,
      target_path TEXT NOT NULL,
      target_dev INTEGER NOT NULL,
      target_ino INTEGER NOT NULL,
      target_size INTEGER NOT NULL,
      library TEXT NOT NULL CHECK (library IN ('movies','shows','anime')),
      plugin_id TEXT NOT NULL,
      root_id TEXT NOT NULL REFERENCES root(root_id),
      nfo_path TEXT,
      nfo_sha256 TEXT,
      sort_title TEXT,
      franchise TEXT,
      display_order TEXT,
      created_ts INTEGER NOT NULL,
      verified_ts INTEGER,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done'))
    );
    CREATE UNIQUE INDEX ux_link_target ON link(target_path);
    CREATE INDEX ix_link_inode ON link(source_dev, source_ino);
    CREATE INDEX ix_link_library ON link(library, status);
    CREATE INDEX ix_link_root ON link(root_id);
    CREATE TABLE run (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_ts INTEGER NOT NULL,
      finished_ts INTEGER,
      trigger TEXT NOT NULL,
      scanned INTEGER DEFAULT 0,
      created INTEGER DEFAULT 0,
      repointed INTEGER DEFAULT 0,
      pruned INTEGER DEFAULT 0,
      nfo_written INTEGER DEFAULT 0,
      superseded INTEGER DEFAULT 0,
      errors INTEGER DEFAULT 0,
      outcome TEXT
    );
    CREATE TABLE attempt_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id INTEGER REFERENCES run(run_id),
      source_path TEXT,
      target_path TEXT,
      action TEXT NOT NULL,
      result TEXT NOT NULL,
      detail TEXT,
      ts INTEGER NOT NULL
    );
    CREATE TABLE dirty_library (
      library TEXT PRIMARY KEY,
      since_ts INTEGER NOT NULL,
      notified_ts INTEGER
    );
    """,
)


@dataclass(frozen=True, slots=True)
class LinkRecord:
    source_path: str
    source_dev: int
    source_ino: int
    source_size: int
    source_mtime_ns: int
    target_path: str
    target_dev: int
    target_ino: int
    target_size: int
    library: str
    plugin_id: str
    root_id: str
    nfo_path: str | None
    nfo_sha256: str | None
    sort_title: str | None
    franchise: str | None
    display_order: str | None
    created_ts: int
    verified_ts: int | None
    status: str


class SingleInstanceLock:
    """One process per state directory, enforced by an OS byte-range lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate(0)
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()
            self._handle = None


class SqliteStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for index in range(current, len(MIGRATIONS)):
            self._conn.executescript(MIGRATIONS[index])
            self._conn.execute(f"PRAGMA user_version={index + 1}")
        self._conn.commit()
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ── meta ────────────────────────────────────────────────────────────────

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM schema_meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    # ── roots and plugin choice ─────────────────────────────────────────────

    def upsert_root(self, root_id: str, path: str, enabled: bool = True) -> None:
        self._conn.execute(
            "INSERT INTO root(root_id, path, enabled) VALUES(?, ?, ?) "
            "ON CONFLICT(root_id) DO UPDATE SET path=excluded.path, enabled=excluded.enabled",
            (root_id, path, int(enabled)),
        )
        self._conn.commit()

    def record_scan(self, root_id: str, signature: str, plugin_id: str, ts: int) -> None:
        self._conn.execute(
            "UPDATE root SET last_signature=?, last_plugin_id=?, last_scan_ts=? WHERE root_id=?",
            (signature, plugin_id, ts, root_id),
        )
        self._conn.commit()

    def roots(self) -> list[dict[str, object]]:
        rows = self._conn.execute("SELECT * FROM root ORDER BY root_id").fetchall()
        return [dict(row) for row in rows]

    def last_signature(self, root_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT last_signature FROM root WHERE root_id=?", (root_id,)
        ).fetchone()
        return None if row is None else row["last_signature"]

    def set_plugin_choice(
        self, root_id: str, plugin_id: str, confidence: float, reason: str, ts: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO plugin_choice(root_id, plugin_id, confidence, reason, decided_ts) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(root_id) DO UPDATE SET "
            "plugin_id=excluded.plugin_id, confidence=excluded.confidence, "
            "reason=excluded.reason, decided_ts=excluded.decided_ts",
            (root_id, plugin_id, confidence, reason, ts),
        )
        self._conn.commit()

    def get_plugin_choice(self, root_id: str) -> tuple[str, float] | None:
        row = self._conn.execute(
            "SELECT plugin_id, confidence FROM plugin_choice WHERE root_id=?", (root_id,)
        ).fetchone()
        return None if row is None else (row["plugin_id"], row["confidence"])

    # ── links ───────────────────────────────────────────────────────────────

    def get_link(self, source_path: str) -> LinkRecord | None:
        row = self._conn.execute(
            "SELECT * FROM link WHERE source_path=?", (source_path,)
        ).fetchone()
        return None if row is None else _link(row)

    def get_by_target(self, target_path: str) -> LinkRecord | None:
        row = self._conn.execute(
            "SELECT * FROM link WHERE target_path=?", (target_path,)
        ).fetchone()
        return None if row is None else _link(row)

    def put_pending(self, record: LinkRecord) -> None:
        """Record intent *before* the syscall. A crash then leaves a row, not a stray file."""
        self._conn.execute(
            "DELETE FROM link WHERE target_path=? AND source_path<>?",
            (record.target_path, record.source_path),
        )
        self._conn.execute(
            "INSERT INTO link(source_path, source_dev, source_ino, source_size, "
            "source_mtime_ns, target_path, target_dev, target_ino, target_size, library, "
            "plugin_id, root_id, nfo_path, nfo_sha256, sort_title, franchise, display_order, "
            "created_ts, verified_ts, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_path) DO UPDATE SET "
            "source_dev=excluded.source_dev, source_ino=excluded.source_ino, "
            "source_size=excluded.source_size, source_mtime_ns=excluded.source_mtime_ns, "
            "target_path=excluded.target_path, target_dev=excluded.target_dev, "
            "target_ino=excluded.target_ino, target_size=excluded.target_size, "
            "library=excluded.library, plugin_id=excluded.plugin_id, root_id=excluded.root_id, "
            "nfo_path=excluded.nfo_path, nfo_sha256=excluded.nfo_sha256, "
            "sort_title=excluded.sort_title, franchise=excluded.franchise, "
            "display_order=excluded.display_order, status=excluded.status",
            (
                record.source_path,
                record.source_dev,
                record.source_ino,
                record.source_size,
                record.source_mtime_ns,
                record.target_path,
                record.target_dev,
                record.target_ino,
                record.target_size,
                record.library,
                record.plugin_id,
                record.root_id,
                record.nfo_path,
                record.nfo_sha256,
                record.sort_title,
                record.franchise,
                record.display_order,
                record.created_ts,
                record.verified_ts,
                record.status,
            ),
        )
        self._conn.commit()

    def confirm(
        self,
        source_path: str,
        target_ino: int,
        target_size: int,
        nfo_sha256: str | None,
        ts: int,
    ) -> None:
        self._conn.execute(
            "UPDATE link SET status='done', target_ino=?, target_size=?, nfo_sha256=?, "
            "verified_ts=? WHERE source_path=?",
            (target_ino, target_size, nfo_sha256, ts, source_path),
        )
        self._conn.commit()

    def set_nfo(self, target_path: str, nfo_sha256: str, ts: int) -> None:
        """Remember the NFO hash of the item whose video lives at ``target_path``."""
        self._conn.execute(
            "UPDATE link SET nfo_sha256=?, verified_ts=? WHERE target_path=?",
            (nfo_sha256, ts, target_path),
        )
        self._conn.commit()

    def delete_link(self, source_path: str) -> None:
        self._conn.execute("DELETE FROM link WHERE source_path=?", (source_path,))
        self._conn.commit()

    def iter_links(
        self,
        *,
        root_id: str | None = None,
        library: str | None = None,
        status: str | None = None,
    ) -> Iterator[LinkRecord]:
        query = "SELECT * FROM link"
        clauses: list[str] = []
        params: list[object] = []
        if root_id is not None:
            clauses.append("root_id=?")
            params.append(root_id)
        if library is not None:
            clauses.append("library=?")
            params.append(library)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        for row in self._conn.execute(query + " ORDER BY target_path", params):
            yield _link(row)

    def find_by_inode(self, dev: int, ino: int) -> tuple[LinkRecord, ...]:
        rows = self._conn.execute(
            "SELECT * FROM link WHERE (source_dev=? AND source_ino=?) "
            "OR (target_dev=? AND target_ino=?)",
            (dev, ino, dev, ino),
        ).fetchall()
        return tuple(_link(row) for row in rows)

    # ── runs ────────────────────────────────────────────────────────────────

    def begin_run(self, trigger: str, ts: int) -> int:
        cursor = self._conn.execute(
            "INSERT INTO run(started_ts, trigger) VALUES(?, ?)", (ts, trigger)
        )
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(
        self, run_id: int, counts: Mapping[str, int], outcome: str, ts: int
    ) -> None:
        self._conn.execute(
            "UPDATE run SET finished_ts=?, scanned=?, created=?, repointed=?, pruned=?, "
            "nfo_written=?, superseded=?, errors=?, outcome=? WHERE run_id=?",
            (
                ts,
                counts.get("scanned", 0),
                counts.get("created", 0),
                counts.get("repointed", 0),
                counts.get("pruned", 0),
                counts.get("nfo_written", 0),
                counts.get("superseded", 0),
                counts.get("errors", 0),
                outcome,
                run_id,
            ),
        )
        self._conn.commit()

    def log_attempt(
        self, run_id: int, op: Op, result: str, detail: str, ts: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO attempt_log(run_id, source_path, target_path, action, result, detail, ts) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (run_id, op.source_path, op.target_path, op.kind, result, detail, ts),
        )
        self._conn.commit()

    # ── Jellyfin dirty markers ──────────────────────────────────────────────

    def mark_dirty(self, library: str, ts: int) -> None:
        self._conn.execute(
            "INSERT INTO dirty_library(library, since_ts) VALUES(?, ?) "
            "ON CONFLICT(library) DO NOTHING",
            (library, ts),
        )
        self._conn.commit()

    def pop_dirty(self) -> list[str]:
        rows = self._conn.execute("SELECT library FROM dirty_library ORDER BY library").fetchall()
        names = [row["library"] for row in rows]
        if names:
            self._conn.executemany(
                "DELETE FROM dirty_library WHERE library=?", [(name,) for name in names]
            )
            self._conn.commit()
        return names

    def dirty_entries(self) -> list[tuple[str, int]]:
        """Pending Jellyfin refreshes as ``(library, since_ts)``, oldest first.

        Reading without deleting is deliberate: a marker is only cleared once
        Jellyfin has actually accepted the refresh.
        """
        rows = self._conn.execute(
            "SELECT library, since_ts FROM dirty_library ORDER BY since_ts, library"
        ).fetchall()
        return [(row["library"], int(row["since_ts"])) for row in rows]

    def clear_dirty(self, libraries: Iterable[str]) -> None:
        names = list(libraries)
        if not names:
            return
        self._conn.executemany(
            "DELETE FROM dirty_library WHERE library=?", [(name,) for name in names]
        )
        self._conn.commit()

    def pending_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM link WHERE status='pending'").fetchone()
        return int(row["n"])

    def counts_by_library(self) -> dict[str, dict[str, int]]:
        rows = self._conn.execute(
            "SELECT library, status, COUNT(*) AS n FROM link GROUP BY library, status"
        ).fetchall()
        found: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = found.setdefault(row["library"], {})
            bucket[row["status"]] = int(row["n"])
        return found

    def recent_runs(self, limit: int = 5) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM run ORDER BY run_id DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
        return [dict(row) for row in rows]


def _link(row: sqlite3.Row) -> LinkRecord:
    return LinkRecord(
        source_path=row["source_path"],
        source_dev=row["source_dev"],
        source_ino=row["source_ino"],
        source_size=row["source_size"],
        source_mtime_ns=row["source_mtime_ns"],
        target_path=row["target_path"],
        target_dev=row["target_dev"],
        target_ino=row["target_ino"],
        target_size=row["target_size"],
        library=row["library"],
        plugin_id=row["plugin_id"],
        root_id=row["root_id"],
        nfo_path=row["nfo_path"],
        nfo_sha256=row["nfo_sha256"],
        sort_title=row["sort_title"],
        franchise=row["franchise"],
        display_order=row["display_order"],
        created_ts=row["created_ts"],
        verified_ts=row["verified_ts"],
        status=row["status"],
    )
