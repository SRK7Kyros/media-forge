"""Settings: no invented defaults, and no path that only exists on one OS.

Each assertion here fails on the pre-2026-11-20 ``config.py``:

* it fell back to two hard-coded source roots, so "nothing configured" looked
  like "everything configured";
* ``MF_CLONE_ROOT`` defaulted to a fixed directory, so a first run wrote a
  symlink tree into it without anyone naming it;
* the state directory was ``%ProgramData%\\media-forge`` on every platform.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediaforge import config


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    base = {
        "MF_CLONE_ROOT": str(tmp_path / "clone"),
        "MF_STATE_DIR": str(tmp_path / "state"),
    }
    base.update(extra)
    return base


def test_no_sources_configured_is_an_error_not_a_guess(tmp_path: Path) -> None:
    settings = config.load_settings(tmp_path, environ=_env(tmp_path))
    assert settings.sources == ()
    with pytest.raises(config.ConfigError, match="MF_SOURCE_"):
        config.validate(settings)


def test_only_the_named_roots_exist(tmp_path: Path) -> None:
    env = _env(tmp_path, MF_SOURCE_MOVIES=str(tmp_path / "movies"))
    settings = config.load_settings(tmp_path, environ=env)
    assert [root.root_id for root in settings.sources] == ["movies"]


def test_root_id_is_lower_cased(tmp_path: Path) -> None:
    env = _env(tmp_path, MF_SOURCE_ANIME=str(tmp_path / "a"))
    settings = config.load_settings(tmp_path, environ=env)
    assert settings.sources[0].root_id == "anime"


def test_empty_source_value_is_ignored(tmp_path: Path) -> None:
    env = _env(tmp_path, MF_SOURCE_SHOWS="   ")
    settings = config.load_settings(tmp_path, environ=env)
    assert settings.sources == ()


def test_clone_root_has_no_default(tmp_path: Path) -> None:
    env = {"MF_STATE_DIR": str(tmp_path / "state")}
    with pytest.raises(config.ConfigError, match="MF_CLONE_ROOT"):
        config.load_settings(tmp_path, environ=env)


def test_clone_root_is_absolute(tmp_path: Path) -> None:
    env = _env(tmp_path, MF_CLONE_ROOT="relative/clone")
    settings = config.load_settings(tmp_path, environ=env)
    assert settings.clone_root.is_absolute()
    assert settings.clone_root == Path(os.path.abspath("relative/clone"))


def test_state_dir_honours_xdg_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg")
    assert config._default_state_dir() == os.path.join("/tmp/xdg", "media-forge")


def test_state_dir_falls_back_to_home_state_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    home = os.path.expanduser("~")
    assert config._default_state_dir() == os.path.join(home, ".local", "state", "media-forge")


def test_state_dir_uses_programdata_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.os, "name", "nt")
    monkeypatch.setenv("ProgramData", r"C:\PD")
    assert config._default_state_dir() == os.path.join(r"C:\PD", "media-forge")


def test_state_dir_explicit_value_wins(tmp_path: Path) -> None:
    settings = config.load_settings(tmp_path, environ=_env(tmp_path))
    assert settings.state_dir == Path(os.path.abspath(tmp_path / "state"))


def test_project_root_finds_the_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    monkeypatch.delenv("MF_ROOT", raising=False)
    assert config.project_root() == tmp_path


def test_project_root_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MF_ROOT", str(tmp_path / "elsewhere"))
    assert config.project_root() == Path(os.path.abspath(tmp_path / "elsewhere"))
