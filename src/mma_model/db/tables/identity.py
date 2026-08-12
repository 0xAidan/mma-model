"""SQLAlchemy tables for identity review queue and evidence (DWCS-104)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mma_model.db.models import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class IdentityReviewQueue(Base):
    """Reversible review queue for non-auto identity links."""

    __tablename__ = "identity_review_queue"
    __table_args__ = (
        Index(
            "uq_identity_review_open_source_external",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("status IN ('pending', 'approved', 'rejected')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    wikidata_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dob: Mapped[Optional[object]] = mapped_column(Date, nullable=True)
    candidate_canonical_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    bout_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    bout_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    prior_mapping_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decision_canonical_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rule_id: Mapped[str] = mapped_column(String(128), default="")
    resolver_version: Mapped[str] = mapped_column(String(32), default="")
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class IdentityMatchEvidence(Base):
    """Immutable evidence for every link/merge/review/reversal."""

    __tablename__ = "identity_match_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    resolver_version: Mapped[str] = mapped_column(String(32))
    rule_id: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    wikidata_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dob: Mapped[Optional[object]] = mapped_column(Date, nullable=True)
    actor: Mapped[str] = mapped_column(String(128))
    before_canonical_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    after_canonical_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    review_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("identity_review_queue.id"), nullable=True, index=True
    )
    bout_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)


class IdentityScoringBlock(Base):
    """Blocks scoring only for affected evaluated/upcoming bouts."""

    __tablename__ = "identity_scoring_blocks"
    __table_args__ = (
        UniqueConstraint(
            "bout_id",
            "review_id",
            name="uq_identity_scoring_block_bout_review",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    bout_id: Mapped[str] = mapped_column(String(36), index=True)
    review_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("identity_review_queue.id"), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(128))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    evidence_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("identity_match_evidence.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    cleared_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
