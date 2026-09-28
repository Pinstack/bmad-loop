"""The stop-aware child runner (DW-353): the probe, the tree kill, and the
exception-safe teardown behind verify commands and declarative hooks.

The real-process rows drive a genuine process TREE — a shell (or cmd.exe) root
with a grandchild — because the defect this seam fixes is exactly what a
single-process fake cannot show: killing the root alone orphans the grandchild.
The fake-host rows pin the ordering doctrine (harvest before the first signal,
win32 force-kills the root before anything polite) that no single platform can
exercise both arms of.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bmad_loop import childrun, runs
from bmad_loop.process_host import get_process_host

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="drives a /bin/sh process tree")
WIN32_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="drives a cmd.exe process tree")

# A hard-stop-to-return ceiling for the real rows: one poll + four kill steps
# (two root waits, the survivor sub-harvest, the reap) + the drain is ~5.25 s by
# construction; runs._STOP_WAIT_S (10 s) is the budget the
# stop path actually has. Asserting under that budget, not the construction, keeps
# the row about the contract rather than about a slow CI host.
_RETURN_CEILING_S = runs._STOP_WAIT_S - 2.0


@contextlib.contextmanager
def stop_probe(probe: Callable[[], bool]) -> Iterator[None]:
    token = childrun.install_stop_probe(probe)
    try:
        yield
    finally:
        childrun.reset_stop_probe(token)


def _read_pid(pid_file: Path) -> int | None:
    try:
        text = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return int(text) if text.isdigit() else None


def _pid_written(pid_file: Path) -> Callable[[], bool]:
    """A probe that flips True once the grandchild has recorded its pid — the
    point at which the tree is known to be fully formed."""
    return lambda: _read_pid(pid_file) is not None


def _gone(pid: int) -> bool:
    """Dead or a zombie awaiting its (new) parent's reap — either way no longer
    running anything."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError):
            return True
        return stat.rsplit(")", 1)[1].split()[0] in ("Z", "X")
    return not get_process_host().is_alive(pid)


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return _gone(pid)


@pytest.fixture
def reap_leftovers() -> Iterator[list[int]]:
    """Pids a failing row may have leaked; force-killed at teardown so a red row
    never leaves a 60 s sleeper behind."""
    pids: list[int] = []
    yield pids
    host = get_process_host()
    for pid in pids:
        if not _gone(pid):
            with contextlib.suppress(Exception):
                host.force_kill(pid)


def _sh_tree(pid_file: Path) -> str:
    """A /bin/sh root with a backgrounded grandchild that records its pid. Two
    statements plus `wait`, so sh cannot exec the command in place of itself."""
    return f"sleep 60 & echo $! > '{pid_file}'; wait"


# ---- the probe -------------------------------------------------------------------


def test_no_probe_means_never_pending():
    assert childrun.hard_stop_pending() is False


def test_probe_is_scoped_by_its_token():
    with stop_probe(lambda: True):
        assert childrun.hard_stop_pending() is True
    assert childrun.hard_stop_pending() is False


def test_completed_child_reports_its_output_and_rc(tmp_path):
    script = tmp_path / "streams.py"
    script.write_text(
        "import sys\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit(3)\n",
        encoding="utf-8",
    )
    with stop_probe(lambda: False):
        run = childrun.run_child(f'"{sys.executable}" "{script}"', cwd=tmp_path, timeout=30)
    assert run == childrun.ChildRun(3, "out\n", "err\n")


def test_pending_hard_stop_spawns_nothing(tmp_path, monkeypatch):
    """A hard request already pending interrupts before spawn: nothing starts.

    Ablation: drop the pre-spawn probe read and the Popen tripwire fires."""

    def no_spawn(*_args, **_kwargs):
        raise AssertionError("a pending hard stop must spawn nothing")

    monkeypatch.setattr(childrun.subprocess, "Popen", no_spawn)
    with stop_probe(lambda: True):
        run = childrun.run_child("exit 0", cwd=tmp_path, timeout=30)
    assert run == childrun.ChildRun(None, "", "", timed_out=False, interrupted=True)


