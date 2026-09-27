"""`frontmatter.set_frontmatter_status` — the verified in-place status writer.

Four sections. The CHARACTERIZATION half was written
against the pre-rewrite line scanner and is green on both sides of it: it pins
the formatting-preserving minimal edit, and the one surviving deliberate
non-preservation (a quoted value is written back unquoted) that a "make it a YAML
round-trip" refactor would silently change out from under callers three files
away. LINE ENDINGS pins #357 part 1: the same contract, over the bytes the old
`read_text`/`write_text` pair relaid across the whole file. INLINE COMMENTS pins
#357 part 2, where the gating is inverted — `_verified` cannot see a fabricated
comment, so a regex is the gate and the oracle is only the backstop. The BEHAVIOR
half pins the rewrite: a writer that verifies its own edit by re-parsing, and
RAISES rather than returning a `False` nobody reads when the reader can see a
status it cannot safely move.
"""

import os
import sys
from pathlib import Path

import pytest
import yaml

from bmad_loop import frontmatter, platform_util, verify

_PLAIN = (
    "---\ntitle: List command\nstatus: in-review\nowner: amelia\n---\n\n# Spec\n\n"
    "<frozen-after-approval>\nFilter notes by workspace name.\n</frozen-after-approval>\n"
)


def _spec(tmp_path, text: str, name: str = "spec.md"):
    """Write a spec as raw bytes — `write_text` would relay line endings, and
    several of these fixtures are about exactly which bytes survive."""
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


# ============================================================ characterization
#
# Green before AND after the rewrite. Anything that fails here is a change to the
# contract callers already depend on, not a change to the defect being fixed.


def test_a_plain_flip_changes_the_status_line_and_nothing_else(tmp_path):
    """The whole point of a line edit over a YAML round-trip: field order,
    comments, quoting and body survive byte-for-byte.

    The fixture carries the shape the orchestrator actually flips — the other
    frontmatter fields plus a `<frozen-after-approval>` body block the writer must
    never reach. One byte-exact comparison subsumes any list of per-field
    substring assertions: it also fails on what such a list forgot to name."""
    spec = _spec(tmp_path, _PLAIN)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == _PLAIN.replace("status: in-review", "status: done")


def test_flipping_to_the_status_already_there_returns_false_and_does_not_write(tmp_path):
    """Idempotence is observable, not just a return value: a no-op write would
    churn mtime and make every caller's spec look freshly edited to the artifact
    scans that key on it."""
    spec = _spec(tmp_path, _PLAIN.replace("in-review", "done"))
    before = spec.stat().st_mtime_ns
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is False
    assert spec.stat().st_mtime_ns == before


