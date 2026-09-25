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

import pytest

from bmad_loop import childrun, runs
from bmad_loop.process_host import get_process_host

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="drives a /bin/sh process tree")
WIN32_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="drives a cmd.exe process tree")

# A hard-stop-to-return ceiling for the real rows: one poll + three kill steps +
# the drain is ~4.25 s by construction; runs._STOP_WAIT_S (10 s) is the budget the
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

    def descendants(self, pid: int) -> dict[int, float | None]:
        self.calls.append(("descendants", pid))
        return {5001: 1.0, 5002: None}

    def terminate(self, pid: int) -> None:
        self.calls.append(("terminate", pid))
        if pid == _FakeProc.pid and sys.platform != "win32":
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
    root gets SIGTERM. Then the harvested straggler is reaped (terminate, then
    force_kill when it ignores that), and the unstamped member is never touched.

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
    assert host.calls[2:] == [("terminate", 5001), ("force_kill", 5001)]
    assert not any(pid == 5002 for _, pid in host.calls)
    assert host.alive == set()


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
    """A root already reaped has nothing reachable left: no harvest, no signal
    (its pid may already belong to someone else)."""
    host = _FakeHost()
    host.root_dead = True
    monkeypatch.setattr(childrun, "get_process_host", lambda: host)

    childrun.kill_tree(_FakeProc(host), wait_s=0.05)  # pyright: ignore[reportArgumentType]

    assert host.calls == []


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
# a subshell, so it is reparented away from the root before the one pre-signal
# harvest and keeps the pipes open.
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
