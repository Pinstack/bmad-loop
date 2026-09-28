import json
from pathlib import Path

import pytest

from bmad_loop.signals import (
    HookEvent,
    SessionAttribution,
    SignalWatcher,
    attribute_events,
    is_session_event,
    session_events,
)


def write_event(events_dir, ts, task_id, event, **extra):
    payload = {"ts": ts, "event": event, "task_id": task_id, **extra}
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{ts}-{task_id}-{event}.json").write_text(json.dumps(payload))


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"notification_type": "permission_prompt"}, "permission_prompt"),
        ({"notification_type": 3}, None),  # a non-string is dropped, not coerced
        ({}, None),  # an older relay forwards no subtype at all
    ],
)
def test_parse_event_reads_the_notification_type(tmp_path, extra, expected):
    """DW-348: the relay's forwarded subtype lands on the HookEvent (str only)."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "Notification", **extra)
    (event,) = watcher.poll()
    assert event.event == "Notification"
    assert event.notification_type == expected


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({"source": "clear"}, "clear"),
        ({"source": 3}, None),  # a non-string is dropped, not coerced
        ({}, None),  # an older vendored relay forwards no source at all
    ],
)
def test_parse_event_reads_the_session_start_source(tmp_path, extra, expected):
    """#767: the relay's forwarded SessionStart source lands on the HookEvent
    (str only)."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "SessionStart", **extra)
    (event,) = watcher.poll()
    assert event.event == "SessionStart"
    assert event.source == expected


@pytest.mark.parametrize("value", [["A", "B"], {"id": "A"}, 3], ids=["list", "dict", "int"])
def test_parse_event_drops_a_non_string_session_id(tmp_path, value):
    """#767: a non-string id reads as absent, so attribution never hashes it —
    a list-valued child start used to raise TypeError out of the wait."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "SessionStart", session_id="A")
    write_event(
        watcher.events_dir, 2, "t1", "SessionStart", session_id=value, transcript_path=value
    )
    events = watcher.poll()
    assert [(e.session_id, e.transcript_path) for e in events] == [("A", None), (None, None)]
    attribution = SessionAttribution()
    assert [attribution.admit(e) for e in events] == [True, True]


def test_poll_returns_new_events_once(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 2, "t1", "Stop")
    write_event(watcher.events_dir, 1, "t1", "SessionStart")

    events = watcher.poll()
    assert [e.event for e in events] == ["SessionStart", "Stop"]  # sorted by ts
    assert watcher.poll() == []  # consumed


def test_poll_skips_malformed(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    (watcher.events_dir / "bad.json").write_text("{nope")
    (watcher.events_dir / "ignored.tmp").write_text("{}")
    (watcher.events_dir / "incomplete.json").write_text(json.dumps({"event": "Stop"}))
    assert watcher.poll() == []


def test_wait_for_filters_task_and_kind(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "other-task", "Stop")
    write_event(watcher.events_dir, 2, "t1", "PreCompact")
    write_event(watcher.events_dir, 3, "t1", "Stop", session_id="s-123")

    event = watcher.wait_for("t1", {"Stop", "SessionEnd"}, timeout_s=5)
    assert event is not None and event.event == "Stop" and event.session_id == "s-123"


def test_wait_for_buffers_batched_events(tmp_path):
    """SessionStart and Stop landing in one poll must BOTH be deliverable —
    regression test for events lost when several arrive between polls."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 1, "t1", "SessionStart")
    write_event(watcher.events_dir, 2, "t1", "Stop")

    kinds = {"SessionStart", "Stop", "SessionEnd"}
    first = watcher.wait_for("t1", kinds, timeout_s=1)
    second = watcher.wait_for("t1", kinds, timeout_s=1)
    assert (first.event, second.event) == ("SessionStart", "Stop")


