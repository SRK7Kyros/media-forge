# media-forge

Maintains a **Jellyfin-shaped hardlink clone tree** next to the media library you
already have, so Jellyfin can be pointed at a clean `Movies/ Shows/ Anime/` tree
without a byte being copied, renamed or moved in the originals.

```
D:\Shows\streaming-community\MCU 1.1 - Iron Man (2008)\...1080p.mkv
D:\Shows\animeunity\One Piece (ITA) (1999)\One Piece (ITA) (1999) - 700.mp4
        │  NTFS hardlink (same volume, same inode, zero bytes copied)
        ▼
D:\Media\Jellyfin\
    Movies\Iron Man (2008)\Iron Man (2008).mkv + movie.nfo
    Anime\One Piece (1999)\Season 01\One Piece (1999) - S01E700.mp4 + tvshow.nfo
```

The library keeps the naming *you* chose; the clone gets the naming Jellyfin
insists on. Both paths point at the same file, so deleting one never deletes the
content, and nothing is duplicated on disk.

## Commands

| command | what it does |
|---|---|
| `mediaforge init` | create the clone tree, state dir and a `.env` from `example.env` |
| `mediaforge plugins` | show every plugin and the score it gives each source root |
| `mediaforge plan` | print the entire diff — writes nothing |
| `mediaforge sync` | reconcile once (`--dry-run`, `--apply`) |
| `mediaforge watch` | poll the sources and reconcile as they change |
| `mediaforge status` | state, libraries, last run |
| `mediaforge verify` | prove the clone tree matches the database |
| `mediaforge jellyfin-check` | read Jellyfin's library ids into `.env` |

Exit codes: `0` ok, `1` problems (see the reported rows), `2` config, `3`
Jellyfin unreachable, `4` another instance holds the lock.

## Configuration

Everything comes from `.env` in the checkout, overridden by the process
environment. `example.env` documents every key; the ones that matter first:

```ini
MF_CLONE_ROOT=D:\Media\Jellyfin
MF_SOURCE_SHOWS=D:\Shows\streaming-community
MF_SOURCE_ANIME=D:\Shows\animeunity
MF_JELLYFIN_API_KEY=            # empty = maintain the tree, never ask Jellyfin to rescan
```

Sources are `MF_SOURCE_<ROOT_ID>`; any number of them exist, each is matched to a
plugin by content, not by name. `MF_PLUGIN_OVERRIDE_<ROOT_ID>=<plugin id>` pins one.

The clone root **must share a volume with every source** — NTFS hardlinks cannot
cross volumes — and the tool refuses to start otherwise rather than silently
falling back to a copy.

## How it decides

1. **Scan** a root with `stat` only (never an `open`): file list, extension
   histogram, a structural sample, and a signature that changes when a file is
   added, removed, resized or touched.
2. **Detect** the plugin: every plugin in `plugins/` scores the sample; the
   highest wins if it clears `MF_PLUGIN_MIN_CONFIDENCE`, otherwise the root is
   refused — a guess would reorganise the wrong tree. When detection fails on a
   root that *used* to resolve (a finished cleanup, an unmounted drive), the
   plugin recorded in the database is reused so an emptied root can still prune.
3. **Plan** a diff between what the plugin wants, what the clone tree is, and
   what the database remembers. Each disagreement has exactly one operation:
   `MKDIR`, `LINK_CREATE`, `LINK_REPOINT`, `LINK_ADOPT`, `NFO_WRITE`,
   `NFO_DELETE`, `SKIP`, `FILE_PRUNE`, `DIR_PRUNE`.
4. **Reconcile**: directories, then links, then NFOs, then prunes. A row is
   committed *before* its syscall, so an interruption leaves a row without a
   file (recoverable) and never a file without a row (unprovable).

## Safety invariants

- Nothing under a source root is ever written, renamed or deleted. `verify`
  checks the clone against the database; `plan` shows every intended change.
- A file at a target that the database does not claim is **refused**, not
  replaced: `refusing to replace` in the report.
- Pruning requires the recorded inode (`MF_PRUNE_REQUIRE_KNOWN_INODE`), so a
  file that was swapped out is skipped rather than deleted.
- `MF_DRY_RUN=true` makes every command read-only, including `watch`.
- One instance at a time (a lock file in the state dir).

## Layout

```
plugins/            one module per source shape (detect + plan)
src/                config, scan, registry, planner, reconciler, linker,
                    state, nfo, verify, watcher, jellyfin client, cli
service/            Start- and Install-MediaForgeTask.ps1 (boot persistence)
tests/              pytest, including a real NTFS tree end to end
```

A plugin is a plain module exposing `id`, `name`, `version`, `priority`,
`default_library`, `detect(ctx) -> Detected | float` and
`plan(ctx) -> Iterable[PlannedItem]`. See `src/plugin_api.py`.

## Running it for real

```powershell
uv venv .venv --python 3.11
uv pip install --python .venv\Scripts\python.exe -e .[dev]
.venv\Scripts\python.exe -m src init
.venv\Scripts\python.exe -m src plugins   # each root must resolve
.venv\Scripts\python.exe -m src plan      # read this before writing anything
.venv\Scripts\python.exe -m src sync
.venv\Scripts\python.exe -m src verify
```

Boot persistence (runs as SYSTEM, no window, no login required):

```powershell
# elevated
.\service\Install-MediaForgeTask.ps1
Start-ScheduledTask -TaskName MediaForge
```

Logs: `C:\ProgramData\media-forge\logs\mediaforge.log` (rotated daily, 14 kept).
State: `C:\ProgramData\media-forge\mediaforge.db`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src plugins tests
```
