"""The plugin contract.

A plugin is a plain module in ``plugins/`` exposing the attributes and two
functions below. Nothing is instantiated: this mirrors the loader style of the
sibling downloader, where a module *is* the plugin.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import DirectorySample, Library, MediaFile, PlannedItem


class PluginError(RuntimeError):
    """A plugin broke its contract; the loader turns it into a refusal."""


@dataclass(frozen=True, slots=True)
class DetectContext:
    root_id: str
    root_path: Path
    dirs: tuple[DirectorySample, ...]
    total_files: int
    extension_histogram: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class PlanContext:
    root_id: str
    root_path: Path
    files: tuple[MediaFile, ...]
    dirs: tuple[DirectorySample, ...]
    options: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Detected:
    plugin_id: str
    confidence: float
    reasons: tuple[str, ...] = ()


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def read_bool(options: Mapping[str, str], key: str, default: bool = True) -> bool:
    """A boolean switch as a plugin sees it, with the product default."""
    value = options.get(key)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    return default


@runtime_checkable
class Plugin(Protocol):
    id: str
    name: str
    version: str
    priority: int
    default_library: Library

    def detect(self, ctx: DetectContext) -> Detected | float: ...

    def plan(self, ctx: PlanContext) -> Iterable[PlannedItem]: ...
