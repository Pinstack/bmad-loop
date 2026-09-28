import pytest

from bmad_loop.model import TERMINAL_PHASES, Phase, StoryTask
from bmad_loop.sprintstatus import STATUS_ORDER
from bmad_loop.statemachine import (
    BOARD_REGRESSIONS,
    TRANSITIONS,
    IllegalTransition,
    advance,
    check_board_regression,
)


def test_table_covers_every_phase():
    assert set(TRANSITIONS) == set(Phase)


def test_terminal_phases_are_exactly_the_dead_ends():
    """`TERMINAL_PHASES` and the transition table are two spellings of the same
    fact, in two modules, with nothing linking them: a new terminal phase added
    to the table with no outgoing edges but forgotten in the frozenset would be
    driven as if it were still live (`StoryTask.terminal` is False), and every
    other test would still pass. Pin them to each other."""
    dead_ends = {phase for phase, targets in TRANSITIONS.items() if not targets}
    assert dead_ends == set(TERMINAL_PHASES)


@pytest.mark.parametrize("source", list(Phase))
@pytest.mark.parametrize("target", list(Phase))
def test_exhaustive_transitions(source, target):
    task = StoryTask(story_key="1-1-x", epic=1, phase=source)
    if target in TRANSITIONS[source]:
        advance(task, target)
        assert task.phase == target
    else:
        with pytest.raises(IllegalTransition):
            advance(task, target)
        assert task.phase == source


def test_happy_path_sequence():
    task = StoryTask(story_key="1-1-x", epic=1)
    for phase in (
        Phase.DEV_RUNNING,
        Phase.DEV_VERIFY,
        Phase.REVIEW_RUNNING,
        Phase.REVIEW_VERIFY,
        Phase.COMMITTING,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal


def test_park_path_sequence():
    """A story owing human-only external actions rides the NORMAL commit path and
    parks at the end of it: COMMITTING is the only phase it is reachable from, so
    a park can never skip the gates and the commit a DONE story clears."""
    task = StoryTask(story_key="1-1-x", epic=1)
    for phase in (
        Phase.DEV_RUNNING,
        Phase.DEV_VERIFY,
        Phase.REVIEW_RUNNING,
        Phase.REVIEW_VERIFY,
        Phase.COMMITTING,
        Phase.AWAITING_OPERATOR,
    ):
        advance(task, phase)
    assert task.terminal


@pytest.mark.parametrize(
    "source",
    [p for p in Phase if p is not Phase.COMMITTING],
)
def test_awaiting_operator_is_reachable_only_from_committing(source):
    """The explicit inverse of the happy path above. `test_exhaustive_transitions`
    covers this pairwise too, but only as one cell of an N-squared grid whose
    expectation is read out of the table under test — this states the rule
    independently, so widening the table cannot silently widen the test."""
    task = StoryTask(story_key="1-1-x", epic=1, phase=source)
    with pytest.raises(IllegalTransition):
        advance(task, Phase.AWAITING_OPERATOR)
    assert task.phase == source


def test_triage_path_sequence():
    task = StoryTask(story_key="sweep-triage", epic=0)
    for phase in (
        Phase.TRIAGE_RUNNING,
        Phase.TRIAGE_VERIFY,
        Phase.TRIAGE_RUNNING,  # invalid triage output retries
        Phase.TRIAGE_VERIFY,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal


def test_migration_triage_commit_path_sequence():
    task = StoryTask(story_key="sweep-migrate", epic=0)
    for phase in (
        Phase.TRIAGE_RUNNING,
        Phase.TRIAGE_VERIFY,
        Phase.COMMITTING,
        Phase.DONE,
    ):
        advance(task, phase)
    assert task.terminal


def test_pending_migration_with_an_empty_manifest_completes_as_done():
    """DW-440: a sweep migrate task whose input holds no legacy entries has nothing
    to convert, so `_ensure_migration` retires it straight from PENDING to DONE
    through `advance()` rather than dispatching a session. DONE is terminal, so
    `migration_resume` never re-enters it."""
    task = StoryTask(story_key="sweep-migrate", epic=0)
    advance(task, Phase.DONE)
    assert task.phase == Phase.DONE
    assert task.terminal
    with pytest.raises(IllegalTransition):
        advance(task, Phase.TRIAGE_RUNNING)


# ---------------------------------------------------------------------------
# sprint-board regression allowlist (DW-383)


def test_board_regression_allowlist_is_exactly_the_review_demotion():
    """One pair and no more: widening this set silently licenses the sole board
    writer to walk a story backward."""
    assert BOARD_REGRESSIONS == frozenset({("done", "awaiting-operator")})


def test_the_allowlisted_board_regression_passes():
    check_board_regression("1-1-x", "done", "awaiting-operator")  # no raise


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for i, current in enumerate(STATUS_ORDER)
        for target in STATUS_ORDER[:i]
        if (current, target) != ("done", "awaiting-operator")
    ],
)
def test_every_other_board_regression_raises(current, target):
    """Ablation: make `check_board_regression` a no-op (or widen the set) and every
    case here reddens."""
    with pytest.raises(IllegalTransition, match=f"{current} -> {target}"):
        check_board_regression("1-1-x", current, target)