def test_graceful_request_does_not_interrupt(tmp_path):
    """The engine's probe is mode-exact: a pending GRACEFUL request lets the
    child run to completion, exactly as before DW-353."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / runs.STOP_REQUEST_FILE).write_text(
        json.dumps({"mode": "graceful"}), encoding="utf-8"
    )
    assert runs.read_stop_request_mode(run_dir) == "graceful"
    script = tmp_path / "slowish.py"
    script.write_text("import time\ntime.sleep(0.6)\nprint('finished')\n", encoding="utf-8")

    with stop_probe(lambda: runs.read_stop_request_mode(run_dir) == "hard"):
        run = childrun.run_child(f'"{sys.executable}" "{script}"', cwd=tmp_path, timeout=30)

    assert run.interrupted is False and run.timed_out is False
    assert run.returncode == 0 and run.stdout == "finished\n"


# ---- real POSIX trees ------------------------------------------------------------


@POSIX_ONLY
def test_hard_stop_kills_the_whole_sh_tree(tmp_path, reap_leftovers):
    """The defect verbatim: before DW-353 only the sh root died, orphaning the
    grandchild. The probe flips once the grandchild exists; the runner must
    return interrupted, promptly, with the grandchild dead.

    Ablation: skip `_reap_descendants` in `kill_tree` and the grandchild
    survives (sh dies on SIGTERM, `sleep` is reparented and keeps running)."""
    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    with stop_probe(_pid_written(pid_file)):
        run = childrun.run_child(_sh_tree(pid_file), cwd=tmp_path, timeout=120)
    elapsed = time.monotonic() - started
    grandchild = _read_pid(pid_file)
    assert grandchild is not None
    reap_leftovers.append(grandchild)

    assert run.interrupted is True and run.timed_out is False
    assert elapsed < _RETURN_CEILING_S
    assert _wait_gone(grandchild), f"grandchild {grandchild} survived the hard stop"


@POSIX_ONLY
def test_timeout_kills_the_whole_sh_tree(tmp_path, reap_leftovers):
    """The timeout leg tree-kills too; `subprocess.run` killed only the root."""
    pid_file = tmp_path / "grandchild.pid"
    with stop_probe(lambda: False):
        run = childrun.run_child(_sh_tree(pid_file), cwd=tmp_path, timeout=1.0)
    grandchild = _read_pid(pid_file)
    assert grandchild is not None
    reap_leftovers.append(grandchild)

    assert run.timed_out is True and run.interrupted is False
    assert _wait_gone(grandchild), f"grandchild {grandchild} survived the timeout"


@POSIX_ONLY
@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, RuntimeError])
def test_exception_unwinding_kills_the_tree_and_reraises(tmp_path, monkeypatch, exc_type):
    """Whatever unwinds through the poll loop — SIGTERM's `RunStopped`, a raw
    `KeyboardInterrupt` — kills the tree in `finally` and propagates unchanged.

    Raised from the probe, which is exactly where a signal handler's exception
    lands in practice (the loop spends its time between probe reads).

    Ablation: drop the `finally` kill and both the root and the grandchild
    survive the raise."""
    pid_file = tmp_path / "grandchild.pid"
    spawned: list[subprocess.Popen[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(childrun.subprocess, "Popen", recording_popen)

    def exploding_probe() -> bool:
        if _read_pid(pid_file) is not None:
            raise exc_type("unwinding")
        return False

    with stop_probe(exploding_probe), pytest.raises(exc_type, match="unwinding"):
        childrun.run_child(_sh_tree(pid_file), cwd=tmp_path, timeout=120)

    (root,) = spawned
    grandchild = _read_pid(pid_file)
    assert grandchild is not None
    try:
        assert root.poll() is not None, "the root survived the unwinding exception"
        assert _wait_gone(grandchild), f"grandchild {grandchild} survived the unwind"
    finally:
        with contextlib.suppress(Exception):
            get_process_host().force_kill(grandchild)


@POSIX_ONLY
def test_timeout_reaches_the_job_of_an_exited_root(tmp_path, monkeypatch, reap_leftovers):
    """DW-477, exited root: the shell backgrounds a job, lives across a loop
    harvest, then exits, leaving the job holding the pipes. The timeout finds the
    root already reaped — nothing is enumerable from its pid any more — so only
    the tree the loop accumulated while it ran can reach the job.

    Ablation: drop the poll-loop harvest in `run_child` (or pass no `known` to
    `kill_tree`) and the job survives the timeout."""
    monkeypatch.setattr(childrun, "HARVEST_S", 0.1)
    pid_file = tmp_path / "job.pid"
    started = time.monotonic()
    with stop_probe(lambda: False):
        run = childrun.run_child(
            f"sleep 60 & echo $! > '{pid_file}'; sleep 0.6", cwd=tmp_path, timeout=1.5
        )
    elapsed = time.monotonic() - started
    job = _read_pid(pid_file)
    assert job is not None
    reap_leftovers.append(job)

    assert run.timed_out is True and run.interrupted is False
    # anti-vacuity: rc 0 means the root exited on its own before the timeout; a
    # root still up there (-SIGTERM) lets the pre-signal harvest reach the job
    assert run.returncode == 0, "the root was still running at the timeout"
    assert _wait_gone(job), f"job {job} survived the timeout of its exited root"
    assert elapsed < 1.5 + 4 * childrun.KILL_WAIT_S + childrun.DRAIN_S + 2.0


@POSIX_ONLY
def test_unwinding_kill_reaches_the_job_of_an_exited_root(tmp_path, monkeypatch, reap_leftovers):
    """The common POSIX hard stop: the engine's SIGTERM handler raises
    `RunStopped` through the poll loop, so the kill is the `finally` one. It must
    get the known tree too — the root has already exited, so without it nothing
    reaches the job it left holding the pipes.

    Ablation: call `kill_tree(proc)` without `known` in `run_child`'s `finally`
    and the job survives."""
    monkeypatch.setattr(childrun, "HARVEST_S", 0.1)
    pid_file = tmp_path / "job.pid"
    started = time.monotonic()

    def late_raise() -> bool:
        if time.monotonic() - started >= 1.2:  # the root exited at ~0.6 s
            raise RuntimeError("unwinding")
        return False

    with stop_probe(late_raise), pytest.raises(RuntimeError, match="unwinding"):
        childrun.run_child(
            f"sleep 60 & echo $! > '{pid_file}'; sleep 0.6", cwd=tmp_path, timeout=120
        )
    job = _read_pid(pid_file)
    assert job is not None
    reap_leftovers.append(job)

    assert _wait_gone(job), f"job {job} survived the unwinding kill of its exited root"


@POSIX_ONLY
def test_hard_stop_reaches_a_process_forked_in_the_roots_grace(tmp_path, reap_leftovers):
    """DW-477, fork during grace: the root traps TERM and, in the trap, forks a
    job and then exits. The job did not exist at the pre-signal harvest; only the
    re-harvest while the root waits out its grace sees it before the root's exit
    reparents it.

    Ablation: drop the re-harvest in `_grace_wait` and the job survives."""
    ready = tmp_path / "ready"
    pid_file = tmp_path / "job.pid"
    command = (
        f"trap 'sleep 60 & echo $! > \"{pid_file}\"; sleep 0.5; exit 0' TERM; "
        f"echo up > '{ready}'; while :; do sleep 0.05; done"
    )
    started = time.monotonic()
    with stop_probe(ready.exists):
        run = childrun.run_child(command, cwd=tmp_path, timeout=120)
    elapsed = time.monotonic() - started
    job = _read_pid(pid_file)
    assert job is not None, "the TERM trap never ran"
    reap_leftovers.append(job)

    assert run.interrupted is True
    assert elapsed < _RETURN_CEILING_S
    assert _wait_gone(job), f"job {job} forked in the grace survived the hard stop"


# ---- real win32 tree --------------------------------------------------------------


@WIN32_ONLY
def test_hard_stop_kills_a_cmd_rooted_tree(tmp_path, reap_leftovers):
    """shell=True roots the tree at cmd.exe; a python child under it starts a
    python grandchild. The interrupt must leave no member alive."""
    pid_file = tmp_path / "grandchild.pid"
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    with stop_probe(_pid_written(pid_file)):
        run = childrun.run_child(
            f'"{sys.executable}" "{parent}" "{pid_file}"', cwd=tmp_path, timeout=120
        )
    elapsed = time.monotonic() - started
    grandchild = _read_pid(pid_file)
    assert grandchild is not None
    reap_leftovers.append(grandchild)

    assert run.interrupted is True
    assert elapsed < _RETURN_CEILING_S
    assert _wait_gone(grandchild), f"grandchild {grandchild} survived the hard stop"


@WIN32_ONLY
def test_drain_gives_up_on_a_holder_nothing_can_reach(tmp_path, monkeypatch):
    """DW-478: the root exits at once, leaving an orphan holder with the pipes, and
    the harvest never sees it (patched to `{}`), so no kill reaches it. CPython's
    win32 `communicate` reads each pipe in a thread blocked inside `read()` with
    the BufferedReader lock held; closing the stream then waits on that lock for
    the holder's whole life. The runner must return well before the holder dies.

    Ablation: close every stream in `_close_pipes` regardless of its reader
    thread and, where close() blocks, the return waits out the holder's 30 s."""
    pid_file = tmp_path / "holder.pid"
    holder = tmp_path / "holder.py"
    holder.write_text(
        "import os, sys, time\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]],\n"
        "                 stdout=sys.stdout, stderr=sys.stderr)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(get_process_host(), "descendants", lambda pid: {})
    started = time.monotonic()
    holder_alive_after = False
    try:
        with stop_probe(lambda: False):
            run = childrun.run_child(
                f'"{sys.executable}" "{parent}" "{holder}" "{pid_file}"',
                cwd=tmp_path,
                timeout=1.0,
            )
        elapsed = time.monotonic() - started
        deadline = time.monotonic() + 5.0
        while _read_pid(pid_file) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        early_pid = _read_pid(pid_file)
        # read before the finally kills it
        holder_alive_after = early_pid is not None and not _gone(early_pid)
    finally:
        deadline = time.monotonic() + 5.0
        while _read_pid(pid_file) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        holder_pid = _read_pid(pid_file)
        if holder_pid is not None:
            with contextlib.suppress(Exception):
                get_process_host().force_kill(holder_pid)

    assert holder_pid is not None, "the holder never started"
    # anti-vacuity: a slow runner whose cmd.exe was still up at the timeout lets
    # `taskkill /F /T` reach the holder, and the drain-timeout arm is never taken
    assert holder_alive_after, "the kill reached the holder; the unreachable-holder arm never ran"
    assert run.timed_out is True
    assert elapsed < 1.0 + 4 * childrun.KILL_WAIT_S + childrun.DRAIN_S + 2.0


# ---- kill ordering against a fake host ---------------------------------------------


class _FakeProc:
    """A Popen stand-in whose root dies on the first force_kill (win32) or
    terminate (POSIX) the fake host records against it."""

    pid = 4242

    def __init__(self, host: _FakeHost):
        self._host = host

    def poll(self) -> int | None:
        return 0 if self._host.root_dead else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._host.root_dead:
            time.sleep(timeout or 0)  # a real wait blocks; the grace loop must not spin
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return 0

    def kill(self) -> None:  # pragma: no cover - only the ProcessHostError arm
        raise AssertionError("the host seam must be used")

    terminate = kill


class _FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.root_dead = False
        # 5001: a straggler that ignores terminate and dies only to force_kill.
        # 5002: unstamped identity — unconfirmable, so never signalled or polled.
        self.alive = {5001}
        # A root that ignores SIGTERM and dies only to force_kill.
        self.root_ignores_terminate = False
        # descendants() keyed by the pid asked about; a pid absent here has none.
        self.trees: dict[int, dict[int, float | None]] = {
            _FakeProc.pid: {5001: 1.0, 5002: None},
        }

    def descendants(self, pid: int) -> dict[int, float | None]:
        self.calls.append(("descendants", pid))
        return dict(self.trees.get(pid, {}))

    def terminate(self, pid: int) -> None:
        self.calls.append(("terminate", pid))
        if pid == _FakeProc.pid and sys.platform != "win32" and not self.root_ignores_terminate:
            self.root_dead = True

    def force_kill(self, pid: int) -> None:
        self.calls.append(("force_kill", pid))
        if pid == _FakeProc.pid:
            self.root_dead = True
        self.alive.discard(pid)

    def alive_and_ours(self, pid: int, identity: float | None) -> bool:
        assert identity is not None, "an unstamped descendant must never be polled"
        return pid in self.alive


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_kill_tree_ordering(monkeypatch, platform):
    """Harvest before the first signal; on win32 `force_kill(root)` (taskkill /F
    /T) comes before anything polite, because a polite taskkill can reap cmd.exe
    alone and strand the command where /T can no longer find it; on POSIX the
    root gets SIGTERM. Then the harvested straggler's own descendants are
    harvested (DW-477) and it is reaped (terminate, then force_kill when it
    ignores that), and the unstamped member is never touched. The root dies on
    its first signal, so its grace wait takes no re-harvest.

    Ablation: swap the win32 arm to `terminate` and the second-call assertion
    reddens; move the harvest after the root signal and the first does."""
    host = _FakeHost()
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", platform)

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    root = _FakeProc.pid
    assert host.calls[0] == ("descendants", root)
    first_signal = ("force_kill", root) if platform == "win32" else ("terminate", root)
    assert host.calls[1] == first_signal
    if platform == "win32":
        assert ("terminate", root) not in host.calls
    assert host.calls[2:] == [
        ("descendants", 5001),
        ("terminate", 5001),
        ("force_kill", 5001),
    ]
    assert not any(pid == 5002 for _, pid in host.calls)
    assert host.alive == set()


def test_kill_tree_force_kills_a_root_that_outlives_its_grace(monkeypatch):
    """A root that ignores SIGTERM outlives its grace wait: the wait keeps
    re-harvesting its descendants until the deadline, then the root is
    force-killed, and only then are the known stragglers reaped.

    Ablation: make `_grace_wait`'s deadline arm return True and `force_kill(root)`
    never happens."""
    host = _FakeHost()
    host.root_ignores_terminate = True
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    root = _FakeProc.pid
    calls = host.calls
    signalled = calls.index(("terminate", root))
    forced = calls.index(("force_kill", root))
    assert calls[0] == ("descendants", root) and signalled == 1
    assert ("descendants", root) in calls[signalled + 1 : forced], "no grace re-harvest"
    assert calls[forced + 1 :] == [
        ("descendants", 5001),
        ("terminate", 5001),
        ("force_kill", 5001),
    ]
    assert host.alive == set()


def test_kill_tree_reaps_a_survivors_own_descendants(monkeypatch):
    """A known survivor (a reparented job) has children the root harvest never
    listed — they were never under the root. They are harvested from the
    survivor, identity-rechecked, and reaped with it.

    Ablation: drop the survivor-descendant harvest in `_reap_descendants` and
    6001 is never signalled."""
    host = _FakeHost()
    host.trees[5001] = {6001: 2.0}
    host.alive = {5001, 6001}
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    assert ("terminate", 6001) in host.calls
    assert ("force_kill", 6001) in host.calls
    assert host.alive == set()


def test_kill_tree_drops_a_survivor_harvest_whose_pid_was_reused(monkeypatch):
    """The survivor sub-harvest is merged only when the survivor is still ours
    AFTER it was taken: a pid reused before the harvest lists a stranger's
    children, and none of those may be signalled.

    Ablation: merge the sub-harvest without the post-harvest `alive_and_ours`
    recheck and 6001 is signalled."""
    host = _FakeHost()
    host.trees[5001] = {6001: 2.0}
    host.alive = {5001, 6001}
    real_descendants = host.descendants

    def reusing_descendants(pid: int) -> dict[int, float | None]:
        out = real_descendants(pid)
        if pid == 5001:
            host.alive.discard(5001)  # 5001 died and its pid went to a stranger
        return out

    host.descendants = reusing_descendants  # type: ignore[method-assign]
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    assert not any(pid == 6001 for name, pid in host.calls if name != "descendants")
    assert host.alive == {6001}


def test_kill_tree_reaps_the_known_tree_of_an_exited_root(monkeypatch):
    """A root that already exited is reaped — its pid may belong to someone else
    — so it is never signalled or harvested. But the tree the poll loop
    accumulated while it ran is still reaped: the background job that outlived
    its shell (DW-477).

    Ablation: keep the unconditional early return on an exited root and 5001
    survives."""
    host = _FakeHost()
    host.root_dead = True
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(
        _FakeProc(host),  # pyright: ignore[reportArgumentType]
        wait_s=0.05,
        known={5001: 1.0, 5002: None},
    )

    assert not any(pid == _FakeProc.pid for _, pid in host.calls)
    assert ("terminate", 5001) in host.calls and ("force_kill", 5001) in host.calls
    assert not any(pid == 5002 for _, pid in host.calls)
    assert host.alive == set()


def test_kill_tree_never_lets_an_unstamped_harvest_overwrite_a_stamp(monkeypatch):
    """An unstamped entry must not erase the identity an earlier harvest
    recorded: that would turn a member the reap can confirm into one it must
    never touch. The built-in hosts OMIT a member they cannot stamp, so this
    defends against a host that returns unstamped entries — an out-of-tree
    `register_process_host` host, or a platform that never stamps.

    Ablation: let `_merge` overwrite unconditionally and 5001 is never reaped."""
    host = _FakeHost()
    host.trees[_FakeProc.pid] = {5001: None}
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(
        _FakeProc(host),  # pyright: ignore[reportArgumentType]
        wait_s=0.05,
        known={5001: 1.0},
    )

    assert ("force_kill", 5001) in host.calls
    assert host.alive == set()


def test_kill_tree_retry_after_an_unwound_kill_reaps_what_the_first_harvested(monkeypatch):
    """The common POSIX hard stop: SIGTERM's `RunStopped` can land INSIDE the first
    `kill_tree` — after its pre-signal harvest and root signal — and `run_child`'s
    `finally` retries with the same `known`. The root is reaped by then, so the
    retry harvests nothing; only what the first call merged into `known` in place
    is left to reach.

    Ablation: make `kill_tree` work on a copy of `known` (`dict(known)`) and the
    retry finds an empty tree, so 5001 survives."""

    class _UnwindingHost(_FakeHost):
        def terminate(self, pid: int) -> None:
            super().terminate(pid)
            if pid == _FakeProc.pid:
                raise RuntimeError("unwinding")

    host = _UnwindingHost()
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")
    proc = _FakeProc(host)
    known: childrun.KnownTree = {}

    with pytest.raises(RuntimeError, match="unwinding"):
        childrun.kill_tree(proc, wait_s=0.05, known=known)  # pyright: ignore[reportArgumentType]
    assert 5001 in known, "the first call's harvest was not merged into the caller's tree"

    host.calls.clear()
    childrun.kill_tree(proc, wait_s=0.05, known=known)  # pyright: ignore[reportArgumentType]

    assert not any(pid == _FakeProc.pid for _, pid in host.calls)
    assert ("force_kill", 5001) in host.calls
    assert host.alive == set()


def test_kill_tree_bounds_the_survivor_sub_harvest_by_wait_s(monkeypatch):
    """One scan per survivor, so the sub-harvest has its own `wait_s` budget: no
    scan starts past it, and every survivor is still reaped. That keeps the kill's
    fourth step inside the hard-stop-to-return bound on a slow host.

    Ablation: drop the `sub_deadline` break in `_reap_descendants` and every
    survivor is scanned."""
    host = _FakeHost()
    host.root_dead = True
    host.alive = {5001, 5003, 5004}
    real_descendants = host.descendants

    def slow_descendants(pid: int) -> dict[int, float | None]:
        time.sleep(0.1)  # one scan outruns the whole 0.05 s budget
        return real_descendants(pid)

    host.descendants = slow_descendants  # type: ignore[method-assign]
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)
    monkeypatch.setattr(childrun.sys, "platform", "linux")

    childrun.kill_tree(
        _FakeProc(host),  # pyright: ignore[reportArgumentType]
        wait_s=0.05,
        known={5001: 1.0, 5003: 1.0, 5004: 1.0},
    )

    assert [pid for name, pid in host.calls if name == "descendants"] == [5001]
    assert {pid for name, pid in host.calls if name == "force_kill"} == {5001, 5003, 5004}
    assert host.alive == set()


