"""Configuration: ``.env`` plus the process environment, with startup guards.

Every key has a default in code, so a missing key is never fatal. A key in the
process environment wins over the same key in ``.env``. Values may expand
``%VAR%`` and ``${VAR}`` from the process environment.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import fsutil

SOURCE_PREFIX = "MF_SOURCE_"
OVERRIDE_PREFIX = "MF_PLUGIN_OVERRIDE_"
LIBRARIES = ("movies", "shows", "anime")
DEFAULT_LIBRARY_NAMES = {"movies": "Movies", "shows": "Shows", "anime": "Anime"}

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})
_EXPAND_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(RuntimeError):
    """A setting is unusable. The message is shown verbatim and is fatal."""


@dataclass(frozen=True, slots=True)
class SourceRoot:
    root_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class Settings:
    repo_root: Path
    clone_root: Path
    library_dirs: dict[str, str]
    sources: tuple[SourceRoot, ...]
    plugin_dir: Path
    plugin_min_confidence: float
    plugin_overrides: dict[str, str]
    resolution_preference: tuple[str, ...]
    exclude_globs: tuple[str, ...]
    managed_extensions: frozenset[str]
    nfo_enabled: bool
    nfo_sorttitle: bool
    nfo_set: bool
    anime_single_season: bool
    anime_movie_hints: re.Pattern[str]
    watch_mode: str
    poll_interval_s: float
    settle_s: float
    min_gap_s: float
    full_scan_interval_s: float
    startup_grace_s: float
    dry_run: bool
    prune_enabled: bool
    prune_require_known_inode: bool
    state_dir: Path
    log_dir: Path
    log_level: str
    jellyfin_url: str
    jellyfin_api_key: str
    jellyfin_refresh: bool
    jellyfin_debounce_s: float
    jellyfin_timeout_s: float
    jellyfin_lib_ids: dict[str, str]
    raw: dict[str, str]


def repo_root() -> Path:
    """The source checkout that contains ``src/``."""
    override = os.environ.get("MF_ROOT")
    if override:
        return Path(os.path.abspath(override))
    return Path(__file__).resolve().parents[1]


def expand(value: str) -> str:
    """Expand ``%VAR%`` and ``${VAR}``; unknown names are left untouched."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    return _EXPAND_RE.sub(replace, value)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file. Comments and blanks are ignored."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def merged_env(repo: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """``.env`` in the repo root, then the process environment on top."""
    merged = read_env_file(repo / ".env")
    merged.update(dict(os.environ if environ is None else environ))
    return merged


def _get(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    return default if value is None else expand(value)


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None:
        return default
    lowered = expand(value).strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ConfigError(f"{key}={value!r} is not a boolean (use true/false).")


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not expand(raw).strip():
        return default
    try:
        return int(expand(raw).strip())
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not an integer.") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not expand(raw).strip():
        return default
    try:
        return float(expand(raw).strip())
    except ValueError as exc:
        raise ConfigError(f"{key}={raw!r} is not a number.") from exc


def _sources(env: Mapping[str, str]) -> tuple[SourceRoot, ...]:
    found: list[SourceRoot] = []
    for key in sorted(env):
        if not key.startswith(SOURCE_PREFIX):
            continue
        root_id = key[len(SOURCE_PREFIX) :].strip().lower()
        value = expand(env[key]).strip()
        if root_id and value:
            found.append(SourceRoot(root_id, Path(os.path.abspath(value))))
    if not found:
        for root_id, default in (
            ("shows", r"D:\Shows\streaming-community"),
            ("anime", r"D:\Shows\animeunity"),
        ):
            value = _get(env, SOURCE_PREFIX + root_id.upper(), default)
            if value.strip():
                found.append(SourceRoot(root_id, Path(os.path.abspath(value.strip()))))
    return tuple(found)


def _overrides(env: Mapping[str, str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for key in sorted(env):
        if key.startswith(OVERRIDE_PREFIX):
            root_id = key[len(OVERRIDE_PREFIX) :].strip().lower()
            value = expand(env[key]).strip()
            if root_id and value:
                overrides[root_id] = value
    return overrides


def _state_dir(env: Mapping[str, str]) -> Path:
    default = str(Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "media-forge")
    return Path(os.path.abspath(_get(env, "MF_STATE_DIR", default)))


def load_settings(repo: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    """Build the settings object. Never touches the filesystem except for stat."""
    base = Path(os.path.abspath(repo)) if repo else repo_root()
    env = merged_env(base, environ)

    state_dir = _state_dir(env)
    log_dir = Path(os.path.abspath(_get(env, "MF_LOG_DIR", str(state_dir / "logs"))))
    managed = _get(env, "MF_MANAGED_EXTENSIONS", ".mkv,.mp4,.nfo")
    preference = _get(env, "MF_RESOLUTION_PREFERENCE", "1080p,720p")
    exclude = _get(env, "MF_EXCLUDE", "")

    return Settings(
        repo_root=base,
        clone_root=Path(os.path.abspath(_get(env, "MF_CLONE_ROOT", r"D:\Media\Jellyfin"))),
        library_dirs={
            "movies": _get(env, "MF_LIBRARY_MOVIES", DEFAULT_LIBRARY_NAMES["movies"]),
            "shows": _get(env, "MF_LIBRARY_SHOWS", DEFAULT_LIBRARY_NAMES["shows"]),
            "anime": _get(env, "MF_LIBRARY_ANIME", DEFAULT_LIBRARY_NAMES["anime"]),
        },
        sources=_sources(env),
        plugin_dir=Path(os.path.abspath(_get(env, "MF_PLUGIN_DIR", str(base / "plugins")))),
        plugin_min_confidence=_float(env, "MF_PLUGIN_MIN_CONFIDENCE", 0.60),
        plugin_overrides=_overrides(env),
        resolution_preference=tuple(
            part.strip().lower() for part in preference.split(",") if part.strip()
        ),
        exclude_globs=tuple(part.strip() for part in exclude.split(",") if part.strip()),
        managed_extensions=frozenset(
            ext.strip().lower() if ext.strip().startswith(".") else "." + ext.strip().lower()
            for ext in managed.split(",")
            if ext.strip()
        ),
        nfo_enabled=_bool(env, "MF_NFO_ENABLED", True),
        nfo_sorttitle=_bool(env, "MF_NFO_SORTTITLE", True),
        nfo_set=_bool(env, "MF_NFO_SET", True),
        anime_single_season=_bool(env, "MF_ANIME_SINGLE_SEASON", False),
        anime_movie_hints=re.compile(
            _get(env, "MF_ANIME_MOVIE_HINTS", r"\b(Movie|Film)\b"), re.IGNORECASE
        ),
        watch_mode=_get(env, "MF_WATCH_MODE", "poll").strip().lower(),
        poll_interval_s=_float(env, "MF_POLL_INTERVAL_SECONDS", 15.0),
        settle_s=_float(env, "MF_SETTLE_SECONDS", 20.0),
        min_gap_s=_float(env, "MF_MIN_GAP_SECONDS", 30.0),
        full_scan_interval_s=_float(env, "MF_SCAN_FULL_INTERVAL_SECONDS", 21600.0),
        startup_grace_s=_float(env, "MF_STARTUP_GRACE_SECONDS", 60.0),
        dry_run=_bool(env, "MF_DRY_RUN", False),
        prune_enabled=_bool(env, "MF_PRUNE_ENABLED", True),
        prune_require_known_inode=_bool(env, "MF_PRUNE_REQUIRE_KNOWN_INODE", True),
        state_dir=state_dir,
        log_dir=log_dir,
        log_level=_get(env, "MF_LOG_LEVEL", "INFO").strip().upper(),
        jellyfin_url=_get(env, "MF_JELLYFIN_URL", "http://127.0.0.1:8096").rstrip("/"),
        jellyfin_api_key=_get(env, "MF_JELLYFIN_API_KEY", "").strip(),
        jellyfin_refresh=_bool(env, "MF_JELLYFIN_REFRESH", True),
        jellyfin_debounce_s=_float(env, "MF_JELLYFIN_REFRESH_DEBOUNCE_SECONDS", 60.0),
        jellyfin_timeout_s=_float(env, "MF_JELLYFIN_REFRESH_TIMEOUT_SECONDS", 120.0),
        jellyfin_lib_ids={
            lib: _get(env, f"MF_JELLYFIN_LIB_{lib.upper()}", "").strip()
            for lib in LIBRARIES
        },
        raw=env,
    )


def validate(settings: Settings) -> tuple[str, ...]:
    """Raise :class:`ConfigError` for unusable settings, return soft warnings."""
    if settings.watch_mode not in {"poll", "off"}:
        raise ConfigError(f"MF_WATCH_MODE={settings.watch_mode!r}: only 'poll' or 'off' exist.")
    if settings.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError(f"MF_LOG_LEVEL={settings.log_level!r} is not a log level.")
    if not settings.sources:
        raise ConfigError("No sources configured: set at least one MF_SOURCE_<ROOT_ID>.")

    names = [settings.library_dirs[lib] for lib in LIBRARIES]
    for name in names:
        if not name or name in {".", ".."} or any(sep in name for sep in ("/", "\\", ":")):
            raise ConfigError(f"Library folder name {name!r} must be one plain path component.")
    if len(set(n.lower() for n in names)) != len(names):
        raise ConfigError(f"Library folder names collide: {names}.")

    seen: set[str] = set()
    for source in settings.sources:
        if source.root_id in seen:
            raise ConfigError(f"Duplicate source root id {source.root_id!r}.")
        seen.add(source.root_id)

    clone = settings.clone_root
    for source in settings.sources:
        src = source.path
        if fsutil.is_within(clone, src) or fsutil.is_within(src, clone):
            raise ConfigError(
                f"Source {src} and clone root {clone} overlap; they must be disjoint trees."
            )
        clone_volume = fsutil.volume_of(clone)
        source_volume = fsutil.volume_of(src)
        if clone_volume is None or source_volume is None:
            raise ConfigError(f"Cannot determine the volume of {src} or {clone}.")
        if clone_volume != source_volume:
            raise ConfigError(
                f"Clone root {clone} is on volume {fsutil.volume_name(clone)} but source {src} "
                f"is on volume {fsutil.volume_name(src)}. NTFS hardlinks cannot cross volumes."
            )

    warnings: list[str] = []
    if settings.jellyfin_refresh and not settings.jellyfin_api_key:
        warnings.append(
            "MF_JELLYFIN_REFRESH is on but MF_JELLYFIN_API_KEY is empty: "
            "the clone tree will be maintained, Jellyfin will not be told to rescan."
        )
    return tuple(warnings)
