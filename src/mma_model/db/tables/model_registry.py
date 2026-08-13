"""Append-only model registry decision ledger (DWCS-402)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.base import Base

_SHA256_LEN = 64
_ACTIONS_SQL = "'retrain', 'promote', 'rollback', 'reject'"
_LANES_SQL = "'champion', 'shadow', 'none'"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class ModelRegistryDecision(Base):
    """Append-only promotion / retrain / rollback audit row.

    Never UPDATE or DELETE through the registry API. Re-running a failed
    promote inserts another reject row; history is not rewritten.
    """

    __tablename__ = "model_registry_decisions"
    __table_args__ = (
        CheckConstraint(
            f"action IN ({_ACTIONS_SQL})",
            name="ck_model_registry_decisions_action",
        ),
        CheckConstraint(
            f"lane IN ({_LANES_SQL})",
            name="ck_model_registry_decisions_lane",
        ),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_model_registry_decisions_reason_nonempty",
        ),
        CheckConstraint(
            "length(trim(actor)) > 0",
            name="ck_model_registry_decisions_actor_nonempty",
        ),
        CheckConstraint(
            f"("
            f"artifact_digest IS NULL OR length(artifact_digest) = {_SHA256_LEN}"
            f")",
            name="ck_model_registry_decisions_artifact_digest",
        ),
        CheckConstraint(
            f"("
            f"config_hash IS NULL OR length(config_hash) = {_SHA256_LEN}"
            f")",
            name="ck_model_registry_decisions_config_hash",
        ),
        CheckConstraint(
            f"("
            f"prior_champion_digest IS NULL OR "
            f"length(prior_champion_digest) = {_SHA256_LEN}"
            f")",
            name="ck_model_registry_decisions_prior_digest",
        ),
        CheckConstraint(
            f"("
            f"evaluator_hash IS NULL OR length(evaluator_hash) = {_SHA256_LEN}"
            f")",
            name="ck_model_registry_decisions_evaluator_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    lane: Mapped[str] = mapped_column(String(32), default="none")
    artifact_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prior_champion_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    evaluator_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    health_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    gates_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    # Monotonic sequence for append-only audits (not unique across DBs).
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True)


__all__ = ["ModelRegistryDecision"]