def test_unresolvable_host_leaves_a_completing_command_alone(tmp_path, monkeypatch):
    """`run_child` resolves the host up front for the loop harvest. A host that
    cannot be resolved (a bogus `BMAD_LOOP_PROCESS_HOST`) only disables the
    harvest: a command that finishes on its own still returns its result, as it
    did when only the kill resolved the host.

    Ablation: drop the `ProcessHostError` suppression around the resolution in
    `run_child` and the call raises right after spawn."""
    from bmad_loop.process_host import ProcessHostError

    def bogus_host():
        raise ProcessHostError("bogus-host-name")

    monkeypatch.setattr(childrun, "get_process_host", bogus_host)
    with stop_probe(lambda: False):
        run = childrun.run_child("exit 0", cwd=tmp_path, timeout=30)
    assert run == childrun.ChildRun(0, "", "")


def test_loop_harvest_prunes_what_is_gone_and_keeps_what_is_reparented():
    """The loop's known tree stays bounded over a long run: an entry a fresh
    harvest no longer lists is dropped once it is gone or reused, or when it was
    never stamped. One still ours stays — that is the reparented job the tree
    exists to keep in reach. A fresh stamp for a known pid replaces the old one.

    Ablation: drop the prune in `_harvest_and_prune` and 7001/7002 remain."""
    host = _FakeHost()
    host.trees[_FakeProc.pid] = {5001: 3.0, 5003: None}
    host.alive = {5001, 7003}
    known: dict[int, float | None] = {
        5001: 1.0,  # re-stamped by the fresh harvest (new generation)
        7001: 1.0,  # absent and gone: pruned
        7002: None,  # absent and unstamped: pruned
        7003: 1.0,  # absent but still ours (reparented): kept
    }

    harvest: Any = childrun._harvest_and_prune  # pyright: ignore[reportPrivateUsage]
    harvest(host, _FakeProc.pid, known)

    assert known == {5001: 3.0, 5003: None, 7003: 1.0}


