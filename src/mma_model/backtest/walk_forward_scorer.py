"""Card walk-forward scorers: joint M2 with calibrated M1 fallback (DWCS-306)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mma_model.backtest.engine import (
    BacktestError,
    BoutPrediction,
    CardScore,
    ExclusionReason,
    _event_is_holdout_season,
    _selection_id,
    markets_from_joint,
    moneyline_markets,
)
from mma_model.backtest.gates import (
    assert_cutoff_before_results,
    assert_holdout_not_in_train,
    assert_same_card_not_in_train,
)
from mma_model.backtest.metrics import (
    DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
    DEFAULT_BACKTEST_BOOTSTRAP_SEED,
)
from mma_model.dwcs.classification import SeriesVariant
from mma_model.evaluation.contract import EvaluationContract
from mma_model.features.as_of import cutoff_for_event
from mma_model.features.builder import FeatureBuilder
from mma_model.features.snapshot import FeatureSnapshot, SnapshotBout, to_label_version
from mma_model.labels.outcomes import WinnerSide, training_label
from mma_model.markets.derive import interval_count_for_schedule
from mma_model.modeling.artifacts import (
    ESTIMATOR_KIND as RIDGE_ESTIMATOR_KIND,
)
from mma_model.modeling.artifacts import (
    load_ridge_spec,
)
from mma_model.modeling.baselines import (
    LabeledSample,
    MissingNoVig,
    PreCutoffMoneyline,
    TrainError,
    coin_flip_win_prob,
    fit_ridge,
    no_vig_win_prob,
    predict_ridge_raw,
    predict_ridge_win_prob,
    protocol_training_universe,
    sequential_rating_win_prob,
)
from mma_model.modeling.calibration import (
    CalibrationError,
    SigmoidCalibrator,
    TemperatureCalibrator,
    fit_sigmoid_calibrator,
    fit_temperature_calibrator,
    load_oof_bundle,
)
from mma_model.modeling.joint import (
    ESTIMATOR_KIND as JOINT_ESTIMATOR_KIND,
)
from mma_model.modeling.joint import (
    EXPECTED_JOINT_MODEL_ID,
    JointBoutSample,
    JointPredictor,
    MissingJointClassError,
    _sample_from_bout,
    fit_joint_predictor,
    joint_protocol_training_universe,
    load_joint_spec,
    observed_fine_atom,
)
from mma_model.modeling.metrics import stable_logit
from mma_model.modeling.splits import (
    EventGroup,
    FoldKind,
    FoldMetadata,
    FoldRole,
    SplitCard,
)
from mma_model.modeling.uncertainty import BootstrapError, EventBlock, event_block_refit_bootstrap
from mma_model.quality.schema import sha256_canonical

PRODUCTION_P25_REPLICATES = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES


@dataclass
class _OofRecord:
    bout_id: str
    event_id: str
    season: int
    fold_id: str
    fold_kind: str
    test_cutoff: datetime
    train_event_ids: tuple[str, ...]
    train_max_timestamp: datetime | None
    estimator_hash: str
    estimator_kind: str
    model_id: str
    family: str
    raw_probability: float | None = None
    raw_logit: float | None = None
    hazard_logits: tuple[tuple[float, ...], ...] | None = None
    decision_logits: tuple[float, ...] | None = None
    scheduled_rounds: int | None = None


@dataclass
class SnapshotWalkForwardScorer:
    """Try M2 joint, else calibrated M1. Never silently emit uncalibrated evidence."""

    snapshot: FeatureSnapshot
    eval_event_ids: frozenset[str]
    contract: EvaluationContract
    bootstrap_replicates: int = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES
    bootstrap_seed: int = DEFAULT_BACKTEST_BOOTSTRAP_SEED
    last_fit_hashes: dict[str, str] = field(default_factory=dict)
    _oof: list[_OofRecord] = field(default_factory=list)
    _holdout_frozen: bool = False
    _frozen_ridge: SigmoidCalibrator | None = None
    _frozen_joint: TemperatureCalibrator | None = None
    _pending_joint_logits: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def score_card(self, group: EventGroup, fold: FoldMetadata) -> CardScore:
        assert_same_card_not_in_train(
            test_event_id=group.event_id, train_event_ids=fold.train_event_ids
        )
        assert_cutoff_before_results(
            max_train_timestamp=fold.max_train_timestamp,
            cutoff=group.cutoff.cutoff,
            event_id=group.event_id,
        )
        holdout_seasons = tuple(self.contract.splits.holdout.seasons)
        event_seasons, holdout_ids = self._event_season_index(holdout_seasons)
        assert_holdout_not_in_train(
            fold.train_event_ids,
            event_seasons=event_seasons,
            holdout_event_ids=holdout_ids,
            holdout_seasons=holdout_seasons,
        )
        train_m1 = self._train_samples(group)
        train_events = tuple(sorted({sample.event_id for sample in train_m1}))
        train_seasons = tuple(
            self._season_for_event(event_id) for event_id in train_events
        )
        assert_holdout_not_in_train(
            train_events,
            train_seasons=[year for year in train_seasons if year is not None],
            event_seasons=event_seasons,
            holdout_event_ids=holdout_ids,
            holdout_seasons=holdout_seasons,
        )
        if any(sample.event_id == group.event_id for sample in train_m1):
            raise BacktestError(f"same-card sample leaked into train for {group.event_id}")
        if not train_m1:
            return self._insufficient(group, fold, train_events)
        ridge_cal, joint_cal = self._calibrators_for_card(group)
        joint_samples = self._joint_samples(group)
        joint_model: JointPredictor | None = None
        if joint_samples:
            try:
                joint_model = fit_joint_predictor(joint_samples, load_joint_spec())
            except (TrainError, MissingJointClassError):
                joint_model = None
        if joint_model is not None and joint_cal is not None:
            score = self._score_joint(
                group=group,
                fold=fold,
                train_events=train_events,
                train_m1=train_m1,
                joint_train=joint_samples,
                model=joint_model,
                calibrator=joint_cal,
                ridge_cal=ridge_cal,
            )
        else:
            score = self._score_m1(
                group=group,
                fold=fold,
                train_events=train_events,
                train_samples=train_m1,
                calibrator=ridge_cal,
                fallback_reason=(
                    ExclusionReason.M1_MONEYLINE_FALLBACK.value
                    if joint_model is None
                    else ExclusionReason.UNCALIBRATED.value
                ),
            )
            if joint_model is not None:
                self._record_joint_raw_oof(group, fold, joint_model)
        if group.role is not FoldRole.HOLDOUT:
            self._append_oof(group, fold, score)
        return score

    def _insufficient(
        self,
        group: EventGroup,
        fold: FoldMetadata,
        train_events: tuple[str, ...],
    ) -> CardScore:
        return CardScore(
            event_id=group.event_id,
            estimator_hash="none",
            train_event_ids=train_events,
            max_train_timestamp=fold.max_train_timestamp,
            holdout_in_train=False,
            predictions=(),
            unavailable=tuple(
                (bout_id, ExclusionReason.INSUFFICIENT_TRAIN) for bout_id in group.bout_ids
            ),
        )

    def _calibrators_for_card(
        self, group: EventGroup
    ) -> tuple[SigmoidCalibrator | None, TemperatureCalibrator | None]:
        if group.role is FoldRole.HOLDOUT:
            if not self._holdout_frozen:
                self._frozen_ridge = self._fit_ridge_calibrator(group.cutoff.cutoff)
                self._frozen_joint = self._fit_joint_calibrator(group.cutoff.cutoff)
                self._holdout_frozen = True
            return self._frozen_ridge, self._frozen_joint
        return (
            self._fit_ridge_calibrator(group.cutoff.cutoff),
            self._fit_joint_calibrator(group.cutoff.cutoff),
        )

    def _event_season_index(
        self, holdout_seasons: Sequence[int]
    ) -> tuple[dict[str, int], tuple[str, ...]]:
        locked = {int(year) for year in holdout_seasons}
        seasons: dict[str, int] = {}
        holdout_ids: list[str] = []
        for event in self.snapshot.events:
            year = self._season_for_event(event.event_id)
            if year is None:
                continue
            seasons[event.event_id] = year
            if year in locked:
                holdout_ids.append(event.event_id)
        return seasons, tuple(holdout_ids)

    def _season_for_event(self, event_id: str) -> int | None:
        event = next((item for item in self.snapshot.events if item.event_id == event_id), None)
        if event is None or event.scheduled_start_at is None:
            return None
        start = event.scheduled_start_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        return start.year

    def _prior_cards(self, group: EventGroup) -> tuple[SplitCard, ...]:
        events = {event.event_id: event for event in self.snapshot.events}
        bouts_by_event: dict[str, list[SnapshotBout]] = {}
        for bout in self.snapshot.bouts:
            bouts_by_event.setdefault(bout.event_id, []).append(bout)
        cards: list[SplitCard] = []
        for event_id, bouts in bouts_by_event.items():
            event = events.get(event_id)
            if event is None or event.scheduled_start_at is None:
                continue
            start = event.scheduled_start_at
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            if event_id == group.event_id:
                continue
            if start >= group.cutoff.cutoff:
                continue
            if _event_is_holdout_season(start.year, self.contract):
                continue
            cards.append(
                SplitCard(
                    event_id=event_id,
                    scheduled_start_at=start,
                    event_date=event.event_date,
                    series_variant=SeriesVariant.STANDARD,
                    bout_ids=tuple(item.bout_id for item in bouts),
                )
            )
        return tuple(cards)

    def _train_samples(self, group: EventGroup) -> tuple[LabeledSample, ...]:
        builder = FeatureBuilder(self.snapshot)
        samples: list[LabeledSample] = []
        for card in self._prior_cards(group):
            cutoff = cutoff_for_event(card)
            for bout_id in card.bout_ids:
                bout = self.snapshot.bout_by_id(bout_id)
                if bout is None:
                    continue
                versions = [
                    to_label_version(row)
                    for row in self.snapshot.result_versions
                    if row.bout_id == bout.bout_id
                ]
                label = training_label(versions, group.cutoff.cutoff)
                if label.binary_winner is None:
                    continue
                row = builder.build(
                    bout.fighter_a_id,
                    bout.fighter_b_id,
                    cutoff,
                    bout_id=bout.bout_id,
                )
                samples.append(
                    LabeledSample(
                        sample_id=bout.bout_id,
                        event_id=card.event_id,
                        fighter_a_id=bout.fighter_a_id,
                        fighter_b_id=bout.fighter_b_id,
                        cutoff=cutoff.cutoff,
                        values=row.values,
                        names=row.names,
                        binary_winner=label.binary_winner,
                    )
                )
        return tuple(samples)

    def _joint_samples(self, group: EventGroup) -> tuple[JointBoutSample, ...]:
        builder = FeatureBuilder(self.snapshot)
        spec = load_joint_spec()
        samples: list[JointBoutSample] = []
        for card in self._prior_cards(group):
            cutoff = cutoff_for_event(card)
            for bout_id in card.bout_ids:
                bout = self.snapshot.bout_by_id(bout_id)
                if bout is None:
                    continue
                try:
                    sample = _sample_from_bout(
                        self.snapshot,
                        builder,
                        card,
                        bout,
                        cutoff=cutoff,
                        label_at=group.cutoff.cutoff,
                        spec=spec,
                    )
                except (TrainError, MissingJointClassError, ValueError):
                    sample = None
                if sample is not None:
                    samples.append(sample)
        return tuple(samples)

    def _oof_payloads(
        self, *, family: str, cutoff: datetime
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        payloads: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []
        for record in self._oof:
            if record.family != family:
                continue
            if _event_is_holdout_season(record.season, self.contract):
                continue
            versions = [
                to_label_version(row)
                for row in self.snapshot.result_versions
                if row.bout_id == record.bout_id
            ]
            label = training_label(versions, cutoff)
            train_ids = list(record.train_event_ids)
            base = {
                "bout_id": record.bout_id,
                "estimator_hash": record.estimator_hash,
                "estimator_kind": record.estimator_kind,
                "event_id": record.event_id,
                "fold_id": record.fold_id,
                "fold_kind": record.fold_kind,
                "model_id": record.model_id,
                "test_cutoff": record.test_cutoff.isoformat(),
                "train_event_ids": train_ids,
                "train_event_ids_hash": sha256_canonical({"train_event_ids": train_ids}),
                "train_max_timestamp": (
                    None
                    if record.train_max_timestamp is None
                    else record.train_max_timestamp.isoformat()
                ),
            }
            if family == "ridge":
                if label.binary_winner is WinnerSide.A:
                    y = 1
                elif label.binary_winner is WinnerSide.B:
                    y = 0
                else:
                    exclusions.append({"test_bout_ids": [record.bout_id], "n_test": 1})
                    continue
                payloads.append(
                    {
                        **base,
                        "raw_logit": record.raw_logit,
                        "raw_probability": record.raw_probability,
                        "y": y,
                    }
                )
                continue
            if label.terminal_atom is None:
                exclusions.append({"test_bout_ids": [record.bout_id], "n_test": 1})
                continue
            observed = label.terminal_atom.value
            bout = self.snapshot.bout_by_id(record.bout_id)
            event = next(
                (item for item in self.snapshot.events if item.event_id == record.event_id),
                None,
            )
            if bout is not None and event is not None and event.scheduled_start_at is not None:
                card = SplitCard(
                    event_id=record.event_id,
                    scheduled_start_at=event.scheduled_start_at,
                    event_date=event.event_date,
                    series_variant=SeriesVariant.STANDARD,
                    bout_ids=(record.bout_id,),
                )
                try:
                    rebuilt = _sample_from_bout(
                        self.snapshot,
                        FeatureBuilder(self.snapshot),
                        card,
                        bout,
                        cutoff=cutoff_for_event(card),
                        label_at=cutoff,
                        spec=load_joint_spec(),
                    )
                    if rebuilt is not None:
                        observed = observed_fine_atom(rebuilt)
                except (TrainError, MissingJointClassError, ValueError):
                    observed = label.terminal_atom.value
            if record.hazard_logits is None or record.decision_logits is None:
                exclusions.append({"test_bout_ids": [record.bout_id], "n_test": 1})
                continue
            payloads.append(
                {
                    **base,
                    "decision_logits": list(record.decision_logits),
                    "hazard_logits": [list(row) for row in record.hazard_logits],
                    "observed_fine_atom": observed,
                    "observed_frozen_atom": label.terminal_atom.value,
                    "scheduled_rounds": record.scheduled_rounds,
                }
            )
        return payloads, exclusions

    def _fit_ridge_calibrator(self, cutoff: datetime) -> SigmoidCalibrator | None:
        payloads, exclusions = self._oof_payloads(family="ridge", cutoff=cutoff)
        if not payloads:
            return None
        try:
            bundle = load_oof_bundle(
                payloads,
                exclusions,
                family="ridge",
                model_id="M1",
            )
            return fit_sigmoid_calibrator(bundle)
        except (CalibrationError, ValueError):
            return None

    def _fit_joint_calibrator(self, cutoff: datetime) -> TemperatureCalibrator | None:
        payloads, exclusions = self._oof_payloads(family="joint", cutoff=cutoff)
        if not payloads:
            return None
        try:
            bundle = load_oof_bundle(
                payloads,
                exclusions,
                family="joint",
                model_id=EXPECTED_JOINT_MODEL_ID,
            )
            return fit_temperature_calibrator(bundle)
        except (CalibrationError, ValueError):
            return None

    def _m1_blocks(self, samples: Sequence[LabeledSample]) -> tuple[EventBlock[LabeledSample], ...]:
        grouped: dict[str, list[LabeledSample]] = {}
        for sample in samples:
            grouped.setdefault(sample.event_id, []).append(sample)
        return tuple(
            EventBlock(event_id=event_id, samples=tuple(rows))
            for event_id, rows in sorted(grouped.items())
        )

    def _joint_blocks(
        self, samples: Sequence[JointBoutSample]
    ) -> tuple[EventBlock[JointBoutSample], ...]:
        grouped: dict[str, list[JointBoutSample]] = {}
        for sample in samples:
            grouped.setdefault(sample.event_id, []).append(sample)
        return tuple(
            EventBlock(event_id=event_id, samples=tuple(rows))
            for event_id, rows in sorted(grouped.items())
        )

    def _p25_m1(
        self,
        *,
        train: Sequence[LabeledSample],
        targets: Mapping[str, Sequence[float]],
        estimator_hash: str,
        calibrator: SigmoidCalibrator | None,
        fold: FoldMetadata,
    ) -> dict[str, tuple[float, float]]:
        if calibrator is None or not train or not targets:
            return {}
        spec = load_ridge_spec()
        blocks = self._m1_blocks(train)
        if not blocks:
            return {}

        def refit(bag: tuple[LabeledSample, ...]):
            return fit_ridge(bag, spec)

        def predict(fitted) -> dict[str, float]:
            out: dict[str, float] = {}
            for bout_id, values in targets.items():
                raw = float(predict_ridge_win_prob(fitted, values))
                out[bout_id] = float(calibrator.apply_probability(raw))
            return out

        try:
            summary = event_block_refit_bootstrap(
                blocks,
                refit=refit,
                predict=predict,
                n_replicates=self.bootstrap_replicates,
                seed=self.bootstrap_seed,
                estimator_hash=estimator_hash,
                config_hash=fold.config_hash,
                data_hash=fold.data_hash,
                contract_hash=fold.contract_hash,
            )
        except (BootstrapError, TrainError):
            return {}
        out: dict[str, tuple[float, float]] = {}
        for target in summary.targets:
            out[target.target_id] = (float(target.p25), float(target.p75))
        return out

    def _p25_joint(
        self,
        *,
        train: Sequence[JointBoutSample],
        targets: Sequence[tuple[str, Sequence[float], int]],
        estimator_hash: str,
        calibrator: TemperatureCalibrator,
        fold: FoldMetadata,
    ) -> dict[str, float]:
        if not train or not targets:
            return {}
        spec = load_joint_spec()
        blocks = self._joint_blocks(train)
        if not blocks:
            return {}

        def refit(bag: tuple[JointBoutSample, ...]) -> JointPredictor:
            return fit_joint_predictor(bag, spec)

        def predict(fitted: JointPredictor) -> dict[str, float]:
            out: dict[str, float] = {}
            for bout_id, values, rounds_n in targets:
                hazard = [
                    [float(x) for x in fitted.raw_hazard_logits(values, interval)]
                    for interval in range(interval_count_for_schedule(rounds_n))
                ]
                decision = [float(x) for x in fitted.raw_decision_logits(values)]
                atoms = calibrator.apply_logits(
                    hazard, decision, scheduled_rounds=rounds_n
                )
                for market in markets_from_joint(atoms, scheduled_rounds=rounds_n):
                    if not market.available or not market.outcome_key:
                        continue
                    key = (
                        f"{bout_id}|"
                        f"{_selection_id(market.family, market.outcome_key, market.line_point)}"
                    )
                    out[key] = float(market.p50)
            return out

        try:
            summary = event_block_refit_bootstrap(
                blocks,
                refit=refit,
                predict=predict,
                n_replicates=self.bootstrap_replicates,
                seed=self.bootstrap_seed,
                estimator_hash=estimator_hash,
                config_hash=fold.config_hash,
                data_hash=fold.data_hash,
                contract_hash=fold.contract_hash,
            )
        except (BootstrapError, TrainError, MissingJointClassError):
            return {}
        return {target.target_id: float(target.p25) for target in summary.targets}

    def _score_m1(
        self,
        *,
        group: EventGroup,
        fold: FoldMetadata,
        train_events: tuple[str, ...],
        train_samples: Sequence[LabeledSample],
        calibrator: SigmoidCalibrator | None,
        fallback_reason: str,
    ) -> CardScore:
        try:
            model = fit_ridge(train_samples, load_ridge_spec())
        except TrainError:
            return self._insufficient(group, fold, train_events)
        estimator_hash = model.predictor.identity_hash()
        self.last_fit_hashes[group.event_id] = estimator_hash
        calibrator_hash = (
            None
            if calibrator is None
            else sha256_canonical({"a": calibrator.a, "b": calibrator.b})
        )
        builder = FeatureBuilder(self.snapshot)
        target_values: dict[str, tuple[float, ...]] = {}
        built: dict[str, Any] = {}
        unavailable: list[tuple[str, ExclusionReason]] = []
        for bout_id in group.bout_ids:
            bout = self.snapshot.bout_by_id(bout_id)
            if bout is None:
                unavailable.append((bout_id, ExclusionReason.MISSING_FEATURES))
                continue
            row = builder.build(
                bout.fighter_a_id,
                bout.fighter_b_id,
                group.cutoff,
                bout_id=bout_id,
            )
            built[bout_id] = (bout, row)
            target_values[bout_id] = row.values
        p25_map = self._p25_m1(
            train=train_samples,
            targets=target_values,
            estimator_hash=estimator_hash,
            calibrator=calibrator,
            fold=fold,
        )
        predictions: list[BoutPrediction] = []
        for bout_id, (_bout, row) in built.items():
            raw_p = float(predict_ridge_raw(model, row.values))
            if calibrator is not None:
                p_a = float(calibrator.apply_probability(raw_p))
                uncalibrated = False
            else:
                p_a = float(predict_ridge_win_prob(model, row.values))
                uncalibrated = True
            p_a = min(max(p_a, 1e-15), 1.0 - 1e-15)
            p_b = 1.0 - p_a
            p25_p75 = p25_map.get(bout_id)
            p25 = None if p25_p75 is None else p25_p75[0]
            p75 = None if p25_p75 is None else p25_p75[1]
            p25_reason = None
            if uncalibrated:
                p25_reason = ExclusionReason.UNCALIBRATED.value
                p25 = None
                p75 = None
            elif p25 is None:
                p25_reason = ExclusionReason.MISSING_P25.value
            moneyline = self._quote_moneyline(bout_id, group.cutoff.cutoff)
            no_vig = no_vig_win_prob(moneyline, cutoff=group.cutoff.cutoff)
            no_vig_p = None if isinstance(no_vig, MissingNoVig) else float(no_vig)
            predictions.append(
                BoutPrediction(
                    bout_id=bout_id,
                    event_id=group.event_id,
                    model_id="M1",
                    p_fighter_a=p_a,
                    p_fighter_b=p_b,
                    p_draw=0.0,
                    p50=p_a,
                    p25=p25,
                    joint_atoms=None,
                    markets=moneyline_markets(
                        p_a=p_a,
                        p_b=p_b,
                        p_draw=0.0,
                        p25=p25,
                        p75=p75,
                        fallback_reason=fallback_reason,
                    ),
                    estimator_hash=estimator_hash,
                    calibrator_hash=calibrator_hash,
                    train_event_ids=train_events,
                    max_train_timestamp=fold.max_train_timestamp,
                    baseline_fifty=coin_flip_win_prob(row.values),
                    baseline_rating=sequential_rating_win_prob(row.values),
                    baseline_no_vig=no_vig_p,
                    baseline_m1=raw_p,
                    p25_unavailable_reason=p25_reason,
                )
            )
        return CardScore(
            event_id=group.event_id,
            estimator_hash=estimator_hash,
            train_event_ids=train_events,
            max_train_timestamp=fold.max_train_timestamp,
            holdout_in_train=False,
            predictions=tuple(predictions),
            unavailable=tuple(unavailable),
        )

    def _score_joint(
        self,
        *,
        group: EventGroup,
        fold: FoldMetadata,
        train_events: tuple[str, ...],
        train_m1: Sequence[LabeledSample],
        joint_train: Sequence[JointBoutSample],
        model: JointPredictor,
        calibrator: TemperatureCalibrator,
        ridge_cal: SigmoidCalibrator | None,
    ) -> CardScore:
        estimator_hash = model.identity_hash()
        self.last_fit_hashes[group.event_id] = estimator_hash
        calibrator_hash = sha256_canonical({"temperature": calibrator.temperature})
        builder = FeatureBuilder(self.snapshot)
        targets: list[tuple[str, tuple[float, ...], int]] = []
        built: dict[str, Any] = {}
        unavailable: list[tuple[str, ExclusionReason]] = []
        for bout_id in group.bout_ids:
            bout = self.snapshot.bout_by_id(bout_id)
            if bout is None:
                unavailable.append((bout_id, ExclusionReason.MISSING_FEATURES))
                continue
            row = builder.build(
                bout.fighter_a_id,
                bout.fighter_b_id,
                group.cutoff,
                bout_id=bout_id,
            )
            rounds_n = int(bout.scheduled_rounds or 3)
            built[bout_id] = (bout, row, rounds_n)
            targets.append((bout_id, row.values, rounds_n))
        p25_by_key = self._p25_joint(
            train=joint_train,
            targets=targets,
            estimator_hash=estimator_hash,
            calibrator=calibrator,
            fold=fold,
        )
        m1_raw: dict[str, float] = {}
        try:
            ridge = fit_ridge(train_m1, load_ridge_spec())
        except TrainError:
            ridge = None
        predictions: list[BoutPrediction] = []
        for bout_id, (_bout, row, rounds_n) in built.items():
            n_intervals = interval_count_for_schedule(rounds_n)
            hazard = [
                [float(x) for x in model.raw_hazard_logits(row.values, interval)]
                for interval in range(n_intervals)
            ]
            decision = [float(x) for x in model.raw_decision_logits(row.values)]
            self._pending_joint_logits[(group.event_id, bout_id)] = {
                "decision_logits": tuple(decision),
                "hazard_logits": tuple(tuple(item) for item in hazard),
            }
            atoms = calibrator.apply_logits(
                hazard, decision, scheduled_rounds=rounds_n
            )
            p25_map = {
                key.split("|", 1)[1]: value
                for key, value in p25_by_key.items()
                if key.startswith(f"{bout_id}|")
            }
            markets = markets_from_joint(
                atoms, scheduled_rounds=rounds_n, p25_by_selection=p25_map
            )
            p_a = float(sum(v for k, v in atoms.items() if k.startswith("a_")))
            p_b = float(sum(v for k, v in atoms.items() if k.startswith("b_")))
            p_draw = float(atoms.get("draw", 0.0))
            p25_a = p25_map.get(_selection_id("moneyline", "fighter_a", None))
            if ridge is not None:
                m1_raw[bout_id] = float(predict_ridge_raw(ridge, row.values))
            moneyline = self._quote_moneyline(bout_id, group.cutoff.cutoff)
            no_vig = no_vig_win_prob(moneyline, cutoff=group.cutoff.cutoff)
            no_vig_p = None if isinstance(no_vig, MissingNoVig) else float(no_vig)
            missing = any(
                market.available and market.outcome_key and market.p25 is None
                for market in markets
            )
            predictions.append(
                BoutPrediction(
                    bout_id=bout_id,
                    event_id=group.event_id,
                    model_id=EXPECTED_JOINT_MODEL_ID,
                    p_fighter_a=p_a,
                    p_fighter_b=p_b,
                    p_draw=p_draw,
                    p50=p_a,
                    p25=p25_a,
                    joint_atoms=atoms,
                    markets=markets,
                    estimator_hash=estimator_hash,
                    calibrator_hash=calibrator_hash,
                    train_event_ids=train_events,
                    max_train_timestamp=fold.max_train_timestamp,
                    baseline_fifty=coin_flip_win_prob(row.values),
                    baseline_rating=sequential_rating_win_prob(row.values),
                    baseline_no_vig=no_vig_p,
                    baseline_m1=m1_raw.get(bout_id),
                    p25_unavailable_reason=(
                        ExclusionReason.MISSING_P25.value if missing else None
                    ),
                )
            )
        return CardScore(
            event_id=group.event_id,
            estimator_hash=estimator_hash,
            train_event_ids=train_events,
            max_train_timestamp=fold.max_train_timestamp,
            holdout_in_train=False,
            predictions=tuple(predictions),
            unavailable=tuple(unavailable),
        )

    def _record_joint_raw_oof(
        self,
        group: EventGroup,
        fold: FoldMetadata,
        model: JointPredictor,
    ) -> None:
        """Keep joint logits for later temperature fit when the public card is M1."""
        if group.role is FoldRole.HOLDOUT:
            return
        builder = FeatureBuilder(self.snapshot)
        fold_kind = "outer" if fold.kind is FoldKind.OUTER else str(fold.kind.value)
        estimator_hash = model.identity_hash()
        for bout_id in group.bout_ids:
            bout = self.snapshot.bout_by_id(bout_id)
            if bout is None:
                continue
            row = builder.build(
                bout.fighter_a_id,
                bout.fighter_b_id,
                group.cutoff,
                bout_id=bout_id,
            )
            rounds_n = int(bout.scheduled_rounds or 3)
            n_intervals = interval_count_for_schedule(rounds_n)
            hazard = tuple(
                tuple(float(x) for x in model.raw_hazard_logits(row.values, interval))
                for interval in range(n_intervals)
            )
            decision = tuple(float(x) for x in model.raw_decision_logits(row.values))
            self._pending_joint_logits[(group.event_id, bout_id)] = {
                "decision_logits": decision,
                "hazard_logits": hazard,
            }
            self._oof.append(
                _OofRecord(
                    bout_id=bout_id,
                    event_id=group.event_id,
                    season=group.season,
                    fold_id=fold.fold_id,
                    fold_kind=fold_kind,
                    test_cutoff=group.cutoff.cutoff,
                    train_event_ids=tuple(fold.train_event_ids),
                    train_max_timestamp=fold.max_train_timestamp,
                    estimator_hash=estimator_hash,
                    estimator_kind=JOINT_ESTIMATOR_KIND,
                    model_id=EXPECTED_JOINT_MODEL_ID,
                    family="joint",
                    hazard_logits=hazard,
                    decision_logits=decision,
                    scheduled_rounds=rounds_n,
                )
            )

    def _append_oof(self, group: EventGroup, fold: FoldMetadata, score: CardScore) -> None:
        if group.role is FoldRole.HOLDOUT:
            return
        fold_kind = "outer" if fold.kind is FoldKind.OUTER else str(fold.kind.value)
        for prediction in score.predictions:
            bout = self.snapshot.bout_by_id(prediction.bout_id)
            if bout is None:
                continue
            if prediction.model_id == "M1":
                raw_p = float(prediction.baseline_m1 or prediction.p50)
                self._oof.append(
                    _OofRecord(
                        bout_id=prediction.bout_id,
                        event_id=group.event_id,
                        season=group.season,
                        fold_id=fold.fold_id,
                        fold_kind=fold_kind,
                        test_cutoff=group.cutoff.cutoff,
                        train_event_ids=prediction.train_event_ids,
                        train_max_timestamp=fold.max_train_timestamp,
                        estimator_hash=prediction.estimator_hash,
                        estimator_kind=RIDGE_ESTIMATOR_KIND,
                        model_id="M1",
                        family="ridge",
                        raw_probability=min(max(raw_p, 1e-15), 1.0 - 1e-15),
                        raw_logit=float(stable_logit(raw_p)),
                    )
                )
                continue
            logits = self._pending_joint_logits.get((group.event_id, prediction.bout_id))
            if not logits:
                continue
            self._oof.append(
                _OofRecord(
                    bout_id=prediction.bout_id,
                    event_id=group.event_id,
                    season=group.season,
                    fold_id=fold.fold_id,
                    fold_kind=fold_kind,
                    test_cutoff=group.cutoff.cutoff,
                    train_event_ids=prediction.train_event_ids,
                    train_max_timestamp=fold.max_train_timestamp,
                    estimator_hash=prediction.estimator_hash,
                    estimator_kind=JOINT_ESTIMATOR_KIND,
                    model_id=EXPECTED_JOINT_MODEL_ID,
                    family="joint",
                    hazard_logits=logits.get("hazard_logits"),
                    decision_logits=logits.get("decision_logits"),
                    scheduled_rounds=int(bout.scheduled_rounds or 3),
                )
            )

    def _quote_moneyline(self, bout_id: str, cutoff: datetime) -> PreCutoffMoneyline | None:
        return None


class ProtocolWalkForwardScorer(SnapshotWalkForwardScorer):
    """Protocol fixture: real FeatureBuilder + M1/M2 fit callbacks."""

    def __init__(
        self,
        contract: EvaluationContract,
        *,
        bootstrap_replicates: int = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
        bootstrap_seed: int = DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    ) -> None:
        cards, snapshot, odds = protocol_training_universe()
        super().__init__(
            snapshot=snapshot,
            eval_event_ids=frozenset(card.event_id for card in cards),
            contract=contract,
            bootstrap_replicates=bootstrap_replicates,
            bootstrap_seed=bootstrap_seed,
        )
        self._odds = odds

    def _quote_moneyline(self, bout_id: str, cutoff: datetime) -> PreCutoffMoneyline | None:
        quote = self._odds.get(bout_id)
        if quote is None:
            return None
        if quote.observed_at > cutoff:
            return None
        return quote


class JointProtocolWalkForwardScorer(SnapshotWalkForwardScorer):
    """Joint protocol fixture universe with coherent multi-market atoms."""

    def __init__(
        self,
        contract: EvaluationContract,
        *,
        bootstrap_replicates: int = DEFAULT_BACKTEST_BOOTSTRAP_REPLICATES,
        bootstrap_seed: int = DEFAULT_BACKTEST_BOOTSTRAP_SEED,
    ) -> None:
        cards, snapshot = joint_protocol_training_universe()
        super().__init__(
            snapshot=snapshot,
            eval_event_ids=frozenset(card.event_id for card in cards),
            contract=contract,
            bootstrap_replicates=bootstrap_replicates,
            bootstrap_seed=bootstrap_seed,
        )
