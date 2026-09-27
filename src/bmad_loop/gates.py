"""Gate evaluation and human notification (desktop + ATTENTION file)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .policy import Policy

ATTENTION_FILE = "ATTENTION"

# Notice line shaping (DW-13/DW-332). Every `notify` payload passes through
# `notice_line` (or `notice_block` for a deliberately multi-line notice) before it
# reaches the ATTENTION file or a desktop toast. See `notify` for the doctrine.
#
# NOTICE_LINE_MAX is a BACKSTOP, not a display preference. A CRITICAL notice is
# `escalation.display_critical_reason` (at most `CRITICAL_DISPLAY_MAX` = 2000
# characters, recovery hint included) plus the engine's short
# "— resolve, then `bmad-loop resume <run-id>`" suffix, and a verify reason is
# its command line plus an output tail of at most 2000 characters, so plain text
# of either fits well under 4000. The cap fires only when shaping itself pushes
# text past it: control-dense text (each `\xNN` escape turns 1 character into 4)
# or line-dense text (each fold turns 1 line break into 3). The cut then drops
# the tail — including any trailing recovery or resume hint — and the marker
# points at the journal, which keeps the raw text.
NOTICE_LINE_MAX = 4000
NOTICE_FULL_DETAIL_SOURCE = "journal.jsonl"
# One definition, shared with escalation's CRITICAL display truncation.
NOTICE_TRUNCATION_MARKER = f" [… truncated; full detail in {NOTICE_FULL_DETAIL_SOURCE}]"
# Joins the folded segments of a multi-line one-line notice: every line survives.
NOTICE_LINE_SEPARATOR = " ⏎ "


def _escape_controls(text: str) -> str:
    """``text`` with every C0/C1 control character (and DEL) replaced by a visible
    ``\\xNN`` escape, every lone surrogate (U+D800-U+DFFF, e.g. a
    ``surrogateescape``'d path byte in ``str(e)``) by a visible ``\\uNNNN``
    escape, and TAB by a single space. Line breaks must already be split out: this
    runs per line. Escaping surrogates makes the result UTF-8 encodable, so neither
    the ATTENTION write nor a desktop notifier's argv/env can choke on it (DW-419)."""
    if text.isprintable():
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\t":
            out.append(" ")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif 0xD800 <= code <= 0xDFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def notice_line(text: str) -> str:
    """``text`` shaped as ONE ATTENTION/toast line.

    Control characters are escaped visibly (``\\x1b``), lone surrogates likewise
    (``\\udcff``), TAB becomes a space. Line breaks — the whole ``str.splitlines``
    set (``\\r\\n``, lone ``\\r``, ``\\v``, ``\\f``, ``\\x1c``-``\\x1e``, ``\\x85``,
    U+2028/2029) — FOLD the text: each
    segment is stripped, blank segments are dropped, and the rest are joined by
    ``NOTICE_LINE_SEPARATOR``, so every line survives. A result longer than
    ``NOTICE_LINE_MAX`` is cut and ends in ``NOTICE_TRUNCATION_MARKER``, which names
    where the full text lives; the total never exceeds ``NOTICE_LINE_MAX``.

    Text with no line break, no control or surrogate character, and within the cap
    is returned byte-for-byte unchanged.
    """
    lines = text.splitlines()
    if lines == [text]:
        shaped = _escape_controls(text)
    else:
        segments = (_escape_controls(line).strip() for line in lines)
        shaped = NOTICE_LINE_SEPARATOR.join(seg for seg in segments if seg)
    if len(shaped) > NOTICE_LINE_MAX:
        keep = NOTICE_LINE_MAX - len(NOTICE_TRUNCATION_MARKER)
        shaped = shaped[:keep].rstrip() + NOTICE_TRUNCATION_MARKER
    return shaped


def notice_block(text: str) -> str:
    """``text`` shaped as a deliberately MULTI-line notice (``notify(...,
    multiline=True)``): line breaks are kept, normalized to ``\\n``, with trailing
    blank lines dropped; control and surrogate characters are escaped as in
    ``notice_line``; indentation is kept; there is no length cap (these are
    operator action lists, not captured output). Untrusted fragments interpolated
    into such a notice (``{error}``, agent-authored actions) are folded through
    ``notice_line`` by the caller, so they stay one segment of their line and
    each is individually capped at ``NOTICE_LINE_MAX`` (the raw text stays in the
    caller's journal row) (DW-417)."""
    lines = [_escape_controls(line) for line in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


# The untrusted notification title/message (story keys, `str(e)` error tails) are
# handed to osascript/PowerShell through these environment variables rather than
# interpolated into the command text, so quotes/newlines/AppleScript-or-PowerShell
# metacharacters cannot break out of the string. notify-send takes them as argv,
# which is already injection-safe.
_TITLE_ENV = "BMAD_LOOP_NOTIFY_TITLE"
_MESSAGE_ENV = "BMAD_LOOP_NOTIFY_MESSAGE"

# WinRT ToastNotificationManager — the dependency-free toast path on Windows 10+
# (works under both pwsh and powershell.exe). Reads title/message from $env, so no
# PowerShell-string interpolation of user text.
_WIN_TOAST_PS = (
    "$ErrorActionPreference='Stop';"
    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
    "ContentType=WindowsRuntime]|Out-Null;"
    "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$x=$t.GetElementsByTagName('text');"
    "$x.Item(0).AppendChild($t.CreateTextNode($env:BMAD_LOOP_NOTIFY_TITLE))|Out-Null;"
    "$x.Item(1).AppendChild($t.CreateTextNode($env:BMAD_LOOP_NOTIFY_MESSAGE))|Out-Null;"
    "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
    # Windows only shows a toast for a *registered* AppUserModelID; 'bmad-loop' has
    # no Start-menu shortcut carrying it, so that toast would be silently dropped.
    # Reuse Windows PowerShell's own default-registered AUMID instead (the toast is
    # attributed to "Windows PowerShell"). Raw strings: the AUMID's backslashes must
    # stay literal — `\v1.0` would otherwise be parsed as a vertical tab.
    r"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    r"'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe')"
    r".Show($n)"
)


def desktop_notifier_kind() -> str | None:
    """The desktop notifier available on THIS platform, or ``None``. Read-only —
    ``validate`` and the engine call it to decide whether ``notify.desktop`` can do
    anything here. Gated on ``sys.platform`` first (not ``which`` alone): PowerShell
    Core can exist on Linux/macOS, but only Windows should reach the toast path and
    Linux must keep picking ``notify-send``."""
    if sys.platform == "darwin":
        return "osascript" if shutil.which("osascript") else None
    if sys.platform == "win32":
        return "powershell" if (shutil.which("pwsh") or shutil.which("powershell")) else None
    return "notify-send" if shutil.which("notify-send") else None


def _notifier_argv(kind: str, title: str, message: str) -> tuple[list[str], dict[str, str]]:
    """``(argv, env-overrides)`` for ``kind``. osascript/powershell carry the
    untrusted text via env (never argv); notify-send takes it as argv."""
    if kind == "osascript":
        return (
            [
                "osascript",
                "-e",
                f'display notification (system attribute "{_MESSAGE_ENV}") '
                f'with title (system attribute "{_TITLE_ENV}")',
            ],
            {_TITLE_ENV: title, _MESSAGE_ENV: message},
        )
    if kind == "powershell":
        pwsh = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return (
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST_PS],
            {_TITLE_ENV: title, _MESSAGE_ENV: message},
        )
    # `--` ends GLib option parsing: an untrusted title/message beginning with
    # `-`/`--` (e.g. a plugin veto reason of `--help`) is then taken as positional
    # SUMMARY/BODY text, not parsed as a notify-send option.
    return (["notify-send", "--app-name=bmad-loop", "--", title, message], {})


