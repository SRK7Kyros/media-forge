# media-forge

Maintains a **Jellyfin-shaped clone tree** next to the media library you
already have, so Jellyfin can be pointed at a clean `Movies/ Shows/ Anime/` tree
without a byte being copied, renamed or moved in the originals.

```
D:\Shows\streaming-community\MCU 1.1 - Iron Man (2008)\...1080p.mkv
D:\Shows\animeunity\One Piece (ITA) (1999)\One Piece (ITA) (1999) - 700.mp4
        -> symbolic link (zero bytes; deleting the source frees it at once)
        ▼
D:\Media\Jellyfin\
    Movies\Iron Man (2008)\Iron Man (2008).mkv + movie.nfo
    Anime\One Piece (1999)\Season 17\One Piece (1999) - S17E700.mp4 + tvshow.nfo
```

The library keeps the naming *you* chose; the clone gets the naming Jellyfin
insists on. Every clone entry is a symbolic link, so nothing is duplicated on
disk and deleting a source frees its bytes immediately - the dangling entry is
swept within seconds (`MF_LIVENESS_INTERVAL_SECONDS`), so the mirror only ever
shows what the sources still have.

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

- Sources are `MF_SOURCE_<ROOT_ID>`; any number of them exist, each is matched to a
plugin by content, not by name. `MF_PLUGIN_OVERRIDE_<ROOT_ID>=<plugin id>` pins one.
`MF_ANIME_SINGLE_SEASON` and `plugins/anime_seasons.json` control the anime season
layout (see *Anime season layout* below).

Symbolic links cross volumes freely, so the clone root may live on another
disk. `MF_LINK_MODE=hardlink` (the pre-2026-11-13 behaviour) still requires
the clone root to share a volume with every source, and the tool refuses to
start otherwise rather than silently falling back to a copy.

## Link mode and liveness

`MF_LINK_MODE` decides what a clone entry is:

- `symlink` (default) - a symbolic link. It holds no bytes, so deleting a source
  frees the space at once. What is left is a dangling link, which
  `LivenessSweeper` removes once the source has been missing for
  `MF_LIVENESS_GRACE_SECONDS` (checked every `MF_LIVENESS_INTERVAL_SECONDS`,
  default 1 s), so a delete-and-recreate by a downloader does not flap the
  mirror. A series folder that empties out loses its `tvshow.nfo` and its
  directory in the same sweep. A second, slower pass walks the clone tree every
  `MF_ORPHAN_SCAN_INTERVAL_SECONDS` (default 60 s, `0` disables it) and removes
  dangling links no database row explains - a restored state database, a run
  killed between the link and its row - and clears a folder NFO left in a title
  folder that holds nothing else.
- `hardlink` - an NTFS hard link: same inode, no bytes copied either, but the
  data stays on disk until every name is gone. Same volume only.

Pruning is suspended for any source root whose path cannot be read when the
sweep runs: an unplugged drive, a renamed folder or a dropped share makes every
file under it read as deleted, and that reading must never be acted on. The
watch log says `pruning paused for <root>` once, and `pruning resumed` when it
comes back; nothing under that root is removed in between.

Creating a symbolic link on Windows needs `SeCreateSymbolicLinkPrivilege`:
either an elevated process (the `MediaForge` task runs with RunLevel Highest) or
Windows Developer Mode. A Medium-integrity shell has neither - `mediaforge
status` reports the mode, and `validate()` warns when it cannot be honoured.
Switching an existing hard-link tree to `symlink` repoints each entry on the next
sync (`LINK_REPOINT`); a legacy hard link whose source is already gone is
reclaimed through its recorded inode rather than left behind.

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

## Anime season layout

Absolute-numbered anime (`One Piece … - 700.mp4`) lands in one flat
`Season 01` with `<displayorder>absolute</displayorder>`, so Jellyfin matches
episode 700 against TMDB's absolute list. A show listed in
`plugins/anime_seasons.json` instead gets **its provider's seasons**:

```json
{ "title": "One Piece", "year": 1999, "mode": "absolute",
  "seasons": [[1, 61], [2, 16], …] }
```

`seasons` is `[season, episode_count]` in ascending order, specials excluded, and
the counts are cumulative, so absolute 700 falls in the season that owns
693–748. `mode: absolute` keeps the provider's global episode number in the file
name (`S17E700`, which is how TMDB numbers One Piece inside season 17);
`mode: per_season` restarts at 1 (`S02E16`).

