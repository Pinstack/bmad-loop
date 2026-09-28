"""Unit tests for the detect-only Unity modal-dialog probe (``unity_dialog_probe.py``).

The probe is launched detached by ``UnityPlugin`` to watch (via xdotool, X11 only)
for the run-freezing Unity modal dialogs and REPORT them — it never clicks or keys
anything. These drive its pure detection / report / dedupe functions with a stubbed
xdotool runner (no real Editor or X server), and its ``main()`` no-op skips. The
plugin-side lifecycle (launch at pre_run/pre_worktree_setup, reap at post_run /
unity_teardown.py) lives in ``test_engine_plugin.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os

from bmad_loop.plugins import get_plugin


def _load_probe():
    path = os.path.join(get_plugin("unity").scripts_dir, "unity_dialog_probe.py")
    spec = importlib.util.spec_from_file_location("unity_dialog_probe_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _make_run(windows):
    """A stub xdotool runner. ``windows`` maps window id -> title: ``search`` returns
    the ids (exit 1 when none, as real xdotool does), ``getwindowname`` the title."""

    def run(args, timeout=10.0):
        if args[0] == "search":
            return _FakeProc(0 if windows else 1, "\n".join(windows))
        if args[0] == "getwindowname":
            return _FakeProc(0, windows.get(args[1], ""))
        return _FakeProc(0, "")

    return run


# ------------------------------------------------------------------ detection


def test_matches_dialog_is_case_insensitive_substring():
    mod = _load_probe()
    assert mod._matches_dialog("Scene(s) Have Been Modified")
    assert mod._matches_dialog("Do you want to SAVE the changes?")
    assert not mod._matches_dialog("MyGame - Main - PC, Mac & Linux - Unity 2022.3")


def test_scan_only_flags_dialog_titled_windows():
    """The main Editor window is ignored; only a window whose title matches a known
    modal-dialog phrase is a hit."""
    mod = _load_probe()
    run = _make_run({"111": "MyGame - Main - Unity 2022", "222": "Scene(s) Have Been Modified"})
    assert mod._scan_for_dialogs(run) == [("222", "Scene(s) Have Been Modified")]


def test_scan_empty_when_no_unity_windows():
    """xdotool exits non-zero when nothing matches — the normal 'no dialog' case,
    not an error — so the scan is empty, never a crash."""
    mod = _load_probe()
    assert mod._scan_for_dialogs(_make_run({})) == []


# --------------------------------------------------------------- report / dedupe


def test_report_writes_jsonl_and_attention(tmp_path, monkeypatch):
    mod = _load_probe()
    monkeypatch.setattr(mod.shutil, "which", lambda _c: None)  # no notify-send present
    mod._report(tmp_path, "999", "conflicting scene changes", notify=True)

    rec = json.loads((tmp_path / mod.PROBE_JSONL).read_text(encoding="utf-8").strip())
    assert rec["window"] == "999"
    assert rec["title"] == "conflicting scene changes"
    assert "ts" in rec
    # ATTENTION line matches gates.notify's "[stamp] title: message" shape
    att = (tmp_path / mod.ATTENTION_FILE).read_text(encoding="utf-8")
    assert att.startswith("[")
    assert "Unity modal dialog detected: window 999: conflicting scene changes" in att


def test_report_notify_gating(tmp_path, monkeypatch):
    mod = _load_probe()
    called = []
    monkeypatch.setattr(mod.shutil, "which", lambda _c: "/usr/bin/notify-send")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: called.append(a))
    mod._report(tmp_path, "1", "changed on disk", notify=True)
    assert called and called[0][0][0] == "notify-send"  # notify fired
    called.clear()
    mod._report(tmp_path, "2", "changed on disk", notify=False)
    assert called == []  # notify disabled → JSONL/ATTENTION only, no desktop popup


def test_report_shapes_an_untrusted_title_like_gates_notify(tmp_path, monkeypatch):
    """DW-418: the X11 window title is untrusted. ATTENTION gets exactly ONE line with
    the title shaped by ``gates.notice_line`` (control bytes escaped, line breaks
    folded), notify-send gets the same shaped text after ``--`` (as
    ``gates._notifier_argv`` builds it), and the JSONL keeps the raw title.

    Ablation: write the raw ``message`` in ``_report`` and a forged ``fake line``
    record lands in ATTENTION; drop ``--`` and the argv assertion reddens."""
    mod = _load_probe()
    calls = []
    monkeypatch.setattr(mod.shutil, "which", lambda _c: "/usr/bin/notify-send")
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **k: calls.append(argv))
    raw = "Save?\x1b[31m\nfake line"
    mod._report(tmp_path, "7", raw, notify=True)

    shaped = "window 7: Save?\\x1b[31m ⏎ fake line"
    attention = (tmp_path / mod.ATTENTION_FILE).read_text(encoding="utf-8")
    assert attention.count("\n") == 1 and attention.endswith("\n")
    assert attention.startswith("[")
    assert attention.rstrip("\n").endswith(f"] Unity modal dialog detected: {shaped}")
    assert "\x1b" not in attention
    assert calls == [
        ["notify-send", "--app-name=bmad-loop", "--", "Unity modal dialog detected", shaped]
    ]
    rec = json.loads((tmp_path / mod.PROBE_JSONL).read_text(encoding="utf-8"))
    assert rec["title"] == raw  # the full-detail record stays raw


def test_report_passes_a_dash_title_after_the_option_terminator(tmp_path, monkeypatch):
    """The notify-send argv carries ``--`` before the summary/body, mirroring
    ``gates._notifier_argv`` (DW-418). Defense-in-depth: the body always starts with
    ``window <id>: `` today, so even a ``-``-leading title is not an option.

    Ablation: drop ``--`` from the argv and this reddens."""
    mod = _load_probe()
    calls = []
    monkeypatch.setattr(mod.shutil, "which", lambda _c: "/usr/bin/notify-send")
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **k: calls.append(argv))
    mod._report(tmp_path, "8", "--help save changes before", notify=True)
    (argv,) = calls
    assert argv[:3] == ["notify-send", "--app-name=bmad-loop", "--"]
    assert argv[3:] == ["Unity modal dialog detected", "window 8: --help save changes before"]


def test_report_survives_a_lone_surrogate_title(tmp_path, monkeypatch):
    """A lone surrogate in the title is escaped visibly, so the ATTENTION write
    cannot raise ``UnicodeEncodeError`` (DW-419's shaping, shared via
    ``gates.notice_line``)."""
    mod = _load_probe()
    monkeypatch.setattr(mod.shutil, "which", lambda _c: None)
    mod._report(tmp_path, "9", "bad \udcff do you want to save", notify=True)
    attention = (tmp_path / mod.ATTENTION_FILE).read_text(encoding="utf-8")
    assert "window 9: bad \\udcff do you want to save" in attention


def test_scan_once_dedupes_repeat_detections(tmp_path):
    mod = _load_probe()
    run = _make_run({"222": "save changes before closing"})
    seen: set[str] = set()
    assert mod._scan_once(run, tmp_path, seen, notify=False) == 1
    assert mod._scan_once(run, tmp_path, seen, notify=False) == 0  # same wid → no re-report
    lines = (tmp_path / mod.PROBE_JSONL).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # exactly one record for the window


def test_scan_once_reports_each_distinct_window(tmp_path):
    mod = _load_probe()
    run = _make_run(
        {"1": "changed on disk", "2": "conflicting scene changes", "3": "unrelated editor"}
    )
    seen: set[str] = set()
    assert mod._scan_once(run, tmp_path, seen, notify=False) == 2  # two dialogs, editor skipped
    assert seen == {"1", "2"}


# ----------------------------------------------------------------- main() skips


def test_interval_default_floor_and_bad_value(monkeypatch):
    mod = _load_probe()
    monkeypatch.delenv("BMAD_LOOP_UNITY_DIALOG_PROBE_INTERVAL_SEC", raising=False)
    assert mod._interval() == 5.0
    monkeypatch.setenv("BMAD_LOOP_UNITY_DIALOG_PROBE_INTERVAL_SEC", "0")
    assert mod._interval() == 1.0  # floored to the 1s minimum
    monkeypatch.setenv("BMAD_LOOP_UNITY_DIALOG_PROBE_INTERVAL_SEC", "bogus")
    assert mod._interval() == 5.0


def test_main_noop_without_display(monkeypatch, capsys):
    mod = _load_probe()
    monkeypatch.delenv("DISPLAY", raising=False)
    assert mod.main() == 0
    assert "DISPLAY unset" in capsys.readouterr().err


def test_main_noop_without_xdotool(monkeypatch):
    mod = _load_probe()
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(mod.shutil, "which", lambda _c: None)
    assert mod.main() == 0


def test_main_noop_without_run_dir(monkeypatch, capsys):
    mod = _load_probe()
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(mod.shutil, "which", lambda _c: "/usr/bin/xdotool")
    monkeypatch.delenv("BMAD_LOOP_RUN_DIR", raising=False)
    assert mod.main() == 0
    assert "refusing to run" in capsys.readouterr().err


def test_main_scans_once_then_exits_when_engine_dies(tmp_path, monkeypatch):
    """The loop runs while the engine pid is alive, detects+reports a dialog, and
    exits when engine_alive flips to False — leaving its pid-file reap handle."""
    mod = _load_probe()
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("BMAD_LOOP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(mod.shutil, "which", lambda c: "/usr/bin/" + c)
    monkeypatch.setattr(mod.time, "sleep", lambda *_a: None)
    alive = iter([True, False])  # alive for the first pass, dead thereafter
    monkeypatch.setattr(mod, "engine_alive", lambda _rd: next(alive, False))

    def fake_xdotool(args, timeout=10.0):
        if args[0] == "search":
            return _FakeProc(0, "555")
        if args[0] == "getwindowname":
            return _FakeProc(0, "Do you want to save the changes")
        return _FakeProc(0, "")

    monkeypatch.setattr(mod, "_run_xdotool", fake_xdotool)

    assert mod.main() == 0
    lines = (tmp_path / mod.PROBE_JSONL).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # detected + reported exactly once
    assert (tmp_path / mod.PROBE_PID_FILE).exists()  # wrote its primary reap handle
