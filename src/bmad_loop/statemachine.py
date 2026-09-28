"""Story lifecycle transition table — the single source of truth for legal moves.

Two tables live here. :data:`TRANSITIONS` is the task-phase graph
:func:`advance` enforces. :data:`BOARD_REGRESSIONS` is the sprint-board side:
``sprintstatus.advance`` never moves a row backward through its lifecycle order,
and the pairs listed here are the only exceptions it may perform — each one only
when its caller opts in explicitly (:func:`check_board_regression`).
"""

from __future__ import annotations

from .model import Phase, StoryTask


class IllegalTransition(Exception):
    pass


TRANSITIONS: dict[Phase, frozenset[Phase]] = {
    # TRIAGE_RUNNING: a sweep run's triage task — also reused by the sweep's
    # legacy-ledger migration task (same lifecycle, its own task key);
    # story tasks go to DEV_RUNNING.
    # DONE: the sweep migrate task's empty manifest (DW-440) — its input holds no
    # legacy entries, so the migration is a completed no-op and dispatches nothing
    Phase.PENDING: frozenset({Phase.DEV_RUNNING, Phase.TRIAGE_RUNNING, Phase.DONE}),
    Phase.DEV_RUNNING: frozenset({Phase.DEV_VERIFY}),
    # COMMITTING: review.enabled = false skips the review loop entirely, so a
    # verified dev pass commits straight from DEV_VERIFY
    Phase.DEV_VERIFY: frozenset(
        {Phase.DEV_RUNNING, Phase.REVIEW_RUNNING, Phase.COMMITTING, Phase.DEFERRED, Phase.ESCALATED}
    ),
    Phase.REVIEW_RUNNING: frozenset({Phase.REVIEW_VERIFY}),
    Phase.REVIEW_VERIFY: frozenset(
        # DEV_RUNNING: fix session after a clean review whose verify commands failed
        {
            Phase.REVIEW_RUNNING,
            Phase.DEV_RUNNING,
            Phase.COMMITTING,
            Phase.DEFERRED,
            Phase.ESCALATED,
        }
    ),
    # AWAITING_OPERATOR: a story owing human-only external actions parks on the
    # NORMAL commit path — the work commits, then the final phase is chosen by
    # whether the task carries operator_actions. Reachable only from COMMITTING
    # precisely so a park can never skip the gates/commit a DONE story clears.
    Phase.COMMITTING: frozenset({Phase.DONE, Phase.ESCALATED, Phase.AWAITING_OPERATOR}),
    Phase.TRIAGE_RUNNING: frozenset({Phase.TRIAGE_VERIFY}),
    # TRIAGE_RUNNING: invalid triage output retries with feedback, like DEV_VERIFY
    Phase.TRIAGE_VERIFY: frozenset(
        {Phase.TRIAGE_RUNNING, Phase.COMMITTING, Phase.DONE, Phase.ESCALATED}
    ),
    Phase.DONE: frozenset(),
    Phase.DEFERRED: frozenset(),
    Phase.ESCALATED: frozenset(),
    # terminal: `bmad-loop confirm` completes a parked story out of band, not by
    # transitioning the (by then finished) run's task.
    Phase.AWAITING_OPERATOR: frozenset(),
}


def advance(task: StoryTask, to: Phase) -> None:
    allowed = TRANSITIONS[task.phase]
    if to not in allowed:
        raise IllegalTransition(
            f"{task.story_key}: {task.phase} -> {to} (allowed: {sorted(allowed)})"
        )
    task.phase = to


# The sprint-board regressions `sprintstatus.advance` may perform on explicit
# opt-in (`allow_regression=True`); every other backward move stays illegal.
# ("done", "awaiting-operator"): a review pass that finds a `done` story owes
# human-only external actions demotes it to a park (`[operator]
# on_review_demotion = "park"`, DW-383). The board sign-off the dev leg wrote is
# `done`, and without this exception never-regress would leave no honest move.
# Kept as (current, target) status-token pairs so this module stays free of any
# sprintstatus import (sprintstatus imports this one).
BOARD_REGRESSIONS: frozenset[tuple[str, str]] = frozenset({("done", "awaiting-operator")})


def check_board_regression(story_key: str, current: str, target: str) -> None:
    """Refuse a board regression ``current -> target`` that is not allowlisted.

    Called by the sole board writer only for a move it has already classified as
    a regression; a no-op for a pair in :data:`BOARD_REGRESSIONS`, and
    :class:`IllegalTransition` for anything else."""
    if (current, target) not in BOARD_REGRESSIONS:
        raise IllegalTransition(
            f"{story_key}: sprint-status {current} -> {target} is a regression "
            f"(allowed: {sorted(BOARD_REGRESSIONS)})"
        )
