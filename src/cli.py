"""The command line.

Two commands do the work (``sync`` and ``watch``) and the rest exist to make the
tool auditable before and after it runs: ``plan`` shows what would happen,
``verify`` proves what did, ``status`` and ``plugins`` explain the current state,
and ``jellyfin-check`` fills in the library ids.
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, fsutil
from .config import LIBRARIES, ConfigError, Settings, load_settings, validate
from .jellyfin import HttpJellyfinClient, JellyfinError
from .jellyfin_refresh import Refresher
from .linker import Linker
from .logging_setup import configure
from .pipeline import Pipeline, RootOutcome, SyncReport, inspect_sources, plugin_dir
from .plugin_loader import load_plugins
from .scan import ScanError, Scanner
from .state import SingleInstanceLock, SqliteStateStore
from .verify import Verifier, summarise
from .watcher import PollingWatcher

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_CONFIG = 2
EXIT_UNREACHABLE = 3
EXIT_LOCKED = 4


# ── bootstrap ────────────────────────────────────────────────────────────────


def _load(args: argparse.Namespace) -> Settings:
    base = Path(args.root) if getattr(args, "root", None) else None
    settings = load_settings(base)
    for warning in validate(settings):
        print(f"warning: {warning}", file=sys.stderr)
    return settings


def _open_store(settings: Settings) -> SqliteStateStore:
    return SqliteStateStore(settings.state_dir / "mediaforge.db")


def _lock(settings: Settings) -> SingleInstanceLock:
    return SingleInstanceLock(settings.state_dir / "mediaforge.lock")


def _emit(args: argparse.Namespace, payload: Any, lines: Sequence[str]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("\n".join(lines))


# ── formatting ───────────────────────────────────────────────────────────────


def _stamp(ts: object) -> str:
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _root_line(outcome: RootOutcome) -> str:
    head = f"{outcome.root_id:<9} {outcome.plugin_id or '-':<22} {outcome.confidence:4.2f}"
    if outcome.error:
        return f"{head}  error  {_first_line(outcome.error)}"
    if outcome.unchanged:
        return f"{head}  {outcome.scanned:>5} files  unchanged"
    counts = outcome.result.counts if outcome.result else {}
    bits = ", ".join(
        f"{key} {counts[key]}"
        for key in ("created", "repointed", "pruned", "nfo_written", "errors")
        if counts.get(key)
    )
    verdict = outcome.result.outcome if outcome.result else "planned"
    detail = f"  ({bits})" if bits else ""
    return f"{head}  {outcome.scanned:>5} files  {outcome.op_count:>4} ops  {verdict}{detail}"


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def _op_lines(outcome: RootOutcome, limit: int | None) -> list[str]:
    if not outcome.plan:
        return []
    lines: list[str] = []
    ops = outcome.plan.ops if limit is None else outcome.plan.ops[:limit]
    for op in ops:
        detail = op.detail_dict()
        reason = detail.get("reason") or detail.get("relative") or ""
        suffix = f"  [{reason}]" if reason and op.kind != "NFO_WRITE" else ""
        lines.append(f"  {op.kind:<12} {op.target_path}{suffix}")
    if limit is not None and len(outcome.plan.ops) > limit:
        lines.append(f"  ... and {len(outcome.plan.ops) - limit} more")
    return lines


def _sync_payload(report: SyncReport, *, dry_run: bool) -> dict[str, Any]:
    roots = []
    for outcome in report.roots:
        entry: dict[str, Any] = {
            "root_id": outcome.root_id,
            "plugin_id": outcome.plugin_id,
            "confidence": round(outcome.confidence, 4),
            "files": outcome.scanned,
            "ops": outcome.op_count,
            "unchanged": outcome.unchanged,
            "error": outcome.error,
        }
        if outcome.result:
            entry["outcome"] = outcome.result.outcome
            entry["counts"] = outcome.result.counts
            entry["libraries"] = sorted(outcome.result.libraries)
            entry["errors"] = outcome.result.errors
        roots.append(entry)
    return {"dry_run": dry_run, "roots": roots, "errors": report.errors}


# ── commands ─────────────────────────────────────────────────────────────────


def _cmd_init(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.root) if getattr(args, "root", None) else None)
    created: list[str] = []
    for library in LIBRARIES:
        path = settings.clone_root / settings.library_dirs[library]
        if fsutil.make_dirs(path):
            created.append(str(path))
    for path in (settings.state_dir, settings.log_dir):
        if fsutil.make_dirs(path):
            created.append(str(path))

    env_target = settings.repo_root / ".env"
    example = settings.repo_root / "example.env"
    if not env_target.exists() and example.exists():
        shutil.copyfile(example, env_target)
        created.append(str(env_target))

    lines = [f"clone root   {settings.clone_root}"]
    lines += [f"created      {path}" for path in created] or [
        "created      nothing (already present)"
    ]
    lines += [
        f"state dir    {settings.state_dir}",
        "",
        "next:",
        "  1. edit .env (sources, clone root, Jellyfin api key)",
        "  2. mediaforge plugins      # confirm each root is recognised",
        "  3. mediaforge plan         # read the full diff, write nothing",
        "  4. mediaforge sync         # create the links and NFOs",
        "  5. Install-MediaForgeTask  # or: mediaforge watch",
    ]
    _emit(args, {"clone_root": str(settings.clone_root), "created": created}, lines)
    return EXIT_OK


def _run_sync(
    args: argparse.Namespace,
    *,
    dry_run: bool | None,
    list_ops: bool,
    trigger: str,
    all_ops: bool = False,
) -> int:
    settings = _load(args)
    effective_dry_run = settings.dry_run if dry_run is None else dry_run
    logger = configure(settings)
    store = _open_store(settings)
    lock = _lock(settings)
    if not lock.acquire():
        print(
            f"another mediaforge holds {lock.path}; stop it or wait for it to finish",
            file=sys.stderr,
        )
        store.close()
        return EXIT_LOCKED
    try:
        pipeline = Pipeline(settings, store, Linker(), dry_run=effective_dry_run)
        root_ids = tuple(args.source) if getattr(args, "source", None) else None
        report = pipeline.sync(root_ids=root_ids, trigger=trigger)

        lines = [
            f"media-forge {__version__}  {'dry run' if effective_dry_run else 'apply'}"
        ]
        lines += [_root_line(outcome) for outcome in report.roots]
        for outcome in report.roots:
            if outcome.error:
                continue
            error_lines = outcome.result.errors if outcome.result else []
            warnings = outcome.plan.warnings if outcome.plan else []
            for warning in warnings:
                lines.append(f"  warning: {warning}")
            for error in error_lines:
                lines.append(f"  error: {error}")
        if list_ops:
            for outcome in report.roots:
                listed = _op_lines(outcome, None if all_ops else _limit(args))
                if listed:
                    lines.append("")
                    lines.append(f"{outcome.root_id}:")
                    lines += listed
        elif effective_dry_run:
            for outcome in report.roots:
                listed = _op_lines(outcome, _limit(args))
                if listed:
                    lines.append("")
                    lines.append(
                        f"{outcome.root_id} (first {_limit(args)} ops, use 'plan' for all):"
                    )
                    lines += listed

        refreshed = False
        if not effective_dry_run:
            refreshed = _refresh_now(settings, store, logger)
        if refreshed:
            lines.append("jellyfin: refresh requested")

        _emit(args, _sync_payload(report, dry_run=effective_dry_run), lines)
        return EXIT_OK if report.ok else EXIT_PROBLEMS
    finally:
        store.close()
        lock.release()


def _limit(args: argparse.Namespace) -> int:
    return int(getattr(args, "limit", 40) or 40)


def _refresh_now(settings: Settings, store: SqliteStateStore, logger) -> bool:
    """One-shot runs refresh immediately instead of waiting out the debounce."""
    if not settings.jellyfin_refresh or not settings.jellyfin_api_key:
        return False
    client = HttpJellyfinClient(
        settings.jellyfin_url, settings.jellyfin_api_key, timeout_s=5.0
    )
    refresher = Refresher(
        store,
        client,
        library_ids=settings.jellyfin_lib_ids,
        debounce_s=0.0,
        timeout_s=settings.jellyfin_timeout_s,
    )
    outcome = refresher.tick()
    if outcome is None:
        return False
    if not outcome.accepted:
        logger.warning("jellyfin refresh not accepted: %s", outcome.error)
    return outcome.accepted


def _cmd_sync(args: argparse.Namespace) -> int:
    dry_run = True if args.dry_run else (False if args.apply else None)
    return _run_sync(
        args,
        dry_run=dry_run,
        list_ops=False,
        trigger="manual",
    )


def _cmd_plan(args: argparse.Namespace) -> int:
    return _run_sync(args, dry_run=True, list_ops=True, trigger="plan", all_ops=True)


def _cmd_watch(args: argparse.Namespace) -> int:
    settings = _load(args)
    logger = configure(settings)
    store = _open_store(settings)
    lock = _lock(settings)
    if not lock.acquire():
        print(f"another mediaforge holds {lock.path}", file=sys.stderr)
        store.close()
        return EXIT_LOCKED

    dry_run = True if args.dry_run else (False if args.apply else settings.dry_run)
    stop = _stop_event()
    refresher = Refresher(
        store,
        HttpJellyfinClient(settings.jellyfin_url, settings.jellyfin_api_key),
        library_ids=settings.jellyfin_lib_ids,
        debounce_s=settings.jellyfin_debounce_s,
        timeout_s=settings.jellyfin_timeout_s,
    )
    if settings.jellyfin_refresh and not settings.jellyfin_api_key:
        logger.warning("Jellyfin refresh is on but the API key is empty; refreshes are skipped")
    if not settings.jellyfin_refresh:
        logger.info("Jellyfin refresh disabled by MF_JELLYFIN_REFRESH=false")

    scanner = Scanner(settings.exclude_globs)
    watcher = PollingWatcher(
        tuple(source.root_id for source in settings.sources),
        settle_s=settings.settle_s,
        min_gap_s=settings.min_gap_s,
        full_interval_s=settings.full_scan_interval_s,
        startup_grace_s=settings.startup_grace_s,
    )
    watcher.start()
    pipeline = Pipeline(settings, store, Linker(), dry_run=dry_run, interrupt=stop)
    logger.info(
        "watching %d root(s) every %.0fs -> %s (settle %.0fs)",
        len(settings.sources),
        settings.poll_interval_s,
        settings.clone_root,
        settings.settle_s,
    )

    try:
        while not stop.is_set():
            scans = {}
            for source in settings.sources:
                try:
                    scans[source.root_id] = scanner.enumerate_root(source)
                except ScanError as exc:
                    logger.error("scan failed for %s: %s", source.root_id, exc)
            signatures = {root_id: scan.signature for root_id, scan in scans.items()}
            ready = watcher.poll(signatures)
            if ready and not stop.is_set():
                logger.info("reconciling %s", ", ".join(ready))
                report = pipeline.sync(
                    root_ids=tuple(ready), trigger="watch", scans=scans
                )
                for outcome in report.roots:
                    if outcome.error:
                        logger.error("%s: %s", outcome.root_id, _first_line(outcome.error))
                    elif outcome.unchanged:
                        logger.debug("%s: unchanged", outcome.root_id)
                    else:
                        logger.info("%s: %s", outcome.root_id, _root_line(outcome).strip())
                    if outcome.result:
                        for error in outcome.result.errors:
                            logger.error("%s: %s", outcome.root_id, error)
            refresher.tick()
            stop.wait(settings.poll_interval_s)
    except KeyboardInterrupt:
        logger.info("interrupted")
    finally:
        logger.info("stopping")
        store.close()
        lock.release()
    return EXIT_OK


def _stop_event() -> threading.Event:
    stop = threading.Event()

    def handler(_signum: int, _frame: object) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        candidate = getattr(signal, name, None)
        if candidate is None:
            continue
        try:
            signal.signal(candidate, handler)
        except (OSError, ValueError):
            continue
    return stop


def _cmd_status(args: argparse.Namespace) -> int:
    settings = _load(args)
    store = _open_store(settings)
    try:
        libraries = store.counts_by_library()
        roots = store.roots()
        runs = store.recent_runs(5)
        dirty = store.dirty_entries()
        pending = store.pending_count()
    finally:
        store.close()

    lines = [
        f"media-forge {__version__}",
        f"clone root   {settings.clone_root}",
    ]
    for source in settings.sources:
        lines.append(f"source       {source.root_id:<8} {source.path}")
    lines.append(f"dry run      {settings.dry_run}")
    lines.append(
        f"jellyfin     {settings.jellyfin_url} "
        f"(refresh {'on' if settings.jellyfin_refresh else 'off'}, "
        f"key {'set' if settings.jellyfin_api_key else 'empty'})"
    )
    lines.append(f"state        {settings.state_dir}")
    lines.append("")
    if libraries:
        for library in LIBRARIES:
            counts = libraries.get(library)
            if not counts:
                continue
            parts = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
            lines.append(f"library      {library:<7} {parts}")
    else:
        lines.append("library      (no links recorded yet)")
    if pending:
        lines.append(f"pending      {pending} row(s) from an interrupted run")
    if dirty:
        lines.append(
            "dirty        " + ", ".join(f"{lib} since {_stamp(ts)}" for lib, ts in dirty)
        )
    lines.append("")
    if roots:
        for row in roots:
            lines.append(
                f"root         {row['root_id']:<8} {row['last_plugin_id'] or '-':<22} "
                f"last scan {_stamp(row['last_scan_ts'])} "
                f"sig {str(row['last_signature'] or '-')[:12]}"
            )
    if runs:
        lines.append("")
        for row in runs:
            counts = ", ".join(
                f"{key} {row[key]}"
                for key in ("created", "repointed", "pruned", "nfo_written", "errors")
                if row.get(key)
            )
            lines.append(
                f"run #{row['run_id']:<4} {_stamp(row['started_ts'])} "
                f"{row['trigger']:<6} {row['outcome'] or 'running':<11} "
                f"{counts or 'no changes'}"
            )

    payload = {
        "version": __version__,
        "clone_root": str(settings.clone_root),
        "sources": {s.root_id: str(s.path) for s in settings.sources},
        "dry_run": settings.dry_run,
        "jellyfin": {
            "url": settings.jellyfin_url,
            "refresh": settings.jellyfin_refresh,
            "key_set": bool(settings.jellyfin_api_key),
            "dirty": [lib for lib, _ in dirty],
        },
        "libraries": libraries,
        "pending": pending,
        "roots": roots,
        "runs": runs,
    }
    _emit(args, payload, lines)
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    settings = _load(args)
    store = _open_store(settings)
    try:
        report = Verifier(settings, store, Linker()).run(
            include_orphans=not args.no_orphans
        )
    finally:
        store.close()
    lines = summarise(report, limit=int(args.limit))
    payload = {
        "tracked": report.tracked,
        "healthy": report.healthy,
        "clone_files": report.scanned_clone_files,
        "missing_source": [r.target_path for r in report.missing_source],
        "missing_target": [r.target_path for r in report.missing_target],
        "not_linked": [r.target_path for r in report.not_linked],
        "inode_changed": [r.target_path for r in report.inode_changed],
        "missing_nfo": [r.target_path for r in report.missing_nfo],
        "pending": len(report.pending),
        "orphans": report.orphan_files,
        "ok": report.ok,
    }
    _emit(args, payload, lines)
    return EXIT_OK if report.ok else EXIT_PROBLEMS


def _cmd_plugins(args: argparse.Namespace) -> int:
    settings = _load(args)
    plugins = load_plugins(plugin_dir(settings))
    lines = [f"plugin directory {plugin_dir(settings)}"]
    for loaded in plugins:
        state = "ok" if loaded.ok else f"invalid: {loaded.error}"
        identifier = loaded.plugin_id
        version = getattr(loaded.module, "version", "-") if loaded.module else "-"
        priority = getattr(loaded.module, "priority", "-") if loaded.module else "-"
        library = getattr(loaded.module, "default_library", "-") if loaded.module else "-"
        lines.append(
            f"  {identifier:<24} v{version:<4} priority {priority:<3} "
            f"library {library:<6} {state}"
        )
    if not plugins:
        lines.append("  (no plugin modules found)")

    outcomes = inspect_sources(settings, plugins)
    lines.append("")
    for outcome in outcomes:
        lines.append(f"root {outcome.root_id} ({_source_path(settings, outcome.root_id)})")
        if outcome.error:
            for line in outcome.error.splitlines():
                lines.append(f"  {line}")
            continue
        for candidate in outcome.candidates:
            mark = "*" if candidate.plugin_id == outcome.plugin_id else " "
            detail = "; ".join(candidate.reasons) or "no reasons"
            lines.append(
                f" {mark} {candidate.plugin_id:<24} {candidate.confidence:4.2f}  {detail}"
            )
        lines.append(
            f"   -> {outcome.plugin_id} at {outcome.confidence:.2f} "
            f"(floor {settings.plugin_min_confidence:.2f})"
        )

    payload = {
        "plugin_dir": str(plugin_dir(settings)),
        "plugins": [
            {
                "id": loaded.plugin_id,
                "ok": loaded.ok,
                "error": loaded.error,
                "path": str(loaded.path),
            }
            for loaded in plugins
        ],
        "roots": [
            {
                "root_id": outcome.root_id,
                "plugin_id": outcome.plugin_id,
                "confidence": outcome.confidence,
                "error": outcome.error,
                "candidates": [
                    {
                        "plugin_id": candidate.plugin_id,
                        "confidence": candidate.confidence,
                        "reasons": list(candidate.reasons),
                        "error": candidate.error,
                    }
                    for candidate in outcome.candidates
                ],
            }
            for outcome in outcomes
        ],
    }
    _emit(args, payload, lines)
    return EXIT_OK if all(not outcome.error for outcome in outcomes) else EXIT_PROBLEMS


def _source_path(settings: Settings, root_id: str) -> str:
    for source in settings.sources:
        if source.root_id == root_id:
            return str(source.path)
    return "-"


def _cmd_jellyfin_check(args: argparse.Namespace) -> int:
    settings = _load(args)
    if not settings.jellyfin_api_key:
        print(
            "MF_JELLYFIN_API_KEY is empty: create an API key in Jellyfin's dashboard "
            "(Administration -> API Keys) and put it in .env",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    client = HttpJellyfinClient(
        settings.jellyfin_url, settings.jellyfin_api_key, timeout_s=5.0
    )
    if not client.available():
        print(f"Jellyfin is not answering at {settings.jellyfin_url}", file=sys.stderr)
        return EXIT_UNREACHABLE
    try:
        libraries = client.libraries()
    except JellyfinError as exc:
        print(f"cannot read Jellyfin libraries: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE

    lines = [f"Jellyfin at {settings.jellyfin_url}"]
    for library in libraries:
        lines.append(f"  {library.name:<24} {library.collection_type or '-':<8} {library.item_id}")

    mapping = _map_libraries(settings, libraries)
    lines.append("")
    lines.append("add these to .env:")
    for library, item_id in mapping.items():
        lines.append(f"MF_JELLYFIN_LIB_{library.upper()}={item_id}")
    missing = [library for library in LIBRARIES if library not in mapping]
    if missing:
        lines.append("")
        lines.append(f"unmatched: {', '.join(missing)} (name the library folder accordingly)")

    payload = {
        "libraries": [
            {"name": lib.name, "type": lib.collection_type, "id": lib.item_id}
            for lib in libraries
        ],
        "env": {f"MF_JELLYFIN_LIB_{k.upper()}": v for k, v in mapping.items()},
        "unmatched": missing,
    }
    _emit(args, payload, lines)
    return EXIT_OK if not missing else EXIT_PROBLEMS


def _map_libraries(settings: Settings, libraries: list) -> dict[str, str]:
    """Match Jellyfin's virtual folders to this tool's three libraries by name."""
    mapping: dict[str, str] = {}
    wanted = {
        library: settings.library_dirs[library].lower() for library in LIBRARIES
    }
    for library, wanted_name in wanted.items():
        for ref in libraries:
            if ref.name.strip().lower() == wanted_name:
                mapping[library] = ref.item_id
                break
    for library, collection in (
        ("movies", "movies"),
        ("shows", "tvshows"),
    ):
        if library in mapping:
            continue
        for ref in libraries:
            if ref.collection_type == collection or ref.collection_type == collection.rstrip("s"):
                mapping[library] = ref.item_id
                break
    return mapping


# ── parser ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediaforge",
        description="Maintain a Jellyfin-shaped hardlink clone tree beside a media library.",
    )
    parser.add_argument("--version", action="version", version=f"media-forge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, *, sources: bool = True) -> None:
        sp.add_argument(
            "--root",
            help="checkout whose .env to read (default: the installed checkout)",
        )
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        if sources:
            sp.add_argument(
                "--source",
                action="append",
                metavar="ROOT_ID",
                help="limit to one source root (repeatable)",
            )

    init = sub.add_parser("init", help="create the clone tree, state dir and .env")
    common(init, sources=False)
    init.set_defaults(func=_cmd_init)

    plan = sub.add_parser("plan", help="print the full diff and write nothing")
    common(plan)
    plan.add_argument("--limit", type=int, default=40, help=argparse.SUPPRESS)
    plan.set_defaults(func=_cmd_plan)

    sync = sub.add_parser("sync", help="reconcile once")
    common(sync)
    sync.add_argument("--dry-run", action="store_true", help="force a dry run")
    sync.add_argument("--apply", action="store_true", help="force writes, overriding MF_DRY_RUN")
    sync.add_argument("--limit", type=int, default=40, help="ops to list in a dry run")
    sync.set_defaults(func=_cmd_sync)

    watch = sub.add_parser("watch", help="poll the sources and reconcile as they change")
    common(watch)
    watch.add_argument("--dry-run", action="store_true", help="force a dry run")
    watch.add_argument("--apply", action="store_true", help="force writes, overriding MF_DRY_RUN")
    watch.set_defaults(func=_cmd_watch)

    status = sub.add_parser("status", help="state, libraries and the last runs")
    common(status, sources=False)
    status.set_defaults(func=_cmd_status)

    check = sub.add_parser("verify", help="prove the clone tree matches the database")
    common(check, sources=False)
    check.add_argument("--no-orphans", action="store_true", help="skip the clone-tree walk")
    check.add_argument("--limit", type=int, default=20, help="rows shown per problem class")
    check.set_defaults(func=_cmd_verify)

    plugins = sub.add_parser("plugins", help="list plugins and the score for each root")
    common(plugins, sources=False)
    plugins.set_defaults(func=_cmd_plugins)

    jellyfin = sub.add_parser("jellyfin-check", help="print the .env library ids")
    common(jellyfin, sources=False)
    jellyfin.set_defaults(func=_cmd_jellyfin_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except JellyfinError as exc:
        print(f"jellyfin: {exc}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_PROBLEMS


if __name__ == "__main__":
    raise SystemExit(main())
