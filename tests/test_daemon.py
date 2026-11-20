"""The daemon decides *when*, never *what*: every action is a CLI verb in a child.

These tests pin the rule that makes the split safe - the supervisor owns no
state, holds no lock, and claims a run only when the child said it happened - and
the three inputs that decide when: a changed signature (a share can give nothing
else), a filesystem event (a hint), and the two safety nets (a full pass, an idle
sweep).

Nothing here starts a real child: the runner is a recorder, so the assertions are
about argv and exit codes, which is exactly the contract between the two halves.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mediaforge import daemon as daemon_module
from mediaforge.daemon import IDLE_WAIT_S, RETRY_MIN_S, Daemon, SubprocessRunner
from mediaforge.events import Event, EventKind
from mediaforge.exitcodes import EXIT_CONFIG, EXIT_LOCKED, EXIT_OK

from .conftest import Fixture, build_library


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSource:
    """No OS events: every root is polled, and ``wait`` never produces anything."""

    name = "fake"

    def __init__(self, polled: tuple[str, ...] = ("shows", "anime")) -> None:
        self.polled = frozenset(polled)
        self.watched = frozenset()
        self.started = False
        self.timeout: float | None = None

    def start(self) -> None:
        self.started = True

    def wait(self, timeout_s: float) -> tuple[Event, ...]:
        self.timeout = timeout_s
        return ()

    def stop(self) -> None:
        return None


class FakeScanner:
    """Signatures the test can rewrite between ticks.

    The dict is **shared** with the caller on purpose: a copy would make the test
    mutate a map the daemon never reads, and every "nothing changed" assertion
    would pass for the wrong reason.
    """

    def __init__(self, signatures: dict[str, str]) -> None:
        self.signatures = signatures

    def enumerate_root(self, source) -> SimpleNamespace:  # noqa: ANN001 - duck-typed
        return SimpleNamespace(signature=self.signatures[source.root_id])


class Runner:
    """Records argv and answers with the queued exit codes."""

    def __init__(self, codes: tuple[int, ...] = ()) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.codes = list(codes)

    def __call__(self, argv: tuple[str, ...]) -> int:
        self.calls.append(argv)
        return self.codes.pop(0) if self.codes else EXIT_OK


def _daemon(
    tmp_path: Path,
    clock: Clock,
    *,
    codes: tuple[int, ...] = (),
    library: Fixture | None = None,
    **overrides: object,
) -> tuple[Daemon, Runner, dict[str, str]]:
    library = library or build_library(tmp_path)
    options: dict[str, object] = {
        "settle_s": 10.0,
        "min_gap_s": 0.0,
        "poll_interval_s": 15.0,
        "full_scan_interval_s": 21600.0,
        "liveness_interval_s": 30.0,
        "liveness_grace_s": 2.0,
        "startup_grace_s": 0.0,
    }
    options.update(overrides)
    settings = replace(library.settings, **options)
    signatures = {"shows": "a", "anime": "a"}
    runner = Runner(codes)
    daemon = Daemon(
        settings,
        runner=runner,
        logger=logging.getLogger("mediaforge"),
        event_source=FakeSource(),
        clock=clock,
    )
    daemon.scanner = FakeScanner(signatures)
    return daemon, runner, signatures


# ── the loop ────────────────────────────────────────────────────────────────


def test_the_first_tick_is_a_full_pass(tmp_path: Path) -> None:
    """A restart reconciles everything without waiting for an event."""
    daemon, runner, _ = _daemon(tmp_path, Clock())
    daemon.tick()
    assert runner.calls == [("sync", "--full")]


def test_an_idle_sweep_runs_on_its_own_clock(tmp_path: Path) -> None:
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock)
    daemon.tick()  # full pass
    daemon.tick()  # the idle sweep is due at startup
    assert runner.calls[-1] == ("sweep", "--grace", "0")
    clock.advance(29)
    daemon.tick()
    assert runner.calls.count(("sweep", "--grace", "0")) == 1
    clock.advance(2)
    daemon.tick()
    assert runner.calls.count(("sweep", "--grace", "0")) == 2


def test_a_changed_signature_syncs_only_that_root(tmp_path: Path) -> None:
    clock = Clock()
    daemon, runner, signatures = _daemon(tmp_path, clock)
    daemon.tick()  # full pass
    daemon.tick()  # idle sweep
    clock.advance(1)
    daemon.tick()  # baseline: a first signature is not a change
    assert not [call for call in runner.calls if call[0] == "sync"][1:]

    signatures["shows"] = "b"
    clock.advance(16)
    daemon.tick()  # the poll sees the change and arms the root
    assert runner.calls[-1] != ("sync", "--source", "shows")
    clock.advance(11)
    daemon.tick()  # settled
    assert runner.calls[-1] == ("sync", "--source", "shows")


def test_an_event_arms_a_root_without_waiting_for_the_poll(tmp_path: Path) -> None:
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock)
    daemon.tick()
    daemon.tick()
    daemon.absorb((Event(EventKind.CREATED, "shows"),))
    clock.advance(11)
    daemon.tick()
    assert runner.calls[-1] == ("sync", "--source", "shows")


def test_a_delete_waits_the_grace_window_before_sweeping(tmp_path: Path) -> None:
    """A downloader's delete-and-recreate must not flap the mirror."""
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock, settle_s=60.0)
    daemon.tick()
    daemon.tick()
    runner.calls.clear()

    daemon.absorb((Event(EventKind.DELETED, "shows"),))
    clock.advance(1)
    daemon.tick()
    assert runner.calls == []  # the EVENT is not the pruning decision
    clock.advance(1)
    daemon.tick()
    assert runner.calls == [("sweep", "--grace", "0")]


