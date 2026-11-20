# media-forge — agent context

Maintains a Jellyfin-shaped **clone tree** beside an existing media library. Every
clone entry is a symbolic link, so deleting a source frees its bytes at once and
the dangling entry is swept within seconds. It is the non-destructive replacement for
`streaming-community`'s deleted `src/tools/migrate_jellyfin.py`, which restructured
the source in place. **Never call, restore or re-implement that tool.**

## Contract

- Source trees are read-only. A test fails if any file under a source root is
  modified, renamed or deleted (`tests/test_pipeline.py`).
- A target that the database does not claim is never replaced or pruned.
- `plan` must always equal what `sync` then does. A new operation kind needs a
  planner branch, a reconciler branch, and a test that observes both.
- `MF_LINK_MODE=symlink` is the default and the contract: a clone entry holds no
  bytes, so a deleted source frees them immediately. `MF_LINK_MODE=hardlink` is
  the pre-2026-11-13 behaviour and stays supported (same volume only,
  `src/config.py::validate` enforces it). A copy is never an acceptable fallback
  (it doubles 670 GB of media).

## Layout

| path | role |
|---|---|
| `src/config.py` | `.env` + env → `Settings`; `validate()` is the only place that may reject startup |
| `src/scan.py` | one `stat` walk per root; `signature` drives polling |
| `src/registry.py` | confidence competition between plugins |
| `src/planner.py` | diff → ordered `Op` list; owns every prune decision |
| `src/reconciler.py` | executes `Op`s; journaled, row-before-syscall |
| `src/liveness.py` | prunes entries whose source is gone, on its own clock |
| `src/state.py` | SQLite: provenance, NFO hashes, run journal, lock |
| `src/naming/seasons.py` | absolute episode numbers → a provider's season layout |
| `plugins/*.py` | one module per source shape (`detect` + `plan`) |
| `plugins/anime_seasons.json` | season-layout snapshots, one entry per mapped show |
| `service/*.ps1` | boot persistence via Task Scheduler |

## Conventions

- uv-managed, Python 3.11, ruff line length 100 (`E,F,W,I,B,UP`), pytest.
- `src/` package, tests import it as `from src.x import y` (there is a
  `tests/__init__.py` so the suites can share `conftest` helpers).
- Functions small, no mutation of dataclass inputs, no `print` in library code
  (the CLI prints; everything else logs through `mediaforge`).

## Environment on this host

- Real sources: `D:\Shows\streaming-community` (movies + series),
  `D:\Shows\animeunity` (absolute-numbered anime; One Piece is mapped to its TMDB
  arc seasons, the other shows stay flat). Clone root: `D:\Media\Jellyfin`.
- State/logs: `C:\ProgramData\media-forge`. Scheduled task name: `MediaForge`
  (runs as **the user**, S4U, at startup + every 5 min).
- Running `sync`/`watch` while another instance runs returns exit code 4 — that is
  the lock, not a bug.

## Traps measured on this host (do not re-learn them)

- **Jellyfin's `DisplayOrder` is sticky and an NFO cannot clear it.**
  `MetadataService.MergeDisplayOrder` only assigns when the *source* value is
  non-empty, even with `ReplaceAllMetadata` — so a stored `absolute` survives any
  number of rescan and refresh runs. Changing it means `POST /Items/{id}` with
  `DisplayOrder` set (which queues a full refresh itself), and that endpoint writes
  every other field unconditionally, so round-trip the full DTO from `GET /Items/{id}`
  instead of posting a partial one. The valid values for the TMDB provider are
  `originalAirDate|absolute|dvd|digital|storyArc|production|tv`; empty = the
  provider's own seasons. **Never write `aired`** — it is the episode NFO's
  premiere-date element, not a display order. One Piece (series `864fc5f1…`) carried
  `absolute` from the flat era and was cleared this way on 2026-11-12; its seasons are
  now 49/55/47 episodes (TMDB arcs 17–19) with absolute episode numbers inside them.
- **Arc seasons arrive unnamed unless TMDb's `ImportSeasonName` is on.** The plugin
  ships it `false` (`Jellyfin.Plugin.Tmdb.xml`), so a mapped show reads "Season 17";
  `POST /Plugins/b8715ed1-6c47-4528-9ad3-f72deb539cd4/Configuration` with
  `ImportSeasonName=true` plus a `FullRefresh` of each season renames them to
  "Saga Di Dressrosa (2)" / "Saga di Zo" (TMDB in the library's metadata language;
  `en-US` gives "Dressrosa (2)").
