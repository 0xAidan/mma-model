"""Deterministic identity audit reports (DWCS-104)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import CanonicalFighter, FighterSourceId
from mma_model.db.tables.identity import (
    IdentityMatchEvidence,
    IdentityReviewQueue,
    IdentityScoringBlock,
)
from mma_model.identity.constants import RESOLVER_VERSION
from mma_model.sources.policy import load_source_policy


@dataclass(frozen=True)
class IdentityAuditReport:
    series: str | None
    resolver_version: str
    canonical_fighter_count: int
    exact_espn_mappings: int
    pending_reviews: int
    unresolved_conflicts: int
    upcoming_blocks: int
    evidence_rows: int
    report_hash: str
    config_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "resolver_version": self.resolver_version,
            "canonical_fighter_count": self.canonical_fighter_count,
            "exact_espn_mappings": self.exact_espn_mappings,
            "pending_reviews": self.pending_reviews,
            "unresolved_conflicts": self.unresolved_conflicts,
            "upcoming_blocks": self.upcoming_blocks,
            "evidence_rows": self.evidence_rows,
            "report_hash": self.report_hash,
            "config_hash": self.config_hash,
        }

    def human_summary(self) -> str:
        return (
            f"identity audit series={self.series or '*'} "
            f"fighters={self.canonical_fighter_count} espn={self.exact_espn_mappings} "
            f"pending={self.pending_reviews} conflicts={self.unresolved_conflicts} "
            f"blocks={self.upcoming_blocks} hash={self.report_hash[:12]}"
        )


def _config_hash() -> str:
    policy = load_source_policy()
    payload = {
        "policy_mode": policy.policy_mode,
        "identity_rules": policy.identity_rules.model_dump(mode="json"),
        "resolver_version": RESOLVER_VERSION,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def build_identity_audit(
    session: Session, *, series: str | None = None
) -> IdentityAuditReport:
    canonical_fighter_count = int(
        session.scalar(select(func.count()).select_from(CanonicalFighter)) or 0
    )
    exact_espn_mappings = int(
        session.scalar(
            select(func.count()).select_from(FighterSourceId).where(
                FighterSourceId.source == "espn"
            )
        )
        or 0
    )
    pending_reviews = int(
        session.scalar(
            select(func.count())
            .select_from(IdentityReviewQueue)
            .where(IdentityReviewQueue.status == "pending")
        )
        or 0
    )
    unresolved_conflicts = int(
        session.scalar(
            select(func.count())
            .select_from(IdentityReviewQueue)
            .where(
                IdentityReviewQueue.status == "pending",
                IdentityReviewQueue.rule_id.in_(
                    (
                        "identity_conflict_queue",
                        "ambiguous_identity_queue",
                        "same_normalized_name_queue",
                    )
                ),
            )
        )
        or 0
    )
    upcoming_blocks = int(
        session.scalar(
            select(func.count())
            .select_from(IdentityScoringBlock)
            .where(IdentityScoringBlock.active.is_(True))
        )
        or 0
    )
    evidence_rows = int(
        session.scalar(select(func.count()).select_from(IdentityMatchEvidence)) or 0
    )
    config_hash = _config_hash()
    body = {
        "series": series,
        "resolver_version": RESOLVER_VERSION,
        "canonical_fighter_count": canonical_fighter_count,
        "exact_espn_mappings": exact_espn_mappings,
        "pending_reviews": pending_reviews,
        "unresolved_conflicts": unresolved_conflicts,
        "upcoming_blocks": upcoming_blocks,
        "evidence_rows": evidence_rows,
        "config_hash": config_hash,
    }
    report_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return IdentityAuditReport(
        series=series,
        resolver_version=RESOLVER_VERSION,
        canonical_fighter_count=canonical_fighter_count,
        exact_espn_mappings=exact_espn_mappings,
        pending_reviews=pending_reviews,
        unresolved_conflicts=unresolved_conflicts,
        upcoming_blocks=upcoming_blocks,
        evidence_rows=evidence_rows,
        report_hash=report_hash,
        config_hash=config_hash,
    )
