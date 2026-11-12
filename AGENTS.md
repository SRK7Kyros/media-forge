# media-forge — agent context

Maintains a Jellyfin-shaped **hardlink** clone tree beside an existing media
library. It is the non-destructive replacement for
`streaming-community`'s deleted `src/tools/migrate_jellyfin.py`, which restructured
the source in place. **Never call, restore or re-implement that tool.**

## Contract

- Source trees are read-only. A test fails if any file under a source root is
  modified, renamed or deleted (`tests/test_pipeline.py`).
- A target that the database does not claim is never replaced or pruned.
- `plan` must always equal what `sync` then does. A new operation kind needs a
  planner branch, a reconciler branch, and a test that observes both.
- Hardlinks only, same volume only. `src/config.py::validate` enforces it; a copy
  is never an acceptable fallback (it doubles 670 GB of media).

## Layout

| path | role |
|---|---|
| `src/config.py` | `.env` + env → `Settings`; `validate()` is the only place that may reject startup |
| `src/scan.py` | one `stat` walk per root; `signature` drives polling |
| `src/registry.py` | confidence competition between plugins |
| `src/planner.py` | diff → ordered `Op` list; owns every prune decision |
| `src/reconciler.py` | executes `Op`s; journaled, row-before-syscall |
| `src/state.py` | SQLite: provenance, NFO hashes, run journal, lock |
| `plugins/*.py` | one module per source shape (`detect` + `plan`) |
| `service/*.ps1` | boot persistence via Task Scheduler |

## Conventions

- uv-managed, Python 3.11, ruff line length 100 (`E,F,W,I,B,UP`), pytest.
- `src/` package, tests import it as `from src.x import y` (there is a
  `tests/__init__.py` so the suites can share `conftest` helpers).
- Functions small, no mutation of dataclass inputs, no `print` in library code
  (the CLI prints; everything else logs through `mediaforge`).

## Environment on this host

- Real sources: `D:\Shows\streaming-community` (movies + series),
  `D:\Shows\animeunity` (absolute-numbered anime). Clone root: `D:\Media\Jellyfin`.
- State/logs: `C:\ProgramData\media-forge`. Scheduled task name: `MediaForge`
  (runs as **the user**, S4U, at startup + every 5 min).
- Running `sync`/`watch` while another instance runs returns exit code 4 — that is
  the lock, not a bug.

## Traps measured on this host (do not re-learn them)

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