def test_loop_harvest_stops_once_the_root_is_reaped_or_the_host_faults():
    """The loop harvests only from an unreaped root (the Popen pins its pid) and
    never lets a host fault escape: it stops harvesting instead, and the kill
    reports a broken host loudly.

    Ablation: drop the `poll()` gate and the reaped root's pid is harvested;
    drop the `ProcessHostError` catch and the fault escapes."""
    from bmad_loop.process_host import ProcessHostError

    host = _FakeHost()
    host.root_dead = True
    known: dict[int, float | None] = {}
    loop_harvest: Any = childrun._loop_harvest  # pyright: ignore[reportPrivateUsage]
    got = loop_harvest(host, _FakeProc(host), known)
    assert got is None and host.calls == [] and known == {}

    class _FaultingHost(_FakeHost):
        def alive_and_ours(self, pid: int, identity: float | None) -> bool:
            raise ProcessHostError("no psutil")

    faulting = _FaultingHost()
    known = {7001: 1.0}  # absent from the fresh harvest, so the prune reads liveness
    got = loop_harvest(faulting, _FakeProc(faulting), known)
    assert got is None


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_kill_tree_strikes_the_root_once_before_reraising_a_host_error(monkeypatch, platform):
    """An explicit-but-bogus process-host override raises `ProcessHostError` by
    doctrine, but the root must not be left alive behind the raise: exactly one
    legacy Popen strike (`kill` on win32, `terminate` on POSIX), then the error
    propagates (precedent: the opencode adapter's `_kill_process` row).

    Ablation: drop the strike in `kill_tree`'s `except ProcessHostError` and the
    strike counts read zero; swallow the error and `pytest.raises` fails."""
    from bmad_loop.process_host import ProcessHostError

    class _StrikeCountingPopen:
        pid = 4242

        def __init__(self) -> None:
            self.terminated = 0
            self.killed = 0

        def poll(self) -> int | None:
            return None  # alive: kill_tree must not early-return

        def terminate(self) -> None:
            self.terminated += 1

        def kill(self) -> None:
            self.killed += 1

    def bogus_host():
        raise ProcessHostError("bogus-host-name")

    monkeypatch.setattr(childrun, "get_process_host", bogus_host)
    monkeypatch.setattr(childrun.sys, "platform", platform)
    proc = _StrikeCountingPopen()

    with pytest.raises(ProcessHostError, match="bogus-host-name"):
        childrun.kill_tree(proc)  # pyright: ignore[reportArgumentType]

    if platform == "win32":
        assert (proc.killed, proc.terminated) == (1, 0)
    else:
        assert (proc.killed, proc.terminated) == (0, 1)


