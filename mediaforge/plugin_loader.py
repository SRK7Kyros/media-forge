"""Plugin discovery, contract validation and crash isolation."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import LIBRARIES
from .plugin_api import DetectContext, Detected, PlanContext


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    plugin_id: str
    path: Path
    module: object | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.module is not None and self.error is None


REQUIRED_STRINGS = ("id", "name", "version")
REQUIRED_CALLABLES = ("detect", "plan")


def _validate(module: object, path: Path) -> str | None:
    for attribute in REQUIRED_STRINGS:
        value = getattr(module, attribute, None)
        if not isinstance(value, str) or not value.strip():
            return f"{path.name}: '{attribute}' must be a non-empty string"
    for attribute in REQUIRED_CALLABLES:
        if not callable(getattr(module, attribute, None)):
            return f"{path.name}: '{attribute}' must be a function"
    priority = getattr(module, "priority", None)
    if not isinstance(priority, int):
        return f"{path.name}: 'priority' must be an int"
    library = getattr(module, "default_library", None)
    if library not in LIBRARIES:
        return f"{path.name}: 'default_library' must be one of {LIBRARIES}"
    return None


def load_plugins(plugin_dir: Path) -> tuple[LoadedPlugin, ...]:
    """Import every ``*.py`` in ``plugin_dir``. A broken plugin is reported, not fatal."""
    if not plugin_dir.is_dir():
        return ()
    loaded: list[LoadedPlugin] = []
    for path in sorted(plugin_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"mediaforge_plugins.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                loaded.append(LoadedPlugin(path.stem, path, None, "cannot build an import spec"))
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # a plugin must never take the loader down
            loaded.append(LoadedPlugin(path.stem, path, None, f"{type(exc).__name__}: {exc}"))
            continue
        error = _validate(module, path)
        plugin_id = getattr(module, "id", path.stem)
        loaded.append(LoadedPlugin(plugin_id, path, module, error))
    return tuple(loaded)


def safe_detect(plugin: object, ctx: DetectContext) -> tuple[Detected | None, str | None]:
    """Run ``detect`` and turn any exception into a refusal with a reason."""
    try:
        result = plugin.detect(ctx)  # type: ignore[attr-defined]
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if isinstance(result, Detected):
        return result, None
    if isinstance(result, (int, float)):
        return Detected(
            plugin_id=getattr(plugin, "id", "unknown"),
            confidence=float(result),
            reasons=(),
        ), None
    return None, f"detect() returned {type(result).__name__}, expected Detected or float"


def safe_plan(
    plugin: object, ctx: PlanContext
) -> tuple[tuple[object, ...], str | None]:
    """Collect ``plan()`` output, turning an exception into a per-root error."""
    try:
        items = tuple(plugin.plan(ctx))  # type: ignore[attr-defined]
    except Exception as exc:
        return (), f"{type(exc).__name__}: {exc}"
    return items, None
