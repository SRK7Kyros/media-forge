"""Shared fixtures: a realistic two-root library, and a settings object for it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediaforge import fsutil
from mediaforge.config import Settings, load_settings
from mediaforge.linker import Linker
from mediaforge.pipeline import Pipeline
from mediaforge.plugin_loader import load_plugins
from mediaforge.state import SqliteStateStore

REPO = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "plugins"

SYMLINKS_AVAILABLE = fsutil.symlink_supported()
requires_symlinks = pytest.mark.skipif(
    not SYMLINKS_AVAILABLE,
    reason=(
        "this account cannot create symbolic links (no SeCreateSymbolicLinkPrivilege, "
        "Developer Mode off); run the suite from an elevated process"
    ),
)


@pytest.fixture(autouse=True)
def _clean_mf_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may inherit MF_* from the developer's shell."""
    for key in list(os.environ):
        if key.startswith("MF_"):
            monkeypatch.delenv(key, raising=False)


def write(path: Path, data: bytes = b"x" * 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_env(clone: Path, state: Path, sources: dict[str, Path], **extra: str) -> dict[str, str]:
    env = {
        "MF_CLONE_ROOT": str(clone),
        "MF_STATE_DIR": str(state),
        "MF_PLUGIN_DIR": str(PLUGIN_DIR),
        "MF_JELLYFIN_REFRESH": "false",
        "MF_SETTLE_SECONDS": "0",
    }
    for root_id, path in sources.items():
        env[f"MF_SOURCE_{root_id.upper()}"] = str(path)
    env.update({key: str(value) for key, value in extra.items()})
    return env


class Fixture:
    __slots__ = ("settings", "shows", "anime", "clone", "state", "plugins")

    def __init__(self, settings: Settings, shows: Path, anime: Path, state: Path) -> None:
        self.settings = settings
        self.shows = shows
        self.anime = anime
        self.clone = settings.clone_root
        self.state = state
        self.plugins = load_plugins(PLUGIN_DIR)

    def source(self, root_id: str) -> Path:
        for entry in self.settings.sources:
            if entry.root_id == root_id:
                return entry.path
        raise KeyError(f"no source root {root_id!r}")


def build_library(tmp_path: Path, **extra: str) -> Fixture:
    """The exact shapes of the real library, shrunk to a handful of files."""
    shows = tmp_path / "source-shows"
    anime = tmp_path / "source-anime"

    write(shows / "Diavoli (2020)" / "Diavoli (2020) 1080p - S01E01.mkv")
    write(shows / "Diavoli (2020)" / "Diavoli (2020) 1080p - S01E02.mkv")
    write(shows / "Diavoli (2020)" / "Diavoli.json", b"{}")
    write(shows / "MCU 1.1 - Iron Man (2008)" / "MCU 1.1 - Iron Man (2008) 1080p.mkv")
    write(shows / "MCU 1.1 - Iron Man (2008)" / "Iron Man.json", b"{}")

    write(anime / "One Piece (ITA) (1999)" / "One Piece (ITA) (1999) - 700.mp4")
    write(anime / "One Piece (ITA) (1999)" / "One Piece (ITA) (1999) - 701.mp4")
    write(anime / "Inazuma Eleven (ITA) (2008)" / "Inazuma Eleven (ITA) (2008) - 001.mp4")
    write(anime / "Inazuma Eleven (ITA) (2008)" / "Inazuma Eleven (ITA) (2008) - 003.mp4")
    write(anime / "Nana (ITA) (2006)" / "Nana (ITA) (2006) - 001.mp4")
    write(anime / "Death Note (ITA) (2006)" / "Death Note (ITA) (2006) - 001.mp4")
    write(anime / "Fullmetal Alchemist (ITA) (2009)" / "Fullmetal Alchemist (ITA) (2009) - 001.mp4")

    clone = tmp_path / "clone"
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = make_env(clone, state, {"shows": shows, "anime": anime}, **extra)
    settings = load_settings(repo, environ=env)
    return Fixture(settings, shows, anime, state)


@pytest.fixture
def library(tmp_path: Path) -> Fixture:
    return build_library(tmp_path)


@pytest.fixture
def pipeline(library: Fixture) -> tuple[Pipeline, SqliteStateStore]:
    store = SqliteStateStore(library.state / "mediaforge.db")
    pipe = Pipeline(
        library.settings,
        store,
        Linker(),
        dry_run=False,
        plugins=library.plugins,
    )
    yield pipe, store
    store.close()