def test_wait_for_ignores_events_before_since_ns(tmp_path):
    """A re-armed run reuses the task_id; a fresh watcher must not replay the
    previous cycle's Stop (which would read a stale result.json)."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 100, "t1", "Stop", session_id="old")  # prior cycle
    write_event(watcher.events_dir, 200, "t1", "Stop", session_id="new")  # this launch

    event = watcher.wait_for("t1", {"Stop"}, timeout_s=1, since_ns=150)
    assert event is not None and event.session_id == "new"


def test_wait_for_since_ns_times_out_when_only_stale(tmp_path):
    """When the only matching event predates the floor, wait_for must not return
    it — the session is still running, so this is a timeout."""
    watcher = SignalWatcher(tmp_path / "events")
    write_event(watcher.events_dir, 100, "t1", "Stop", session_id="old")
    now = {"t": 0.0}

    def clock():
        return now["t"]

    def sleep(seconds):
        now["t"] += seconds

    out = watcher.wait_for("t1", {"Stop"}, timeout_s=5, clock=clock, sleep=sleep, since_ns=150)
    assert out is None


def test_wait_for_timeout_with_fake_clock(tmp_path):
    watcher = SignalWatcher(tmp_path / "events")
    now = {"t": 0.0}

    def clock():
        return now["t"]

    def sleep(seconds):
        now["t"] += seconds

    assert watcher.wait_for("t1", {"Stop"}, timeout_s=10, clock=clock, sleep=sleep) is None
    assert now["t"] >= 10


# ------------------------------------------------------ dual poll (#494 skew guard)


def test_poll_sees_an_event_written_only_to_the_legacy_dir(tmp_path):
    """THE version-skew case, and the reason the legacy dir is polled at all.

    The relay a target project runs is a COPY taken at init time, so an upgraded
    orchestrator routinely drives sessions whose hook knows only the pre-#494
    in-tree `<run_dir>/events`. Nothing in the new location, everything in the old
    one, and the Stop must still be observed — the alternative is that EVERY
    session under such a project stalls to `session_timeout_min`.

    Ablation guard: drop `legacy_dir` from `_dirs()` and this fails."""
    watcher = SignalWatcher(tmp_path / "state" / "events", tmp_path / "run" / "events")
    write_event(tmp_path / "run" / "events", 1, "t1", "Stop", session_id="legacy")

    event = watcher.wait_for("t1", {"Stop"}, timeout_s=1)
    assert event is not None and event.session_id == "legacy"


def test_poll_orders_both_dirs_by_ts(tmp_path):
    """Ordering is by the event's own `ts`, not by which directory it came from —
    a run mid-upgrade could take a SessionStart from one relay and a Stop from
    another, and `wait_for`'s buffering hands them out in poll order."""
    primary = tmp_path / "state" / "events"
    legacy = tmp_path / "run" / "events"
    watcher = SignalWatcher(primary, legacy)
    write_event(legacy, 3, "t1", "Stop")
    write_event(primary, 2, "t1", "PreCompact")
    write_event(legacy, 1, "t1", "SessionStart")

    assert [e.event for e in watcher.poll()] == ["SessionStart", "PreCompact", "Stop"]


def test_poll_tolerates_a_missing_legacy_dir(tmp_path):
    """The ordinary case once every relay is current: nothing ever creates the
    in-tree dir, so it simply is not there. That must not raise — and must not be
    papered over by creating it either (see the next test)."""
    primary = tmp_path / "state" / "events"
    watcher = SignalWatcher(primary, tmp_path / "run" / "events")
    write_event(primary, 1, "t1", "Stop", session_id="s1")

    assert [e.session_id for e in watcher.poll()] == ["s1"]


def test_only_the_primary_dir_is_created(tmp_path):
    """The whole point of #494 is that the run's control plane stops living in the
    project tree. An orchestrator that re-created `<run_dir>/events` to poll it
    would put the directory back in the operator's `git status` for nothing: a
    legacy relay makes it itself, and a current one never writes there."""
    legacy = tmp_path / "run" / "events"
    SignalWatcher(tmp_path / "state" / "events", legacy)

    assert (tmp_path / "state" / "events").is_dir()
    assert not legacy.exists()


def test_the_same_file_name_in_both_dirs_yields_both_events(tmp_path):
    """`_consumed` is keyed by (dir, name), so consuming a name from one directory
    cannot mask a different event of that name in the other. The names collide on
    (ts, task_id, event), which two independent relays can produce; a masked event
    here would be a lost Stop.

    Ablation guard: key `_consumed` on `entry.name` alone and this fails."""
    primary = tmp_path / "state" / "events"
    legacy = tmp_path / "run" / "events"
    watcher = SignalWatcher(primary, legacy)
    write_event(primary, 1, "t1", "Stop", session_id="from-primary")
    write_event(legacy, 1, "t1", "Stop", session_id="from-legacy")

    assert sorted(e.session_id for e in watcher.poll()) == ["from-legacy", "from-primary"]
    assert watcher.poll() == []  # both consumed