def test_a_quoted_value_is_written_back_unquoted(tmp_path):
    """A deliberate non-preservation, and the most load-bearing pin in this file.
    `conftest.write_spec` writes `status: '<v>'`, and substring assertions read the
    result back UNQUOTED: `status: in-review` in
    `test_runs.test_rearm_restore_mode_sets_in_review_strips_arr_and_latches`, and
    both that and `status: done` in
    `test_stories_e2e.test_e2e_{sprint,sweep}_intent_gap_patch_restore`. A refactor
    that "also preserves the value's quotes" breaks those three from here.

    Named rather than cited by line, and stated as the property rather than one
    value: the old line numbers had gone stale before anyone noticed, and the old
    wording named only `done` — which is not the value the first of those three
    asserts at all."""
    spec = _spec(tmp_path, "---\nstatus: 'in-review'\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus: done\n---\nbody\n"


def test_a_status_prefixed_key_is_never_targeted(tmp_path):
    spec = _spec(tmp_path, "---\nstatus_note: keep me\nstatus: in-review\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus_note: keep me\nstatus: done\n---\nbody\n"


def test_a_commented_out_status_line_is_never_targeted(tmp_path):
    spec = _spec(tmp_path, "---\n# status: draft\nstatus: in-review\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\n# status: draft\nstatus: done\n---\nbody\n"


def test_no_frontmatter_block_returns_false_with_the_bytes_unchanged(tmp_path):
    spec = _spec(tmp_path, "# just a heading\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is False
    assert spec.read_bytes() == b"# just a heading\n"


def test_a_missing_file_returns_false(tmp_path):
    assert (
        frontmatter.set_frontmatter_status(tmp_path / "nope.md", "done", confine_root=tmp_path)
        is False
    )


def test_a_block_with_no_status_key_returns_false(tmp_path):
    """Contrast with `verify.set_frontmatter_field`, which INSERTS a missing key.
    The status helper never invents a status."""
    spec = _spec(tmp_path, "---\ntitle: t\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is False
    assert spec.read_bytes().decode() == "---\ntitle: t\n---\nbody\n"


def test_a_present_but_empty_status_is_filled(tmp_path):
    """A bmad-dev-auto template can leave the line blank. It reads as a status
    the writer must be able to move, not as a missing key."""
    spec = _spec(tmp_path, "---\nstatus:\ntitle: t\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus: done\ntitle: t\n---\nbody\n"


def test_indentation_on_the_status_line_survives(tmp_path):
    spec = _spec(tmp_path, "---\n  status: in-review\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\n  status: done\n---\nbody\n"


def test_set_frontmatter_status_preserves_triple_dash_in_value(tmp_path):
    """A `---` inside a scalar is not the closing delimiter: status flips and the
    ---bearing title + body survive (a plain split("---", 2) corrupted this)."""
    text = "---\ntitle: 'restore --- review'\nstatus: in-review\n---\nbody text\n"
    spec = _spec(tmp_path, text)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    fm = frontmatter.read_frontmatter(spec)
    assert fm["status"] == "done"
    assert fm["title"] == "restore --- review"  # scalar with --- intact
    assert spec.read_bytes().decode() == text.replace("status: in-review", "status: done")


# =============================================================== line endings
#
# The half of "only the status value changes" the writer used to break on every
# call (#357). `read_text`/`write_text` relaid EVERY ending in the file — a CRLF
# spec came back all-LF on POSIX and an all-LF spec came back all-CRLF on
# Windows — so these were unwritable before the byte-level read/write. Two gates
# per case, because either alone passes for the wrong reason: byte-exact equality
# (which a substring assertion cannot give) AND "no bare LF was introduced", the
# one that catches `_replace_value` re-emitting a flat `"\n"` for the single line
# it touched and leaving the file mixed.
#
# WHICH PLATFORM PROVES WHAT. These three cover the READ half; reverting it to
# `read_text` fails all three anywhere. The WRITE half is POSIX-invisible —
# `write_text`'s `newline=None` translates `"\n"` to `os.linesep`, which on POSIX
# is `"\n"` — so reverting `write_bytes` keeps this whole file green on Linux and
# only the Windows CI leg catches it. What catches it there is the byte-exact
# equality in the CHARACTERIZATION half above: those fixtures are all-LF, and
# `write_text` on Windows returns them all-CRLF. That is why those assertions are
# bare comparisons rather than routed through an os.linesep-modelling helper —
# the helper that used to sit here described the defect instead of failing on it.


def test_a_crlf_spec_keeps_every_crlf_and_only_the_status_line_changes(tmp_path):
    crlf = _PLAIN.replace("\n", "\r\n")
    spec = _spec(tmp_path, crlf)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    text = spec.read_bytes().decode()
    assert text == crlf.replace("status: in-review", "status: done")
    assert "\n" not in text.replace("\r\n", "")  # not one bare LF, anywhere
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == "done"


def test_a_cr_only_spec_keeps_its_bare_carriage_returns(tmp_path):
    """`_split_frontmatter` and `_replace_value` are both `splitlines`-based, which
    treats a bare `\\r` as a line break — so this writer rewrites a CR-only spec as
    authored. Its `devcontract.reset_spec_status` sibling is regex-based on
    `\\r?\\n` and no-ops on the same input; that asymmetry is pinned deliberately
    in tests/test_devcontract.py."""
    cr = _PLAIN.replace("\n", "\r")
    spec = _spec(tmp_path, cr)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    text = spec.read_bytes().decode()
    assert text == cr.replace("status: in-review", "status: done")
    assert "\n" not in text
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == "done"


def test_a_mixed_ending_spec_keeps_each_line_its_own_ending(tmp_path):
    """The ending is carried from the line being edited, not detected once for the
    file. The status line is the odd one out on purpose — a whole-file `nl =
    "\\r\\n" if "\\r\\n" in text else "\\n"` reading is green on the pure-CRLF case
    above and rewrites this LF status line to CRLF."""
    mixed = "---\r\ntitle: List command\r\nstatus: in-review\nowner: amelia\r\n---\r\nbody\r\n"
    spec = _spec(tmp_path, mixed)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == mixed.replace("status: in-review", "status: done")
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == "done"


# ============================================================= inline comments
#
# #357 part 2. The other half of "comments survive": a trailing comment on the
# STATUS line itself, which this writer used to drop on every call.
#
# WHAT GATES WHAT, because it is the reverse of the rest of this module. Every
# other edit here is gated by `_verified` re-parsing the candidate. That oracle
# is BLIND to this one: `yaml.safe_load` strips comments before it compares, so a
# render that invents a comment out of the tail of a quoted value produces a
# block whose `status` is exactly right and whose other keys are untouched, and
# all three gates pass it. `_VALUE_COMMENT_RE`'s conservative token class is the
# real gate; `_verified` is only the backstop. The quoted-`#` case below is the
# test that holds that line — ablate it by widening `val` to `[^#]*?` and it
# writes `status: done # b"`.


def test_a_trailing_inline_comment_on_the_status_line_is_preserved(tmp_path):
    """This used to be the file's other deliberate non-preservation. The
    separating whitespace comes through as authored (two spaces here), so a
    hand-aligned comment column is not silently reflowed by a status flip."""
    spec = _spec(tmp_path, "---\nstatus: in-review  # set by step-03\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus: done  # set by step-03\n---\nbody\n"
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == "done"


def test_a_hash_inside_a_quoted_value_never_becomes_a_comment(tmp_path):
    """The case the whole design is shaped around. `status: "a # b"` carries no
    comment at all — the `#` is scalar text — so the only correct answers are the
    full drop or a refusal, and the full drop is the one that keeps working specs
    working. A renderer that split at the `#` would write `status: done # b"`,
    which reads back as a clean `status: done` and therefore SURVIVES `_verified`
    with a fabricated comment attached."""
    spec = _spec(tmp_path, '---\nstatus: "a # b"\ntitle: t\n---\nbody\n')
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus: done\ntitle: t\n---\nbody\n"


def test_a_hash_abutting_the_scalar_is_part_of_the_value_not_a_comment(tmp_path):
    """`status: in-review#x` is the single value `in-review#x` — YAML needs
    whitespace before a `#` for it to open a comment. `_VALUE_COMMENT_RE`'s `sep`
    requires that whitespace, so this falls to the full drop rather than carrying
    `#x` forward as a comment the spec never had."""
    spec = _spec(tmp_path, "---\nstatus: in-review#x\ntitle: t\n---\nbody\n")
    assert frontmatter.read_frontmatter(spec)["status"] == "in-review#x"  # one scalar
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus: done\ntitle: t\n---\nbody\n"


def test_a_quoted_value_keeps_its_comment_while_losing_its_quotes(tmp_path):
    """The two preservations pull in opposite directions on the same line, and
    both answers are deliberate: the comment is carried, the quotes are not (see
    test_a_quoted_value_is_written_back_unquoted for what depends on that)."""
    spec = _spec(tmp_path, "---\nstatus: 'in-review' # c\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus: done # c\n---\nbody\n"


def test_a_present_but_empty_status_keeps_its_comment(tmp_path):
    """`val` matching empty is not an oversight — a template's blank
    `status:` line is a shape the writer must fill (see
    test_a_present_but_empty_status_is_filled), and its comment is as real as any
    other. The value slots in ahead of the comment instead of replacing it."""
    spec = _spec(tmp_path, "---\nstatus:  # set by step-03\ntitle: t\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == "---\nstatus: done # set by step-03\ntitle: t\n---\nbody\n"


def test_a_comment_glued_to_the_colon_falls_back_to_the_full_drop(tmp_path):
    """Pinned against the renderer, not through a spec, because no spec can reach
    it: PyYAML needs whitespace after a `:` for it to separate a key, so
    `status:# c` is the plain scalar `"status:# c"` and `read_frontmatter` sees
    no mapping at all — asserted here so the reason is checked, not just claimed.
    That leaves `_replace_value` the only layer where the shape is observable,
    and it is worth observing: it is the boundary of `sep`'s `[ \\t]+`, the one
    character that decides whether a `#` is a comment or scalar text."""
    spec = _spec(tmp_path, "---\nstatus:# c\n---\nbody\n")
    assert frontmatter.read_frontmatter(spec) == {}  # not a mapping — unreachable
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is False
    line = "status:# c\n"
    m = frontmatter._STATUS_KEY_RE.match(line)
    assert m is not None  # the KEY pattern matches; only the value render declines
    assert frontmatter._replace_value(line, m, "done") == "status: done\n"


def test_a_comment_and_a_crlf_ending_both_survive_the_same_write(tmp_path):
    """Part 1 and part 2 of #357 stack: the comment is carried from the value
    side of the line and the terminator from the end of it, so a CRLF spec with a
    commented status line comes back with both. Written as one test because the
    render builds them in a single f-string — a fix for either that dropped the
    other would pass both of their own tests."""
    crlf = (
        "---\r\ntitle: t\r\nstatus: in-review  # set by step-03\r\nowner: amelia\r\n---\r\nbody\r\n"
    )
    spec = _spec(tmp_path, crlf)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    text = spec.read_bytes().decode()
    assert text == crlf.replace("status: in-review", "status: done")
    assert "\n" not in text.replace("\r\n", "")  # not one bare LF, anywhere
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == "done"


# ================================================================== behavior
#
# The rewrite. Every shape below reads as a real status through
# `read_frontmatter`, and the old line scanner answered each of them either with
# a silent `False` nobody read or with a corrupting write.

# name -> (spec text, the status `read_frontmatter` sees before AND after)
_UNREWRITABLE = {
    "flow-mapping": ("---\n{status: in-review, keep: 1}\n---\nbody\n", "in-review"),
    "block-scalar": ("---\nstatus: |\n  in-review\ntitle: t\n---\nbody\n", "in-review"),
    "value-on-the-next-line": ("---\nstatus:\n  in-review\ntitle: t\n---\nbody\n", "in-review"),
    "value-wrapped-over-two-lines": (
        "---\nstatus: awaiting\n  operator\ntitle: t\n---\nbody\n",
        "awaiting operator",
    ),
    "anchor-another-key-aliases": (
        "---\nstatus: &a in-review\nother: *a\n---\nbody\n",
        "in-review",
    ),
    # The one shape the "every other key unchanged" comparison is the SOLE gate
    # for — the status the reader resolves comes from a merged anchor block, so
    # the only line an edit can reach belongs to `defaults:`, shared state this
    # story does not own. The trial produces the right top-level status and
    # rewrites someone else's mapping to get it; every other check here passes it.
    # Ablating the comparison turns this row into a silent wrong-target write.
    "status-merged-in-from-an-anchor": (
        "---\ndefaults: &d\n  status: in-review\n  owner: amelia\n<<: *d\n---\nbody\n",
        "in-review",
    ),
}


@pytest.mark.parametrize("shape", sorted(_UNREWRITABLE), ids=sorted(_UNREWRITABLE))
def test_a_status_no_line_edit_can_move_raises_and_leaves_the_file_alone(tmp_path, shape):
    """The old scanner's two failure modes, one per row: a silent no-op (flow
    mapping) or a corrupting write (everything else — `status: done in-review`,
    a block scalar with its indicator eaten, an alias left dangling). The file
    must come out byte-identical and still READ as the status it had, so nothing
    downstream can mistake a refused write for a landed one."""
    text, reads_as = _UNREWRITABLE[shape]
    spec = _spec(tmp_path, text)
    with pytest.raises(frontmatter.FrontmatterWriteError):
        frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path)
    assert spec.read_bytes() == text.encode("utf-8")
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == reads_as


def test_an_unparseable_block_raises_where_the_reader_degrades(tmp_path):
    """The doctrine asymmetry, stated as a test. `read_frontmatter` turns this
    same input into `{}` (pinned at tests/test_verify.py:1980) because observation
    may degrade. A writer that did the same would conclude "no status here" from
    a block it could not read and report a repair it never made."""
    text = "---\nstatus: in-review\ntitle: [unclosed\n---\nbody\n"
    spec = _spec(tmp_path, text)
    assert frontmatter.read_frontmatter(spec) == {}  # the reader degrades...
    with pytest.raises(frontmatter.FrontmatterWriteError, match="does not parse as YAML"):
        frontmatter.set_frontmatter_status(
            spec, "done", confine_root=tmp_path
        )  # ...the writer does not
    assert spec.read_bytes() == text.encode("utf-8")


def test_a_nested_status_is_not_the_status_and_is_never_written(tmp_path):
    """The old scanner rewrote the FIRST line that looked like `status:`, so a
    `meta:` block carrying one got the write and the story's real status never
    moved. Here the reader sees no top-level status, so there is nothing to
    change — and the decoy is left exactly as authored."""
    text = "---\nmeta:\n  status: in-review\n---\nbody\n"
    spec = _spec(tmp_path, text)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is False
    assert spec.read_bytes() == text.encode("utf-8")


@pytest.mark.parametrize(
    ("decoy", "label"),
    [
        ("meta:\n  status: keep\n", "nested-key"),
        ("notes: |\n  status: keep this verbatim\n", "literal-block"),
    ],
    ids=["nested-key", "literal-block"],
)
def test_a_decoy_before_the_real_status_does_not_capture_the_write(tmp_path, decoy, label):
    """Candidates are iterated, not broken on at the first match. The decoy comes
    FIRST on purpose: under the old `break` it took the write and the real status
    line was never reached."""
    text = f"---\n{decoy}status: in-review\n---\nbody\n"
    spec = _spec(tmp_path, text)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == f"---\n{decoy}status: done\n---\nbody\n"
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == "done"


@pytest.mark.parametrize(
    ("key", "value"),
    [('"status"', "in-review"), ("'status'", "in-review"), ("status ", "in-review")],
    ids=["double-quoted-key", "single-quoted-key", "space-before-colon"],
)
def test_a_key_spelling_yaml_accepts_is_rewritten_with_its_formatting_kept(tmp_path, key, value):
    """All three read as `status` through `read_frontmatter`; all three were a
    silent no-op under `lstrip().startswith("status:")`. The key's own spelling
    survives the rewrite — only the value moves. (A TAB before the colon is not
    in this list on purpose: PyYAML rejects it outright, so it is not a shape the
    reader ever accepts — it lands on the unparseable-block raise above.)"""
    spec = _spec(tmp_path, f"---\n{key}: {value}\n---\nbody\n")
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == f"---\n{key}: done\n---\nbody\n"
    assert frontmatter.status_of(frontmatter.read_frontmatter(spec)) == "done"


def test_a_key_that_merely_ends_where_status_does_is_not_a_status_line(tmp_path):
    """`(?P=q)` backreference: an unquoted key ending in a stray quote char reads
    as the key `status"`, not `status`. The reader sees no top-level status, so
    there is nothing to change — and nothing is written to a key nobody asked
    about."""
    text = '---\nstatus": in-review\ntitle: t\n---\nbody\n'
    spec = _spec(tmp_path, text)
    assert frontmatter.read_frontmatter(spec) == {'status"': "in-review", "title": "t"}
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is False
    assert spec.read_bytes() == text.encode("utf-8")


def test_verify_re_exports_the_same_exception_object(tmp_path):
    """`verify` re-exports the frontmatter names so every `verify.<name>` call
    site stays valid; an `except verify.FrontmatterWriteError` must catch what
    `frontmatter` raises, which only holds if it is the same class."""
    assert verify.FrontmatterWriteError is frontmatter.FrontmatterWriteError
    spec = _spec(tmp_path, "---\n{status: in-review}\n---\nbody\n")
    with pytest.raises(verify.FrontmatterWriteError):
        verify.set_frontmatter_status(spec, "done", confine_root=tmp_path)


def test_the_verified_edit_is_never_a_yaml_round_trip(tmp_path):
    """`yaml.safe_load` is the oracle, never the serializer. Round-tripping this
    block through `yaml.safe_dump` would reorder the keys, drop the comment, and
    re-quote the values — the whole reason the writer is a line edit."""
    text = (
        "---\n"
        "title: 'restore --- review'  # keep this comment\n"
        "tags: [a, b]\n"
        "status: in-review\n"
        "owner: amelia\n"
        "---\n\nbody\n"
    )
    spec = _spec(tmp_path, text)
    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True
    assert spec.read_bytes().decode() == text.replace("status: in-review", "status: done")
    fm = frontmatter.read_frontmatter(spec)
    assert fm["status"] == "done" and fm["tags"] == ["a", "b"]
    assert list(fm) == ["title", "tags", "status", "owner"]  # order survives
    assert yaml.safe_dump(fm) not in spec.read_text()  # ...and it is not a dump


# ------------------------------------------------------------ ATOMIC WRITE (#379)
#
# The writer was byte-preserving but truncating; the module docstring called a torn
# write "a separate concern". `devcontract._atomic_write_spec` had already disproved
# that on the same files, with fault injection: it cut a 46-byte spec to 12. These
# three rows grade the three distinct choices at this call site — that it goes
# through the helper at all, that it is the BYTES helper, and that it does not
# follow a link.


def test_set_frontmatter_status_write_failure_raises_and_keeps_the_spec(tmp_path, monkeypatch):
    """A spec is laid out `before + edited + after`, so the truncating write this
    replaced could publish intact frontmatter saying `status: done` over a
    decapitated body — a spec that lies, which the loop then commits. The helper
    leaves either the old file or the whole new one, and the raise still propagates:
    this is the repair path, and repair writes must raise (AGENTS.md).

    Patched at frontmatter's OWN binding, never `Path.write_bytes`: the helper writes
    through an `mkstemp` fd via `os.fdopen`, so a `Path` patch never fires and this
    would pass having exercised nothing. `verify` holds a SEPARATE binding of the
    same helper for `set_frontmatter_field` — patching one does not reach the other,
    which is why that site has a row of its own in tests/test_resolve.py.

    Which binding is the CONFINED one (#593): `spec` sits under `tmp_path`, the
    root passed below, so the chokepoint takes the confined arm.
    `frontmatter.atomic_write_bytes` still exists for the out-of-tree arm, so
    patching that name installs cleanly and never fires — `pytest.raises` catches
    the difference.

    Ablation: restore `path.write_bytes(...)` at the call site and this reddens
    alone, on `pytest.raises` not raising (the import stays, so the stub still
    installs — it simply never gets called)."""
    spec = _spec(tmp_path, _PLAIN)
    before = spec.read_bytes()

    def boom(path, data, *, confine_root, require_writable_target=False, root_identity=None):
        raise OSError("no space left on device")

    monkeypatch.setattr(frontmatter, "atomic_write_bytes_confined", boom)
    with pytest.raises(OSError, match="no space left"):
        frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path)

    assert spec.read_bytes() == before
    assert b"status: done" not in spec.read_bytes()  # the mutation that must not land


def test_set_frontmatter_status_hands_the_helper_bytes_not_text(tmp_path, monkeypatch):
    """Grades BYTES-vs-text at this site, platform-independently. The LINE ENDINGS
    section above already forbids relaying a CRLF spec — but `atomic_write_text`
    keeps `Path.write_text`'s translating newline default, and on POSIX
    `os.linesep == "\\n"`, so swapping the text helper in reddens those rows on
    WINDOWS ONLY and CI's Linux leg would call the swap green.

    So inspect the payload the helper is handed, upstream of any translation: bytes,
    carrying CRLF verbatim. The binding is WRAPPED rather than replaced, so the real
    write still happens and the surrounding round-trip is unchanged.

    BOTH helper names are wrapped into the same list, the text one with
    `raising=False` since this module does not import it. That is what makes the
    `isinstance` line the assertion that fires: wrapping only the bytes name would
    grade "the bytes helper was called", so the swap would redden on an empty `seen`
    and this row would be claiming more than it checked.

    The CONFINED pair is what is wrapped (#593): `spec` is under `tmp_path`, so
    that is the arm the chokepoint takes. The plain `atomic_write_bytes` binding
    survives for out-of-tree specs, so wrapping it instead would record nothing and
    `len(seen) == 1` — not the `isinstance` row — is what would fire.

    Ablation: swap `atomic_write_bytes_confined` for `atomic_write_text_confined`
    (dropping the `.encode`) and this reddens on every platform, on the
    `isinstance` row."""
    seen: list[bytes | str] = []
    real = frontmatter.atomic_write_bytes_confined

    def record(path, data, *, confine_root, require_writable_target=False, root_identity=None):
        seen.append(data)
        blob = data if isinstance(data, bytes) else data.encode("utf-8")
        real(
            path,
            blob,
            confine_root=confine_root,
            require_writable_target=require_writable_target,
            root_identity=root_identity,
        )

    monkeypatch.setattr(frontmatter, "atomic_write_bytes_confined", record)
    monkeypatch.setattr(frontmatter, "atomic_write_text_confined", record, raising=False)
    spec = _spec(tmp_path, "---\r\ntitle: t\r\nstatus: in-review\r\n---\r\n\r\nbody\r\n")

    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=tmp_path) is True

    assert len(seen) == 1  # exactly one write — no retry loop crept in
    assert isinstance(seen[0], bytes)
    assert b"status: done\r\n" in seen[0]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_set_frontmatter_status_replaces_a_planted_symlink(tmp_path):
    """The row that grades this SITE's choice of a writer that replaces the NAME,
    rather than the helper's implementation of it (pinned in test_platform_util.py,
    where the helper is called directly). An in-tree spec no longer spells that
    choice as `follow_symlinks=False`: since #593 it routes to
    `atomic_write_bytes_confined`, which is no-follow by construction. The
    out-of-tree arm keeps the plain no-follow write — the three-row chokepoint
    section below grades that routing.

    Behaviour-preserving first: `devcontract._atomic_write_spec` writes these same
    specs through a name-replacing `atomic_replace`, so replacing the name is the
    family's existing semantics, not a tightening. It is also the security choice —
    the spec path reaches this writer from a scan of a directory a driven session
    owns, so writing THROUGH a link planted at that name would hand the session a
    host-side write to any operator-writable path.

    Ablation: swap the in-tree arm's writer for `atomic_write_bytes(path, payload)`
    at its follow-the-link default and this reddens on the link surviving and the
    planted target rewritten (the confined-parent rows below redden with it — the
    swap un-guards them too). No other row in this file plants a symlink at the
    spec's own name."""
    real = _spec(tmp_path, _PLAIN, name="someone-elses-file")
    link = tmp_path / "spec.md"
    link.symlink_to(real)

    assert frontmatter.set_frontmatter_status(link, "done", confine_root=tmp_path) is True

    assert not link.is_symlink()  # the NAME was replaced
    assert frontmatter.read_frontmatter(link)["status"] == "done"
    assert real.read_bytes() == _PLAIN.encode("utf-8")  # not written through


# --------------------------------------------- CONFINED PARENT (#593) + #597
#
# The chokepoint rule this writer states in its own docstring, graded as three
# rows: an in-tree spec takes the confined arm, an out-of-tree one takes the
# plain no-follow arm, and a redirected parent inside the tree is REFUSED. The
# fourth row is #597 — an operator's read-only spec is answered, not routed
# around.
#
# `confine_root` here is always a real ANCESTOR of the spec's directory, never
# the directory itself: the anchored walk covers the components strictly BELOW
# the root and opens the root without O_NOFOLLOW, so `confine_root=spec.parent`
# would walk nothing, refuse nothing, and leave every row below green while the
# escape stayed wide open.


def _tree(tmp_path):
    """A checkout root with one artifacts component below it, plus a sibling
    directory genuinely outside that root to redirect into."""
    root = tmp_path / "checkout"
    (root / "artifacts").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return root, outside


def _tap(label: str, seen: list[str], real):
    def record(path, data, **kw):
        seen.append(label)
        return real(path, data, **kw)

    return record


def test_set_frontmatter_status_takes_the_confined_arm_for_an_in_tree_spec(tmp_path, monkeypatch):
    """The positive control, and the row that grades WHICH writer an in-tree spec
    reaches rather than only that the edit landed.

    Both bindings are wrapped and both keep the real write, so this is a control
    and not a stub measurement — the flip below actually lands on disk. Wrapping
    only one would grade "a write happened", which the twenty characterization
    rows above already do.

    Ablation: swap the two arms of the `is_relative_to` branch and this fails on
    `seen`, with the file still correctly rewritten."""
    root, _ = _tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    seen: list[str] = []
    monkeypatch.setattr(
        frontmatter,
        "atomic_write_bytes_confined",
        _tap("confined", seen, frontmatter.atomic_write_bytes_confined),
    )
    monkeypatch.setattr(
        frontmatter, "atomic_write_bytes", _tap("plain", seen, frontmatter.atomic_write_bytes)
    )

    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=root) is True

    assert seen == ["confined"]
    assert frontmatter.read_frontmatter(spec)["status"] == "done"


def test_set_frontmatter_status_keeps_the_plain_write_for_an_out_of_tree_spec(
    tmp_path, monkeypatch
):
    """The else-arm, and the reason the chokepoint is a branch rather than a
    straight conversion: an artifacts folder configured OUTSIDE the checkout is
    supported configuration, and the confined writer cannot vouch for a tree it
    was not given. Refusing there would break working setups rather than close a
    hole, so the plain no-follow write is kept — exactly what this site did before
    #593.

    Ablation: drop the `is_relative_to` branch and call the confined writer
    unconditionally, and this fails with `UnconfinedWriteError` — the spec never
    rewritten."""
    root, outside = _tree(tmp_path)
    spec = _spec(outside, _PLAIN)
    seen: list[str] = []
    monkeypatch.setattr(
        frontmatter,
        "atomic_write_bytes_confined",
        _tap("confined", seen, frontmatter.atomic_write_bytes_confined),
    )
    monkeypatch.setattr(
        frontmatter, "atomic_write_bytes", _tap("plain", seen, frontmatter.atomic_write_bytes)
    )

    assert frontmatter.set_frontmatter_status(spec, "done", confine_root=root) is True

    assert seen == ["plain"]
    assert frontmatter.read_frontmatter(spec)["status"] == "done"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_set_frontmatter_status_refuses_a_symlinked_parent(tmp_path):
    """The escape #593 names, at this site. `follow_symlinks=False` stopped a link
    planted at the SPEC; it did nothing about one planted at the directory holding
    it, because `mkstemp(dir=...)` and `os.replace`'s destination were still
    ordinary path lookups — so the temp and the published spec both landed
    wherever the link pointed, outside the checkout entirely.

    The read half still resolves through the link (the writer reads by name), so
    the edit is computed and only the WRITE refuses: this reaches the writer
    rather than bailing out at `is_file`.

    The last assertions are the load-bearing ones — refusing loudly is worth
    nothing if the bytes already escaped.

    Ablation: revert the call to
    `atomic_write_bytes(path, payload, follow_symlinks=False)` and this fails
    `DID NOT RAISE`, with the victim spec rewritten out in `outside/`."""
    root, outside = _tree(tmp_path)
    victim = _spec(outside, _PLAIN, name="victim.md")
    (root / "artifacts").rmdir()
    (root / "artifacts").symlink_to(outside, target_is_directory=True)
    spec = root / "artifacts" / "victim.md"
    assert spec.is_file()  # the read still resolves through the planted link

    with pytest.raises(platform_util.UnconfinedWriteError):
        frontmatter.set_frontmatter_status(spec, "done", confine_root=root)

    assert victim.read_bytes() == _PLAIN.encode("utf-8")  # not rewritten
    assert sorted(p.name for p in outside.iterdir()) == ["victim.md"]  # nor staged


def test_set_frontmatter_status_refuses_a_readonly_spec(tmp_path):
    """#597 at this site. `os.replace` needs write permission on the parent
    DIRECTORY, never on the entry it replaces, so a spec an operator marked
    read-only was rewritten anyway — and the mode came back `0444`, leaving
    nothing in the permission bits to record it. The `PermissionError` here is the
    one a bare `write_bytes` raised before this writer went atomic.

    `0o444` sets the READONLY attribute on win32 too, so this runs unskipped on
    both platforms; the chmod is on a file in this test's own tmp_path and is
    restored in a `finally` (Windows rmtree refuses a READONLY leftover).

    Ablation: drop `require_writable_target=True` from the confined call and this
    fails `DID NOT RAISE`, with the spec reading `status: done` and still `0444`."""
    root, _ = _tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    spec.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            frontmatter.set_frontmatter_status(spec, "done", confine_root=root)
    finally:
        spec.chmod(0o644)

    assert spec.read_bytes() == _PLAIN.encode("utf-8")
    assert list((root / "artifacts").glob("*.tmp")) == []  # a refusal stages nothing


_FRESH = "b" * 40
_STALE = "a" * 40


@pytest.mark.parametrize(
    "fm,expected",
    [
        # the key the skill actually stamps, alone
        ({"baseline_revision": _FRESH}, _FRESH),
        # legacy-only spec: the orchestrator's own name, kept readable
        ({"baseline_commit": _STALE}, _STALE),
        # THE #716 CASE. `rearm_escalation` inserts `baseline_revision` and never
        # removes a pre-existing `baseline_commit`, so a re-armed spec carries both.
        # The replaced expression was `fm.get("baseline_commit", fm.get(...))`, which
        # ranked the leftover FIRST and failed an attempt that did everything right.
        ({"baseline_revision": _FRESH, "baseline_commit": _STALE}, _FRESH),
        # `dict.get`'s default fires only on a MISSING key, so an empty legacy value
        # was SELECTED and yielded "" — which every consumer reads as "no claim",
        # disabling the baseline-match gate outright.
        ({"baseline_revision": _FRESH, "baseline_commit": ""}, _FRESH),
        # the fallback is on the VALUE, not the key: an empty fresh key defers
        ({"baseline_revision": "", "baseline_commit": _STALE}, _STALE),
        # YAML-null on either key is absent, never the token "None" (#358)
        ({"baseline_commit": None}, ""),
        ({"baseline_revision": None, "baseline_commit": _STALE}, _STALE),
        ({"baseline_revision": None, "baseline_commit": None}, ""),
        # A YAML boolean is the same trap class: PyYAML resolves `no`/`off`/`false`
        # to False, `str(False)` is the token "False", and the truthiness test runs on
        # that STRING — so an unguarded bool reads back as a claimed sha.
        ({"baseline_revision": False}, ""),
        ({"baseline_revision": True}, ""),
        ({"baseline_commit": False}, ""),
        # the sharp case: a bool on the WINNING key must defer to a valid legacy sha
        # rather than shadow it. Unguarded this returns "False", which the gate's
        # non-empty filter admits and `_canonical_commit_oid` then rejects — refusing
        # an attempt whose correct baseline was sitting on the very next line.
        ({"baseline_revision": False, "baseline_commit": _STALE}, _STALE),
        ({"baseline_revision": True, "baseline_commit": _STALE}, _STALE),
        ({"baseline_revision": f"  {_FRESH}  "}, _FRESH),  # stripped
        ({}, ""),  # claims nothing
        ({"baseline_revision": 123}, "123"),  # a non-string scalar still reads back
    ],
)
def test_auto_dev_baseline_of_precedence(fm, expected):
    """The one reader both consumers of a claimed baseline go through (#716).

    Lives here rather than in `test_verify.py` (which reached it through verify's
    re-export) because AGENTS.md has the flat `tests/` mirror src modules by name and
    the symbol is defined in `frontmatter.py`.

    Ablation for the negative rows: delete the ``raw is None`` guard and the
    YAML-null rows read back the token ``"None"``; delete the ``isinstance(raw, bool)``
    guard and the boolean rows read back ``"True"``/``"False"``, including in place of
    the legacy sha they must defer to; delete the ``if value:`` guard and the
    empty-legacy-key row reads back ``""``.
    """
    assert frontmatter.auto_dev_baseline_of(fm) == expected


# ============================================================ anchored writer
#
# `set_frontmatter_status_anchored` (DW-319/DW-323): the same edit, bound to one
# target inode from the read through staging, publication and acceptance. One row
# per I/O-matrix scenario of the spec that introduced it.

requires_anchored = pytest.mark.skipif(
    not platform_util.HANDLE_ANCHORED_WRITES, reason="needs a handle-anchored write arm"
)
_CRLF = _PLAIN.replace("\n", "\r\n")


def _anchored_tree(tmp_path):
    """`_tree`, canonicalized: the external arm demands a canonical parent, and a
    tmp_path behind a link (macOS /var -> /private/var) would otherwise read as
    an unsafe spelling rather than as the row being tested."""
    return _tree(tmp_path.resolve())


def _swap_in(spec, data: bytes) -> None:
    """Replace ``spec`` with a NEW inode holding ``data`` — an editor's save."""
    staged = spec.with_name(spec.name + ".swap")
    staged.write_bytes(data)
    os.replace(staged, spec)


@requires_anchored
def test_anchored_in_project_normalize_keeps_every_other_byte(tmp_path):
    """Matrix row "in-project normalize": CRLF intact, only the status moved, and
    the returned identity is the inode now at the name with the bytes it holds."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _CRLF)

    published = frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    expected = _CRLF.replace("status: in-review", "status: done").encode("utf-8")
    assert spec.read_bytes() == expected
    assert published.data == expected
    assert os.path.samestat(published.stat, os.stat(spec))
    assert list((root / "artifacts").glob("*.tmp")) == []


@requires_anchored
def test_anchored_external_normalize_walks_from_the_filesystem_root(tmp_path, monkeypatch):
    """Matrix row "external normalize": a spec outside ``confine_root`` is no
    longer handed to the plain path writer (DW-323). It is reached by a
    filesystem-root walk, and the parent is re-walked after publication.

    Ablation: route the external arm back to `set_frontmatter_status` and this
    fails on ``roots`` (no anchored walk at all)."""
    root, outside = _anchored_tree(tmp_path)
    spec = _spec(outside, _PLAIN)
    roots: list[tuple] = []
    real_open = frontmatter.open_dir_confined

    def record(walk_root, target, **kwargs):
        roots.append((walk_root, target, kwargs.get("search_only", False)))
        return real_open(walk_root, target, **kwargs)

    monkeypatch.setattr(frontmatter, "open_dir_confined", record)

    frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    anchor = Path(spec.anchor)
    assert roots == [(anchor, outside, True), (anchor, outside, True)]  # walk + re-probe
    assert spec.read_bytes() == _PLAIN.replace("in-review", "done").encode("utf-8")


@requires_anchored
def test_anchored_already_at_target_writes_nothing(tmp_path):
    """Matrix row "already at target": no write, same inode and version, and the
    returned identity is the file as read."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    before = os.stat(spec)

    current = frontmatter.set_frontmatter_status_anchored(spec, "in-review", confine_root=root)

    after = os.stat(spec)
    assert os.path.samestat(before, after)
    assert (before.st_mtime_ns, before.st_ctime_ns) == (after.st_mtime_ns, after.st_ctime_ns)
    assert current.data == _PLAIN.encode("utf-8")
    assert os.path.samestat(current.stat, after)


@requires_anchored
@pytest.mark.parametrize("edit", ["in-place", "new-inode-same-bytes", "same-stat-other-bytes"])
def test_anchored_refuses_a_target_changed_since_expected(tmp_path, edit):
    """Matrix row "restore then operator edit", at the primitive: an ``expected``
    identity the file no longer matches refuses before any write. The
    same-bytes swap pins that bytes alone are not the identity; the
    same-stat case (an ``expected`` carrying the file's live stat but other
    bytes — what one mtime tick can hide) pins that the bytes are compared, not
    just the stat. The in-place edit is the ordinary operator save.

    Ablation: drop the ``expected`` comparison and every case rewrites the
    operator's file to ``status: done``; compare stats only and the
    same-stat case does."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    established = frontmatter.set_frontmatter_status_anchored(spec, "in-review", confine_root=root)
    if edit == "same-stat-other-bytes":
        operator = _PLAIN.encode("utf-8")
        established = frontmatter.FileIdentity(established.stat, b"other")
    elif edit == "in-place":
        operator = _PLAIN.replace("# Spec", "# Operator").encode("utf-8")
        with open(spec, "r+b") as fh:
            fh.write(operator)
    else:
        operator = _PLAIN.encode("utf-8")
        _swap_in(spec, operator)

    with pytest.raises(frontmatter.FrontmatterTargetChangedError):
        frontmatter.set_frontmatter_status_anchored(
            spec, "done", confine_root=root, expected=established
        )

    assert spec.read_bytes() == operator


@requires_anchored
def test_anchored_accepts_the_identity_it_was_handed(tmp_path):
    """The positive control for the row above: an unchanged ``expected`` writes."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    established = frontmatter.set_frontmatter_status_anchored(spec, "in-review", confine_root=root)

    frontmatter.set_frontmatter_status_anchored(
        spec, "done", confine_root=root, expected=established
    )

    assert frontmatter.read_frontmatter(spec)["status"] == "done"


def _hooked_writer(monkeypatch, hook: str, action):
    """Wrap the anchored writer so ``action`` runs just before ``hook`` does."""
    real = frontmatter.atomic_write_bytes_at

    def wrapped(dir_fd, name, data, **kwargs):
        inner = kwargs[hook]

        def racing(*args):
            action()
            return inner(*args)

        kwargs[hook] = racing
        return real(dir_fd, name, data, **kwargs)

    monkeypatch.setattr(frontmatter, "atomic_write_bytes_at", wrapped)


@requires_anchored
@pytest.mark.parametrize("hook", ["_before_staging", "_before_replace"])
@pytest.mark.parametrize("race", ["swap", "edit"])
def test_anchored_refuses_a_swap_before_publication(tmp_path, monkeypatch, hook, race):
    """Matrix row "swap mid-transaction": a replace or in-place edit of the target
    between the read and either pre-publication hook publishes nothing and
    leaves no temp behind.

    Ablation: drop the ``_before_staging``/``_before_replace`` revalidation and
    the operator's bytes are overwritten with the stale edit."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    operator = _PLAIN.replace("# Spec", "# Operator").encode("utf-8")

    def interfere():
        if race == "swap":
            _swap_in(spec, operator)
        else:
            with open(spec, "r+b") as fh:
                fh.write(operator)

    _hooked_writer(monkeypatch, hook, interfere)

    with pytest.raises(frontmatter.FrontmatterTargetChangedError):
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    assert spec.read_bytes() == operator
    assert list((root / "artifacts").glob("*.tmp")) == []


@requires_anchored
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows may refuse to replace a name whose inode the writer still holds open",
)
def test_anchored_post_publication_swap_fails_acceptance(tmp_path, monkeypatch):
    """A name swapped away from the published inode fails acceptance loudly —
    the writer never reports a publication the name no longer carries."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    operator = b"---\nstatus: draft\n---\nlate operator save\n"
    _hooked_writer(monkeypatch, "_after_replace", lambda: _swap_in(spec, operator))

    with pytest.raises(frontmatter.FrontmatterTargetChangedError):
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    assert spec.read_bytes() == operator


@requires_anchored
@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks may need elevation")
def test_anchored_external_parent_retarget_fails_acceptance(tmp_path, monkeypatch):
    """The external arm's last acceptance step: a parent renamed and replaced by
    a link after publication fails the fresh root walk."""
    root, outside = _anchored_tree(tmp_path)
    spec = _spec(outside, _PLAIN)
    moved = outside.with_name("moved")

    def retarget():
        outside.rename(moved)
        outside.symlink_to(moved, target_is_directory=True)

    real_accept = frontmatter._accept_published

    def accept_then_retarget(*args):
        identity = real_accept(*args)
        retarget()
        return identity

    monkeypatch.setattr(frontmatter, "_accept_published", accept_then_retarget)

    with pytest.raises(frontmatter.FrontmatterTargetChangedError):
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)


@requires_anchored
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@pytest.mark.parametrize("where", ["in-project", "external"])
def test_anchored_refuses_a_link_at_the_final_name(tmp_path, where):
    """Matrix row "link target": a symlink at the spec name is never followed —
    the file it points at is neither read into the edit nor written."""
    root, outside = _anchored_tree(tmp_path)
    victim = _spec(tmp_path.resolve(), _PLAIN, name="victim.md")
    home = root / "artifacts" if where == "in-project" else outside
    spec = home / "spec.md"
    spec.symlink_to(victim)

    with pytest.raises(frontmatter.FrontmatterTargetChangedError):
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    assert spec.is_symlink()
    assert victim.read_bytes() == _PLAIN.encode("utf-8")


@requires_anchored
@pytest.mark.parametrize("swapped", ["payload-bytes", "other-bytes"])
def test_anchored_settled_readback_refuses_a_swapped_inode(tmp_path, monkeypatch, swapped):
    """The returned identity is read back after the writer releases the
    published inode, and it must still BE that inode holding the payload. A
    name swapped to a new inode in that window — even one carrying the payload
    bytes — raises rather than being reported as what was published.

    Ablation: drop the samestat/bytes conditions of the settled read-back and
    both cases return the swapped file's identity."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    payload = _PLAIN.replace("in-review", "done").encode("utf-8")
    replacement = payload if swapped == "payload-bytes" else b"---\nstatus: draft\n---\nlate\n"
    real_writer = frontmatter.atomic_write_bytes_at

    def write_then_swap(*args, **kwargs):
        real_writer(*args, **kwargs)
        _swap_in(spec, replacement)

    monkeypatch.setattr(frontmatter, "atomic_write_bytes_at", write_then_swap)

    with pytest.raises(frontmatter.FrontmatterTargetChangedError, match="right after"):
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    assert spec.read_bytes() == replacement


@requires_anchored
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_anchored_fifo_racing_the_name_stat_is_opened_without_blocking(tmp_path, monkeypatch):
    """The name stat passes (a regular file stood there), then the open meets a
    FIFO. ``AT_NONBLOCK`` makes that open return at once — a read-only
    nonblocking FIFO open succeeds, and the descriptor's own fstat then refuses
    it — instead of waiting forever for a writer.

    Ablation: drop ``AT_NONBLOCK`` from the target open and the call never
    returns (the thread below is still blocked when the join times out)."""
    import threading

    root, _ = _anchored_tree(tmp_path)
    regular = _spec(tmp_path.resolve(), _PLAIN, name="regular.md")
    spec = root / "artifacts" / "spec.md"
    os.mkfifo(spec)
    real_stat = frontmatter._named_regular_stat
    calls: list[int] = []

    def regular_first(dir_fd, name, path):
        calls.append(1)
        if len(calls) == 1:
            return os.stat(regular)
        return real_stat(dir_fd, name, path)

    monkeypatch.setattr(frontmatter, "_named_regular_stat", regular_first)
    outcome: list[BaseException] = []

    def attempt():
        try:
            frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)
        except BaseException as exc:  # noqa: BLE001 — handed back to the test thread
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        with open(spec, "wb"):  # release the blocked open so the thread can end
            pass
        pytest.fail("the anchored read blocked on a FIFO")
    assert len(outcome) == 1
    assert isinstance(outcome[0], frontmatter.FrontmatterTargetChangedError)


@requires_anchored
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_anchored_link_racing_the_name_stat_is_refused_by_the_open(tmp_path, monkeypatch):
    """The name stat reports the link's TARGET (as if a regular file stood
    there), then the open meets the link. ``AT_NOFOLLOW`` fails that open with
    ``ELOOP``, and the errno is translated to `FrontmatterTargetChangedError`.

    Ablation: drop ``AT_NOFOLLOW`` and the open follows the link — every check
    agrees with the patched stat and the link is replaced by a write; drop the
    errno translation and a raw ``OSError`` escapes instead."""
    import errno

    root, _ = _anchored_tree(tmp_path)
    victim = _spec(tmp_path.resolve(), _PLAIN, name="victim.md")
    spec = root / "artifacts" / "spec.md"
    spec.symlink_to(victim)
    monkeypatch.setattr(frontmatter, "_named_regular_stat", lambda *_a: os.stat(victim))

    with pytest.raises(frontmatter.FrontmatterTargetChangedError) as raised:
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.errno == errno.ELOOP
    assert spec.is_symlink()
    assert victim.read_bytes() == _PLAIN.encode("utf-8")


@requires_anchored
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_anchored_refuses_a_fifo_without_blocking(tmp_path):
    """Matrix row "non-regular target": a reader-less FIFO at the name raises
    instead of wedging the read forever."""
    root, _ = _anchored_tree(tmp_path)
    spec = root / "artifacts" / "spec.md"
    os.mkfifo(spec)

    with pytest.raises(frontmatter.FrontmatterTargetChangedError):
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)


@requires_anchored
@pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks may need elevation")
@pytest.mark.parametrize("where", ["in-project", "external"])
def test_anchored_refuses_a_symlinked_parent(tmp_path, where):
    """Matrix row "symlinked parent component": the walk refuses and nothing
    lands behind the link. External arm included — the plain path writer it
    replaces would have followed this link."""
    root, outside = _anchored_tree(tmp_path)
    elsewhere = tmp_path.resolve() / "elsewhere"
    elsewhere.mkdir()
    victim = _spec(elsewhere, _PLAIN)
    if where == "in-project":
        parent = root / "artifacts"
        parent.rmdir()
    else:
        parent = outside / "linked"
    parent.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(platform_util.UnconfinedWriteError):
        frontmatter.set_frontmatter_status_anchored(parent / "spec.md", "done", confine_root=root)

    assert victim.read_bytes() == _PLAIN.encode("utf-8")
    assert sorted(p.name for p in elsewhere.iterdir()) == ["spec.md"]


@requires_anchored
def test_anchored_refuses_a_readonly_spec(tmp_path):
    """Matrix row "read-only spec": the kernel's `PermissionError`, as the path
    writer raises (#597), and nothing staged."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    spec.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)
    finally:
        spec.chmod(0o644)

    assert spec.read_bytes() == _PLAIN.encode("utf-8")
    assert list((root / "artifacts").glob("*.tmp")) == []


@requires_anchored
def test_anchored_keeps_the_unrewritable_shape_refusal(tmp_path):
    """The edit semantics are shared, refusal included."""
    root, _ = _anchored_tree(tmp_path)
    text, _reads_as = _UNREWRITABLE["block-scalar"]
    spec = _spec(root / "artifacts", text)

    with pytest.raises(frontmatter.FrontmatterWriteError) as raised:
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    assert not isinstance(raised.value, frontmatter.FrontmatterTargetChangedError)
    assert spec.read_bytes() == text.encode("utf-8")


def test_anchored_without_handle_anchored_writes_raises_before_reading(tmp_path, monkeypatch):
    """Matrix row "no anchored writes": no path-based fallback — the primitive
    raises before it walks, reads, or writes anything."""
    root, _ = _anchored_tree(tmp_path)
    spec = _spec(root / "artifacts", _PLAIN)
    monkeypatch.setattr(frontmatter, "HANDLE_ANCHORED_WRITES", False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the anchored writer walked without handle-anchored writes")

    monkeypatch.setattr(frontmatter, "open_dir_confined", forbidden)
    monkeypatch.setattr(frontmatter, "atomic_write_bytes", forbidden)
    monkeypatch.setattr(frontmatter, "atomic_write_bytes_confined", forbidden)

    with pytest.raises(frontmatter.FrontmatterWriteError):
        frontmatter.set_frontmatter_status_anchored(spec, "done", confine_root=root)

    assert spec.read_bytes() == _PLAIN.encode("utf-8")


# ============================================================ worktree-mount pin (DW-423)
#
# `root_identity=` on `set_frontmatter_status`'s confined arm pins an
# orchestrator-minted worktree mount. The swap: the mount renamed aside and a link
# planted at its name to an outside tree carrying the same spec subpath.

requires_symlinked_mount_swap = pytest.mark.skipif(
    not platform_util.DIR_FD_ANCHORED_WRITES or sys.platform == "win32",
    reason="dir-fd anchoring and POSIX symlinks",
)


def _mount_swap_pair(tmp_path, text: str) -> tuple[Path, Path, os.stat_result]:
    """A mount holding ``specs/6-4.md``, its accepted ``lstat`` identity, then the
    mount swapped for a link to an outside copy. Returns (mount, outside spec,
    identity)."""
    mount = tmp_path / "project" / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    (mount / "specs").mkdir(parents=True)
    (mount / "specs" / "6-4.md").write_text(text, encoding="utf-8")
    identity = os.lstat(mount)
    outside = tmp_path / "outside"
    (outside / "specs").mkdir(parents=True)
    (outside / "specs" / "6-4.md").write_text(text, encoding="utf-8")
    mount.rename(mount.with_name("1-aside"))
    mount.symlink_to(outside, target_is_directory=True)
    return mount, outside / "specs" / "6-4.md", identity


@requires_symlinked_mount_swap
def test_set_frontmatter_status_pinned_mount_refuses_a_mount_swapped_for_a_link(tmp_path):
    """The pinned write refuses a swapped mount and leaves the outside bytes alone;
    the unpinned control shows the same swap really lands outside.

    Ablation: drop the `root_identity=` forward in `set_frontmatter_status` and the
    pinned call lands outside instead of raising."""
    text = "---\nstatus: blocked\n---\nbody\n"
    mount, outside_spec, identity = _mount_swap_pair(tmp_path, text)
    spec = mount / "specs" / "6-4.md"

    with pytest.raises(platform_util.UnconfinedWriteError):
        frontmatter.set_frontmatter_status(
            spec, "in-progress", confine_root=mount, root_identity=identity
        )
    assert outside_spec.read_text(encoding="utf-8") == text

    # Control: unpinned, the swap carries the write outside the repository.
    assert frontmatter.set_frontmatter_status(spec, "in-progress", confine_root=mount)
    assert "status: in-progress" in outside_spec.read_text(encoding="utf-8")


@requires_symlinked_mount_swap
def test_set_frontmatter_status_anchored_pinned_mount_refuses_a_mount_swapped_for_a_link(
    tmp_path,
):
    """DW-445: the descriptor-anchored writer (recovery's attempt-owned
    normalization) pins its in-project root the same way — a swapped mount refuses
    with `UnconfinedWriteError` and the outside bytes are unchanged; the unpinned
    control shows the same swap really lands outside.

    Ablation: drop the `root_identity` forward from `set_frontmatter_status_anchored`
    to `_open_anchored_parent` (or from there to `open_dir_confined`) and the pinned
    call rewrites the outside copy instead of raising."""
    text = "---\nstatus: done\n---\nbody\n"
    mount, outside_spec, identity = _mount_swap_pair(tmp_path, text)
    spec = mount / "specs" / "6-4.md"

    with pytest.raises(platform_util.UnconfinedWriteError):
        frontmatter.set_frontmatter_status_anchored(
            spec, "ready-for-dev", confine_root=mount, root_identity=identity
        )
    assert outside_spec.read_text(encoding="utf-8") == text

    # Control: unpinned, the swap carries the write outside the repository.
    frontmatter.set_frontmatter_status_anchored(spec, "ready-for-dev", confine_root=mount)
    assert "status: ready-for-dev" in outside_spec.read_text(encoding="utf-8")


@requires_symlinked_mount_swap
def test_set_frontmatter_status_anchored_pinned_intact_mount_writes(tmp_path):
    """The pin binds nothing extra on an intact mount: the identity matches and the
    write lands in the mount."""
    mount = tmp_path / "project" / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    (mount / "specs").mkdir(parents=True)
    spec = mount / "specs" / "6-4.md"
    spec.write_text("---\nstatus: done\n---\nbody\n", encoding="utf-8")

    frontmatter.set_frontmatter_status_anchored(
        spec, "ready-for-dev", confine_root=mount, root_identity=os.lstat(mount)
    )
    assert "status: ready-for-dev" in spec.read_text(encoding="utf-8")


# ---------------------------------------------- external-arm pin pre-check (DW-445)
#
# A pinned writer handed a path OUTSIDE its `confine_root` — here the spec's resolved
# spelling after the mount was swapped for a link, which is what a reset or a relative
# binding resolved through the swap produces — pre-checks that the root is still the
# pinned directory. An intact mount whose `_bmad-output` is a link resolving outside
# it passes that check and writes exactly as unpinned.


def _external_after_swap(tmp_path, text: str) -> tuple[Path, Path, os.stat_result]:
    """(mount, the spec's canonical OUTSIDE path, the mount's accepted identity) —
    the mount already swapped for a link to the outside copy."""
    mount, outside_spec, identity = _mount_swap_pair(tmp_path, text)
    return mount, outside_spec.resolve(), identity


def _external_behind_linked_output(tmp_path, text: str) -> tuple[Path, Path, os.stat_result]:
    """(intact mount, the spec's canonical path under a `_bmad-output` link resolving
    outside the mount, the mount's identity)."""
    mount = tmp_path / "project" / ".bmad-loop" / "runs" / "r1" / "worktrees" / "1"
    mount.mkdir(parents=True)
    shared = tmp_path / "shared-output"
    (shared / "specs").mkdir(parents=True)
    (shared / "specs" / "6-4.md").write_text(text, encoding="utf-8")
    (mount / "_bmad-output").symlink_to(shared, target_is_directory=True)
    return mount, (shared / "specs" / "6-4.md").resolve(), os.lstat(mount)


@requires_symlinked_mount_swap
def test_set_frontmatter_status_external_arm_refuses_a_swapped_pinned_root(tmp_path):
    """The path writer's external arm pre-checks a given pin: the mount swapped
    before the path was resolved refuses and the outside bytes are unchanged; the
    unpinned control (None) writes them as before.

    Ablation: drop the `require_root_pinned` pre-check from the external arm and
    the pinned call rewrites the outside copy."""
    text = "---\nstatus: done\n---\nbody\n"
    mount, outside_spec, identity = _external_after_swap(tmp_path, text)

    with pytest.raises(platform_util.UnconfinedWriteError, match="pinned to"):
        frontmatter.set_frontmatter_status(
            outside_spec, "in-progress", confine_root=mount, root_identity=identity
        )
    assert outside_spec.read_text(encoding="utf-8") == text

    assert frontmatter.set_frontmatter_status(outside_spec, "in-progress", confine_root=mount)
    assert "status: in-progress" in outside_spec.read_text(encoding="utf-8")


@requires_symlinked_mount_swap
def test_set_frontmatter_status_external_arm_writes_through_an_intact_pinned_root(tmp_path):
    """An intact pinned mount whose `_bmad-output` resolves outside it: the external
    arm's pre-check passes and the write lands as it does unpinned."""
    text = "---\nstatus: done\n---\nbody\n"
    mount, spec, identity = _external_behind_linked_output(tmp_path, text)

    assert frontmatter.set_frontmatter_status(
        spec, "in-progress", confine_root=mount, root_identity=identity
    )
    assert "status: in-progress" in spec.read_text(encoding="utf-8")


@requires_symlinked_mount_swap
def test_set_frontmatter_status_anchored_external_arm_refuses_a_swapped_pinned_root(tmp_path):
    """The anchored writer's external arm (recovery's binding resolved through a
    swapped mount is canonical OUTSIDE it) pre-checks a given pin: refused, outside
    bytes unchanged; the unpinned control rewrites them.

    Ablation: drop the `require_root_pinned` pre-check from `_open_anchored_parent`'s
    external arm (or the `root_identity` forward from `set_frontmatter_status_anchored`)
    and the pinned call rewrites the outside copy."""
    text = "---\nstatus: done\n---\nbody\n"
    mount, outside_spec, identity = _external_after_swap(tmp_path, text)

    with pytest.raises(platform_util.UnconfinedWriteError, match="pinned to"):
        frontmatter.set_frontmatter_status_anchored(
            outside_spec, "ready-for-dev", confine_root=mount, root_identity=identity
        )
    assert outside_spec.read_text(encoding="utf-8") == text

    frontmatter.set_frontmatter_status_anchored(outside_spec, "ready-for-dev", confine_root=mount)
    assert "status: ready-for-dev" in outside_spec.read_text(encoding="utf-8")


@requires_symlinked_mount_swap
def test_set_frontmatter_status_anchored_external_arm_writes_through_an_intact_pinned_root(
    tmp_path,
):
    """An intact pinned mount with a linked `_bmad-output`: the anchored external
    arm writes as it does unpinned."""
    text = "---\nstatus: done\n---\nbody\n"
    mount, spec, identity = _external_behind_linked_output(tmp_path, text)

    frontmatter.set_frontmatter_status_anchored(
        spec, "ready-for-dev", confine_root=mount, root_identity=identity
    )
    assert "status: ready-for-dev" in spec.read_text(encoding="utf-8")


@requires_symlinked_mount_swap
def test_set_frontmatter_field_external_arm_refuses_a_swapped_pinned_root(tmp_path):
    """`verify.set_frontmatter_field` shares the rule: a pinned external write
    through a swapped mount refuses, outside bytes unchanged; the unpinned control
    lands; an intact pinned mount behind a linked `_bmad-output` lands too.

    Ablation: drop the `require_root_pinned` pre-check from its external arm and the
    pinned call rewrites the outside copy."""
    text = "---\nstatus: done\n---\nbody\n"
    mount, outside_spec, identity = _external_after_swap(tmp_path / "swap", text)

    with pytest.raises(platform_util.UnconfinedWriteError, match="pinned to"):
        verify.set_frontmatter_field(
            outside_spec, "baseline_revision", "abc", confine_root=mount, root_identity=identity
        )
    assert outside_spec.read_text(encoding="utf-8") == text
    assert verify.set_frontmatter_field(
        outside_spec, "baseline_revision", "abc", confine_root=mount
    )
    assert "baseline_revision: abc" in outside_spec.read_text(encoding="utf-8")

    mount, spec, identity = _external_behind_linked_output(tmp_path / "intact", text)
    assert verify.set_frontmatter_field(
        spec, "baseline_revision", "abc", confine_root=mount, root_identity=identity
    )
    assert "baseline_revision: abc" in spec.read_text(encoding="utf-8")
