"""Append-only prediction / recommendation / grading ledger service (DWCS-400).

Jobs must call these helpers; they must never INSERT ledger rows ad hoc.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.db.tables.recommendations import (
    ModelRun,
    ObservedPrice,
    OfficialPublication,
    Prediction,
    PredictionGrade,
    PriceTarget,
    RecommendationSettlement,
    RecommendationStateEvent,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey, RecommendationState
from mma_model.markets.price_targets import decimal_to_american
from mma_model.markets.rules import SettlementRuleSet
from mma_model.markets.settlement import (
    BoutSettlementFacts,
    MarketSelection,
    SettlementDecision,
    SettlementResult,
    settle,
)
from mma_model.recommend.policy import QuoteSourceKind, RenderedThresholds
from mma_model.value.ev import flat_unit_profit, unsafe_same_line_probability_clv
from mma_model.value.odds import validate_decimal_odds

ResultVersionKind = Literal["event_night", "current"]
PerformanceLane = Literal["qualified", "paper", "experimental"]
PublicationKind = Literal["t60"]

SHA256_HEX_LEN: Final = 64
DEFAULT_SERIES: Final = "dwcs"
PUBLICATION_KIND_T60: Final = "t60"


class GradeLedgerError(Exception):
    """Base error for grading ledger operations."""


class GradeLedgerValidationError(GradeLedgerError, ValueError):
    """Invalid ledger input."""


class GradeLedgerMutationError(GradeLedgerError, RuntimeError):
    """Attempted destructive mutation of an append-only ledger row."""


class StateEventType(StrEnum):
    LINE_CHANGE = "line_change"
    REPLACEMENT_INVALIDATED = "replacement_invalidated"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"
    OTHER = "other"


def _require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise GradeLedgerValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _require_sha256(value: str, *, field: str) -> str:
    digest = value.strip().lower()
    if len(digest) != SHA256_HEX_LEN or any(ch not in "0123456789abcdef" for ch in digest):
        raise GradeLedgerValidationError(f"{field} must be a 64-char lowercase hex SHA-256")
    return digest


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), sort_keys=False, separators=(",", ":"), ensure_ascii=True)


def _same_optional_float(left: float | None, right: float | None) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return float(left) == float(right)


def _same_aware_dt(left: datetime, right: datetime) -> bool:
    return _require_aware(left, field="left") == _require_aware(right, field="right")


def _raise_key_conflict(entity: str, mismatches: Sequence[str]) -> None:
    fields = ", ".join(mismatches)
    raise GradeLedgerValidationError(
        f"{entity} idempotency key conflict: material fields differ ({fields})"
    )


def _collect_mismatches(
    pairs: Sequence[tuple[str, bool]],
) -> list[str]:
    return [name for name, matches in pairs if not matches]


def official_t60_idempotency_key(
    *,
    event_id: str,
    bout_id: str,
    selection_id: str,
    cutoff_at: datetime,
    publication_kind: PublicationKind = PUBLICATION_KIND_T60,
) -> str:
    cutoff = _require_aware(cutoff_at, field="cutoff_at")
    return (
        f"t60:{publication_kind}:{event_id}:{bout_id}:{selection_id}:"
        f"{cutoff.isoformat()}"
    )


def prediction_grade_idempotency_key(
    *,
    prediction_id: str,
    result_version_kind: ResultVersionKind,
    revision: int,
) -> str:
    return f"grade:{prediction_id}:{result_version_kind}:{revision}"


def settlement_idempotency_key(
    *,
    official_publication_id: str,
    observed_price_id: str,
    rule_set_id: str,
    rule_set_version: str,
    result_version_kind: ResultVersionKind,
    revision: int,
) -> str:
    return (
        f"settle:{official_publication_id}:{observed_price_id}:"
        f"{rule_set_id}:{rule_set_version}:{result_version_kind}:{revision}"
    )


def thresholds_content_hash(thresholds: RenderedThresholds) -> str:
    return _sha256_payload(thresholds.as_dict())


def quote_content_hash(
    *,
    sportsbook: str,
    decimal_odds: float,
    source_type: QuoteSourceKind | str,
    source_timestamp: datetime,
    region: str | None = None,
) -> str:
    ts = _require_aware(source_timestamp, field="source_timestamp")
    kind = source_type.value if isinstance(source_type, QuoteSourceKind) else str(source_type)
    return _sha256_payload(
        {
            "decimal_odds": decimal_odds,
            "region": region,
            "source_timestamp": ts.isoformat(),
            "source_type": kind,
            "sportsbook": sportsbook,
        }
    )


@dataclass(frozen=True)
class ReconstructedModelIdentity:
    model_run_id: str
    artifact_digest: str
    model_hash: str
    feature_hash: str
    config_hash: str
    data_hash: str
    spec_id: str
    cutoff_at: datetime | None
    published_at: datetime | None


@dataclass(frozen=True)
class ReconstructedThresholds:
    price_target_id: str
    fair_decimal: float
    actionable_decimal: float
    strong_value_decimal: float
    fair_american: float
    actionable_american: float
    strong_value_american: float
    thresholds_hash: str
    published_at: datetime


@dataclass(frozen=True)
class ReconstructedQuote:
    observed_price_id: str
    sportsbook: str
    decimal_odds: float
    american_odds: float
    source_type: str
    source_timestamp: datetime
    quote_hash: str
    region: str | None


@dataclass(frozen=True)
class PerformanceView:
    lane: str
    publications: int
    confirmed_value: int
    price_target: int
    no_bet: int
    graded_predictions: int
    settlements: int
    profit_sum: float | None
    roi_mean: float | None
    clv_mean: float | None


@dataclass(frozen=True)
class SeriesAudit:
    series: str
    counts: dict[str, int]
    grades_by_result: dict[str, int]
    settlements_by_result: dict[str, int]
    performance: dict[str, dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "grades_by_result": self.grades_by_result,
            "performance": self.performance,
            "series": self.series,
            "settlements_by_result": self.settlements_by_result,
        }


def publish_model_run(
    session: Session,
    *,
    idempotency_key: str,
    spec_id: str,
    artifact_digest: str,
    model_hash: str,
    feature_hash: str,
    config_hash: str,
    data_hash: str,
    series: str = DEFAULT_SERIES,
    created_at: datetime | None = None,
) -> tuple[ModelRun, bool]:
    """Insert a model run or return the existing row for an identical retry.

    Same key with material field mismatches fails closed (no insert/mutate).
    Returns ``(row, created)``.
    """
    key = idempotency_key.strip()
    if not key:
        raise GradeLedgerValidationError("idempotency_key must be non-empty")
    artifact = _require_sha256(artifact_digest, field="artifact_digest")
    model = _require_sha256(model_hash, field="model_hash")
    feature = _require_sha256(feature_hash, field="feature_hash")
    config = _require_sha256(config_hash, field="config_hash")
    data = _require_sha256(data_hash, field="data_hash")
    spec = spec_id.strip()
    existing = session.scalar(select(ModelRun).where(ModelRun.idempotency_key == key))
    if existing is not None:
        mismatches = _collect_mismatches(
            (
                ("series", existing.series == series),
                ("spec_id", existing.spec_id == spec),
                ("artifact_digest", existing.artifact_digest == artifact),
                ("model_hash", existing.model_hash == model),
                ("feature_hash", existing.feature_hash == feature),
                ("config_hash", existing.config_hash == config),
                ("data_hash", existing.data_hash == data),
            )
        )
        if mismatches:
            _raise_key_conflict("model_run", mismatches)
        return existing, False
    row = ModelRun(
        idempotency_key=key,
        series=series,
        spec_id=spec,
        artifact_digest=artifact,
        model_hash=model,
        feature_hash=feature,
        config_hash=config,
        data_hash=data,
        created_at=_require_aware(created_at or datetime.now(UTC), field="created_at"),
    )
    session.add(row)
    session.flush()
    return row, True


def publish_predictions(
    session: Session,
    *,
    model_run: ModelRun,
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[Prediction, bool]]:
    """Publish prediction snapshots.

    Identical retry for the same key is a no-op; material mismatches fail closed.
    """
    out: list[tuple[Prediction, bool]] = []
    for raw in rows:
        key = str(raw["idempotency_key"]).strip()
        if not key:
            raise GradeLedgerValidationError("prediction idempotency_key must be non-empty")
        cutoff = _require_aware(raw["cutoff_at"], field="cutoff_at")
        published = _require_aware(raw["published_at"], field="published_at")
        if published < cutoff:
            raise GradeLedgerValidationError("published_at must be >= cutoff_at")
        family = raw["market_family"]
        outcome = raw["outcome_key"]
        family_value = family.value if isinstance(family, MarketFamily) else str(family)
        outcome_value = outcome.value if isinstance(outcome, OutcomeKey) else str(outcome)
        series = str(raw.get("series") or model_run.series)
        line_point = raw.get("line_point")
        p50 = float(raw["p50"])
        p25 = None if raw.get("p25") is None else float(raw["p25"])
        semantics = str(raw.get("probability_semantics") or "exhaustive")
        event_id = str(raw["event_id"])
        bout_id = str(raw["bout_id"])
        selection_id = str(raw["selection_id"])
        existing = session.scalar(select(Prediction).where(Prediction.idempotency_key == key))
        if existing is not None:
            mismatches = _collect_mismatches(
                (
                    ("series", existing.series == series),
                    ("event_id", existing.event_id == event_id),
                    ("bout_id", existing.bout_id == bout_id),
                    ("selection_id", existing.selection_id == selection_id),
                    ("market_family", existing.market_family == family_value),
                    ("outcome_key", existing.outcome_key == outcome_value),
                    ("line_point", _same_optional_float(existing.line_point, line_point)),
                    ("p50", _same_optional_float(existing.p50, p50)),
                    ("p25", _same_optional_float(existing.p25, p25)),
                    ("probability_semantics", existing.probability_semantics == semantics),
                    ("cutoff_at", _same_aware_dt(existing.cutoff_at, cutoff)),
                    ("published_at", _same_aware_dt(existing.published_at, published)),
                    ("model_run_id", existing.model_run_id == model_run.id),
                    ("artifact_digest", existing.artifact_digest == model_run.artifact_digest),
                    ("model_hash", existing.model_hash == model_run.model_hash),
                    ("feature_hash", existing.feature_hash == model_run.feature_hash),
                    ("config_hash", existing.config_hash == model_run.config_hash),
                    ("data_hash", existing.data_hash == model_run.data_hash),
                )
            )
            if mismatches:
                _raise_key_conflict("prediction", mismatches)
            out.append((existing, False))
            continue
        row = Prediction(
            idempotency_key=key,
            series=series,
            event_id=event_id,
            bout_id=bout_id,
            selection_id=selection_id,
            market_family=family_value,
            outcome_key=outcome_value,
            line_point=line_point,
            p50=p50,
            p25=p25,
            probability_semantics=semantics,
            cutoff_at=cutoff,
            published_at=published,
            model_run_id=model_run.id,
            artifact_digest=model_run.artifact_digest,
            model_hash=model_run.model_hash,
            feature_hash=model_run.feature_hash,
            config_hash=model_run.config_hash,
            data_hash=model_run.data_hash,
        )
        session.add(row)
        session.flush()
        out.append((row, True))
    return out


def _persist_price_target(
    session: Session,
    *,
    idempotency_key: str,
    prediction_id: str | None,
    thresholds: RenderedThresholds,
    published_at: datetime,
) -> tuple[PriceTarget, bool]:
    key = idempotency_key.strip()
    published = _require_aware(published_at, field="published_at")
    digest = thresholds_content_hash(thresholds)
    existing = session.scalar(select(PriceTarget).where(PriceTarget.idempotency_key == key))
    if existing is not None:
        mismatches = _collect_mismatches(
            (
                ("prediction_id", existing.prediction_id == prediction_id),
                ("thresholds_hash", existing.thresholds_hash == digest),
                (
                    "fair_decimal",
                    _same_optional_float(existing.fair_decimal, thresholds.fair_decimal),
                ),
                (
                    "actionable_decimal",
                    _same_optional_float(
                        existing.actionable_decimal, thresholds.actionable_decimal
                    ),
                ),
                (
                    "strong_value_decimal",
                    _same_optional_float(
                        existing.strong_value_decimal, thresholds.strong_value_decimal
                    ),
                ),
                ("published_at", _same_aware_dt(existing.published_at, published)),
            )
        )
        if mismatches:
            _raise_key_conflict("price_target", mismatches)
        return existing, False
    row = PriceTarget(
        idempotency_key=key,
        prediction_id=prediction_id,
        fair_decimal=thresholds.fair_decimal,
        actionable_decimal=thresholds.actionable_decimal,
        strong_value_decimal=thresholds.strong_value_decimal,
        fair_american=thresholds.fair_american,
        actionable_american=thresholds.actionable_american,
        strong_value_american=thresholds.strong_value_american,
        actionable_ev_target=thresholds.actionable_ev_target,
        strong_value_ev_target=thresholds.strong_value_ev_target,
        thresholds_hash=digest,
        published_at=published,
    )
    session.add(row)
    session.flush()
    return row, True


def _assert_official_t60_matches(
    session: Session,
    existing: OfficialPublication,
    *,
    event_id: str,
    bout_id: str,
    selection_id: str,
    state_value: str,
    cutoff: datetime,
    published: datetime,
    market_family: str | None,
    outcome_key: str | None,
    line_point: float | None,
    prediction_id: str | None,
    model_run_id: str | None,
    policy_hash: str | None,
    config_hash: str | None,
    performance_lane: str,
    series: str,
    publication_kind: str,
    thresholds: RenderedThresholds | None,
) -> None:
    mismatches = _collect_mismatches(
        (
            ("series", existing.series == series),
            ("publication_kind", existing.publication_kind == publication_kind),
            ("event_id", existing.event_id == event_id),
            ("bout_id", existing.bout_id == bout_id),
            ("selection_id", existing.selection_id == selection_id),
            ("state", existing.state == state_value),
            ("market_family", existing.market_family == market_family),
            ("outcome_key", existing.outcome_key == outcome_key),
            ("line_point", _same_optional_float(existing.line_point, line_point)),
            ("prediction_id", existing.prediction_id == prediction_id),
            ("model_run_id", existing.model_run_id == model_run_id),
            ("policy_hash", existing.policy_hash == policy_hash),
            ("config_hash", existing.config_hash == config_hash),
            ("performance_lane", existing.performance_lane == performance_lane),
            ("cutoff_at", _same_aware_dt(existing.cutoff_at, cutoff)),
            ("published_at", _same_aware_dt(existing.published_at, published)),
        )
    )
    if thresholds is None:
        if existing.price_target_id is not None:
            mismatches.append("thresholds")
    else:
        digest = thresholds_content_hash(thresholds)
        if existing.price_target_id is None:
            mismatches.append("thresholds_hash")
        else:
            target = session.get(PriceTarget, existing.price_target_id)
            if target is None or target.thresholds_hash != digest:
                mismatches.append("thresholds_hash")
    if mismatches:
        _raise_key_conflict("official_publication", mismatches)


def publish_official_t60(
    session: Session,
    *,
    event_id: str,
    bout_id: str,
    selection_id: str,
    state: RecommendationState | str,
    cutoff_at: datetime,
    published_at: datetime,
    reasons: Sequence[str] = (),
    primary_reason: str | None = None,
    detail: str = "",
    market_family: MarketFamily | str | None = None,
    outcome_key: OutcomeKey | str | None = None,
    line_point: float | None = None,
    prediction_id: str | None = None,
    thresholds: RenderedThresholds | None = None,
    model_run_id: str | None = None,
    policy_hash: str | None = None,
    config_hash: str | None = None,
    performance_lane: PerformanceLane = "paper",
    series: str = DEFAULT_SERIES,
    publication_kind: PublicationKind = PUBLICATION_KIND_T60,
) -> tuple[OfficialPublication, bool]:
    """Persist the official T-60m outcome.

    Same key + identical immutable fields → existing row (retry).
    Same key + material mismatch → fail closed (no insert/mutate).
    """
    state_value = state.value if isinstance(state, RecommendationState) else str(state)
    if state_value not in {
        RecommendationState.CONFIRMED_VALUE.value,
        RecommendationState.PRICE_TARGET.value,
        RecommendationState.NO_BET.value,
    }:
        raise GradeLedgerValidationError(f"unsupported recommendation state: {state_value}")
    cutoff = _require_aware(cutoff_at, field="cutoff_at")
    published = _require_aware(published_at, field="published_at")
    if published < cutoff:
        raise GradeLedgerValidationError("published_at must be >= cutoff_at")
    key = official_t60_idempotency_key(
        event_id=event_id,
        bout_id=bout_id,
        selection_id=selection_id,
        cutoff_at=cutoff,
        publication_kind=publication_kind,
    )
    family_value = None
    if market_family is not None:
        family_value = (
            market_family.value
            if isinstance(market_family, MarketFamily)
            else str(market_family)
        )
    outcome_value = None
    if outcome_key is not None:
        outcome_value = (
            outcome_key.value if isinstance(outcome_key, OutcomeKey) else str(outcome_key)
        )
    policy = None if policy_hash is None else _require_sha256(policy_hash, field="policy_hash")
    config = None if config_hash is None else _require_sha256(config_hash, field="config_hash")

    existing = session.scalar(
        select(OfficialPublication).where(OfficialPublication.idempotency_key == key)
    )
    if existing is not None:
        _assert_official_t60_matches(
            session,
            existing,
            event_id=event_id,
            bout_id=bout_id,
            selection_id=selection_id,
            state_value=state_value,
            cutoff=cutoff,
            published=published,
            market_family=family_value,
            outcome_key=outcome_value,
            line_point=line_point,
            prediction_id=prediction_id,
            model_run_id=model_run_id,
            policy_hash=policy,
            config_hash=config,
            performance_lane=performance_lane,
            series=series,
            publication_kind=publication_kind,
            thresholds=thresholds,
        )
        return existing, False

    price_target_id: str | None = None
    if state_value != RecommendationState.NO_BET.value:
        if thresholds is None:
            raise GradeLedgerValidationError(
                "thresholds are required for confirmed_value and price_target"
            )
        target, _created = _persist_price_target(
            session,
            idempotency_key=f"pt:{key}",
            prediction_id=prediction_id,
            thresholds=thresholds,
            published_at=published,
        )
        price_target_id = target.id
    elif thresholds is not None:
        # Optional thresholds on no_bet (still stored immutably when provided).
        target, _created = _persist_price_target(
            session,
            idempotency_key=f"pt:{key}",
            prediction_id=prediction_id,
            thresholds=thresholds,
            published_at=published,
        )
        price_target_id = target.id

    row = OfficialPublication(
        idempotency_key=key,
        series=series,
        publication_kind=publication_kind,
        event_id=event_id,
        bout_id=bout_id,
        selection_id=selection_id,
        market_family=family_value,
        outcome_key=outcome_value,
        line_point=line_point,
        state=state_value,
        performance_lane=performance_lane,
        reasons_json=_json_list(tuple(str(r) for r in reasons)),
        primary_reason=primary_reason,
        detail=detail,
        prediction_id=prediction_id,
        price_target_id=price_target_id,
        model_run_id=model_run_id,
        policy_hash=policy,
        config_hash=config,
        cutoff_at=cutoff,
        published_at=published,
    )
    session.add(row)
    session.flush()
    return row, True


def append_state_event(
    session: Session,
    *,
    official_publication_id: str,
    event_type: StateEventType | str,
    observed_at: datetime,
    reason_code: str | None = None,
    detail: str = "",
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> tuple[RecommendationStateEvent, bool]:
    """Append a later line/state change without mutating the official row."""
    publication = session.get(OfficialPublication, official_publication_id)
    if publication is None:
        raise GradeLedgerValidationError(
            f"unknown official_publication_id: {official_publication_id}"
        )
    event_value = event_type.value if isinstance(event_type, StateEventType) else str(event_type)
    observed = _require_aware(observed_at, field="observed_at")
    payload_map = dict(payload or {})
    key = (
        idempotency_key.strip()
        if idempotency_key
        else (
            f"state:{official_publication_id}:{event_value}:"
            f"{observed.isoformat()}:{_sha256_payload(payload_map)[:16]}"
        )
    )
    existing = session.scalar(
        select(RecommendationStateEvent).where(RecommendationStateEvent.idempotency_key == key)
    )
    if existing is not None:
        return existing, False
    row = RecommendationStateEvent(
        idempotency_key=key,
        official_publication_id=official_publication_id,
        event_type=event_value,
        reason_code=reason_code,
        detail=detail,
        payload_json=_canonical_json(payload_map),
        observed_at=observed,
    )
    session.add(row)
    session.flush()
    return row, True


def record_observed_price(
    session: Session,
    *,
    official_publication_id: str,
    sportsbook: str,
    decimal_odds: float,
    source_type: QuoteSourceKind | str,
    source_timestamp: datetime,
    region: str | None = None,
    detail: str = "",
    american_odds: float | None = None,
    idempotency_key: str | None = None,
) -> tuple[ObservedPrice, bool]:
    """Record a real timestamped quote. Never invents a price."""
    publication = session.get(OfficialPublication, official_publication_id)
    if publication is None:
        raise GradeLedgerValidationError(
            f"unknown official_publication_id: {official_publication_id}"
        )
    decimal = validate_decimal_odds(decimal_odds, field="decimal_odds")
    kind = source_type.value if isinstance(source_type, QuoteSourceKind) else str(source_type)
    if kind not in {QuoteSourceKind.AUTOMATIC.value, QuoteSourceKind.USER_OBSERVED.value}:
        raise GradeLedgerValidationError(f"unsupported quote source_type: {kind}")
    ts = _require_aware(source_timestamp, field="source_timestamp")
    american = (
        float(american_odds)
        if american_odds is not None
        else float(decimal_to_american(decimal))
    )
    digest = quote_content_hash(
        sportsbook=sportsbook,
        decimal_odds=decimal,
        source_type=kind,
        source_timestamp=ts,
        region=region,
    )
    key = idempotency_key.strip() if idempotency_key else f"quote:{digest}"
    existing = session.scalar(select(ObservedPrice).where(ObservedPrice.idempotency_key == key))
    if existing is not None:
        return existing, False
    by_hash = session.scalar(
        select(ObservedPrice).where(
            ObservedPrice.official_publication_id == official_publication_id,
            ObservedPrice.quote_hash == digest,
        )
    )
    if by_hash is not None:
        return by_hash, False
    row = ObservedPrice(
        idempotency_key=key,
        official_publication_id=official_publication_id,
        sportsbook=sportsbook.strip(),
        decimal_odds=decimal,
        american_odds=american,
        source_type=kind,
        source_timestamp=ts,
        quote_hash=digest,
        region=region,
        detail=detail,
    )
    session.add(row)
    session.flush()
    return row, True


def grade_predictions(
    session: Session,
    *,
    prediction_ids: Sequence[str],
    facts_by_bout: Mapping[str, BoutSettlementFacts],
    result_version_kind: ResultVersionKind = "event_night",
    revision: int = 1,
    bout_result_version_ids: Mapping[str, int] | None = None,
    rule_set_id: str | None = None,
    rule_set: SettlementRuleSet | None = None,
    graded_at: datetime | None = None,
) -> list[tuple[PredictionGrade, bool]]:
    """Grade sporting outcomes. Same idempotency key → existing row."""
    if revision < 1:
        raise GradeLedgerValidationError("revision must be >= 1")
    when = _require_aware(graded_at or datetime.now(UTC), field="graded_at")
    out: list[tuple[PredictionGrade, bool]] = []
    for prediction_id in prediction_ids:
        prediction = session.get(Prediction, prediction_id)
        if prediction is None:
            raise GradeLedgerValidationError(f"unknown prediction_id: {prediction_id}")
        key = prediction_grade_idempotency_key(
            prediction_id=prediction_id,
            result_version_kind=result_version_kind,
            revision=revision,
        )
        existing = session.scalar(
            select(PredictionGrade).where(PredictionGrade.idempotency_key == key)
        )
        if existing is not None:
            out.append((existing, False))
            continue
        facts = facts_by_bout.get(prediction.bout_id)
        if facts is None:
            raise GradeLedgerValidationError(
                f"missing BoutSettlementFacts for bout_id={prediction.bout_id}"
            )
        selection = MarketSelection(
            family=MarketFamily(prediction.market_family),
            outcome=OutcomeKey(prediction.outcome_key),
            line_point=prediction.line_point,
        )
        decision = settle(
            selection, facts, rule_set_id=rule_set_id, rule_set=rule_set
        )
        row = PredictionGrade(
            idempotency_key=key,
            prediction_id=prediction_id,
            sporting_result=decision.result.value,
            reason_code=decision.reason,
            result_version_kind=result_version_kind,
            revision=revision,
            bout_result_version_id=(
                None
                if bout_result_version_ids is None
                else bout_result_version_ids.get(prediction.bout_id)
            ),
            rule_set_id=decision.rule_set_id,
            rule_set_version=decision.rule_set_version,
            rule_content_hash=decision.content_hash,
            graded_at=when,
        )
        session.add(row)
        session.flush()
        out.append((row, True))
    return out


def _settlement_pnl(
    *,
    state: str,
    observed: ObservedPrice | None,
    decision: SettlementDecision,
    closing_decimal: float | None,
) -> tuple[float | None, float | None, float | None]:
    """Return profit/roi/clv only for priced confirmed_value rows."""
    if (
        observed is None
        or state != RecommendationState.CONFIRMED_VALUE.value
        or decision.result is SettlementResult.UNRESOLVED
    ):
        return None, None, None
    profit = flat_unit_profit(
        settlement=decision.result,
        offered_decimal=observed.decimal_odds,
    )
    roi = profit  # flat 1-unit stake
    clv: float | None = None
    if closing_decimal is not None:
        clv = unsafe_same_line_probability_clv(
            bet_decimal=observed.decimal_odds,
            close_decimal=validate_decimal_odds(closing_decimal, field="closing_decimal"),
        )
    return profit, roi, clv


def settle_recommendations(
    session: Session,
    *,
    official_publication_ids: Sequence[str],
    facts_by_bout: Mapping[str, BoutSettlementFacts],
    result_version_kind: ResultVersionKind = "event_night",
    revision: int = 1,
    rule_set_id: str | None = None,
    rule_set: SettlementRuleSet | None = None,
    closing_decimal_by_publication: Mapping[str, float] | None = None,
    settled_at: datetime | None = None,
) -> list[tuple[RecommendationSettlement, bool]]:
    """Settle betting performance only when a timestamped observed price exists.

    Price-target-only and unpriced rows produce no settlement (and never
    synthetic profit/ROI/CLV). Re-running the same key is a no-op.
    """
    if revision < 1:
        raise GradeLedgerValidationError("revision must be >= 1")
    when = _require_aware(settled_at or datetime.now(UTC), field="settled_at")
    out: list[tuple[RecommendationSettlement, bool]] = []
    for publication_id in official_publication_ids:
        publication = session.get(OfficialPublication, publication_id)
        if publication is None:
            raise GradeLedgerValidationError(
                f"unknown official_publication_id: {publication_id}"
            )
        # Never settle price_target or no_bet for betting PnL.
        if publication.state != RecommendationState.CONFIRMED_VALUE.value:
            continue
        if publication.market_family is None or publication.outcome_key is None:
            raise GradeLedgerValidationError(
                f"confirmed_value publication {publication_id} missing market/outcome"
            )
        observed = session.scalar(
            select(ObservedPrice)
            .where(ObservedPrice.official_publication_id == publication_id)
            .order_by(ObservedPrice.source_timestamp.asc(), ObservedPrice.created_at.asc())
            .limit(1)
        )
        if observed is None:
            # Unpriced confirmed_value: no settlement PnL and no manufactured price.
            continue
        facts = facts_by_bout.get(publication.bout_id)
        if facts is None:
            raise GradeLedgerValidationError(
                f"missing BoutSettlementFacts for bout_id={publication.bout_id}"
            )
        decision = settle(
            MarketSelection(
                family=MarketFamily(publication.market_family),
                outcome=OutcomeKey(publication.outcome_key),
                line_point=publication.line_point,
            ),
            facts,
            rule_set_id=rule_set_id,
            rule_set=rule_set,
        )
        key = settlement_idempotency_key(
            official_publication_id=publication_id,
            observed_price_id=observed.id,
            rule_set_id=decision.rule_set_id,
            rule_set_version=decision.rule_set_version,
            result_version_kind=result_version_kind,
            revision=revision,
        )
        existing = session.scalar(
            select(RecommendationSettlement).where(
                RecommendationSettlement.idempotency_key == key
            )
        )
        if existing is not None:
            out.append((existing, False))
            continue
        closing = None
        if closing_decimal_by_publication is not None:
            closing = closing_decimal_by_publication.get(publication_id)
        profit, roi, clv = _settlement_pnl(
            state=publication.state,
            observed=observed,
            decision=decision,
            closing_decimal=closing,
        )
        row = RecommendationSettlement(
            idempotency_key=key,
            official_publication_id=publication_id,
            observed_price_id=observed.id,
            settlement_result=decision.result.value,
            reason_code=decision.reason,
            result_version_kind=result_version_kind,
            revision=revision,
            rule_set_id=decision.rule_set_id,
            rule_set_version=decision.rule_set_version,
            rule_content_hash=decision.content_hash,
            profit=profit,
            roi=roi,
            clv=clv,
            closing_decimal=closing,
            settled_at=when,
        )
        session.add(row)
        session.flush()
        out.append((row, True))
    return out


def reconstruct_model_identity(
    session: Session, *, prediction_id: str
) -> ReconstructedModelIdentity:
    prediction = session.get(Prediction, prediction_id)
    if prediction is None:
        raise GradeLedgerValidationError(f"unknown prediction_id: {prediction_id}")
    run = session.get(ModelRun, prediction.model_run_id)
    if run is None:
        raise GradeLedgerValidationError(f"missing model_run for prediction {prediction_id}")
    return ReconstructedModelIdentity(
        model_run_id=run.id,
        artifact_digest=run.artifact_digest,
        model_hash=run.model_hash,
        feature_hash=run.feature_hash,
        config_hash=run.config_hash,
        data_hash=run.data_hash,
        spec_id=run.spec_id,
        cutoff_at=prediction.cutoff_at,
        published_at=prediction.published_at,
    )


def reconstruct_thresholds(
    session: Session, *, price_target_id: str
) -> ReconstructedThresholds:
    target = session.get(PriceTarget, price_target_id)
    if target is None:
        raise GradeLedgerValidationError(f"unknown price_target_id: {price_target_id}")
    return ReconstructedThresholds(
        price_target_id=target.id,
        fair_decimal=target.fair_decimal,
        actionable_decimal=target.actionable_decimal,
        strong_value_decimal=target.strong_value_decimal,
        fair_american=target.fair_american,
        actionable_american=target.actionable_american,
        strong_value_american=target.strong_value_american,
        thresholds_hash=target.thresholds_hash,
        published_at=target.published_at,
    )


def reconstruct_quote(session: Session, *, observed_price_id: str) -> ReconstructedQuote:
    quote = session.get(ObservedPrice, observed_price_id)
    if quote is None:
        raise GradeLedgerValidationError(f"unknown observed_price_id: {observed_price_id}")
    return ReconstructedQuote(
        observed_price_id=quote.id,
        sportsbook=quote.sportsbook,
        decimal_odds=quote.decimal_odds,
        american_odds=quote.american_odds,
        source_type=quote.source_type,
        source_timestamp=quote.source_timestamp,
        quote_hash=quote.quote_hash,
        region=quote.region,
    )


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _lane_performance(
    session: Session,
    *,
    series: str,
    lane: str | None = None,
    model_run_id: str | None = None,
) -> PerformanceView:
    pubs_q = select(OfficialPublication).where(OfficialPublication.series == series)
    if lane is not None:
        pubs_q = pubs_q.where(OfficialPublication.performance_lane == lane)
    if model_run_id is not None:
        pubs_q = pubs_q.where(OfficialPublication.model_run_id == model_run_id)
    publications = list(session.scalars(pubs_q))
    pub_ids = [p.id for p in publications]
    confirmed = sum(1 for p in publications if p.state == RecommendationState.CONFIRMED_VALUE.value)
    price_target = sum(1 for p in publications if p.state == RecommendationState.PRICE_TARGET.value)
    no_bet = sum(1 for p in publications if p.state == RecommendationState.NO_BET.value)

    pred_ids = [p.prediction_id for p in publications if p.prediction_id]
    graded = 0
    if pred_ids:
        graded = int(
            session.scalar(
                select(func.count())
                .select_from(PredictionGrade)
                .where(PredictionGrade.prediction_id.in_(pred_ids))
            )
            or 0
        )

    settlements: list[RecommendationSettlement] = []
    if pub_ids:
        settlements = list(
            session.scalars(
                select(RecommendationSettlement).where(
                    RecommendationSettlement.official_publication_id.in_(pub_ids)
                )
            )
        )
    # Exclude price-target-only from ROI/CLV/profit totals by only summing
    # settlements that already carry non-null profit (priced confirmed_value).
    profits = [s.profit for s in settlements if s.profit is not None]
    rois = [s.roi for s in settlements if s.roi is not None]
    clvs = [s.clv for s in settlements if s.clv is not None]
    return PerformanceView(
        lane=lane or (f"model_run:{model_run_id}" if model_run_id else "all"),
        publications=len(publications),
        confirmed_value=confirmed,
        price_target=price_target,
        no_bet=no_bet,
        graded_predictions=graded,
        settlements=len(settlements),
        profit_sum=None if not profits else float(sum(profits)),
        roi_mean=_mean([float(v) for v in rois]),
        clv_mean=_mean([float(v) for v in clvs]),
    )


def audit_series(session: Session, *, series: str = DEFAULT_SERIES) -> SeriesAudit:
    """Qualified / paper / experimental / model-version performance views."""
    counts = {
        "model_runs": int(
            session.scalar(
                select(func.count()).select_from(ModelRun).where(ModelRun.series == series)
            )
            or 0
        ),
        "predictions": int(
            session.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(Prediction.series == series)
            )
            or 0
        ),
        "official_publications": int(
            session.scalar(
                select(func.count())
                .select_from(OfficialPublication)
                .where(OfficialPublication.series == series)
            )
            or 0
        ),
        "price_targets": int(session.scalar(select(func.count()).select_from(PriceTarget)) or 0),
        "state_events": int(
            session.scalar(select(func.count()).select_from(RecommendationStateEvent)) or 0
        ),
        "observed_prices": int(
            session.scalar(select(func.count()).select_from(ObservedPrice)) or 0
        ),
        "prediction_grades": int(
            session.scalar(select(func.count()).select_from(PredictionGrade)) or 0
        ),
        "recommendation_settlements": int(
            session.scalar(select(func.count()).select_from(RecommendationSettlement)) or 0
        ),
    }
    state_counts = {
        RecommendationState.CONFIRMED_VALUE.value: 0,
        RecommendationState.PRICE_TARGET.value: 0,
        RecommendationState.NO_BET.value: 0,
    }
    for pub in session.scalars(
        select(OfficialPublication).where(OfficialPublication.series == series)
    ):
        if pub.state in state_counts:
            state_counts[pub.state] += 1
    counts.update({f"state_{k}": v for k, v in state_counts.items()})

    grades_by_result: dict[str, int] = {}
    for grade in session.scalars(select(PredictionGrade)):
        grades_by_result[grade.sporting_result] = (
            grades_by_result.get(grade.sporting_result, 0) + 1
        )
    settlements_by_result: dict[str, int] = {}
    for settlement in session.scalars(select(RecommendationSettlement)):
        settlements_by_result[settlement.settlement_result] = (
            settlements_by_result.get(settlement.settlement_result, 0) + 1
        )

    performance: dict[str, dict[str, Any]] = {}
    for lane in ("qualified", "paper", "experimental"):
        view = _lane_performance(session, series=series, lane=lane)
        performance[lane] = {
            "clv_mean": view.clv_mean,
            "confirmed_value": view.confirmed_value,
            "graded_predictions": view.graded_predictions,
            "no_bet": view.no_bet,
            "price_target": view.price_target,
            "profit_sum": view.profit_sum,
            "publications": view.publications,
            "roi_mean": view.roi_mean,
            "settlements": view.settlements,
        }

    model_versions: dict[str, dict[str, Any]] = {}
    for run in session.scalars(select(ModelRun).where(ModelRun.series == series)):
        view = _lane_performance(session, series=series, model_run_id=run.id)
        model_versions[run.spec_id] = {
            "artifact_digest": run.artifact_digest,
            "clv_mean": view.clv_mean,
            "confirmed_value": view.confirmed_value,
            "graded_predictions": view.graded_predictions,
            "model_run_id": run.id,
            "no_bet": view.no_bet,
            "price_target": view.price_target,
            "profit_sum": view.profit_sum,
            "publications": view.publications,
            "roi_mean": view.roi_mean,
            "settlements": view.settlements,
        }
    performance["model_version"] = model_versions

    # Separate event-night vs current settlement series.
    for kind in ("event_night", "current"):
        rows = list(
            session.scalars(
                select(RecommendationSettlement).where(
                    RecommendationSettlement.result_version_kind == kind
                )
            )
        )
        profits = [r.profit for r in rows if r.profit is not None]
        performance[f"settlements_{kind}"] = {
            "count": len(rows),
            "profit_sum": None if not profits else float(sum(profits)),
        }

    return SeriesAudit(
        series=series,
        counts=dict(sorted(counts.items())),
        grades_by_result=dict(sorted(grades_by_result.items())),
        settlements_by_result=dict(sorted(settlements_by_result.items())),
        performance=dict(sorted(performance.items())),
    )


__all__ = [
    "GradeLedgerError",
    "GradeLedgerMutationError",
    "GradeLedgerValidationError",
    "PerformanceView",
    "ReconstructedModelIdentity",
    "ReconstructedQuote",
    "ReconstructedThresholds",
    "SeriesAudit",
    "StateEventType",
    "append_state_event",
    "audit_series",
    "grade_predictions",
    "official_t60_idempotency_key",
    "prediction_grade_idempotency_key",
    "publish_model_run",
    "publish_official_t60",
    "publish_predictions",
    "quote_content_hash",
    "record_observed_price",
    "reconstruct_model_identity",
    "reconstruct_quote",
    "reconstruct_thresholds",
    "settle_recommendations",
    "settlement_idempotency_key",
    "thresholds_content_hash",
]
