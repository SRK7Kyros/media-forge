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
- State/logs: `C:\ProgramData\media-forge`. Scheduled task name: `MediaForge`.
- Running `sync`/`watch` while another instance runs returns exit code 4 — that is
  the lock, not a bug.