- **A moved target is one write *and* one prune under the same source key.** A season
  change plans `LINK_CREATE` at the new path and `FILE_PRUNE` of the old one, both
  carrying that source; links reconcile before prunes, so deleting the row by source
  alone discards the row the same run just wrote — 151 One Piece episodes moved into
  arc seasons and `verify` reported all 303 files as orphans (2026-11-12).
  `Reconciler._forget_pruned` deletes only while the row still names the pruned path,
  and `_prune_ops` skips a target `build()` already pruned, so one move counts once
  (it reported `pruned 302` for 151 files before that).
- **An identical but unclaimed target is adopted, not orphaned.** `_link_kind` returns
  `LINK_ADOPT` when the target is already the link the tool would create (a symbolic
  link naming the source, or a hard link of it) and has no row. Without it `plan` is
  a no-op and an orphan can never be reclaimed - a `verify`-red tree that no number
  of syncs repairs. The hard-link branch keeps the old `st_nlink > 1` safety line:
  `st_nlink == 1` means the source was *moved* into the clone, and adopting it would
  let a later prune delete its only copy.
- **A symbolic link needs `SeCreateSymbolicLinkPrivilege`.** An elevated process or
  Windows Developer Mode has it; a Medium-integrity shell has neither, and
  `os.symlink` fails with WinError 1314. `mediaforge status` prints the mode and
  `validate()` warns. Never run `sync` from a plain shell against a real clone root
  in symlink mode: it would unlink a target and then fail to recreate it. The
  `MediaForge` task runs elevated, so it is the only writer that matters.
- **Legacy hard links are reclaimed through the recorded inode.** Rows written
  before the symlink switch record the file inode, not a link target;
  `fsutil.entry_owned_by` accepts either a link naming the source or a hard link
  whose inode is the recorded one. Without that branch every orphan from the
  hard-link era is refused as "not a link this tool created" and keeps its bytes.
- **A symbolic link does not disappear when its target does.** It dangles, and
  Jellyfin reports it as an error, so `src/liveness.py` sweeps rows whose source has
  been missing for `MF_LIVENESS_GRACE_SECONDS` (default 2 s, checked every 1 s),
  then removes the folder NFO and the now-empty directory. The grace window is what
  keeps a downloader's delete-and-recreate from flapping the mirror.
- **The chain must not depend on the database remembering the entry.** The row
  sweep answers "does this row's source still exist"; a second pass walks the clone
  tree every `MF_ORPHAN_SCAN_INTERVAL_SECONDS` (default 60 s) and removes a
  dangling link whose target is under a source root even when no row explains it -
  after a restored `mediaforge.db`, or a run killed between the link and its row.
  It is also the only thing that clears a `movie.nfo` left in a title folder whose
  rows were pruned by a process that then died. Provenance there is the link's own
  target, never its absence from the table.
- **A source root that is not readable suspends pruning for everything under it.**
  An unplugged drive reads exactly like "every file was deleted", and 1 100 rows
  would be dropped (and every library marked dirty) before anyone noticed. The
  sweep skips a root whose path is not a directory, reports `paused_roots`, and
  resumes on its own; the watch log prints the transition once.
- **`repointed N` on nearly every shows run is `streaming-community` rewriting the
  source, not a link bug.** `refetch --resolution-only` replaces a file (new inode)
  and the clone keeps the old one until the next pass relinks it, so a steady trickle
  is expected while that process runs.
- **Never pipe a native child's stderr into a pipeline with
  `$ErrorActionPreference='Stop'`.** `& python ... 2>&1 | ForEach-Object` turns
  each stderr line into an ErrorRecord, the first throws, the pipe closes and the
  child dies: a 30 s crash loop whose only trace is a repeating "watching N root(s)"
  line. `service\Start-MediaForge.ps1` redirects to `logs\watch.out|err` instead.
- **Task identity and the SQLite sidecars must match.** `mediaforge.db-wal/-shm`
  are created by whoever writes first; a SYSTEM-owned sidecar without the user's
  ACE makes every user-side write fail with `attempt to write a readonly database`
  (`SQLITE_READONLY`, 8 — distinct from `CANTLOCK`, 1032). Grant
  `DESKTOP-1EHVJ82\Giulio:(OI)(CI)M` on the state dir and keep the task as the user.
- **nssm passes `AppParameters` through the Win32 parser unquoted**, so an argument
  value with a space truncates at it. `Install-JellyfinService.ps1` passes only
  space-free paths and relies on `AppDirectory` for the rest.
- Elevated work goes through the `MediaForgeAdmin` scheduled task (RunLevel Highest,
  S4U): drop a script at `C:\ProgramData\media-forge\admin\job.ps1` and
  `Start-ScheduledTask -TaskName MediaForgeAdmin`; output lands in `admin\job.log`,
  and the job file is deleted before it runs to completion, so it cannot re-run.
  That task is a standing privilege boundary — remove it with
  `Unregister-ScheduledTask -TaskName MediaForgeAdmin -Confirm:$false`.
