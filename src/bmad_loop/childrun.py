"""Stop-aware child runner for operator-authored shell commands (DW-353).

The deterministic verify commands (``verify.run_verify_commands``) and the plugin
bus's declarative hooks (``plugins.bus._run_subprocess``) are the two places the
engine blocks on an arbitrary operator command. As plain ``subprocess.run`` calls
they had two faults a hard ``bmad-loop stop`` could land in:

* On native Windows the engine never receives SIGTERM, so the only stop channel
  is the control file — which nobody polled while the command ran. ``stop_run``
  waited out ``runs._STOP_WAIT_S`` and force-killed the engine.
* On POSIX the SIGTERM path, and the timeout leg, killed only the ``/bin/sh``
  root; the command's own children (a test runner's workers, a build daemon)
  were orphaned and kept writing into the worktree.

:func:`run_child` fixes both at one seam. It polls an AMBIENT hard-stop probe
while the child runs and kills the whole process tree on a hard stop, on
timeout, or on any exception unwinding through it, reporting the first as
``interrupted`` — a fact about the run, never a verdict about the command.

The probe is ambient (a ContextVar installed by the outermost ``Engine.run()``,
beside ``runs.set_owner_run_dir``) rather than a parameter, because ``runs``
imports ``verify`` — so neither ``verify`` nor this module can import ``runs`` —
and because the review gates that reach ``run_verify_commands`` hold no run
dir. Outside an engine run no probe is installed (``cli._reverify``, tests,
probes) and the runner never interrupts. The runner only READS the channel;
consuming a hard request stays with the engine's hard-stop arm.

Leaf module: imports nothing from ``runs``, ``verify`` or ``engine``.
"""

from __future__ import annotations

import contextlib
import contextvars
import locale
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .process_host import ProcessHost, ProcessHostError, get_process_host

# How often a running child's stop probe is read. Also the upper bound on how
# long a hard request waits before the kill starts.
STOP_POLL_S = 0.25

# Per-step grace in the tree kill: root wait after the first signal, root wait
# after the force-kill, and the straggler reap. Three steps plus the drain below
# plus one poll bound the hard-stop-to-return latency at ~4.25 s, well under
# ``runs._STOP_WAIT_S`` (10 s) — the budget ``stop_run`` gives the engine before
# it force-kills it.
KILL_WAIT_S = 1.0

# Bounded pipe drain after a kill. A straggler the reap could not confirm (an
# unstamped identity) may still hold the pipes open; the drain must not wait on it.
DRAIN_S = 1.0

# Cadence of the straggler reap's liveness re-read.
_REAP_POLL_S = 0.05

StopProbe = Callable[[], bool]

_stop_probe: contextvars.ContextVar[StopProbe | None] = contextvars.ContextVar(
    "bmad_loop_child_stop_probe", default=None
)


class ChildInterrupted(Exception):
    """A child was interrupted (or never spawned) because a hard stop request is
    pending. Raised by transports whose callers speak exceptions (the plugin
    bus's declarative runner); never a hook error, a failure, or a veto."""


