"""gates.notify — the ATTENTION file sink and the cross-platform desktop
notifier dispatch added for #231 (native macOS/Windows notifications, plus the
untrusted-text-via-env invariant that keeps user text out of the command string)."""

from __future__ import annotations

import subprocess

import pytest

from bmad_loop import gates
from bmad_loop.policy import NotifyPolicy, Policy


def _policy(*, desktop: bool, file: bool) -> Policy:
    return Policy(notify=NotifyPolicy(desktop=desktop, file=file))


def _capture_run(monkeypatch):
    """Record every (argv, kwargs) `gates.notify` hands to `subprocess.run`."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(gates.subprocess, "run", fake_run)
    return calls


# ------------------------------------------------------------- file sink


def test_notify_file_appends_attention_line(tmp_path):
    gates.notify(_policy(desktop=False, file=True), tmp_path, "worktree-open-failed", "mount lost")
    line = (tmp_path / gates.ATTENTION_FILE).read_text(encoding="utf-8")
    assert line.startswith("[")  # timestamp stamp
    assert "worktree-open-failed: mount lost" in line


def test_notify_file_swallows_an_unwritable_attention_path(tmp_path, monkeypatch):
    """The "never raises" contract covers the file sink too. An unwritable
    ATTENTION path is observability degrading, not a reason to break the run —
    every engine caller notifies on a path where a raise would crash the run or
    unwind a decision it has already journaled.

    A directory at the ATTENTION path is the portable way to make the append
    fail: IsADirectoryError on POSIX, PermissionError on Windows, both OSError.
    chmod would not do it under root (CI) or on Windows.
    """
    (tmp_path / gates.ATTENTION_FILE).mkdir()
    # the desktop half must not be what absorbs this
    monkeypatch.setattr(gates, "desktop_notifier_kind", lambda: None)

    gates.notify(_policy(desktop=False, file=True), tmp_path, "story deferred: 1-1-a", "verify")


# ------------------------------------------------ desktop_notifier_kind()


@pytest.mark.parametrize(
    ("platform", "present", "expected"),
    [
        ("darwin", "osascript", "osascript"),
        ("win32", "powershell", "powershell"),
        ("linux", "notify-send", "notify-send"),
    ],
)
def test_desktop_notifier_kind_per_platform(monkeypatch, platform, present, expected):
    monkeypatch.setattr(gates.sys, "platform", platform)
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd == present else None
    )
    assert gates.desktop_notifier_kind() == expected


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_desktop_notifier_kind_none_when_tool_absent(monkeypatch, platform):
    monkeypatch.setattr(gates.sys, "platform", platform)
    monkeypatch.setattr(gates.shutil, "which", lambda _cmd: None)
    assert gates.desktop_notifier_kind() is None


def test_desktop_notifier_kind_win32_accepts_pwsh(monkeypatch):
    """PowerShell Core satisfies the Windows branch even without Windows PowerShell."""
    monkeypatch.setattr(gates.sys, "platform", "win32")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "/usr/bin/pwsh" if cmd == "pwsh" else None
    )
    assert gates.desktop_notifier_kind() == "powershell"


def test_desktop_notifier_kind_linux_ignores_pwsh(monkeypatch):
    """`sys.platform` gates first: pwsh on Linux must not divert from notify-send."""
    monkeypatch.setattr(gates.sys, "platform", "linux")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "/usr/bin/pwsh" if cmd == "pwsh" else None
    )
    assert gates.desktop_notifier_kind() is None  # notify-send absent → nothing, not pwsh


# --------------------------------------------------- desktop dispatch


def test_notify_macos_runs_osascript_via_env(monkeypatch, tmp_path):
    monkeypatch.setattr(gates.sys, "platform", "darwin")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "/usr/bin/osascript" if cmd == "osascript" else None
    )
    calls = _capture_run(monkeypatch)

    message = 'he said "done"\nnow'  # a quote+newline that would break AppleScript if interpolated
    title = "story deferred: 1-2-a"
    gates.notify(_policy(desktop=True, file=False), tmp_path, title, message)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == "osascript"
    # untrusted text travels through env, never interpolated into the command string
    assert kwargs["env"][gates._TITLE_ENV] == title
    # the newline is folded by the shaping chokepoint before it reaches the toast
    assert kwargs["env"][gates._MESSAGE_ENV] == 'he said "done" ⏎ now'
    assert not any("done" in part for part in argv)
    assert not any("1-2-a" in part for part in argv)


def test_notify_windows_runs_powershell(monkeypatch, tmp_path):
    monkeypatch.setattr(gates.sys, "platform", "win32")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "C:\\powershell.exe" if cmd == "powershell" else None
    )
    calls = _capture_run(monkeypatch)

    # both title and message carry PowerShell/shell-sensitive text that must never
    # reach argv — they travel through env, and the -Command script reads $env:.
    title = 'escalation `$(rm)`;"& evil'
    message = 'toast $(bad) "body"'
    gates.notify(_policy(desktop=True, file=False), tmp_path, title, message)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0].lower().endswith(("powershell.exe", "pwsh"))
    assert "-Command" in argv
    assert kwargs["env"][gates._TITLE_ENV] == title
    assert kwargs["env"][gates._MESSAGE_ENV] == message
    # neither the title nor the message is interpolated into the command string
    assert not any(title in part for part in argv)
    assert not any(message in part for part in argv)
    # the toast is delivered under a registered AUMID (Windows drops unregistered ids)
    assert any("WindowsPowerShell" in part for part in argv)


def test_notify_linux_runs_notify_send(monkeypatch, tmp_path):
    monkeypatch.setattr(gates.sys, "platform", "linux")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None
    )
    calls = _capture_run(monkeypatch)

    # option-shaped title/message must not be parsed as notify-send options: the `--`
    # terminator forces GLib to treat them as positional SUMMARY/BODY text.
    gates.notify(_policy(desktop=True, file=False), tmp_path, "--title", "--help")

    assert len(calls) == 1
    argv, kwargs = calls[0]
    # `--` sits before the untrusted text, so a leading-dash payload stays positional
    assert argv == ["notify-send", "--app-name=bmad-loop", "--", "--title", "--help"]
    assert kwargs["env"] is None  # no env override for the notify-send path


def test_notify_desktop_swallows_value_error(monkeypatch, tmp_path):
    """An embedded NUL in the untrusted text makes subprocess.run raise ValueError
    (not a SubprocessError, which is not a ValueError subclass); the best-effort
    boundary must still swallow it rather than crash the run."""
    monkeypatch.setattr(gates.sys, "platform", "linux")
    monkeypatch.setattr(gates.shutil, "which", lambda _cmd: "/usr/bin/notify-send")

    def boom(*_a, **_k):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(gates.subprocess, "run", boom)
    # must not propagate
    gates.notify(_policy(desktop=True, file=False), tmp_path, "title", "mid\x00nul")


def test_notify_windows_dispatches_via_pwsh_only(monkeypatch, tmp_path):
    """PowerShell Core alone (no powershell.exe) still dispatches the toast — the
    dispatch path must select whichever of pwsh/powershell shutil.which resolves."""
    monkeypatch.setattr(gates.sys, "platform", "win32")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "/usr/bin/pwsh" if cmd == "pwsh" else None
    )
    calls = _capture_run(monkeypatch)

    gates.notify(_policy(desktop=True, file=False), tmp_path, "title", "message")

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0].endswith("pwsh")  # not powershell.exe
    assert "-Command" in argv
    assert kwargs["env"][gates._TITLE_ENV] == "title"


def test_notify_desktop_noop_when_no_notifier(monkeypatch, tmp_path):
    monkeypatch.setattr(gates.sys, "platform", "darwin")
    monkeypatch.setattr(gates.shutil, "which", lambda _cmd: None)
    calls = _capture_run(monkeypatch)

    gates.notify(_policy(desktop=True, file=False), tmp_path, "title", "message")

    assert calls == []  # no notifier resolved → subprocess.run never called


def test_notify_desktop_swallows_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(gates.sys, "platform", "linux")
    monkeypatch.setattr(gates.shutil, "which", lambda _cmd: "/usr/bin/notify-send")

    def boom(*_a, **_k):
        raise OSError("no dbus")

    monkeypatch.setattr(gates.subprocess, "run", boom)
    # best-effort: a failing notifier must not propagate out of notify()
    gates.notify(_policy(desktop=True, file=False), tmp_path, "title", "message")


# ----------------------------------------------- notice shaping (DW-13/DW-332)

_ONE_LINE_ROWS = [
    pytest.param("story gated: x", "story gated: x", id="plain-line-unchanged"),
    pytest.param(
        "verify command failed (rc=1): pytest\nFAILED a\n\n  FAILED b\n",
        "verify command failed (rc=1): pytest ⏎ FAILED a ⏎ FAILED b",
        id="multi-line-reason-folds",
    ),
    pytest.param("a\r\nb\rc\vd\fe", "a ⏎ b ⏎ c ⏎ d ⏎ e", id="crlf-cr-vt-ff"),
    pytest.param("a\x1cb\x1dc\x1ed", "a ⏎ b ⏎ c ⏎ d", id="file-group-record-separators"),
    pytest.param("a\x85b\u2028c\u2029d", "a ⏎ b ⏎ c ⏎ d", id="nel-ls-ps"),
    pytest.param("\x1b[31mred\x1b[0m", "\\x1b[31mred\\x1b[0m", id="esc"),
    pytest.param("ding\x07", "ding\\x07", id="bel"),
    pytest.param("mid\x00nul", "mid\\x00nul", id="nul"),
    pytest.param("del\x7f", "del\\x7f", id="del"),
    pytest.param("csi\x9b", "csi\\x9b", id="c1-csi"),
    pytest.param("col\tumn", "col umn", id="tab-to-space"),
    pytest.param("bad \udcff path", "bad \\udcff path", id="lone-low-surrogate"),
    pytest.param("hi\ud800gh", "hi\\ud800gh", id="lone-high-surrogate"),
    pytest.param("", "", id="empty"),
    pytest.param(" \n ", "", id="blank"),
]


@pytest.mark.parametrize(("raw", "shaped"), _ONE_LINE_ROWS)
def test_notice_line_matrix(raw, shaped):
    assert gates.notice_line(raw) == shaped


def test_notice_line_leaves_a_clean_line_byte_for_byte():
    """No line break, no control character, under the cap: returned as-is —
    leading/trailing spaces and non-ASCII included."""
    for text in ("  padded  ", "unicodé — ok […]", "x" * gates.NOTICE_LINE_MAX):
        assert gates.notice_line(text) == text


def test_notice_line_caps_with_the_journal_naming_marker():
    """Over the backstop cap the line is cut, rstripped and marked; the marker
    names where the full text lives and is the one escalation's CRITICAL display
    truncation uses."""
    capped = gates.notice_line("y" * (gates.NOTICE_LINE_MAX + 1))
    assert len(capped) == gates.NOTICE_LINE_MAX
    assert capped.endswith(gates.NOTICE_TRUNCATION_MARKER)
    assert "journal.jsonl" in gates.NOTICE_TRUNCATION_MARKER
    assert capped == "y" * (gates.NOTICE_LINE_MAX - len(gates.NOTICE_TRUNCATION_MARKER)) + (
        gates.NOTICE_TRUNCATION_MARKER
    )
    # the cut lands on whitespace: rstripped, so the total stays under the cap
    spaced = gates.notice_line("a " * gates.NOTICE_LINE_MAX)
    assert len(spaced) <= gates.NOTICE_LINE_MAX
    assert spaced.endswith("a" + gates.NOTICE_TRUNCATION_MARKER)
    # the cap applies to the FOLDED text, not to any one segment
    folded = gates.notice_line(("z" * 100 + "\n") * 60)
    assert len(folded) <= gates.NOTICE_LINE_MAX
    assert folded.endswith(gates.NOTICE_TRUNCATION_MARKER)


def test_notice_line_cap_holds_a_full_critical_display_whole():
    """The backstop sits above the CRITICAL display budget, so a maximal plain
    Wave 3 display (display cap, recovery hint included) is not re-cut. Only
    control- or line-dense text, inflated past the budget by escapes and folds,
    can reach the cap."""
    from bmad_loop import escalation

    shown = escalation.display_critical_reason("r" * 10_000, "s" * 10_000)
    assert len(shown) < gates.NOTICE_LINE_MAX
    assert gates.notice_line(shown) == shown
    assert escalation._CRITICAL_TRUNCATION_MARKER is gates.NOTICE_TRUNCATION_MARKER
    assert escalation.CRITICAL_FALLBACK_SOURCE == gates.NOTICE_FULL_DETAIL_SOURCE


def test_notice_block_keeps_lines_and_escapes_controls():
    assert gates.notice_block("head\n  1. a\x1b\n") == "head\n  1. a\\x1b"
    assert gates.notice_block("a\r\nb\rc\n\n\n") == "a\nb\nc"
    assert gates.notice_block("") == ""
    assert gates.notice_block(" \n ") == ""
    assert gates.notice_block("head\n  1. bad \udcff path") == "head\n  1. bad \\udcff path"
    # no cap on the block path: operator action lists are carried whole
    long = "\n".join("q" * 100 for _ in range(60))
    assert gates.notice_block(long) == long


def _attention_and_argv(monkeypatch, tmp_path, title, message, **kwargs):
    """Run `notify` on BOTH channels (file + Linux notify-send) and return the
    ATTENTION text and the positional SUMMARY/BODY notify-send received."""
    monkeypatch.setattr(gates.sys, "platform", "linux")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None
    )
    calls = _capture_run(monkeypatch)
    gates.notify(_policy(desktop=True, file=True), tmp_path, title, message, **kwargs)
    assert len(calls) == 1
    argv, _kwargs = calls[0]
    assert argv[:3] == ["notify-send", "--app-name=bmad-loop", "--"]
    attention = (tmp_path / gates.ATTENTION_FILE).read_text(encoding="utf-8")
    return attention, argv[3], argv[4]


@pytest.mark.parametrize(("raw", "shaped"), _ONE_LINE_ROWS)
def test_notify_shapes_the_message_on_both_channels(monkeypatch, tmp_path, raw, shaped):
    """Every matrix row, through the chokepoint: the ATTENTION record is exactly
    one line carrying the shaped message, and notify-send's BODY is the same
    shaped text.

    Ablation: write/pass the raw `message` in `notify` (skip `notice_line`) and the
    fold and control-character rows redden on both channels."""
    attention, summary, body = _attention_and_argv(monkeypatch, tmp_path, "t", raw)
    assert attention.count("\n") == 1 and attention.endswith("\n")
    assert attention.rstrip("\n").endswith(f"] t: {shaped}")
    assert summary == "t"
    assert body == shaped


def test_notify_shapes_the_title_too(monkeypatch, tmp_path):
    """Titles carry untrusted text (story keys, plugin names) and are always
    shaped one-line, on the multiline path as well.

    Ablation: skip `notice_line(title)` and the raw ESC/newline reach both sinks."""
    for multiline in (False, True):
        (tmp_path / gates.ATTENTION_FILE).unlink(missing_ok=True)
        attention, summary, _ = _attention_and_argv(
            monkeypatch, tmp_path, "story\x1b[2J\nkey", "m", multiline=multiline
        )
        assert summary == "story\\x1b[2J ⏎ key"
        assert "\x1b" not in attention and attention.count("\n") == 1


def test_notify_nul_no_longer_reaches_the_notifier(monkeypatch, tmp_path):
    """A NUL used to reach argv/env and make `subprocess.run` raise `ValueError:
    embedded null byte` — swallowed, so the toast was silently lost. Shaping
    escapes it first, so the notifier is actually invoked with visible text.

    Ablation: skip `notice_line` in `notify` and the raw NUL reaches argv."""
    attention, summary, body = _attention_and_argv(monkeypatch, tmp_path, "ti\x00tle", "mid\x00nul")
    assert summary == "ti\\x00tle" and body == "mid\\x00nul"
    assert "\x00" not in attention and "\x00" not in summary + body


def test_notify_multiline_keeps_lines_and_escapes_controls(monkeypatch, tmp_path):
    """`multiline=True` (operator action lists): lines stay separate in ATTENTION
    and in the toast body; control characters are still escaped.

    Ablation: route the multiline path through `notice_line` and the lines fold;
    skip shaping entirely and the raw ESC survives."""
    message = "committed, but 2 action(s) are owed:\n  1. a\x1b[0m\n  2. b\n"
    attention, _, body = _attention_and_argv(
        monkeypatch, tmp_path, "story awaiting operator: 1-1-a", message, multiline=True
    )
    assert body == "committed, but 2 action(s) are owed:\n  1. a\\x1b[0m\n  2. b"
    lines = attention.splitlines()
    assert lines[0].endswith("story awaiting operator: 1-1-a: committed, but 2 action(s) are owed:")
    assert lines[1:] == ["  1. a\\x1b[0m", "  2. b"]
    assert "\x1b" not in attention


def test_notify_desktop_shapes_env_payloads(monkeypatch, tmp_path):
    """osascript/PowerShell carry title/message via env: those values are the
    shaped text too."""
    monkeypatch.setattr(gates.sys, "platform", "darwin")
    monkeypatch.setattr(
        gates.shutil, "which", lambda cmd: "/usr/bin/osascript" if cmd == "osascript" else None
    )
    calls = _capture_run(monkeypatch)
    gates.notify(_policy(desktop=True, file=False), tmp_path, "t\x07", "a\nb\x00")
    ((_argv, kwargs),) = calls
    assert kwargs["env"][gates._TITLE_ENV] == "t\\x07"
    assert kwargs["env"][gates._MESSAGE_ENV] == "a ⏎ b\\x00"


# ------------------------------------------------ lone surrogates (DW-419)


def test_notify_survives_a_lone_surrogate_on_both_channels(monkeypatch, tmp_path):
    """A lone surrogate (a ``surrogateescape``'d path byte in ``str(e)``) is not
    UTF-8 encodable: it used to raise ``UnicodeEncodeError`` out of the ATTENTION
    write, breaking the never-raises contract. Shaping now escapes it visibly, so
    both channels carry the same ``\\udcff`` text and ``notify`` returns.

    Ablation: drop the surrogate branch in ``_escape_controls`` and the argv keeps
    the raw surrogate; drop it AND ``errors="backslashreplace"`` and ``notify``
    raises ``UnicodeEncodeError``."""
    for multiline in (False, True):
        (tmp_path / gates.ATTENTION_FILE).unlink(missing_ok=True)
        attention, summary, body = _attention_and_argv(
            monkeypatch, tmp_path, "ti\udcfftle", "bad \udcff path", multiline=multiline
        )
        assert summary == "ti\\udcfftle"
        assert body == "bad \\udcff path"
        assert attention.count("\n") == 1
        assert attention.rstrip("\n").endswith("] ti\\udcfftle: bad \\udcff path")


def test_notify_attention_write_backstops_an_unencodable_payload(monkeypatch, tmp_path):
    """``errors="backslashreplace"`` on the ATTENTION open is a backstop behind the
    shaping: even text that reaches the write unshaped cannot raise.

    Ablation: drop ``errors="backslashreplace"`` and this raises
    ``UnicodeEncodeError``."""
    monkeypatch.setattr(gates, "notice_line", lambda text: text)
    monkeypatch.setattr(gates, "notice_block", lambda text: text)
    gates.notify(_policy(desktop=False, file=True), tmp_path, "t", "bad \udcff path")
    attention = (tmp_path / gates.ATTENTION_FILE).read_text(encoding="utf-8")
    assert attention.rstrip("\n").endswith("] t: bad \\udcff path")