def test_poll_still_raises_when_the_primary_dir_is_gone(tmp_path):
    """The legacy dir's absence is expected; the primary's is not — this watcher
    created it, so something removed a live run's control plane out from under it.
    Unchanged behavior, pinned here so the tolerance added for the legacy dir is
    not quietly widened to both."""
    primary = tmp_path / "state" / "events"
    watcher = SignalWatcher(primary, tmp_path / "run" / "events")
    primary.rmdir()

    with pytest.raises(OSError):
        watcher.poll()


def test_session_events_keeps_only_this_attempt_across_both_channels(tmp_path):
    """The #752 diagnostic's read: one attempt's events from the primary AND legacy
    channels, oldest first, excluding another attempt's id and anything stamped
    before this attempt's launch floor (a resumed run's same-id leftovers).

    Ablation guard: drop the task-id or the floor term from `is_session_event`, or
    the legacy dir from the scan, and this fails."""
    primary, legacy = tmp_path / "state" / "events", tmp_path / "run" / "events"
    write_event(primary, 50, "t-2", "SessionStart")
    write_event(legacy, 60, "t-2", "Stop")
    write_event(primary, 55, "t-1", "Stop")  # another attempt, inside the window
    write_event(legacy, 5, "t-2", "Stop")  # this id, but before the launch floor

    events = session_events(primary, legacy, "t-2", since_ns=10)

    assert [(e.ts, e.event) for e in events] == [(50, "SessionStart"), (60, "Stop")]


def test_session_events_is_read_only_and_tolerates_missing_dirs(tmp_path):
    """A post-mortem read neither creates a channel nor consumes from one: a relay
    that never fired may have left no directory at all, and a live watcher must
    still see every event the diagnostic looked at."""
    primary, legacy = tmp_path / "state" / "events", tmp_path / "run" / "events"
    assert session_events(primary, legacy, "t1") == []
    assert not primary.exists() and not legacy.exists()

    watcher = SignalWatcher(primary, legacy)
    write_event(primary, 1, "t1", "Stop")
    assert len(session_events(primary, legacy, "t1")) == 1
    assert watcher.wait_for("t1", {"Stop"}, timeout_s=1) is not None


def test_session_events_raises_on_an_unreadable_channel(tmp_path):
    """Only absence reads as empty; any other fault surfaces for the caller to
    report as unreadable rather than pass as "no events"."""
    primary = tmp_path / "events"
    primary.write_text("not a directory")

    with pytest.raises(OSError):
        session_events(primary, None, "t1")


def test_is_session_event_is_the_rule_wait_for_matches_on(tmp_path):
    """One correlation rule, shared by the completion wait and the diagnostic."""
    write_event(tmp_path, 7, "t1", "Stop")
    (event,) = SignalWatcher(tmp_path).poll()

    assert is_session_event(event, "t1")
    assert is_session_event(event, "t1", since_ns=7)
    assert not is_session_event(event, "t1", since_ns=8)
    assert not is_session_event(event, "t2")


