"""Minimal NFO rendering.

Local metadata always wins over a remote provider, so an NFO that repeats the
title, plot or year freezes those values in place. This renderer therefore emits
*only* the elements a provider cannot supply: watch order, the movie set, and
the season/episode pin for absolute-numbered anime.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from . import fsutil
from .models import NfoSpec

ROOT_ELEMENTS = {"movie": "movie", "episode": "episodedetails", "tvshow": "tvshow"}


def render(spec: NfoSpec) -> bytes:
    root = ROOT_ELEMENTS[spec.kind]
    lines = ['<?xml version="1.0" encoding="utf-8"?>', f"<{root}>"]
    for key, value in spec.elements:
        lines.append(f"  <{key}>{escape(str(value))}</{key}>")
    lines.append(f"</{root}>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def digest(spec: NfoSpec) -> str:
    return fsutil.sha256_bytes(render(spec))


def is_current(path: Path, expected_sha: str) -> bool:
    return fsutil.exists(path) and fsutil.sha256_file(path) == expected_sha


def write(path: Path, spec: NfoSpec, *, dry_run: bool = False) -> tuple[str, bool]:
    """Write only when the bytes differ, so a no-op run never touches an mtime.

    Returns ``(sha256, wrote)``.
    """
    data = render(spec)
    expected = fsutil.sha256_bytes(data)
    if is_current(path, expected):
        return expected, False
    if not dry_run:
        fsutil.atomic_write_bytes(path, data)
    return expected, True