The map is a **snapshot of a metadata provider**, not a live lookup: the pipeline
never reaches the network and a rerun must be deterministic. Add a show by
copying its season list from TMDB and recording `provider` plus `fetched` — the
schema rejects gaps, duplicates, season 0 and descending order. A show without
an entry keeps the flat layout; `MF_ANIME_SINGLE_SEASON=true` forces the flat
layout for every show.

Mapped shows write a `tvshow.nfo` that **asserts no display order** (Jellyfin's
default is TMDB's own seasons). That empty file matters: it displaces the
`absolute` an earlier run wrote, and a leftover `tvshow.nfo` is never pruned
while its folder still holds links.

Jellyfin keeps the display order **per series, not per folder**, so a series
first scanned under the flat layout keeps `absolute` however often it rescans;
clear it once with `POST /Items/{id}` (`DisplayOrder: ""`, full DTO — see
`AGENTS.md`). Season *names* ("Saga Di Dressrosa") are TMDb's `ImportSeasonName`
setting, not something this tool writes. One order per series is also why the
arc and the flat view cannot both exist in a single library: the seeded layout
gives arc seasons *and* absolute episode numbers (`S17E700`), and a second,
fully flat library over a second clone tree is the only way to also see one
continuous season.

## Safety invariants

- Nothing under a source root is ever written, renamed or deleted. `verify`
  checks the clone against the database; `plan` shows every intended change.
- A file at a target that the database does not claim is **refused**, not
  replaced: `refusing to replace` in the report.
- Pruning requires proof of provenance (`MF_PRUNE_REQUIRE_KNOWN_INODE`): a
  symbolic link must name its recorded source, and a hard link (including a
  legacy one from the hard-link era) must still carry the recorded inode. A
  file that was swapped out is skipped rather than deleted.
- `MF_DRY_RUN=true` makes every command read-only, including `watch`.
- One instance at a time (a lock file in the state dir).

## Layout

```
plugins/            one module per source shape (detect + plan)
src/                config, scan, registry, planner, reconciler, linker,
                    liveness, state, nfo, verify, watcher, jellyfin client, cli
service/            Start-MediaForge.ps1, Install-MediaForgeTask.ps1,
                    Uninstall-MediaForgeTask.ps1, Install-JellyfinService.ps1,
                    Install-AdminTask.ps1, admin-run.ps1
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

Boot persistence (runs as the interactive user via S4U — no password stored, no
window, starts at boot):

```powershell
# elevated, once
.\service\Install-MediaForgeTask.ps1 -RunAsUser
Start-ScheduledTask -TaskName MediaForge
```

The task registers two triggers (at startup, and every 5 min as a safety net) and
runs `service\Start-MediaForge.ps1`, which supervises `mediaforge watch` and
appends every exit to `logs\supervisor.log`. Exit code 4 (lock held by a manual
command) sleeps 60 s and retries instead of fighting for the lock.

**Do not pipe the child's stderr into a PowerShell pipeline while
`$ErrorActionPreference = 'Stop'`**: with `2>&1` each stderr line becomes an
ErrorRecord, the first one throws, the pipe closes and python dies — a silent
30-second crash loop that looks exactly like "the watcher keeps restarting"
(measured 2026-11-12). The supervisor redirects to files instead.

Registered as the user rather than SYSTEM on purpose: the state database lives in
`C:\ProgramData\media-forge`, whose `-wal`/`-shm` sidecars are created by whoever
writes first. A SYSTEM-owned sidecar without the user's ACE makes every later
`mediaforge sync` fail with `attempt to write a readonly database` (`SQLITE_READONLY`).

Logs: `C:\ProgramData\media-forge\logs\mediaforge.log` (rotated daily, 14 kept).
State: `C:\ProgramData\media-forge\mediaforge.db`.

## Jellyfin on this host

`service\Install-JellyfinService.ps1` registers Jellyfin 10.11.11 as the
auto-start service `JellyfinServer` (nssm, data dir `C:\ProgramData\Jellyfin\Server`).
It passes only space-free `--datadir/-configdir/-cachedir/-logdir` arguments:
nssm hands `AppParameters` to the Win32 parser unquoted, so `--webdir "C:\Program
Files\..."` arrives as `--webdir C:\Program` and the server exits with
`content directory is either invalid or empty`.

Then `mediaforge jellyfin-check` prints the three library ids; put them in `.env`
as `MF_JELLYFIN_LIB_MOVIES/SHOWS/ANIME` together with `MF_JELLYFIN_API_KEY`
(`POST /Auth/Keys?App=media-forge`). The refresh path is debounced (60 s), waits
for Jellyfin to report idle, and leaves the markers in place if the call fails.
Host-level detail, library options and the firewall rules: `~/.config/opencode/docs/jellyfin.md`.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check src plugins tests
```