@dataclass(frozen=True)
class ChildRun:
    """One child's observed outcome.

    ``returncode`` is ``None`` only when nothing was spawned (an interrupt that
    was already pending) or the killed root could not be reaped. ``timed_out``
    and ``interrupted`` are exclusive; on either, the streams hold whatever the
    tree wrote before it was killed."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    interrupted: bool = False


def install_stop_probe(probe: StopProbe) -> contextvars.Token[StopProbe | None]:
    """Install ``probe`` as this call stack's hard-stop probe. Returns the token
    the caller must hand to :func:`reset_stop_probe` from a ``finally``."""
    return _stop_probe.set(probe)


def reset_stop_probe(token: contextvars.Token[StopProbe | None]) -> None:
    """Release the probe installed by :func:`install_stop_probe`."""
    _stop_probe.reset(token)


def hard_stop_pending() -> bool:
    """Whether the installed probe reports a pending HARD stop request. ``False``
    outside an engine run, where no probe is installed. A probe that raises is
    not swallowed: a broken stop channel must be loud, not read as "keep going"."""
    probe = _stop_probe.get()
    return probe is not None and bool(probe())


def timeout_stream(value: str | bytes | None) -> str:
    """Normalize a ``TimeoutExpired`` stream payload into what a completed
    ``communicate()`` would have returned.

    Three shapes arrive:

    * ``bytes`` — POSIX. ``Popen._communicate`` raises ``TimeoutExpired`` from
      ``_check_timeout`` with the raw chunks joined, *before* the text-mode
      decode that ends the loop, so ``text=True`` never touched them.
    * ``str`` — Windows, where the text wrapper has already decoded.
    * ``None`` — nothing buffered on that stream (POSIX), or a Windows reader
      thread that was still running when the timeout fired.

    So the bytes branch has to reproduce what text mode would have done to them,
    which is exactly ``Popen._translate_newlines``: decode, then collapse ``\\r\\n``
    and lone ``\\r`` to ``\\n``. Doing neither made the same bytes read back
    differently depending on which path produced them — under an ASCII locale
    ``b"caf\\xc3\\xa9\\r\\n"`` completed as ``"caf\\ufffd\\ufffd\\n"`` but timed out
    as ``"café\\r\\n"``. The codec half keeps host-tool output on the locale
    codec (#378): ``locale.getpreferredencoding(False)`` is what ``text=True``
    resolves for an unset ``encoding`` — deliberately not ``locale.getencoding()``,
    which disagrees with it under UTF-8 mode (PEP 540), a mode the C/POSIX locale
    enables by itself. ``errors="replace"`` for the reason the completed path uses
    it: one undecodable byte must not raise and lose every result.

    The str branch is left alone: its newlines were translated by the text
    wrapper the reader thread read through, so there is nothing left to collapse."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        decoded = value.decode(locale.getpreferredencoding(False), errors="replace")
        return decoded.replace("\r\n", "\n").replace("\r", "\n")
    return value


def run_child(
    command: str,
    *,
    cwd: str | Path | None,
    timeout: float | None,
    env: dict[str, str] | None = None,
) -> ChildRun:
    """Run ``command`` through the host shell, stop-aware and tree-killing.

    Decoding matches the ``subprocess.run(text=True, errors="replace")`` calls
    this replaced: the locale codec, with replacement (#378/#383). Spawn faults
    (``OSError``, ``ValueError`` from ``Popen``) propagate untouched — each
    caller owns its translation.

    The probe is read before spawn (a pending hard request spawns nothing) and
    every :data:`STOP_POLL_S` while the child runs. Polling is a
    ``communicate(timeout=...)`` retried in a loop: CPython keeps the collected
    chunks (POSIX) or reader-thread buffers (Windows) across a ``TimeoutExpired``,
    so a retry loses no output. On interrupt or timeout the tree is killed
    (:func:`kill_tree`) and the pipes drained for at most :data:`DRAIN_S`. Any
    exception unwinding through the loop — SIGTERM's ``RunStopped``, a
    ``KeyboardInterrupt`` — kills the tree in ``finally`` with kill errors
    suppressed, and the original exception propagates.

    A child that exits on its own is not tree-killed: a completed command's
    leftover background processes are the command's business, exactly as they
    were under ``subprocess.run``."""
    if hard_stop_pending():
        return ChildRun(None, "", "", interrupted=True)
    settled = False
    # Operator-authored shell strings (verify commands, declarative plugin hooks);
    # the shell is the contract, so shell=True is intentional here.
    proc = subprocess.Popen(  # nosec B602
        command,
        shell=True,  # portability: operator-authored verify/hook command — sanctioned shell-out (see plan out-of-scope)
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    try:
        # Everything after the spawn sits inside the try, so an exception landing
        # anywhere past `Popen` (SIGTERM's RunStopped) still reaches the kill below.
        deadline = None if timeout is None else time.monotonic() + timeout
        interrupted = False
        while True:
            if hard_stop_pending():
                interrupted = True
                break
            wait = STOP_POLL_S
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                wait = min(wait, remaining)
            try:
                stdout, stderr = proc.communicate(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
            settled = True
            return ChildRun(proc.returncode, stdout or "", stderr or "")
        kill_tree(proc)
        stdout, stderr = _drain(proc)
        settled = True
        return ChildRun(
            proc.returncode,
            stdout,
            stderr,
            timed_out=not interrupted,
            interrupted=interrupted,
        )
    finally:
        if not settled:
            # An exception is unwinding through the loop, the kill, or the drain.
            # Kill errors must not mask the original exception; a second
            # kill_tree on an already-reaped root is a no-op.
            with contextlib.suppress(Exception):
                kill_tree(proc)
            _close_pipes(proc)


def _drain(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect what the killed tree wrote, bounded by :data:`DRAIN_S`. A
    straggler still holding a pipe turns into a partial read — the chunks
    already collected — never a wait on it."""
    try:
        stdout, stderr = proc.communicate(timeout=DRAIN_S)
    except subprocess.TimeoutExpired as exc:
        _close_pipes(proc)
        return timeout_stream(exc.stdout), timeout_stream(exc.stderr)
    return stdout or "", stderr or ""


def _close_pipes(proc: subprocess.Popen[str]) -> None:
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


def kill_tree(proc: subprocess.Popen[str], *, wait_s: float = KILL_WAIT_S) -> None:
    """Kill ``proc`` and every descendant it had, per the gh-183 doctrine
    (template: ``adapters/opencode_http.py::_kill_process``).

    The descendant tree is harvested BEFORE the first signal, while it is
    intact: once the root dies its children reparent and can no longer be
    enumerated from it. On win32 the root gets ``force_kill`` (``taskkill /F
    /T``) first — ``shell=True`` roots the tree at ``cmd.exe``, and a polite
    taskkill can reap ``cmd.exe`` alone, after which ``/T`` can never find the
    command again. On POSIX the root gets ``terminate`` (SIGTERM). Then a
    bounded wait, a ``force_kill`` if the root is still up, and a reap of the
    harvested stragglers — only those whose recorded identity still matches
    (``alive_and_ours``), so a reused pid is never signalled.

    Never ``os.killpg`` or ``os.kill``: the child is not detached into its own
    group (that would change what a console Ctrl-C reaches), and every signal
    goes through the ``ProcessHost`` seam. A root that already exited is left
    alone — its pid is reaped, so nothing it had is reachable from it.

    Known limit (deferred): the tree is whatever ONE pre-signal harvest can reach
    from the root. A descendant already reparented away from the root is out of
    reach — the case of a root that exited while a background job it started
    still holds the pipes (the runner then returns within :data:`DRAIN_S` rather
    than hanging, as ``subprocess.run`` did, but the job survives). So is a
    process spawned after the harvest. Where ``ProcessHost.descendants``
    degrades to ``{}`` (macOS without psutil), only the root is killed."""
    if proc.poll() is not None:
        return
    try:
        host = get_process_host()
    except ProcessHostError:
        # An explicit-but-bogus BMAD_LOOP_PROCESS_HOST override raises loudly by
        # doctrine. The root must not be left alive behind the raise: one legacy
        # Popen strike (no host means no tree kill — an accepted degrade on a loud
        # config error), then re-raise.
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                proc.kill()
            else:
                proc.terminate()
        raise
    tree = host.descendants(proc.pid)
    # The live Popen handle pins the pid (win32 handle / unreaped POSIX child),
    # so signalling the root cannot hit a reused pid.
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            host.force_kill(proc.pid)
    else:
        with contextlib.suppress(OSError):
            host.terminate(proc.pid)
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            host.force_kill(proc.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=wait_s)
    _reap_descendants(host, tree, wait_s)


def _reap_descendants(host: ProcessHost, tree: dict[int, float | None], wait_s: float) -> None:
    """Reap harvested descendants the root signal missed: terminate, bounded
    wait, force-kill. A ``None`` identity is unconfirmable (possible pid reuse),
    so it is never signalled or polled. Already-gone races are swallowed; this
    is best-effort, never a gate."""

    def _survivors() -> list[int]:
        return [
            pid
            for pid, identity in tree.items()
            if identity is not None and host.alive_and_ours(pid, identity)
        ]

    survivors = _survivors()
    if not survivors:
        return
    for pid in survivors:
        with contextlib.suppress(OSError):
            host.terminate(pid)
    deadline = time.monotonic() + wait_s
    while True:
        survivors = _survivors()
        if not survivors or time.monotonic() >= deadline:
            break
        time.sleep(_REAP_POLL_S)
    for pid in survivors:
        with contextlib.suppress(Exception):
            host.force_kill(pid)