def test_an_overflow_forces_a_full_pass_and_a_sweep(tmp_path: Path) -> None:
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock)
    daemon.tick()
    daemon.tick()
    runner.calls.clear()

    daemon.absorb((Event(EventKind.OVERFLOW, "shows"),))
    daemon.tick()
    assert runner.calls == [("sync", "--full")]
    daemon.tick()
    assert runner.calls[-1] == ("sweep", "--grace", "0")


def test_a_failing_root_backs_off_instead_of_spinning(tmp_path: Path) -> None:
    """Only a child that exited 0 counts as a run, and a repeat is not free.

    Without the backoff a root that can never be planned (a plugin refusing the
    shape) would spawn a child and walk the whole tree on the settle clock forever.
    """
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock, codes=(EXIT_OK, 1), liveness_interval_s=0)
    daemon.tick()  # full pass
    daemon.absorb((Event(EventKind.CREATED, "shows"),))
    clock.advance(11)
    daemon.tick()  # settled -> one attempt, one failure
    assert runner.calls[-1] == ("sync", "--source", "shows")
    assert daemon.stats.failures == 1

    clock.advance(RETRY_MIN_S - 1)
    daemon.tick()  # the settle window is not the retry clock any more
    assert len(runner.calls) == 2

    clock.advance(1)
    daemon.tick()  # backoff expired: re-armed, settle window restarts
    assert len(runner.calls) == 2
    clock.advance(11)
    daemon.tick()
    assert len(runner.calls) == 3

    clock.advance(RETRY_MIN_S)
    daemon.tick()  # the second failure doubled the delay to 60 s
    assert len(runner.calls) == 3


def test_an_event_cannot_pull_a_failing_root_forward(tmp_path: Path) -> None:
    """A downloader's event storm must not defeat the backoff it is failing under."""
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock, codes=(EXIT_OK, 1), liveness_interval_s=0)
    daemon.tick()
    daemon.absorb((Event(EventKind.CREATED, "shows"),))
    clock.advance(11)
    daemon.tick()
    assert len(runner.calls) == 2

    for _ in range(5):
        daemon.absorb((Event(EventKind.CREATED, "shows"),))
        clock.advance(2)
        daemon.tick()
    assert len(runner.calls) == 2  # still inside the 30 s backoff


def test_one_failing_root_does_not_penalise_the_other(tmp_path: Path) -> None:
    """Each root is its own child, so its exit code is attributable."""
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock, codes=(EXIT_OK, 1, EXIT_OK), liveness_interval_s=0)
    daemon.tick()  # full pass
    daemon.absorb((Event(EventKind.CREATED, "shows"), Event(EventKind.CREATED, "anime")))
    clock.advance(11)
    daemon.tick()
    assert len(runner.calls) == 3  # full pass + one child per root
    assert len(daemon._retry) == 1  # noqa: SLF001 - exactly one root is backing off


