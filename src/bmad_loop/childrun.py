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
# after the force-kill, the survivors' own-descendant harvest, and the straggler
# reap. Four steps plus the drain below plus one poll bound the hard-stop-to-return
# latency at ~5.25 s (each step may overrun by the one scan in flight at its
# deadline), well under ``runs._STOP_WAIT_S`` (10 s) — the budget ``stop_run``
# gives the engine before it force-kills it.
KILL_WAIT_S = 1.0

# Bounded pipe drain after a kill. A straggler the reap could not confirm (an
# unstamped identity) may still hold the pipes open; the drain must not wait on it.
DRAIN_S = 1.0

# Cadence of the straggler reap's liveness re-read, and of the descendant
# re-harvest while :func:`kill_tree` waits out a root's grace.
_REAP_POLL_S = 0.05

# How often :func:`run_child` re-harvests the running root's descendants into the
# known tree (DW-477). A process forked and reparented away from the root inside
# one interval is never seen; one that lives across a harvest stays reachable after
# the root exits.
HARVEST_S = 1.0

KnownTree = dict[int, float | None]

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
    were under ``subprocess.run``.

    While the root is unreaped the loop re-harvests its descendants every
    :data:`HARVEST_S` into a KNOWN tree (DW-477) that every :func:`kill_tree` call
    here receives, the ``finally`` one included. A background job that lived
    across a harvest therefore stays reachable after its shell exits — the case
    one pre-signal harvest could never see, because a reparented process can no
    longer be enumerated from the root. The tree stays bounded over a long run:
    an entry a fresh harvest no longer lists is pruned once it is gone or reused
    (or was never stamped). A host that cannot be resolved skips harvesting —
    the kill then raises that fault loudly, as it always has."""
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
    known: KnownTree = {}
    harvester: ProcessHost | None = None
    try:
        # Everything after the spawn sits inside the try, so an exception landing
        # anywhere past `Popen` (SIGTERM's RunStopped) still reaches the kill below.
        with contextlib.suppress(ProcessHostError):
            # Unresolvable host: no loop harvest. kill_tree re-resolves it and
            # raises loudly there, exactly as before DW-477.
            harvester = get_process_host()
        deadline = None if timeout is None else time.monotonic() + timeout
        interrupted = False
        next_harvest = time.monotonic() + HARVEST_S
        while True:
            if hard_stop_pending():
                interrupted = True
                break
            if harvester is not None and time.monotonic() >= next_harvest:
                harvester = _loop_harvest(harvester, proc, known)
                next_harvest = time.monotonic() + HARVEST_S
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
        kill_tree(proc, known=known)
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
            # Kill errors must not mask the original exception. If a first
            # kill_tree already ran, it merged everything it harvested into
            # `known`, so this retry signals the root only if it is still
            # unreaped and re-reaps every known member still alive_and_ours —
            # the ones an unwind mid-grace or mid-reap left standing.
            with contextlib.suppress(Exception):
                kill_tree(proc, known=known)
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
    """Close the parent's ends of the pipes after a drain that gave up.

    On win32 a stream whose CPython reader thread is still alive is left open
    (DW-478). ``Popen._communicate`` there reads each pipe in a thread
    (``proc.stdout_thread`` / ``proc.stderr_thread``) that sits in ``fh.read()``
    holding the BufferedReader lock for as long as some unreachable process keeps
    the pipe open, and ``close()`` needs that lock — so closing would block the
    runner on the very holder the bounded drain exists to not wait on. The thread
    closes the stream itself once its read returns. POSIX reads in this thread,
    so nothing holds the lock there and every stream is closed."""
    streams = ((proc.stdout, "stdout_thread"), (proc.stderr, "stderr_thread"))
    for stream, reader_attr in streams:
        if stream is None:
            continue
        if sys.platform == "win32":
            reader = getattr(proc, reader_attr, None)
            if reader is not None and reader.is_alive():
                continue
        with contextlib.suppress(Exception):
            stream.close()


def _merge(tree: KnownTree, fresh: KnownTree) -> None:
    """Fold a fresh harvest into ``tree``. A fresh stamp overwrites (it is the pid's
    current generation, and the harvest just found it under a process we own); an
    unstamped entry never overwrites a stamped one, because that would turn a
    member the reap can confirm into one it must never touch.

    The built-in hosts never produce that case — ``ProcessHost.descendants``
    OMITS a member it cannot stamp, and returns ``None`` only on a platform that
    never stamps at all. The rule defends against a host that does return
    unstamped entries: an out-of-tree ``register_process_host`` host, or a
    platform that never stamps."""
    for pid, identity in fresh.items():
        if identity is not None or pid not in tree:
            tree[pid] = identity


def _harvest_and_prune(host: ProcessHost, root_pid: int, tree: KnownTree) -> None:
    """One poll-loop harvest: merge the root's current descendants into ``tree``,
    then prune every entry the fresh harvest no longer lists that is unstamped or
    no longer ``alive_and_ours`` — so a long run's short-lived descendants do not
    pile up. An entry absent from the harvest but still ours stays: that is the
    reparented job the known tree exists to keep in reach. The caller guarantees
    the root is unreaped, which pins ``root_pid``."""
    fresh = host.descendants(root_pid)
    _merge(tree, fresh)
    for pid in [pid for pid in tree if pid not in fresh]:
        identity = tree[pid]
        if identity is None or not host.alive_and_ours(pid, identity):
            del tree[pid]


def _loop_harvest(
    host: ProcessHost, proc: subprocess.Popen[str], known: KnownTree
) -> ProcessHost | None:
    """The poll loop's periodic harvest. Returns the host to harvest with next
    time, or ``None`` to stop: once the root is reaped nothing new is reachable
    from its pid, and a host fault (``ProcessHostError`` from a liveness read) must
    not escape the loop — the kill reports a broken host, the loop never does."""
    if proc.poll() is not None:
        return None
    try:
        _harvest_and_prune(host, proc.pid, known)
    except ProcessHostError:
        return None
    return host


def kill_tree(
    proc: subprocess.Popen[str],
    *,
    wait_s: float = KILL_WAIT_S,
    known: KnownTree | None = None,
) -> None:
    """Kill ``proc`` and every descendant it had, per the gh-183 doctrine
    (template: ``adapters/opencode_http.py::_kill_process``).

    ``known`` is the tree :func:`run_child` accumulated while the root ran
    (DW-477). Every harvest below merges into it IN PLACE, so a retry after this
    call unwound (an exception landing mid-grace or mid-reap) still knows every
    pid this one found. While the root is unreaped, a fresh
    harvest is merged in BEFORE the first signal, while the tree is intact: once
    the root dies its children reparent and can no longer be enumerated from it.
    On win32 the root gets ``force_kill`` (``taskkill /F /T``) first —
    ``shell=True`` roots the tree at ``cmd.exe``, and a polite taskkill can reap
    ``cmd.exe`` alone, after which ``/T`` can never find the command again. On
    POSIX the root gets ``terminate`` (SIGTERM). Then a bounded wait, a
    ``force_kill`` if the root is still up, and a second bounded wait; both waits
    re-harvest every :data:`_REAP_POLL_S` while the root is unreaped, so a process
    it forks in its grace (a TERM trap) is known before the root's exit reparents
    it. Finally the known stragglers are reaped — only those whose recorded
    identity still matches (``alive_and_ours``), so a reused pid is never
    signalled — after each survivor's own descendants are harvested into the tree.

    A root that already exited is never signalled or harvested (its pid is
    reaped and may belong to someone else), but the known tree is still reaped.
    With no known tree an exited root makes no host calls at all.

    Never ``os.killpg`` or ``os.kill``: the child is not detached into its own
    group (that would change what a console Ctrl-C reaches), and every signal
    goes through the ``ProcessHost`` seam.

    Known limits: only a member still alive and identity-matching is signalled.
    A process is known only if its parent chain was still under the live root
    at one of the harvests, so one that forks and is reparented away inside one
    interval is never seen — a double-fork whose intermediate lives less than
    :data:`HARVEST_S`, or a root that exits right after backgrounding a job (the
    runner then returns within :data:`DRAIN_S` rather than hanging, as
    ``subprocess.run`` did, but the job survives). A TERM trap that forks and
    exits at once is missed the same way: the grace re-harvest catches the fork
    only if the root lives one :data:`_REAP_POLL_S` re-scan after it. The loop
    harvests only while the ROOT runs; once it has exited, a surviving job's own
    children are reached only by the one-shot survivor sub-harvest here, so a
    descendant of that job that was already reparented away from it is missed,
    as is one forked after the sub-harvest (during the reap's wait) or under a
    survivor the sub-harvest's ``wait_s`` budget did not reach. Where
    ``ProcessHost.descendants`` degrades to ``{}`` (macOS without psutil), only
    the root is killed."""
    tree: KnownTree = known if known is not None else {}
    root_running = proc.poll() is None
    if not root_running and not tree:
        return
    try:
        host = get_process_host()
    except ProcessHostError:
        # An explicit-but-bogus BMAD_LOOP_PROCESS_HOST override raises loudly by
        # doctrine. The root must not be left alive behind the raise: one legacy
        # Popen strike (no host means no tree kill — an accepted degrade on a loud
        # config error), then re-raise.
        if root_running:
            with contextlib.suppress(OSError):
                if sys.platform == "win32":
                    proc.kill()
                else:
                    proc.terminate()
        raise
    if root_running:
        _merge(tree, host.descendants(proc.pid))
        # The live Popen handle pins the pid (win32 handle / unreaped POSIX child),
        # so signalling the root cannot hit a reused pid.
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                host.force_kill(proc.pid)
        else:
            with contextlib.suppress(OSError):
                host.terminate(proc.pid)
        if not _grace_wait(host, proc, tree, wait_s):
            with contextlib.suppress(Exception):
                host.force_kill(proc.pid)
            _grace_wait(host, proc, tree, wait_s)
    _reap_descendants(host, tree, wait_s)


def _grace_wait(
    host: ProcessHost, proc: subprocess.Popen[str], tree: KnownTree, wait_s: float
) -> bool:
    """Wait up to ``wait_s`` for the signalled root, merging a fresh harvest of its
    descendants into ``tree`` every :data:`_REAP_POLL_S` while it is unreaped.
    Merge only, never prune: a prune reads liveness, and a host fault must not
    abort the kill before the force-kill and the reap. True once the root is
    reaped."""
    deadline = time.monotonic() + wait_s
    while True:
        if proc.poll() is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _merge(tree, host.descendants(proc.pid))
        remaining = max(deadline - time.monotonic(), 0.0)
        try:
            proc.wait(timeout=min(_REAP_POLL_S, remaining))
        except subprocess.TimeoutExpired:
            continue
        return True


def _reap_descendants(host: ProcessHost, tree: KnownTree, wait_s: float) -> None:
    """Reap known descendants the root signal missed: terminate, bounded wait,
    force-kill. A ``None`` identity is unconfirmable (possible pid reuse), so it
    is never signalled or polled. Already-gone races are swallowed; this is
    best-effort, never a gate.

    Each survivor's own descendants are harvested first (DW-477) — a reparented
    job's children were never under the root, so no root harvest lists them. The
    sub-harvest is merged only when the survivor is STILL ``alive_and_ours``
    after it was taken: a pid reused before the harvest would have listed a
    stranger's children, and the identity recheck that follows the harvest
    catches exactly that."""

    def _survivors() -> list[int]:
        return [
            pid
            for pid, identity in tree.items()
            if identity is not None and host.alive_and_ours(pid, identity)
        ]

    survivors = _survivors()
    if not survivors:
        return
    # One scan per survivor, so the sub-harvest gets its own wait_s budget: no
    # scan starts past it, and the reap below runs either way.
    sub_deadline = time.monotonic() + wait_s
    for pid in survivors:
        if time.monotonic() >= sub_deadline:
            break
        identity = tree[pid]
        sub = host.descendants(pid)
        if sub and identity is not None and host.alive_and_ours(pid, identity):
            _merge(tree, sub)
    survivors = _survivors()
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
