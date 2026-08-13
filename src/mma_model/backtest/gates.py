"""Walk-forward backtest gates (DWCS-306).

These gates enforce frozen hashes, holdout lock, priced-only scope, and
read-only database access. They do not implement DWCS-307 selection policy
and never mutate the evaluation contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Never

from mma_model.backtest.contract import (
    EvaluatorHashMismatchError,
    HashKind,
    verify_evaluator_hashes,
)
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH, EvaluationContract
from mma_model.modeling.splits import FoldRole, HoldoutLockedError
from mma_model.quality.readonly import (
    CoverageDatabaseError,
    is_prohibited_live_url,
    sqlite_path_from_url,
)


class BacktestGateError(ValueError):
    """A walk-forward invariant failed."""


class EvidenceTamperError(BacktestGateError):
    """Stored evidence content hash does not match recomputed digest."""


class EvidenceOverwriteError(BacktestGateError):
    """Refusing to replace an existing evidence file with a different payload."""


class DatabaseMutationError(BacktestGateError):
    """Backtest database access must be read-only."""


class PricedScopeError(BacktestGateError):
    """A threshold-only row received synthetic betting metrics."""


class HoldoutTrainError(HoldoutLockedError):
    """Locked 2025 cards were present in a training set."""


class GateKind(StrEnum):
    HASH = "hash"
    HOLDOUT = "holdout"
    PRICED_SCOPE = "priced_scope"
    READONLY = "readonly"
    TAMPER = "tamper"
    OVERWRITE = "overwrite"
    CUTOFF = "cutoff"


HOLDOUT_YEAR = 2025
PRICED_SCOPE = "priced_only"
THRESHOLD_SCOPE = "threshold_only"
SYNTHETIC_BETTING_FIELDS = frozenset(
    {
        "expected_value",
        "closing_ev",
        "probability_clv",
        "flat_unit_profit",
        "realized_roi",
        "quarter_kelly_fraction",
        "stake_fraction",
        "turnover",
        "maximum_drawdown",
        "longest_losing_run",
    }
)


def assert_cutoff_before_results(
    *,
    max_train_timestamp: datetime | None,
    cutoff: datetime,
    event_id: str,
) -> None:
    """``max_train_timestamp`` must be strictly before the shared card cutoff."""
    if max_train_timestamp is None:
        return
    if max_train_timestamp >= cutoff:
        raise BacktestGateError(
            f"max_train_timestamp {max_train_timestamp.isoformat()} is not strictly "
            f"before cutoff {cutoff.isoformat()} for event {event_id}"
        )


def assert_same_card_not_in_train(
    *,
    test_event_id: str,
    train_event_ids: Sequence[str],
) -> None:
    if test_event_id in set(train_event_ids):
        raise BacktestGateError(
            f"same-card leakage: {test_event_id} appears in train_event_ids"
        )


def assert_holdout_not_in_train(
    train_event_ids: Sequence[str],
    *,
    train_seasons: Sequence[int] | None = None,
    event_seasons: Mapping[str, int] | None = None,
    holdout_event_ids: Sequence[str] | None = None,
    holdout_seasons: Sequence[int] | None = None,
) -> None:
    """Holdout-season cards must never enter any refit, including sealed scoring.

    Season / FoldRole metadata is authoritative. An event named ``ufc-310`` in
    2025 is blocked even though the id string does not contain ``2025``.
    """
    locked_seasons = {int(year) for year in (holdout_seasons or (HOLDOUT_YEAR,))}
    locked_ids = {str(event_id) for event_id in (holdout_event_ids or ())}
    seasons = dict(event_seasons or {})
    for event_id in train_event_ids:
        if str(event_id) in locked_ids:
            raise HoldoutTrainError(
                f"locked holdout event {event_id!r} cannot enter training"
            )
        season = seasons.get(str(event_id))
        if season is not None and int(season) in locked_seasons:
            raise HoldoutTrainError(
                f"locked holdout-season event {event_id!r} ({season}) cannot enter training"
            )
    if train_seasons is None:
        return
    if locked_seasons.intersection(int(year) for year in train_seasons):
        raise HoldoutTrainError("locked holdout-season labels cannot enter training")


def assert_holdout_access(
    *,
    role: FoldRole,
    sealed_holdout: bool,
) -> None:
    if role is FoldRole.DEVELOPMENT:
        return
    if role is FoldRole.VALIDATION:
        return
    if role is FoldRole.HOLDOUT:
        if not sealed_holdout:
            raise HoldoutLockedError(
                "2025 holdout is locked; pass --sealed-holdout to score after freeze"
            )
        return
    never_role: Never = role
    raise BacktestGateError(f"unhandled fold role: {never_role!r}")


def assert_evaluator_hashes(
    *,
    contract_hash: str,
    feature_spec_hash: str,
    data_hash: str,
    config_hash: str,
    expected_data_hash: str | None,
    expected_config_hash: str,
    expected_contract_hash: str = PINNED_CONTRACT_HASH,
    expected_feature_spec_hash: str | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "contract_hash": contract_hash,
        "feature_spec_hash": feature_spec_hash,
        "data_hash": data_hash,
        "config_hash": config_hash,
        "expected_data_hash": expected_data_hash,
        "expected_config_hash": expected_config_hash,
        "expected_contract_hash": expected_contract_hash,
    }
    if expected_feature_spec_hash is not None:
        kwargs["expected_feature_spec_hash"] = expected_feature_spec_hash
    verify_evaluator_hashes(**kwargs)


def assert_contract_frozen(contract: EvaluationContract) -> None:
    if contract.content_hash != PINNED_CONTRACT_HASH:
        raise EvaluatorHashMismatchError(
            HashKind.CONTRACT,
            got=contract.content_hash,
            expected=PINNED_CONTRACT_HASH,
        )


def assert_priced_row_has_metrics(row: Mapping[str, Any]) -> None:
    """Priced rows may carry betting fields; threshold-only rows must not."""
    scope = str(row.get("scope") or "")
    if scope == PRICED_SCOPE:
        return
    if scope != THRESHOLD_SCOPE:
        return
    present = sorted(field for field in SYNTHETIC_BETTING_FIELDS if row.get(field) is not None)
    if present:
        raise PricedScopeError(
            "threshold-only rows must not receive synthetic betting metrics: "
            + ", ".join(present)
        )


def assert_threshold_only_clean(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        assert_priced_row_has_metrics(row)


def assert_readonly_database_url(
    db_url: str,
    *,
    default_url: str | None = None,
) -> str:
    """Reject live/default DBs and non-SQLite URLs. Callers must open read-only."""
    raw = str(db_url or "").strip()
    if not raw:
        raise DatabaseMutationError("empty --database-url")
    lowered = raw.lower()
    if "mode=rw" in lowered or "mode=rwc" in lowered:
        raise DatabaseMutationError("refusing read-write SQLite URI for backtest")
    if is_prohibited_live_url(raw, default_url=default_url):
        raise DatabaseMutationError(
            "refusing default live data/mma.db; pass an explicit disposable "
            "--database-url or omit it for the frozen manifest"
        )
    try:
        sqlite_path_from_url(raw)
    except CoverageDatabaseError as exc:
        raise DatabaseMutationError(str(exc)) from exc
    return raw


def assert_content_hash_matches(payload: Mapping[str, Any], expected: str) -> None:
    got = str(payload.get("content_hash") or "")
    if got != expected:
        raise EvidenceTamperError(f"content hash mismatch: got {got}, expected {expected}")


__all__ = [
    "HOLDOUT_YEAR",
    "PRICED_SCOPE",
    "THRESHOLD_SCOPE",
    "BacktestGateError",
    "DatabaseMutationError",
    "EvidenceOverwriteError",
    "EvidenceTamperError",
    "GateKind",
    "HoldoutTrainError",
    "PricedScopeError",
    "SYNTHETIC_BETTING_FIELDS",
    "assert_content_hash_matches",
    "assert_contract_frozen",
    "assert_cutoff_before_results",
    "assert_evaluator_hashes",
    "assert_holdout_access",
    "assert_holdout_not_in_train",
    "assert_priced_row_has_metrics",
    "assert_readonly_database_url",
    "assert_same_card_not_in_train",
    "assert_threshold_only_clean",
]
