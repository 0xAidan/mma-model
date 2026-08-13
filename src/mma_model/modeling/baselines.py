"""M0/M1 baselines trained through event-grouped folds (DWCS-303).

M0: 50/50, sequential Glicko-lite rating, and optional no-vig moneyline.
M1: standardized ridge logistic with fighter-swap augmentation.

Ordinary train uses DWCS-302 ``tuning_folds`` / ``validation_folds`` only.
Final refit may use development+validation labels and never 2025 holdout.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Never

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy.orm import Session

from mma_model.domain.markets import MarketFamily
from mma_model.dwcs.classification import SeriesVariant
from mma_model.evaluation.contract import EvaluationContract
from mma_model.features.as_of import cutoff_for_event, implied_event_start
from mma_model.features.builder import FeatureBuilder
from mma_model.features.snapshot import (
    FeatureSnapshot,
    SnapshotBout,
    SnapshotEvent,
    SnapshotResultVersion,
    snapshot_from_session,
    to_label_version,
)
from mma_model.features.spec import FEATURE_NAMES, SPEC_VERSION, spec_hash, swap_values
from mma_model.labels.outcomes import WinnerSide, training_label
from mma_model.modeling.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    LoadedArtifact,
    RidgeModelSpec,
    RidgePredictor,
    SavedArtifact,
    compute_code_hash,
    load_ridge_spec,
    resolve_code_commit,
    save_artifact,
)
from mma_model.modeling.splits import (
    FoldPlan,
    FoldRole,
    HoldoutLockedError,
    SplitCard,
    SplitError,
    cards_from_session,
    group_cards,
    protocol_fixture_cards,
    tuning_folds,
    validation_folds,
)
from mma_model.value.devig import IncompleteMarketSet, try_proportional_devig

SWAP_ATOL: Final = 1e-8
COIN_FLIP_PROB: Final = 0.5
RATING_LOGISTIC_SCALE: Final = 400.0
RATING_DIFF_INDEX: Final = FEATURE_NAMES.index("rating_diff")
RATING_SD_SUM_INDEX: Final = FEATURE_NAMES.index("rating_sd_sum")
LABEL_LAG: Final = timedelta(hours=6)
FORBIDDEN_HOLDOUT_METRIC_FRAGMENTS: Final = ("_holdout", "holdout_")
ORDINARY_TRAIN_ROLES: Final = frozenset({FoldRole.DEVELOPMENT, FoldRole.VALIDATION})
SESSION_NO_VIG_NOTE: Final = (
    "session/db train does not join timestamped pre-cutoff quotes; "
    "the no-vig market baseline stays explicit missing until odds plumbing exists"
)


class TrainError(ValueError):
    """Ordinary baseline training cannot proceed."""


class MissingNoVig:
    """Explicit missing market probability. Never a fabricated 0.5."""

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __repr__(self) -> str:
        return f"MissingNoVig({self.reason!r})"


@dataclass(frozen=True)
class PreCutoffMoneyline:
    """Timestamped complete-or-partial moneyline observed before a cutoff."""

    decimal_odds: Mapping[str, float]
    observed_at: datetime


@dataclass(frozen=True)
class LabeledSample:
    sample_id: str
    event_id: str
    fighter_a_id: str
    fighter_b_id: str
    cutoff: datetime
    values: tuple[float, ...]
    names: tuple[str, ...]
    binary_winner: WinnerSide
    moneyline: PreCutoffMoneyline | None = None


@dataclass(frozen=True)
class FittedRidge:
    predictor: RidgePredictor

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self.predictor.feature_names

    @property
    def spec_hash(self) -> str:
        return self.predictor.spec_hash

    @property
    def spec_version(self) -> str:
        return self.predictor.spec_version


@dataclass(frozen=True)
class TrainReport:
    model_id: str
    artifact: SavedArtifact
    metrics: dict[str, Any]
    train_sample_ids: tuple[str, ...]
    max_train_timestamp: datetime | None
    contract_hash: str
    feature_spec_hash: str
    config_hash: str
    data_hash: str
    code_hash: str
    code_commit: str
    code_commit_reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "artifact_path": str(self.artifact.payload_path.resolve()),
            "code_commit": self.code_commit,
            "code_commit_reason": self.code_commit_reason,
            "code_hash": self.code_hash,
            "config_hash": self.config_hash,
            "contract_hash": self.contract_hash,
            "data_hash": self.data_hash,
            "feature_spec_hash": self.feature_spec_hash,
            "manifest_path": str(self.artifact.manifest_path.resolve()),
            "max_train_timestamp": (
                self.max_train_timestamp.isoformat()
                if self.max_train_timestamp is not None
                else None
            ),
            "metrics": self.metrics,
            "model_id": self.model_id,
            "payload_sha256": self.artifact.payload_sha256,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "train_sample_ids": list(self.train_sample_ids),
        }
        _assert_no_holdout_betting_keys(payload)
        return payload


def _assert_no_holdout_betting_keys(payload: Mapping[str, Any]) -> None:
    stack: list[object] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                lowered = str(key).lower()
                if any(fragment in lowered for fragment in FORBIDDEN_HOLDOUT_METRIC_FRAGMENTS):
                    raise TrainError(
                        f"refusing holdout betting-evidence key {key!r} in ordinary train report"
                    )
                stack.append(value)
            continue
        if isinstance(current, list):
            stack.extend(current)


def coin_flip_win_prob(_values: Sequence[float] | None = None) -> float:
    """M0 50/50. Always 0.5, swap-safe by construction."""
    return COIN_FLIP_PROB


def _standard_normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _logistic(x: float) -> float:
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def sequential_rating_win_prob(values: Sequence[float]) -> float:
    """P(A wins) from pre-card rating_diff. Φ(diff/sd) or logistic(diff/400)."""
    if len(values) != len(FEATURE_NAMES):
        raise TrainError("sequential-rating vector length does not match FEATURE_NAMES")
    diff = float(values[RATING_DIFF_INDEX])
    sd_sum = float(values[RATING_SD_SUM_INDEX])
    if sd_sum > 0.0:
        return _standard_normal_cdf(diff / sd_sum)
    return _logistic(diff / RATING_LOGISTIC_SCALE)


def no_vig_win_prob(
    moneyline: PreCutoffMoneyline | None,
    *,
    cutoff: datetime | None = None,
) -> float | MissingNoVig:
    """De-vig P(fighter_a) only from a timestamped complete pre-cutoff moneyline."""
    if moneyline is None:
        return MissingNoVig("no timestamped pre-cutoff complete moneyline")
    observed = moneyline.observed_at
    if observed.tzinfo is None:
        return MissingNoVig("moneyline observed_at must be timezone-aware")
    if cutoff is not None and observed > cutoff:
        return MissingNoVig("odds observed after prediction cutoff")
    result = try_proportional_devig(
        dict(moneyline.decimal_odds),
        family=MarketFamily.MONEYLINE,
    )
    if isinstance(result, IncompleteMarketSet):
        return MissingNoVig(f"incomplete moneyline: {result.reason}")
    mapping = result.as_mapping()
    if "fighter_a" not in mapping:
        return MissingNoVig("de-vig result missing fighter_a")
    return float(mapping["fighter_a"])


def _label_to_y(winner: WinnerSide) -> int:
    if winner is WinnerSide.A:
        return 1
    if winner is WinnerSide.B:
        return 0
    never_winner: Never = winner
    raise TrainError(f"unhandled binary winner: {never_winner!r}")


def _predictor_from_pipeline(pipeline: Pipeline) -> RidgePredictor:
    if "scaler" not in pipeline.named_steps or "clf" not in pipeline.named_steps:
        raise TrainError("fitted pipeline missing scaler or logistic step")
    scaler = pipeline.named_steps["scaler"]
    clf = pipeline.named_steps["clf"]
    n_features = len(FEATURE_NAMES)
    mean = tuple(float(x) for x in np.asarray(scaler.mean_, dtype=np.float64).ravel())
    scale = tuple(float(x) for x in np.asarray(scaler.scale_, dtype=np.float64).ravel())
    coef = tuple(float(x) for x in np.asarray(clf.coef_, dtype=np.float64).ravel())
    intercept = float(np.asarray(clf.intercept_, dtype=np.float64).ravel()[0])
    classes = tuple(int(x) for x in np.asarray(clf.classes_).ravel())
    if len(mean) != n_features or len(scale) != n_features or len(coef) != n_features:
        raise TrainError("fitted ridge shape does not match FEATURE_NAMES")
    if classes != (0, 1):
        raise TrainError(f"fitted ridge classes must be (0, 1), got {classes!r}")
    return RidgePredictor(
        feature_names=FEATURE_NAMES,
        scaler_mean=mean,
        scaler_scale=scale,
        coef=coef,
        intercept=intercept,
        classes=classes,
        spec_hash=spec_hash(),
        spec_version=SPEC_VERSION,
    )


def fit_ridge(
    samples: Sequence[LabeledSample],
    spec: RidgeModelSpec,
) -> FittedRidge:
    if not samples:
        raise TrainError("ridge fit needs at least one labeled bout")
    if spec.ordinary_allow_holdout:
        raise HoldoutLockedError("ridge spec must not enable ordinary holdout")
    rows: list[list[float]] = []
    labels: list[int] = []
    for sample in samples:
        if sample.names != FEATURE_NAMES:
            raise TrainError("sample feature order does not match FEATURE_NAMES")
        y = _label_to_y(sample.binary_winner)
        rows.append(list(sample.values))
        labels.append(y)
        if spec.swap_augment:
            rows.append(list(swap_values(sample.values)))
            labels.append(1 - y)
    x = np.asarray(rows, dtype=np.float64)
    y_arr = np.asarray(labels, dtype=np.int64)
    steps: list[tuple[str, object]] = []
    if spec.standardize:
        steps.append(("scaler", StandardScaler()))
    steps.append(
        (
            "clf",
            LogisticRegression(
                penalty=spec.penalty,
                C=spec.C,
                solver=spec.solver,
                max_iter=spec.max_iter,
                random_state=0,
            ),
        )
    )
    pipeline = Pipeline(steps)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=FutureWarning,
            message=r".*penalty.*",
        )
        pipeline.fit(x, y_arr)
    return FittedRidge(predictor=_predictor_from_pipeline(pipeline))


def predict_ridge_raw(model: FittedRidge, values: Sequence[float]) -> float:
    """Direct scaler+logistic P(A). Used to test fitted-model complementarity."""
    if tuple(model.feature_names) != FEATURE_NAMES:
        raise TrainError("fitted ridge feature order does not match FEATURE_NAMES")
    if len(values) != len(FEATURE_NAMES):
        raise TrainError("prediction vector length does not match FEATURE_NAMES")
    return model.predictor.raw_win_prob(values)


def predict_ridge_win_prob(model: FittedRidge, values: Sequence[float]) -> float:
    if tuple(model.feature_names) != FEATURE_NAMES:
        raise TrainError("fitted ridge feature order does not match FEATURE_NAMES")
    if len(values) != len(FEATURE_NAMES):
        raise TrainError("prediction vector length does not match FEATURE_NAMES")
    return model.predictor.swap_safe_win_prob(values)


def predict_loaded_ridge_raw(loaded: LoadedArtifact, values: Sequence[float]) -> float:
    if len(values) != len(FEATURE_NAMES):
        raise TrainError("prediction vector length does not match FEATURE_NAMES")
    return loaded.predictor.raw_win_prob(values)


def predict_loaded_ridge(loaded: LoadedArtifact, values: Sequence[float]) -> float:
    if len(values) != len(FEATURE_NAMES):
        raise TrainError("prediction vector length does not match FEATURE_NAMES")
    return loaded.predictor.swap_safe_win_prob(values)


def _metric_block(y_true: Sequence[int], probs: Sequence[float]) -> dict[str, float | int]:
    y = np.asarray(list(y_true), dtype=np.int64)
    p = np.clip(np.asarray(list(probs), dtype=np.float64), 1e-15, 1.0 - 1e-15)
    hat = (p >= 0.5).astype(np.int64)
    return {
        "accuracy": float(accuracy_score(y, hat)),
        "brier": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "n": int(len(y)),
    }


def _empty_metric_block() -> dict[str, float | int]:
    return {"accuracy": 0.0, "brier": 0.0, "log_loss": 0.0, "n": 0}


def labeled_samples_from_snapshot(
    snapshot: FeatureSnapshot,
    cards: Sequence[SplitCard],
    *,
    odds_by_bout: Mapping[str, PreCutoffMoneyline] | None = None,
    allowed_roles: frozenset[FoldRole] | None = None,
    allow_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> tuple[LabeledSample, ...]:
    """Build PIT features at the card cutoff; labels from post-card training_label.

    Ordinary callers omit holdout. Locked 2025 cards are dropped before
    ``training_label`` or ``FeatureBuilder.build`` runs.
    """
    roles = allowed_roles if allowed_roles is not None else ORDINARY_TRAIN_ROLES
    if FoldRole.HOLDOUT in roles and not allow_holdout:
        raise HoldoutLockedError(
            "2025 holdout is locked; ordinary labeling must not read holdout cards"
        )
    groups = {group.event_id: group for group in group_cards(cards, contract)}
    eligible = [
        card
        for card in cards
        if card.event_id in groups and groups[card.event_id].role in roles
    ]
    builder = FeatureBuilder(snapshot)
    quotes = odds_by_bout or {}
    samples: list[LabeledSample] = []
    for card in eligible:
        cutoff = cutoff_for_event(card)
        label_at = implied_event_start(cutoff) + LABEL_LAG
        for bout_id in card.bout_ids:
            bout = snapshot.bout_by_id(bout_id)
            if bout is None:
                continue
            versions = [
                to_label_version(row)
                for row in snapshot.result_versions
                if row.bout_id == bout_id
            ]
            label = training_label(versions, label_at)
            if label.binary_winner is None:
                continue
            row = builder.build(
                bout.fighter_a_id,
                bout.fighter_b_id,
                cutoff,
                bout_id=bout_id,
            )
            samples.append(
                LabeledSample(
                    sample_id=bout_id,
                    event_id=card.event_id,
                    fighter_a_id=bout.fighter_a_id,
                    fighter_b_id=bout.fighter_b_id,
                    cutoff=cutoff.cutoff,
                    values=row.values,
                    names=row.names,
                    binary_winner=label.binary_winner,
                    moneyline=quotes.get(bout_id),
                )
            )
    return tuple(samples)


def _add_event(
    snapshot: FeatureSnapshot,
    event_id: str,
    start: datetime,
    *,
    series: str,
) -> None:
    snapshot.events.append(
        SnapshotEvent(
            event_id=event_id,
            scheduled_start_at=start,
            event_date=start.date(),
            series=series,
            name=event_id,
        )
    )


def _add_bout(
    snapshot: FeatureSnapshot,
    bout_id: str,
    event_id: str,
    fighter_a_id: str,
    fighter_b_id: str,
) -> SnapshotBout:
    bout = SnapshotBout(
        bout_id=bout_id,
        event_id=event_id,
        fighter_a_id=fighter_a_id,
        fighter_b_id=fighter_b_id,
        scheduled_rounds=3,
        weight_class="lightweight",
        status="completed",
    )
    snapshot.bouts.append(bout)
    return bout


def _add_result(
    snapshot: FeatureSnapshot,
    bout: SnapshotBout,
    *,
    winner_id: str | None,
    method: str,
    result_type: str,
    at: datetime,
) -> None:
    snapshot.result_versions.append(
        SnapshotResultVersion(
            bout_id=bout.bout_id,
            version_kind="event_night",
            revision=1,
            fighter_a_id=bout.fighter_a_id,
            fighter_b_id=bout.fighter_b_id,
            winner_fighter_id=winner_id,
            result_type=result_type,
            method=method,
            ending_round=3 if result_type != "draw" else 3,
            time_str="5:00",
            effective_at=at,
            observed_at=at,
        )
    )


def protocol_training_universe() -> tuple[
    tuple[SplitCard, ...],
    FeatureSnapshot,
    dict[str, PreCutoffMoneyline],
]:
    """Tiny DWCS-302 chronology with PIT ratings, one draw, and sparse odds."""
    cards = protocol_fixture_cards()
    snapshot = FeatureSnapshot()
    starts = {
        card.event_id: card.scheduled_start_at
        for card in cards
        if card.scheduled_start_at is not None
    }
    _add_event(
        snapshot,
        "prior-2016",
        datetime(2016, 6, 1, 19, 0, tzinfo=UTC),
        series="dwcs",
    )
    prior_a = _add_bout(snapshot, "prior-a", "prior-2016", "v1", "fod1")
    prior_b = _add_bout(snapshot, "prior-b", "prior-2016", "v2", "fod2")
    _add_result(
        snapshot,
        prior_a,
        winner_id="v1",
        method="KO/TKO",
        result_type="decisive",
        at=datetime(2016, 6, 1, 19, 0, tzinfo=UTC),
    )
    _add_result(
        snapshot,
        prior_b,
        winner_id="v2",
        method="SUB",
        result_type="decisive",
        at=datetime(2016, 6, 1, 19, 0, tzinfo=UTC),
    )

    pairings: dict[str, tuple[str, str]] = {
        "2017-a": ("v1", "n1"),
        "2017-b": ("v2", "n2"),
        "br-a": ("v1", "v2"),
        "br-b": ("n1", "n2"),
        "2023-a": ("v1", "n2"),
        "2024-a": ("v2", "n1"),
        "2025-a": ("v1", "v2"),
    }
    winners: dict[str, str | None] = {
        "2017-a": "v1",
        "2017-b": None,
        "br-a": "v1",
        "br-b": "n1",
        "2023-a": "v1",
        "2024-a": "n1",
        "2025-a": "v2",
    }
    methods: dict[str, tuple[str, str]] = {
        "2017-a": ("U-DEC", "decisive"),
        "2017-b": ("DRAW", "draw"),
        "br-a": ("KO/TKO", "decisive"),
        "br-b": ("SUB", "decisive"),
        "2023-a": ("U-DEC", "decisive"),
        "2024-a": ("S-DEC", "decisive"),
        "2025-a": ("U-DEC", "decisive"),
    }
    for card in cards:
        series = "dwcs_brazil" if card.series_variant is SeriesVariant.BRAZIL else "dwcs"
        start = starts[card.event_id]
        _add_event(snapshot, card.event_id, start, series=series)
        for bout_id in card.bout_ids:
            a_id, b_id = pairings[bout_id]
            bout = _add_bout(snapshot, bout_id, card.event_id, a_id, b_id)
            method, result_type = methods[bout_id]
            _add_result(
                snapshot,
                bout,
                winner_id=winners[bout_id],
                method=method,
                result_type=result_type,
                at=start,
            )

    odds = {
        "2024-a": PreCutoffMoneyline(
            decimal_odds={"fighter_a": 1.80, "fighter_b": 2.10},
            observed_at=datetime(2024, 8, 13, 0, 30, tzinfo=UTC),
        ),
        "2023-a": PreCutoffMoneyline(
            decimal_odds={"fighter_a": 1.90},
            observed_at=datetime(2023, 8, 22, 0, 30, tzinfo=UTC),
        ),
    }
    return cards, snapshot, odds


def _samples_for_ids(
    samples: Sequence[LabeledSample],
    bout_ids: Sequence[str],
) -> tuple[LabeledSample, ...]:
    wanted = set(bout_ids)
    return tuple(sample for sample in samples if sample.sample_id in wanted)


def _samples_for_events(
    samples: Sequence[LabeledSample],
    event_ids: Sequence[str],
) -> tuple[LabeledSample, ...]:
    wanted = set(event_ids)
    return tuple(sample for sample in samples if sample.event_id in wanted)


def _score_model(model: FittedRidge, samples: Sequence[LabeledSample]) -> dict[str, float | int]:
    if not samples:
        return _empty_metric_block()
    y = [_label_to_y(sample.binary_winner) for sample in samples]
    p = [predict_ridge_win_prob(model, sample.values) for sample in samples]
    return _metric_block(y, p)


def _score_baseline(
    name: str,
    samples: Sequence[LabeledSample],
) -> dict[str, Any]:
    if name == "coin_flip":
        y = [_label_to_y(sample.binary_winner) for sample in samples]
        p = [coin_flip_win_prob(sample.values) for sample in samples]
        if not samples:
            return _empty_metric_block()
        return _metric_block(y, p)
    if name == "sequential_rating":
        y = [_label_to_y(sample.binary_winner) for sample in samples]
        p = [sequential_rating_win_prob(sample.values) for sample in samples]
        if not samples:
            return _empty_metric_block()
        return _metric_block(y, p)
    if name == "no_vig_market":
        y_m: list[int] = []
        p_m: list[float] = []
        n_missing = 0
        for sample in samples:
            got = no_vig_win_prob(sample.moneyline, cutoff=sample.cutoff)
            if isinstance(got, MissingNoVig):
                n_missing += 1
                continue
            y_m.append(_label_to_y(sample.binary_winner))
            p_m.append(got)
        block: dict[str, Any] = {
            "n_missing": n_missing,
            "n_scored": len(p_m),
        }
        if p_m:
            block.update(_metric_block(y_m, p_m))
        else:
            block["accuracy"] = None
            block["brier"] = None
            block["log_loss"] = None
            block["n"] = 0
        return block
    raise TrainError(f"unhandled baseline name: {name!r}")


def _oof_pairs(
    plan: FoldPlan,
    samples: Sequence[LabeledSample],
    spec: RidgeModelSpec,
) -> tuple[list[int], list[float], list[LabeledSample]]:
    y_all: list[int] = []
    p_all: list[float] = []
    scored: list[LabeledSample] = []
    for fold in plan.folds:
        train = _samples_for_events(samples, fold.train_event_ids)
        test = _samples_for_ids(samples, fold.test_bout_ids)
        if not train or not test:
            continue
        model = fit_ridge(train, spec)
        for sample in test:
            y_all.append(_label_to_y(sample.binary_winner))
            p_all.append(predict_ridge_win_prob(model, sample.values))
            scored.append(sample)
    return y_all, p_all, scored


def _final_refit_samples(
    samples: Sequence[LabeledSample],
    cards: Sequence[SplitCard],
    contract: EvaluationContract | None = None,
) -> tuple[LabeledSample, ...]:
    groups = {group.event_id: group for group in group_cards(cards, contract)}
    eligible: list[LabeledSample] = []
    for sample in samples:
        group = groups.get(sample.event_id)
        if group is None:
            continue
        if group.role is FoldRole.DEVELOPMENT:
            eligible.append(sample)
            continue
        if group.role is FoldRole.VALIDATION:
            eligible.append(sample)
            continue
        if group.role is FoldRole.HOLDOUT:
            continue
        never_role: Never = group.role
        raise SplitError(f"unhandled fold role: {never_role!r}")
    if not eligible:
        raise TrainError("no development/validation labeled bouts for final refit")
    return tuple(eligible)


def _max_timestamp(samples: Sequence[LabeledSample]) -> datetime | None:
    if not samples:
        return None
    return max(sample.cutoff for sample in samples)


def _fold_metrics(
    *,
    spec: RidgeModelSpec,
    samples: Sequence[LabeledSample],
    inner: FoldPlan,
    outer: FoldPlan,
) -> dict[str, Any]:
    tune_y, tune_p, tune_rows = _oof_pairs(inner, samples, spec)
    val_y, val_p, val_rows = _oof_pairs(outer, samples, spec)
    scored = tuple(tune_rows + val_rows)
    metrics: dict[str, Any] = {
        "tuning": _metric_block(tune_y, tune_p) if tune_y else _empty_metric_block(),
        "validation": _metric_block(val_y, val_p) if val_y else _empty_metric_block(),
        "baselines": {
            "coin_flip": _score_baseline("coin_flip", scored),
            "no_vig_market": _score_baseline("no_vig_market", scored),
            "sequential_rating": _score_baseline("sequential_rating", scored),
        },
        "n_labeled": len(samples),
        "n_oof": len(scored),
    }
    _assert_no_holdout_betting_keys(metrics)
    return metrics


def train_ridge_m1(
    *,
    cards: Sequence[SplitCard],
    samples: Sequence[LabeledSample],
    spec: RidgeModelSpec,
    output_path: Path,
    require_target_cards: bool = False,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    """Fit M1 through DWCS-302 folds and refit on development+validation only."""
    if include_holdout or spec.ordinary_allow_holdout:
        raise HoldoutLockedError(
            "2025 holdout is locked; ordinary train must not enable sealed holdout"
        )
    inner = tuning_folds(
        cards, require_target_cards=require_target_cards, contract=contract
    )
    outer = validation_folds(
        cards, require_target_cards=require_target_cards, contract=contract
    )
    metrics = _fold_metrics(spec=spec, samples=samples, inner=inner, outer=outer)
    final_rows = _final_refit_samples(samples, cards, contract)
    model = fit_ridge(final_rows, spec)
    train_ids = tuple(sample.sample_id for sample in final_rows)
    max_ts = _max_timestamp(final_rows)
    code_hash = compute_code_hash(extra_paths=[Path(__file__)])
    code_commit, code_commit_reason = resolve_code_commit()
    saved = save_artifact(
        model.predictor,
        output_path,
        train_sample_ids=train_ids,
        max_train_timestamp=max_ts,
        cutoff_policy=spec.cutoff_policy,
        metrics=metrics,
        contract_hash=outer.contract_hash,
        config_hash=spec.content_hash,
        splits_config_hash=outer.config_hash,
        data_hash=outer.data_hash,
        code_hash=code_hash,
        code_commit=code_commit,
        code_commit_reason=code_commit_reason,
        model_id=spec.model_id,
        spec_id=spec.spec_id,
    )
    return TrainReport(
        model_id=spec.model_id,
        artifact=saved,
        metrics=metrics,
        train_sample_ids=train_ids,
        max_train_timestamp=max_ts,
        contract_hash=outer.contract_hash,
        feature_spec_hash=outer.feature_spec_hash,
        config_hash=spec.content_hash,
        data_hash=outer.data_hash,
        code_hash=code_hash,
        code_commit=code_commit,
        code_commit_reason=code_commit_reason,
    )


def train_from_snapshot(
    snapshot: FeatureSnapshot,
    cards: Sequence[SplitCard],
    *,
    spec: RidgeModelSpec,
    output_path: Path,
    odds_by_bout: Mapping[str, PreCutoffMoneyline] | None = None,
    require_target_cards: bool = False,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    if include_holdout:
        raise HoldoutLockedError(
            "2025 holdout is locked; ordinary train must not enable sealed holdout"
        )
    samples = labeled_samples_from_snapshot(
        snapshot,
        cards,
        odds_by_bout=odds_by_bout,
        allowed_roles=ORDINARY_TRAIN_ROLES,
        allow_holdout=False,
        contract=contract,
    )
    if not samples:
        raise TrainError("no binary-labeled bouts (draws/NC are excluded from the fit)")
    return train_ridge_m1(
        cards=cards,
        samples=samples,
        spec=spec,
        output_path=output_path,
        require_target_cards=require_target_cards,
        include_holdout=False,
        contract=contract,
    )


def train_from_session(
    session: Session,
    *,
    spec: RidgeModelSpec,
    output_path: Path,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    """Train from a canonical DB snapshot.

    Timestamped odds quotes are not joined here, so the no-vig market baseline
    stays explicit missing rather than a fabricated 0.5.
    """
    cards = cards_from_session(session)
    snapshot = snapshot_from_session(session)
    report = train_from_snapshot(
        snapshot,
        cards,
        spec=spec,
        output_path=output_path,
        require_target_cards=True,
        include_holdout=include_holdout,
        contract=contract,
    )
    report.metrics["no_vig_note"] = SESSION_NO_VIG_NOTE
    return report


def run_protocol_train(
    *,
    spec: RidgeModelSpec | None = None,
    output_path: Path,
    include_holdout: bool = False,
    contract: EvaluationContract | None = None,
) -> TrainReport:
    resolved = spec if spec is not None else load_ridge_spec()
    cards, snapshot, odds = protocol_training_universe()
    return train_from_snapshot(
        snapshot,
        cards,
        spec=resolved,
        output_path=output_path,
        odds_by_bout=odds,
        require_target_cards=False,
        include_holdout=include_holdout,
        contract=contract,
    )


def protocol_feature_vector(bout_id: str) -> tuple[tuple[str, ...], tuple[float, ...]]:
    """PIT feature row for a protocol-fixture bout (no holdout labels)."""
    cards, snapshot, _odds = protocol_training_universe()
    card = next((item for item in cards if bout_id in item.bout_ids), None)
    if card is None:
        raise TrainError(f"unknown protocol bout_id {bout_id!r}")
    groups = {group.event_id: group for group in group_cards(cards)}
    role = groups[card.event_id].role
    if role is FoldRole.HOLDOUT:
        raise HoldoutLockedError("refusing protocol feature vector for locked 2025 holdout")
    cutoff = cutoff_for_event(card)
    bout = snapshot.bout_by_id(bout_id)
    if bout is None:
        raise TrainError(f"protocol snapshot missing bout {bout_id!r}")
    row = FeatureBuilder(snapshot).build(
        bout.fighter_a_id,
        bout.fighter_b_id,
        cutoff,
        bout_id=bout_id,
    )
    return row.names, row.values


def predict_m0_bundle(
    sample: LabeledSample,
) -> dict[str, float | None | str]:
    """Score one bout with all M0 baselines. Missing no-vig stays explicit."""
    market = no_vig_win_prob(sample.moneyline, cutoff=sample.cutoff)
    if isinstance(market, MissingNoVig):
        market_p: float | None = None
        market_status = f"missing:{market.reason}"
    else:
        market_p = market
        market_status = "ok"
    return {
        "coin_flip": coin_flip_win_prob(sample.values),
        "no_vig": market_p,
        "no_vig_status": market_status,
        "sequential_rating": sequential_rating_win_prob(sample.values),
    }