def test_kill_tree_leaves_an_exited_root_alone(monkeypatch):
    """A root already reaped with no known tree has nothing reachable left: no
    harvest, no signal (its pid may already belong to someone else). The
    no-`known` call shape stays backward compatible."""
    host = _FakeHost()
    host.root_dead = True
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    assert host.calls == []


class _Stream:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Reader:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _PipedProc:
    """stdout's reader thread is still blocked in read(); stderr's has finished."""

    def __init__(self) -> None:
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.stdout_thread = _Reader(alive=True)
        self.stderr_thread = _Reader(alive=False)


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_close_pipes_skips_a_stream_its_win32_reader_still_holds(monkeypatch, platform):
    """DW-478: on win32 a stream whose reader thread is still alive is left for
    that thread to close — `close()` would block on the BufferedReader lock its
    `read()` holds. A stream whose reader finished is closed. POSIX has no reader
    threads to wait on, so it closes both whatever attribute is present.

    Ablation: drop the reader-thread check in `_close_pipes` and the win32 arm
    closes stdout; make it unconditional and the linux arm leaves stdout open."""
    monkeypatch.setattr(childrun.sys, "platform", platform)
    proc = _PipedProc()

    childrun._close_pipes(proc)  # pyright: ignore[reportArgumentType, reportPrivateUsage]

    assert proc.stderr.closed is True
    assert proc.stdout.closed is (platform != "win32")


