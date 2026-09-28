"""Watch the per-run events directory for hook-written event files.

The hook script (data/bmad_loop_hook.py) and its importable twin (:mod:`events`,
behind ``bmad-loop relay``) write one JSON file per event, atomically (tmp +
rename), named "<ts_ns>-<task_id>-<event>.json". Plain polling of a near-empty
directory is cheap and crash-safe; no inotify.

Two directories, not one (#494). The channel now lives out of the project tree,
under the user-scoped state root (``runs.events_dir_for``), because a branch
switch, a worktree mount or a rollback must not be able to take a live run's
control plane away. The *legacy* in-tree ``<run_dir>/events`` stays polled as
well, and that is the whole version-skew guard: the relay a target project has
installed is a COPY taken at init time, so an upgraded orchestrator routinely
drives sessions whose hook only knows the old location. Without the second poll
that pairing loses every Stop event and every session stalls to
``session_timeout_min`` — the loudest possible regression, delivered silently.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class HookEvent:
    ts: int
    event: str  # Stop | SessionStart | SessionEnd | PreCompact | Notification | parked kinds
    task_id: str
    session_id: str | None
    transcript_path: str | None
    path: Path
    # A Notification event's subtype, forwarded by the relay (DW-348) — e.g.
    # claude's "permission_prompt". None on every other event, on an older
    # relay that predates the field, and for a non-string value. APPENDED with a
    # default so every positional construction stays valid.
    notification_type: str | None = None
    # A SessionStart's `source` (#767) — claude/gemini's startup|resume|clear|
    # compact, or another CLI's own value. None on every other event, on an older
    # vendored relay that predates the field, and for a non-string value.
    # APPENDED with a default, like notification_type above.
    source: str | None = None


def _event_dirs(events_dir: Path, legacy_dir: Path | None) -> list[Path]:
    """Primary first, then the legacy dir when there is a distinct one. The
    equality check keeps a caller that passes the same path twice from
    double-scanning; it cannot produce duplicate events either way (the
    watcher's ``_consumed`` key would repeat), but the second scan would be
    pure waste."""
    if legacy_dir is None or legacy_dir == events_dir:
        return [events_dir]
    return [events_dir, legacy_dir]


def _parse_event(entry: Path) -> HookEvent | None:
    """One event file, or ``None`` when it is not a well-formed event."""
    try:
        data = json.loads(entry.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "event" not in data or "task_id" not in data:
        return None
    session_id = data.get("session_id")
    transcript_path = data.get("transcript_path")
    notification_type = data.get("notification_type")
    source = data.get("source")
    # Payload values are forwarded from the CLI unvalidated, so a non-string one
    # reads as absent rather than reaching attribution's set arithmetic (#767).
    return HookEvent(
        ts=int(data.get("ts", 0)),
        event=str(data["event"]),
        task_id=str(data["task_id"]),
        session_id=session_id if isinstance(session_id, str) else None,
        transcript_path=transcript_path if isinstance(transcript_path, str) else None,
        path=entry,
        notification_type=notification_type if isinstance(notification_type, str) else None,
        source=source if isinstance(source, str) else None,
    )


def is_session_event(event: HookEvent, task_id: str, since_ns: int = 0) -> bool:
    """The one rule that ties a hook event to a session attempt. ``task_id`` is
    the session's own id, which folds in the attempt number and generation
    (``engine._session_task_id``), so another attempt's events never match;
    ``since_ns`` is that attempt's launch floor, which drops a stale event a
    resumed run left under the same re-minted id (see ``SignalWatcher.wait_for``).

    It is the first of two layers. The task id comes from the relay's inherited
    environment, so a nested coding-CLI process started inside the session
    stamps its own events with the same id; :class:`SessionAttribution` is the
    second layer, deciding which CLI session inside the attempt's stream is the
    launched one."""
    return event.task_id == task_id and (not since_ns or event.ts >= since_ns)


# SessionStart sources that keep the launched session's identity across an id
# change: claude/gemini rotate the session id on /clear and may on compaction.
REBIND_SOURCES = frozenset({"clear", "compact"})


@dataclass
class SessionAttribution:
    """Second layer after :func:`is_session_event`: which CLI session inside one
    attempt's event stream is the launched one (#767).

    A nested coding-CLI process started from inside the session inherits the
    relay environment, so its SessionStart/Stop/SessionEnd land in the parent's
    stream under the parent's task id. The rule is a deny-list: an id is foreign
    only once it has announced its own SessionStart after the launched session's
    first SessionStart. Every other event is admitted — id-less events, ids that
    never announced a start (a rotated id, a Copilot ``toolu_`` subagent Stop),
    and anything before the first start, including an identified SessionEnd from
    a CLI that exited before its SessionStart fired (#727). Failing toward
    acceptance keeps attribution a pure filter: it only ever drops a known
    child's events and never adds a completion path.

    The first SessionStart is the launched session's whether identified or not:
    an anonymous start (a payload the relay could not read) still uses up the
    parent's slot, so a child's identified start after it is foreign.

    A later SessionStart with a new id whose ``source`` is in
    :data:`REBIND_SOURCES` ("clear", "compact") is the launched session itself
    rotating its id, so it rebinds rather than going foreign. Deliberately not
    "resume" — a nested child launched with ``--resume`` must stay foreign, and a
    bmad-loop resume is a new attempt with a fresh task id and so a fresh
    attribution — and not "startup", which is exactly what a nested child sends.
    An older vendored relay forwards no ``source``, so there a clear/compact start
    with a new id reads as foreign and the session falls back to window death or
    its timeout; ``bmad-loop init`` re-vendors the relay.

    ``source`` alone is not trusted. An id already found foreign never rebinds
    (a child compacting under its own id), and a "clear" start after a foreign
    id's SessionEnd is that child clearing — claude ends the old session with a
    SessionEnd before the clear start — so the new id is foreign too, unless the
    bound session also ended since the last start (the two relays write
    independently, so a child's end can land between the parent's end and its
    clear start). A child that rotates its id without a preceding SessionEnd
    still rebinds; only a relay-side lineage check could tell it apart.

    Accepted limitation: a child SessionEnd whose child never announced a
    SessionStart is indistinguishable from the parent's own and is admitted.
    Nested CLIs announce their start, so this is documented, not defended."""

    started: bool = False  # the launched session's first SessionStart was seen
    bound_id: str | None = None  # its id (None when that start was anonymous)
    foreign_ids: set[str] = field(default_factory=set)
    # Which sessions ended since the last SessionStart: evidence for whose
    # "clear" start comes next.
    bound_ended: bool = False
    foreign_ended: bool = False

    def admit(self, event: HookEvent) -> bool:
        """Whether ``event`` belongs to the launched session. Stateful: a
        SessionStart can bind the session or mark its id foreign."""
        sid = event.session_id
        if event.event == "SessionStart":
            bound_ended, foreign_ended = self.bound_ended, self.foreign_ended
            self.bound_ended = self.foreign_ended = False
            if not self.started:
                self.started, self.bound_id = True, sid
                return True
            if not sid or sid == self.bound_id:
                return True
            if (
                sid not in self.foreign_ids
                and event.source in REBIND_SOURCES
                and not (event.source == "clear" and foreign_ended and not bound_ended)
            ):
                self.bound_id = sid
                return True
            self.foreign_ids.add(sid)
            return False
        if event.event == "SessionEnd" and sid:
            if sid == self.bound_id:
                self.bound_ended = True
            elif sid in self.foreign_ids:
                self.foreign_ended = True
        return not (sid and sid in self.foreign_ids)


def attribute_events(events: list[HookEvent]) -> tuple[list[HookEvent], set[str]]:
    """Replay :class:`SessionAttribution` over an oldest-first snapshot (e.g.
    :func:`session_events`): the admitted events, and every id found foreign."""
    attribution = SessionAttribution()
    admitted = [event for event in events if attribution.admit(event)]
    return admitted, attribution.foreign_ids


def session_events(
    events_dir: Path, legacy_dir: Path | None, task_id: str, since_ns: int = 0
) -> list[HookEvent]:
    """Every event on disk for one session attempt, across both channels, oldest
    first — a read-only snapshot for post-mortem diagnosis (#752), never a
    completion signal. Unlike ``SignalWatcher.poll`` it creates nothing and
    consumes nothing, so it sees events the watcher already delivered. A missing
    directory, primary included, reads as empty (a run whose relay never fired
    may never have had one made); any other ``OSError`` raises to the caller."""
    events: list[HookEvent] = []
    for directory in _event_dirs(events_dir, legacy_dir):
        try:
            entries = list(directory.iterdir())
        except FileNotFoundError:
            continue
        for entry in entries:
            if entry.suffix != ".json":
                continue
            event = _parse_event(entry)
            if event is not None and is_session_event(event, task_id, since_ns):
                events.append(event)
    events.sort(key=lambda e: e.ts)
    return events


class SignalWatcher:
    """Poll one or two event directories for a run's hook events.

    ``events_dir`` is the primary — the out-of-tree channel this orchestrator
    directs its sessions to via ``BMAD_LOOP_EVENTS_DIR``, and the only one
    created here. ``legacy_dir`` is the pre-#494 in-tree ``<run_dir>/events``,
    polled when given so a project carrying an older installed relay still
    completes its sessions (see the module docstring). It is deliberately NOT
    created: an orchestrator that recreated the in-tree directory would undo the
    move for the operator's `git status` while gaining nothing — a legacy relay
    that writes there makes the directory itself.

    The single-positional-argument form is unchanged, and stays the shape the
    probe (``probe.py``, watching its own capture dir) and the unit tests use.
    """

    def __init__(self, events_dir: Path, legacy_dir: Path | None = None):
        self.events_dir = events_dir
        self.legacy_dir = legacy_dir
        # Keyed by (directory, filename), not by filename: one name identifies an
        # event only WITHIN a directory, and the two dirs are written by two
        # independent relays. Keying on the name alone would let a file consumed
        # from one dir mask a different event of the same name in the other, and a
        # masked event here is a lost Stop — the run then waits out
        # session_timeout_min with its completion signal sitting on disk.
        self._consumed: set[tuple[str, str]] = set()
        self._pending: list[HookEvent] = []  # polled but not yet delivered via wait_for
        events_dir.mkdir(parents=True, exist_ok=True)

    def _dirs(self) -> list[Path]:
        return _event_dirs(self.events_dir, self.legacy_dir)

    def poll(self) -> list[HookEvent]:
        """Return new, well-formed events since the last poll, oldest first.

        Ordering is by the parsed ``ts`` across BOTH directories, so which relay
        wrote an event never affects where it lands in the sequence.

        A missing *legacy* directory is the normal case (nothing installed writes
        there any more) and is skipped silently. A missing *primary* still raises,
        as it always has: this watcher created it, so its absence means something
        removed the live control plane out from under the run.
        """
        events: list[HookEvent] = []
        dirs = self._dirs()
        for directory in dirs:
            try:
                entries = list(directory.iterdir())
            except OSError:
                if directory == self.events_dir:
                    raise
                continue  # legacy dir absent — the ordinary case
            for entry in entries:
                key = (str(directory), entry.name)
                if key in self._consumed or entry.suffix != ".json":
                    continue
                self._consumed.add(key)
                event = _parse_event(entry)
                if event is not None:
                    events.append(event)
        events.sort(key=lambda e: e.ts)
        return events

    def wait_for(
        self,
        task_id: str,
        kinds: set[str],
        timeout_s: float,
        poll_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        since_ns: int = 0,
    ) -> HookEvent | None:
        """Block until an event for task_id with kind in `kinds` arrives, or timeout.

        Events polled but not matched stay buffered for later wait_for calls —
        several events often land in one poll (e.g. SessionStart + Stop) and
        none may be lost.

        Events older than `since_ns` (wall-clock ns, the session's launch time)
        are dropped: a resumed run reuses task_ids, and a fresh watcher re-sees the
        events directory from scratch, so a prior cycle's Stop event would
        otherwise replay instantly and the old result.json be read as a bogus
        completion. Sessions run sequentially, so since_ns only advances; anything
        below the current floor is genuinely stale and safe to discard.

        A re-ARMED run no longer reuses them — `runs.rearm_escalation` bumps
        `StoryTask.generation`, which `engine._session_task_id` folds into the id
        (#705). That removes one source of collision; it does not make this floor
        redundant, because a plain resume re-mints the SAME id by design (that is
        how crash replay finds its record) and is the case this guard was written
        for.
        """
        deadline = clock() + timeout_s
        while True:
            self._pending.extend(self.poll())
            if since_ns:
                self._pending = [e for e in self._pending if e.ts >= since_ns]
            for i, event in enumerate(self._pending):
                if is_session_event(event, task_id, since_ns) and event.event in kinds:
                    return self._pending.pop(i)
            if clock() >= deadline:
                return None
            sleep(poll_interval)
