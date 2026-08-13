"""Append-only prediction, recommendation, and grading ledgers (DWCS-400).

Official T-60m publications, price targets, and settlements are never updated
or deleted in place. Later line changes append state events; current-result
corrections append new grade/settlement rows without rewriting event-night PnL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.base import Base

_REC_STATE_SQL = "'confirmed_value', 'price_target', 'no_bet'"
_QUOTE_SOURCE_SQL = "'automatic', 'user_observed'"
_RESULT_KIND_SQL = "'event_night', 'current'"
_LANE_SQL = "'qualified', 'paper', 'experimental'"
_PUB_KIND_SQL = "'t60'"
_SHA256_LEN = 64


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class ModelRun(Base):
    """Immutable model/artifact identity for a scoring run."""

    __tablename__ = "model_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_model_runs_idempotency"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_model_runs_idem_nonempty",
        ),
        CheckConstraint(
            f"length(artifact_digest) = {_SHA256_LEN}",
            name="ck_model_runs_artifact_digest_sha256",
        ),
        CheckConstraint(
            f"length(model_hash) = {_SHA256_LEN}",
            name="ck_model_runs_model_hash_sha256",
        ),
        CheckConstraint(
            f"length(feature_hash) = {_SHA256_LEN}",
            name="ck_model_runs_feature_hash_sha256",
        ),
        CheckConstraint(
            f"length(config_hash) = {_SHA256_LEN}",
            name="ck_model_runs_config_hash_sha256",
        ),
        CheckConstraint(
            f"length(data_hash) = {_SHA256_LEN}",
            name="ck_model_runs_data_hash_sha256",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(160), index=True)
    series: Mapped[str] = mapped_column(String(32), index=True, default="dwcs")
    spec_id: Mapped[str] = mapped_column(String(64), index=True)
    artifact_digest: Mapped[str] = mapped_column(String(64), index=True)
    model_hash: Mapped[str] = mapped_column(String(64))
    feature_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    data_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class Prediction(Base):
    """Append-only predictive probability snapshot at a cutoff."""

    __tablename__ = "predictions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_predictions_idempotency"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_predictions_idem_nonempty",
        ),
        CheckConstraint("p50 > 0 AND p50 < 1", name="ck_predictions_p50_unit"),
        CheckConstraint(
            "p25 IS NULL OR (p25 > 0 AND p25 < 1)",
            name="ck_predictions_p25_unit",
        ),
        CheckConstraint(
            "published_at >= cutoff_at",
            name="ck_predictions_published_ge_cutoff",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(200), index=True)
    series: Mapped[str] = mapped_column(String(32), index=True, default="dwcs")
    event_id: Mapped[str] = mapped_column(String(36), index=True)
    bout_id: Mapped[str] = mapped_column(String(36), index=True)
    selection_id: Mapped[str] = mapped_column(String(160), index=True)
    market_family: Mapped[str] = mapped_column(String(64), index=True)
    outcome_key: Mapped[str] = mapped_column(String(64))
    line_point: Mapped[float | None] = mapped_column(Float, nullable=True)
    p50: Mapped[float] = mapped_column(Float)
    p25: Mapped[float | None] = mapped_column(Float, nullable=True)
    probability_semantics: Mapped[str] = mapped_column(String(32))
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    model_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("model_runs.id"), index=True
    )
    artifact_digest: Mapped[str] = mapped_column(String(64))
    model_hash: Mapped[str] = mapped_column(String(64))
    feature_hash: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    data_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class PriceTarget(Base):
    """Immutable fair / actionable / strong-value thresholds after publication."""

    __tablename__ = "price_targets"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_price_targets_idempotency"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_price_targets_idem_nonempty",
        ),
        CheckConstraint(
            "fair_decimal > 1 AND actionable_decimal > 1 AND strong_value_decimal > 1",
            name="ck_price_targets_decimals_gt_one",
        ),
        CheckConstraint(
            f"length(thresholds_hash) = {_SHA256_LEN}",
            name="ck_price_targets_thresholds_hash_sha256",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(200), index=True)
    prediction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("predictions.id"), nullable=True, index=True
    )
    fair_decimal: Mapped[float] = mapped_column(Float)
    actionable_decimal: Mapped[float] = mapped_column(Float)
    strong_value_decimal: Mapped[float] = mapped_column(Float)
    fair_american: Mapped[float] = mapped_column(Float)
    actionable_american: Mapped[float] = mapped_column(Float)
    strong_value_american: Mapped[float] = mapped_column(Float)
    actionable_ev_target: Mapped[float] = mapped_column(Float)
    strong_value_ev_target: Mapped[float] = mapped_column(Float)
    thresholds_hash: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class OfficialPublication(Base):
    """Official T-60m confirmed_value / price_target / no_bet snapshot.

    Republish with the same idempotency key is a no-op (unique key), never an
    in-place overwrite. Later line changes append ``recommendation_state_events``.
    """

    __tablename__ = "official_publications"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_official_publications_idempotency"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_official_publications_idem_nonempty",
        ),
        CheckConstraint(
            f"state IN ({_REC_STATE_SQL})",
            name="ck_official_publications_state",
        ),
        CheckConstraint(
            f"publication_kind IN ({_PUB_KIND_SQL})",
            name="ck_official_publications_kind",
        ),
        CheckConstraint(
            f"performance_lane IN ({_LANE_SQL})",
            name="ck_official_publications_lane",
        ),
        CheckConstraint(
            "published_at >= cutoff_at",
            name="ck_official_publications_published_ge_cutoff",
        ),
        # Price-target and confirmed_value rows must retain thresholds; no_bet may omit.
        CheckConstraint(
            "("
            "state = 'no_bet' OR price_target_id IS NOT NULL"
            ")",
            name="ck_official_publications_priced_has_target",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(220), index=True)
    series: Mapped[str] = mapped_column(String(32), index=True, default="dwcs")
    publication_kind: Mapped[str] = mapped_column(String(16), default="t60")
    event_id: Mapped[str] = mapped_column(String(36), index=True)
    bout_id: Mapped[str] = mapped_column(String(36), index=True)
    selection_id: Mapped[str] = mapped_column(String(160), index=True)
    market_family: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    line_point: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    performance_lane: Mapped[str] = mapped_column(String(32), index=True, default="paper")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    primary_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    prediction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("predictions.id"), nullable=True, index=True
    )
    price_target_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("price_targets.id"), nullable=True, index=True
    )
    model_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("model_runs.id"), nullable=True, index=True
    )
    policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class RecommendationStateEvent(Base):
    """Append-only later line/state changes against an official publication."""

    __tablename__ = "recommendation_state_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_recommendation_state_events_idem"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_recommendation_state_events_idem_nonempty",
        ),
        CheckConstraint(
            "length(trim(event_type)) > 0",
            name="ck_recommendation_state_events_type_nonempty",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(220), index=True)
    official_publication_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("official_publications.id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class ObservedPrice(Base):
    """Timestamped automatic or user-observed quote; never invented."""

    __tablename__ = "observed_prices"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_observed_prices_idempotency"),
        UniqueConstraint(
            "official_publication_id",
            "quote_hash",
            name="uq_observed_prices_pub_quote_hash",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_observed_prices_idem_nonempty",
        ),
        CheckConstraint(
            f"source_type IN ({_QUOTE_SOURCE_SQL})",
            name="ck_observed_prices_source_type",
        ),
        CheckConstraint("decimal_odds > 1", name="ck_observed_prices_decimal_gt_one"),
        CheckConstraint(
            f"length(quote_hash) = {_SHA256_LEN}",
            name="ck_observed_prices_quote_hash_sha256",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(220), index=True)
    official_publication_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("official_publications.id"), index=True
    )
    sportsbook: Mapped[str] = mapped_column(String(64), index=True)
    decimal_odds: Mapped[float] = mapped_column(Float)
    american_odds: Mapped[float] = mapped_column(Float)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    quote_hash: Mapped[str] = mapped_column(String(64), index=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class PredictionGrade(Base):
    """Sporting outcome grade for a prediction (event-night or current correction)."""

    __tablename__ = "prediction_grades"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_prediction_grades_idempotency"),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_prediction_grades_idem_nonempty",
        ),
        CheckConstraint(
            f"result_version_kind IN ({_RESULT_KIND_SQL})",
            name="ck_prediction_grades_result_kind",
        ),
        CheckConstraint("revision >= 1", name="ck_prediction_grades_revision_positive"),
        CheckConstraint(
            "length(trim(sporting_result)) > 0",
            name="ck_prediction_grades_result_nonempty",
        ),
        CheckConstraint(
            "length(trim(reason_code)) > 0",
            name="ck_prediction_grades_reason_nonempty",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(220), index=True)
    prediction_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("predictions.id"), index=True
    )
    sporting_result: Mapped[str] = mapped_column(String(32), index=True)
    reason_code: Mapped[str] = mapped_column(String(96))
    result_version_kind: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    bout_result_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_set_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rule_set_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rule_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    graded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class RecommendationSettlement(Base):
    """Betting settlement for priced confirmed-value recommendations only.

    Profit / ROI / CLV stay NULL unless an observed price exists and the
    official state is confirmed_value. Event-night rows are frozen; current
    corrections append a separate row.
    """

    __tablename__ = "recommendation_settlements"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_recommendation_settlements_idempotency"
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_recommendation_settlements_idem_nonempty",
        ),
        CheckConstraint(
            f"result_version_kind IN ({_RESULT_KIND_SQL})",
            name="ck_recommendation_settlements_result_kind",
        ),
        CheckConstraint(
            "revision >= 1", name="ck_recommendation_settlements_revision_positive"
        ),
        CheckConstraint(
            "length(trim(settlement_result)) > 0",
            name="ck_recommendation_settlements_result_nonempty",
        ),
        CheckConstraint(
            "length(trim(reason_code)) > 0",
            name="ck_recommendation_settlements_reason_nonempty",
        ),
        # PnL only when an observed price is attached (never for synthetic rows).
        CheckConstraint(
            "("
            "observed_price_id IS NOT NULL"
            ") OR ("
            "profit IS NULL AND roi IS NULL AND clv IS NULL"
            ")",
            name="ck_recommendation_settlements_pnl_requires_price",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    idempotency_key: Mapped[str] = mapped_column(String(260), index=True)
    official_publication_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("official_publications.id"), index=True
    )
    observed_price_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("observed_prices.id"), nullable=True, index=True
    )
    settlement_result: Mapped[str] = mapped_column(String(32), index=True)
    reason_code: Mapped[str] = mapped_column(String(96))
    result_version_kind: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    rule_set_id: Mapped[str] = mapped_column(String(64))
    rule_set_version: Mapped[str] = mapped_column(String(32))
    rule_content_hash: Mapped[str] = mapped_column(String(64))
    # NULL unless priced confirmed_value (never synthetic for price_target/no_bet).
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    roi: Mapped[float | None] = mapped_column(Float, nullable=True)
    clv: Mapped[float | None] = mapped_column(Float, nullable=True)
    closing_decimal: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


__all__ = [
    "ModelRun",
    "ObservedPrice",
    "OfficialPublication",
    "Prediction",
    "PredictionGrade",
    "PriceTarget",
    "RecommendationSettlement",
    "RecommendationStateEvent",
]