def test_timeout_stream_normalises_bytes_like_text_mode():
    assert childrun.timeout_stream(None) == ""
    assert childrun.timeout_stream("already\r\ntext") == "already\r\ntext"
    assert childrun.timeout_stream(b"a\r\nb\rc\n") == "a\nb\nc\n"


# ---- the drain-timeout arm ---------------------------------------------------------
#
# The only production path into `timeout_stream` since DW-353: a timed-out or
# interrupted command's output normally comes from the completed post-kill
# `communicate`, already text-mode decoded. Only when a pipe-holder the tree kill
# cannot reach outlives `DRAIN_S` does the drain raise `TimeoutExpired`, handing
# over the raw POSIX chunks for `timeout_stream` to decode. The holder here is the
# documented `kill_tree` limit made concrete: a background job double-forked out of
# a subshell that exits at once, so it is reparented away from the root before any
# harvest (the loop's first one is `HARVEST_S` in) and keeps the pipes open.
#
# Driven inside an ASCII-locale child interpreter for the reason the #378 rows in
# tests/test_verify.py give: every CI leg is UTF-8, where the locale codec and a
# hardcoded UTF-8 decode agree, so the codec half of the normalisation can only be
# observed under `LC_ALL=C` + `PYTHONUTF8=0`.

_DRAIN_RAW = b"caf\xc3\xa9\r\nsecond\rthird\n"


