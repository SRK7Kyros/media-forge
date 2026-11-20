"""Plugin loading, confidence competition, overrides and crash isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from mediaforge.plugin_api import DetectContext
from mediaforge.plugin_loader import load_plugins, safe_detect
from mediaforge.registry import NoPluginError, select, summarise

GOOD = '''
id = "good"
name = "Good plugin"
version = "1"
priority = 5
default_library = "shows"

def detect(ctx):
    return 0.9

def plan(ctx):
    return []
'''

WEAK = '''
id = "weak"
name = "Weak plugin"
version = "1"
priority = 1
default_library = "anime"

def detect(ctx):
    return 0.2

def plan(ctx):
    return []
'''

BROKEN = '''
id = "broken"
name = "Broken plugin"
version = "1"
priority = 0
default_library = "shows"

def detect(ctx):
    raise RuntimeError("boom")

def plan(ctx):
    return []
'''

INVALID = '''
id = "invalid"
version = "1"
'''


def _context() -> DetectContext:
    return DetectContext(
        root_id="shows",
        root_path=Path(r"D:\Shows\streaming-community"),
        dirs=(),
        total_files=0,
        extension_histogram={},
    )


def _plugins(tmp_path: Path) -> tuple:
    (tmp_path / "good.py").write_text(GOOD, encoding="utf-8")
    (tmp_path / "weak.py").write_text(WEAK, encoding="utf-8")
    (tmp_path / "broken.py").write_text(BROKEN, encoding="utf-8")
    (tmp_path / "invalid.py").write_text(INVALID, encoding="utf-8")
    return load_plugins(tmp_path)


def test_a_broken_or_invalid_plugin_is_reported_not_fatal(tmp_path: Path) -> None:
    loaded = {item.plugin_id: item for item in _plugins(tmp_path)}
    assert loaded["good"].ok
    assert loaded["weak"].ok
    assert loaded["broken"].ok  # it imports; only detect() explodes
    assert not loaded["invalid"].ok
    assert "must be" in (loaded["invalid"].error or "")


def test_detect_exception_becomes_a_refusal(tmp_path: Path) -> None:
    loaded = {item.plugin_id: item for item in _plugins(tmp_path)}
    detected, error = safe_detect(loaded["broken"].module, _context())
    assert detected is None
    assert error is not None and "boom" in error


def test_highest_confidence_wins(tmp_path: Path) -> None:
    selection = select("shows", _plugins(tmp_path), _context(), min_confidence=0.6)
    assert selection.plugin_id == "good"
    assert selection.confidence == 0.9
    assert not selection.overridden
    assert selection.losers()


def test_an_override_is_honoured_even_below_the_floor(tmp_path: Path) -> None:
    selection = select(
        "shows", _plugins(tmp_path), _context(), override="weak", min_confidence=0.6
    )
    assert selection.plugin_id == "weak"
    assert selection.overridden
    assert "override" in selection.reason


def test_an_override_naming_a_missing_plugin_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(NoPluginError, match="not installed"):
        select("shows", _plugins(tmp_path), _context(), override="nope")


def test_below_the_floor_the_tool_refuses_rather_than_guesses(tmp_path: Path) -> None:
    (tmp_path / "weak.py").write_text(WEAK, encoding="utf-8")
    with pytest.raises(NoPluginError, match="Refusing to guess"):
        select("anime", load_plugins(tmp_path), _context(), min_confidence=0.6)


def test_candidate_summary_names_every_plugin(tmp_path: Path) -> None:
    selection = select("shows", _plugins(tmp_path), _context(), min_confidence=0.6)
    text = summarise(selection.candidates)
    assert "good" in text and "weak" in text