def _event(kind, session_id=None, ts=1, source=None):
    return HookEvent(
        ts=ts,
        event=kind,
        task_id="t1",
        session_id=session_id,
        transcript_path=None,
        path=Path("x"),
        source=source,
    )


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        pytest.param(
            [("SessionStart", "A"), ("Stop", "A")], [True, True], id="first-identified-start-binds"
        ),
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", "A"), ("Stop", "A")],
            [True, True, True],
            id="same-id-restart-admitted",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B"),
                ("Stop", "B"),
                ("SessionEnd", "B"),
                ("Stop", "A"),
            ],
            [True, False, False, False, True],
            id="announced-child-is-foreign",
        ),
        pytest.param(
            [("SessionStart", None), ("SessionStart", "B"), ("Stop", "B"), ("Stop", "A")],
            [True, False, False, True],
            id="anonymous-first-start-uses-the-parent-slot",
        ),
        pytest.param(
            [("SessionStart", "A"), ("Stop", "B"), ("SessionEnd", "B")],
            [True, True, True],
            id="unannounced-rotated-id-admitted",  # M1: /clear or compaction
        ),
        pytest.param(
            [("SessionEnd", "A")], [True], id="identified-end-before-any-start-admitted"
        ),  # B2: the #727 trust-dialog exit
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", None), ("Stop", None), ("SessionEnd", None)],
            [True, True, True, True],
            id="id-less-events-always-admitted",
        ),
        pytest.param(
            [("SessionStart", "main"), ("Stop", "toolu_bdrk_x"), ("Stop", "main")],
            [True, True, True],
            id="never-announced-toolu-stop-admitted",  # the copilot subagent filter owns it
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "clear"),
                ("Stop", "B"),
                ("SessionStart", "C", "startup"),
                ("Stop", "C"),
                ("Stop", "B"),
            ],
            [True, True, True, False, False, True],
            id="clear-start-rebinds-then-startup-child-is-foreign",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "compact"),
                ("Stop", "B"),
                ("SessionStart", "C", "startup"),
                ("Stop", "C"),
                ("Stop", "B"),
            ],
            [True, True, True, False, False, True],
            id="compact-start-rebinds-then-startup-child-is-foreign",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "startup"),
                ("SessionStart", "B", "compact"),
                ("Stop", "B"),
                ("Stop", "A"),
            ],
            [True, False, False, False, True],
            id="foreign-id-compacting-stays-foreign",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "startup"),
                ("SessionEnd", "B"),
                ("SessionStart", "C", "clear"),
                ("Stop", "C"),
                ("Stop", "A"),
            ],
            [True, False, False, False, False, True],
            id="clear-after-foreign-end-is-the-child-clearing",
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionEnd", "A"),
                ("SessionStart", "B", "clear"),
                ("Stop", "B"),
            ],
            [True, True, True, True],
            id="clear-after-own-end-rebinds",  # claude ends the old id before a clear start
        ),
        pytest.param(
            [
                ("SessionStart", "A"),
                ("SessionStart", "B", "startup"),
                ("SessionEnd", "A"),
                ("SessionEnd", "B"),
                ("SessionStart", "C", "clear"),
                ("Stop", "C"),
            ],
            [True, False, True, False, True, True],
            id="child-end-racing-the-parent-clear-still-rebinds",
        ),
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", "B", "resume"), ("Stop", "B")],
            [True, False, False],
            id="resume-start-is-foreign",  # a nested child launched with --resume
        ),
        pytest.param(
            [("SessionStart", "A"), ("SessionStart", "B", None), ("Stop", "B")],
            [True, False, False],
            id="sourceless-start-is-foreign",  # an older relay: no rebind
        ),
    ],
)
def test_session_attribution_admits(sequence, expected):
    """#767: the deny-list rule, event by event. Only an id that announced its
    own SessionStart after the launched session's first one is dropped."""
    attribution = SessionAttribution()
    admitted = []
    for kind, sid, *source in sequence:  # an optional third item is the start's source
        admitted.append(attribution.admit(_event(kind, sid, source=source[0] if source else None)))
    assert admitted == expected


def test_session_attribution_clear_start_moves_the_binding():
    """#767: a clear/compact start with a new id is the launched session
    rotating its id, so the binding follows it rather than marking it foreign."""
    attribution = SessionAttribution()
    attribution.admit(_event("SessionStart", "A"))
    assert attribution.admit(_event("SessionStart", "B", source="clear"))
    assert attribution.bound_id == "B"
    assert attribution.foreign_ids == set()


def test_attribute_events_returns_admitted_and_foreign_ids():
    """The replay form the post-mortem diagnostic uses: admitted events in
    order, plus every id found foreign."""
    events = [
        _event("SessionStart", "A", ts=1),
        _event("SessionStart", "B", ts=2),
        _event("Stop", "B", ts=3),
        _event("SessionStart", "C", ts=4),
        _event("Stop", "A", ts=5),
    ]
    admitted, foreign = attribute_events(events)
    assert [(e.event, e.session_id) for e in admitted] == [("SessionStart", "A"), ("Stop", "A")]
    assert foreign == {"B", "C"}