@POSIX_ONLY
def test_drain_timeout_output_goes_through_timeout_stream(tmp_path):
    """A pipe-holder the kill cannot reach forces the drain-timeout arm; what the
    tree wrote must still read back exactly as a completed run's output does —
    locale codec, newlines collapsed — and the runner must return within the
    drain bound rather than wait on the holder.

    Ablation: return `exc.stdout` raw (or decode it as UTF-8, or skip the newline
    collapse) in `_drain`'s except arm and `drained_stdout` stops matching
    `completed_stdout`; the `timeout_stream_calls` anti-vacuity check fails if the
    arm is ever not reached."""
    emit = tmp_path / "emit.py"
    emit.write_text(
        "import sys\n" f"sys.stdout.buffer.write({_DRAIN_RAW!r})\n" "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    holder_pid = tmp_path / "holder.pid"
    driver = tmp_path / "drive.py"
    driver.write_text(
        "import json, locale, sys, time\n"
        "from bmad_loop import childrun\n"
        "emit, holder_pid, cwd = sys.argv[1:4]\n"
        "calls = []\n"
        "real = childrun.timeout_stream\n"
        "def spy(value):\n"
        "    calls.append(type(value).__name__)\n"
        "    return real(value)\n"
        "childrun.timeout_stream = spy\n"
        "childrun.DRAIN_S = 0.3\n"
        'py = \'"%s" "%s"\' % (sys.executable, emit)\n'
        "done = childrun.run_child(py, cwd=cwd, timeout=30)\n"
        "# the subshell exits at once, so `sleep 30` is reparented away from the\n"
        "# root before any harvest and holds stdout/stderr open past the kill\n"
        "cmd = \"%s; (sleep 30 & echo $! > '%s'); sleep 60\" % (py, holder_pid)\n"
        "started = time.monotonic()\n"
        "hung = childrun.run_child(cmd, cwd=cwd, timeout=1.0)\n"
        "elapsed = time.monotonic() - started\n"
        "json.dump({'encoding': locale.getpreferredencoding(False),\n"
        "           'completed_stdout': done.stdout, 'drained_stdout': hung.stdout,\n"
        "           'drained_stderr': hung.stderr, 'timed_out': hung.timed_out,\n"
        "           'elapsed': elapsed, 'timeout_stream_calls': calls}, sys.stdout)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "LANG", "LC_CTYPE")}
    env["LC_ALL"] = "C"
    env["PYTHONUTF8"] = "0"  # without this the C locale would resolve to UTF-8 (PEP 540)

    try:
        proc = subprocess.run(
            [sys.executable, str(driver), str(emit), str(holder_pid), str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
        )
    finally:
        holder = _read_pid(holder_pid)
        if holder is not None and not _gone(holder):
            with contextlib.suppress(Exception):
                get_process_host().force_kill(holder)

    assert proc.returncode == 0, proc.stderr
    observed = json.loads(proc.stdout)
    decoded = _DRAIN_RAW.decode(observed["encoding"], errors="replace")
    # anti-vacuity: a UTF-8 codec or a payload without CRs would pass with the bug in
    assert decoded != _DRAIN_RAW.decode("utf-8", errors="replace")
    assert "\r" in decoded
    # the drain-timeout arm was actually taken, on the raw POSIX bytes
    assert "bytes" in observed["timeout_stream_calls"]

    assert observed["completed_stdout"] == decoded.replace("\r\n", "\n").replace("\r", "\n")
    assert observed["timed_out"] is True
    assert observed["drained_stdout"] == observed["completed_stdout"]
    assert observed["drained_stderr"] == ""
    # timeout + kill steps + the (patched) drain — never the holder's 30 s
    assert observed["elapsed"] < 1.0 + 3 * childrun.KILL_WAIT_S + 0.3 + 2.0