def notify(
    policy: Policy,
    run_dir: Path,
    title: str,
    message: str,
    *,
    multiline: bool = False,
) -> None:
    """Best-effort human notification: append the ATTENTION file (if notify.file)
    and fire a native desktop notification (if notify.desktop). Never raises — a
    failing notifier must not crash the run. Headless CI cannot observe a real
    notification (no macOS job), so the native macOS/Windows paths are unit-tested
    at the command-construction level; verify visual delivery manually per OS.

    This is the one shaping chokepoint for both channels (DW-13/DW-332). ATTENTION
    is one ``[stamp] title: message`` record per line, but the text callers pass
    is routinely MULTI-line — a ``Decision.reason`` from
    ``verify.verify_command_results_outcome`` carries the failing command's output
    tail below its first line on purpose, because a repair session reads that tail
    as its feedback — and may carry terminal control bytes from captured output.
    So ``title`` is always, and ``message`` by default, passed through
    ``notice_line`` (control characters escaped, line breaks folded, backstop cap).
    Nothing is lost: callers write the raw text to the journal (and state) first,
    and the cap marker names ``journal.jsonl``.

    ``multiline=True`` is for notices that are multi-line BY DESIGN (numbered
    operator action lists, the run summary): ``message`` goes through
    ``notice_block`` instead, which keeps its line breaks but still escapes
    control characters. One-record-per-line is therefore a property of the
    default path, not of the ATTENTION file as a whole."""
    title = notice_line(title)
    message = notice_block(message) if multiline else notice_line(message)
    if policy.notify.file:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            # backslashreplace: a backstop behind the shaping above (which already
            # escapes lone surrogates) — an unencodable character must never turn
            # the write into a UnicodeEncodeError out of a never-raises notifier.
            with (run_dir / ATTENTION_FILE).open(
                "a", encoding="utf-8", errors="backslashreplace"
            ) as f:
                f.write(f"[{stamp}] {title}: {message}\n")
        except OSError:
            # observe-degrade: an unwritable ATTENTION file is observability,
            # never a reason to break the loop (the _write_heartbeat doctrine,
            # already applied to this same call in the adapter budget guards).
            # Without it the "never raises" contract above was false for the
            # file half, and an unwritable run dir turned an advisory notice
            # into a run crash at every record-a-decision site. The journal
            # entry each caller writes first stays the durable record.
            pass
    # Native desktop notification per platform: osascript (macOS), a best-effort
    # WinRT PowerShell toast (Windows), notify-send (Linux). None → silently skip;
    # `validate` and run start warn separately when notify.desktop is inert here.
    if policy.notify.desktop:
        kind = desktop_notifier_kind()
        if kind:
            argv, env = _notifier_argv(kind, title, message)
            try:
                subprocess.run(
                    argv,
                    timeout=10,
                    capture_output=True,
                    env={**os.environ, **env} if env else None,
                )
            except (subprocess.SubprocessError, OSError, ValueError):
                # best-effort: a failing notifier must never crash the run. Shaping
                # already escapes NUL, so `ValueError: embedded null byte` from argv
                # (notify-send) or an env value (osascript/PowerShell) is no longer
                # reachable through the payload; the catch stays as a backstop.
                pass


def pause_at_epic_boundary(policy: Policy) -> bool:
    return policy.gates.mode in ("per-epic", "per-story-spec-approval")


def pause_after_spec(policy: Policy) -> bool:
    return policy.gates.mode == "per-story-spec-approval"
