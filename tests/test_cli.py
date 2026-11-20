"""The verbs, driven through ``main`` exactly as a user types them.

The unit tests cover the pieces; this file covers the seam the scheduled task
uses: ``sync`` writes, ``sweep`` removes, ``sync --full`` does both, and a config
that cannot work exits 2 instead of half-running.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mediaforge.cli import _cmd_daemon, build_parser, main
from mediaforge.exitcodes import EXIT_CONFIG, EXIT_OK
from mediaforge.state import SqliteStateStore

from .conftest import Fixture, build_library, make_env, requires_symlinks


def _use(monkeypatch: pytest.MonkeyPatch, library: Fixture) -> str:
    """Point the CLI at the fixture's library the way ``.env`` would."""
    env = make_env(
        library.clone,
        library.state,
        {"shows": library.shows, "anime": library.anime},
        MF_JELLYFIN_REFRESH="false",
        MF_SETTLE_SECONDS="0",
        MF_LIVENESS_GRACE_SECONDS="0",
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return str(library.settings.repo_root)


def _link_for(library: Fixture, needle: str):
    store = SqliteStateStore(library.state / "mediaforge.db")
    try:
        rows = list(store.iter_links())
    finally:
        store.close()
    return next(row for row in rows if needle in row.source_path)


@requires_symlinks
def test_sync_writes_and_sweep_removes_the_entry(tmp_path: Path, monkeypatch, capsys) -> None:
    library = build_library(tmp_path)
    root = _use(monkeypatch, library)
    assert main(["sync", "--root", root, "--apply"]) == EXIT_OK

    row = _link_for(library, "S01E01")
    assert os.path.lexists(row.target_path), "sync must have created the link"
    capsys.readouterr()

    Path(row.source_path).unlink()
    assert main(["sweep", "--root", root, "--apply", "--grace", "0"]) == EXIT_OK

    assert not os.path.lexists(row.target_path)
    assert "pruned" in capsys.readouterr().out


@requires_symlinks
def test_sweep_dry_run_reports_but_keeps_the_entry(tmp_path: Path, monkeypatch, capsys) -> None:
    library = build_library(tmp_path)
    root = _use(monkeypatch, library)
    assert main(["sync", "--root", root, "--apply"]) == EXIT_OK
    row = _link_for(library, "S01E01")
    Path(row.source_path).unlink()
    capsys.readouterr()

    assert main(["sweep", "--root", root, "--dry-run", "--grace", "0"]) == EXIT_OK

    assert os.path.lexists(row.target_path)
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "pruned" in out


def test_sweep_on_a_clean_tree_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    library = build_library(tmp_path)
    root = _use(monkeypatch, library)
    assert main(["sweep", "--root", root, "--apply", "--grace", "0"]) == EXIT_OK
    assert "no dangling entries" in capsys.readouterr().out


def test_sync_full_reports_the_sweep_in_its_payload(tmp_path: Path, monkeypatch, capsys) -> None:
    """``--full`` is the safety net the daemon runs periodically, so it is observable."""
    library = build_library(tmp_path)
    root = _use(monkeypatch, library)
    assert main(["sync", "--root", root, "--dry-run", "--full", "--json"]) == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["sweep"] == {
        "dry_run": True,
        "pruned": [],
        "errors": [],
        "libraries": [],
        "paused_roots": [],
    }


def test_a_config_that_cannot_work_exits_two(monkeypatch, tmp_path: Path, capsys) -> None:
    for key in ("MF_SOURCE_SHOWS", "MF_SOURCE_ANIME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MF_CLONE_ROOT", str(tmp_path / "clone"))
    monkeypatch.setenv("MF_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MF_PLUGIN_DIR", str(tmp_path / "plugins"))
    assert main(["sweep", "--root", str(tmp_path), "--apply"]) == EXIT_CONFIG
    assert "MF_SOURCE_" in capsys.readouterr().err


def test_watch_is_still_the_old_name_for_the_daemon() -> None:
    """The deployed task runs ``mediaforge watch``; it must keep landing on the loop."""
    args = build_parser().parse_args(["watch"])
    assert args.func is _cmd_daemon
    assert build_parser().parse_args(["daemon"]).func is _cmd_daemon


def test_the_sweep_grace_defaults_to_the_configured_window() -> None:
    assert build_parser().parse_args(["sweep"]).grace is None
    assert build_parser().parse_args(["sweep", "--grace", "0"]).grace == 0.0
