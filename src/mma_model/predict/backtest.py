"""Legacy fight-by-fight walk-forward wrapper (DWCS-306).

The historical evaluator advanced one fight at a time and could train on
same-card outcomes. That path is not betting evidence. Callers stay callable
and receive a deprecation record, or should use
``mma_model.backtest.engine.run_walk_forward``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from mma_model.backtest.engine import (
    WalkForwardDeprecatedError,
    legacy_deprecation_record,
    run_walk_forward,
)

UNSAFE_FIGHT_BY_FIGHT_DISABLED = (
    "The fight-by-fight walk_forward_backtest is fail-closed and does not "
    "invoke the event-grouped engine. It is not betting evidence. Use "
    "mma-model backtest run for event-grouped replay."
)


def walk_forward_backtest(
    session: Session | None = None,
    *,
    min_train_fights: int = 30,
    min_prior_fights: int = 1,
    last_n: int = 5,
    max_predictions: int | None = None,
    random_state: int = 42,
    allow_unsafe: bool = False,
) -> dict[str, Any]:
    """Compatibility wrapper. Fail-closed: does not invoke the walk-forward engine.

    ``allow_unsafe`` is rejected. Same-card results cannot enter later fights
    because this function never scores fight-by-fight.
    """
    if allow_unsafe:
        raise WalkForwardDeprecatedError(UNSAFE_FIGHT_BY_FIGHT_DISABLED)
    record = legacy_deprecation_record()
    record["legacy_args"] = {
        "last_n": last_n,
        "max_predictions": max_predictions,
        "min_prior_fights": min_prior_fights,
        "min_train_fights": min_train_fights,
        "random_state": random_state,
        "session_provided": session is not None,
    }
    record["same_card_leakage"] = False
    record["unsafe_evaluator_ran"] = False
    return record


def event_grouped_backtest(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Delegate to the DWCS-306 engine when callers opt in explicitly."""
    return run_walk_forward(*args, **kwargs)
