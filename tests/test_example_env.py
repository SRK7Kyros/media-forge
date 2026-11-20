"""``example.env`` is the file `init` copies to `.env`: a contract with a stranger.

So it has to document every key the code reads, and it must not configure a
source of its own - the two things this file was wrong about.
"""

from __future__ import annotations

import re
from pathlib import Path

from mediaforge.config import read_env_file

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "example.env"
CONFIG_SOURCE = REPO / "mediaforge" / "config.py"

_KEY_LINE = re.compile(r"^\s*#?\s*(MF_[A-Z0-9_]+)=")
_LITERAL_KEY = re.compile(r'"(MF_[A-Z0-9_]+)"')


def _documented(example: Path) -> set[str]:
    """Every ``MF_*`` key the file mentions, commented out or not."""
    keys: set[str] = set()
    for line in example.read_text(encoding="utf-8").splitlines():
        match = _KEY_LINE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def _read_by_code() -> set[str]:
    """Every literal ``MF_*`` key ``config.py`` reads.

    A name built from a prefix (``MF_SOURCE_<ID>``, ``MF_PLUGIN_OVERRIDE_<ID>``,
    ``MF_JELLYFIN_LIB_<LIB>``) appears in the source only as its prefix; the
    prefix is the thing that has to be documented, and it ends with ``_``.
    """
    return set(_LITERAL_KEY.findall(CONFIG_SOURCE.read_text(encoding="utf-8")))


def test_every_key_the_code_reads_is_documented() -> None:
    missing = {key for key in _read_by_code() if not key.endswith("_")} - _documented(EXAMPLE)
    assert not missing, f"example.env does not document {sorted(missing)}"


def test_no_source_is_configured_by_default() -> None:
    """A fresh install must not start pointed at somebody else's library."""
    active = sorted(
        key for key in read_env_file(EXAMPLE) if key.startswith("MF_SOURCE_")
    )
    assert active == [], f"example.env enables {active} out of the box"