def test_a_success_clears_the_backoff(tmp_path: Path) -> None:
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock, liveness_interval_s=0)
    daemon.tick()  # full pass
    daemon._retry["shows"] = (4, clock.now + 5)  # noqa: SLF001 - a failing history

    clock.advance(6)
    daemon.tick()  # the backoff expired, so the root is armed again
    assert len(runner.calls) == 1

    clock.advance(11)
    daemon.tick()  # settled, and this time the child exits 0
    assert runner.calls[-1] == ("sync", "--source", "shows")
    assert "shows" not in daemon._retry  # noqa: SLF001


def test_a_successful_sync_is_not_repeated(tmp_path: Path) -> None:
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock, liveness_interval_s=0)
    daemon.tick()
    daemon.absorb((Event(EventKind.CREATED, "shows"),))
    clock.advance(11)
    daemon.tick()
    assert runner.calls[-1] == ("sync", "--source", "shows")

    clock.advance(11)
    daemon.tick()
    assert len(runner.calls) == 2


def test_a_failed_full_pass_backs_off_and_tries_again(tmp_path: Path) -> None:
    clock = Clock()
    daemon, runner, _ = _daemon(tmp_path, clock, codes=(1, 1), liveness_interval_s=0)
    daemon.tick()
    assert runner.calls == [("sync", "--full")]
    assert daemon.stats.full_passes == 1
    assert daemon.stats.failures == 1

    daemon.tick()
    assert runner.calls == [("sync", "--full")]  # not retried on the next tick
    assert daemon.full_wanted is True
    clock.advance(RETRY_MIN_S)
    daemon.tick()
    assert runner.calls == [("sync", "--full"), ("sync", "--full")]
    assert daemon.stats.failures == 2


def test_a_locked_child_is_not_a_failure(tmp_path: Path) -> None:
    """Exit 4 is another instance holding the lock, not a defect in this run."""
    daemon, runner, _ = _daemon(tmp_path, Clock(), codes=(EXIT_LOCKED,), liveness_interval_s=0)
    daemon.tick()
    assert runner.calls == [("sync", "--full")]
    assert daemon.stats.failures == 0
    assert daemon.full_wanted is True


def test_the_daemon_refuses_to_start_with_watching_off(tmp_path: Path) -> None:
    daemon, runner, _ = _daemon(tmp_path, Clock(), watch_mode="off")
    assert daemon.run() == EXIT_CONFIG
    assert runner.calls == []


def test_the_wait_is_bounded(tmp_path: Path) -> None:
    daemon, _runner, _ = _daemon(tmp_path, Clock())
    daemon.tick()
    assert 0.0 <= daemon.wait_timeout() <= IDLE_WAIT_S


def test_an_armed_root_shortens_the_wait_until_it_settles(tmp_path: Path) -> None:
    clock = Clock()
    daemon, _runner, _ = _daemon(tmp_path, clock)
    daemon.tick()  # full pass
    daemon.tick()  # the idle sweep, so its own deadline is in the future
    daemon.absorb((Event(EventKind.CREATED, "shows"),))
    assert daemon.wait_timeout() == pytest.approx(10.0)


# ── the subprocess boundary ──────────────────────────────────────────────────


def test_the_child_runs_the_same_checkout(tmp_path: Path, monkeypatch) -> None:
    library = build_library(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(cmd, cwd=None, check=False):  # noqa: ANN001 - subprocess.run's shape
        seen["cmd"] = cmd
        seen["cwd"] = cwd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(daemon_module.subprocess, "run", fake_run)
    runner = SubprocessRunner(library.settings, extra_args=("--apply",))
    assert runner(("sync", "--source", "shows")) == 0

    cmd = seen["cmd"]
    assert cmd[:3] == [sys.executable, "-m", "mediaforge"]
    assert "--root" in cmd and "--apply" in cmd
    assert cmd[3:5] == ["sync", "--source"]
    assert seen["cwd"] == str(library.settings.repo_root)


def test_the_supervisor_holds_no_state_and_takes_no_lock() -> None:
    """The contract, as text: nothing here may learn to open the database.

    A second writer in the supervisor would race the children for the lock and
    for the journal, and every log line would stop naming a command you can run.
    """
    source = Path(daemon_module.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]  # ignore the module docstring
    for forbidden in ("SqliteStateStore", "SingleInstanceLock", "Reconciler", "Pipeline"):
        assert forbidden not in body
