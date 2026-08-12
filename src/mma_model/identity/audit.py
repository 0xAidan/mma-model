"""Deterministic identity audit reports (DWCS-104)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterSourceId,
)
from mma_model.db.tables.identity import (
    IdentityMatchEvidence,
    IdentityReviewQueue,
    IdentityScoringBlock,
)
from mma_model.identity.adjudicated import evaluate_frozen_adjudicated_fixture
from mma_model.identity.constants import RESOLVER_VERSION
from mma_model.sources.policy import load_source_policy

FROZEN_AUDIT_SERIES = frozenset({"dwcs"})
CONFLICT_RULE_IDS = (
    "identity_conflict_queue",
    "ambiguous_identity_queue",
    "same_normalized_name_queue",
)


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
    denominator_all: int
    denominator_auto_eligible: int
    auto_true_pos: int
    auto_false_pos: int
    auto_false_neg: int
    precision: float
    recall: float
    queued: int
    queue_rate: float
    blocked: int
    blocked_rate: float
    coverage: float
    same_name_conflations: int

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
            "denominator_all": self.denominator_all,
            "denominator_auto_eligible": self.denominator_auto_eligible,
            "auto_true_pos": self.auto_true_pos,
            "auto_false_pos": self.auto_false_pos,
            "auto_false_neg": self.auto_false_neg,
            "precision": self.precision,
            "recall": self.recall,
            "queued": self.queued,
            "queue_rate": self.queue_rate,
            "blocked": self.blocked,
            "blocked_rate": self.blocked_rate,
            "coverage": self.coverage,
            "same_name_conflations": self.same_name_conflations,
        }

    def human_summary(self) -> str:
        return (
            f"identity audit series={self.series or '*'} "
            f"denominator_all={self.denominator_all} "
            f"denominator_auto_eligible={self.denominator_auto_eligible} "
            f"TP={self.auto_true_pos} FP={self.auto_false_pos} FN={self.auto_false_neg} "
            f"precision={self.precision:.6f} recall={self.recall:.6f} "
            f"queued={self.queued} queue_rate={self.queue_rate:.6f} "
            f"blocked={self.blocked} blocked_rate={self.blocked_rate:.6f} "
            f"coverage={self.coverage:.6f} "
            f"same_name_conflations={self.same_name_conflations} "
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


def _assert_series_supported(session: Session, series: str | None) -> None:
    if series is None:
        return
    present = {
        value
        for value in session.scalars(select(CanonicalEvent.series).distinct()).all()
        if value
    }
    allowed = FROZEN_AUDIT_SERIES | present
    if series not in allowed:
        raise ValueError(f"unsupported series: {series}")


def _event_series_clause(series: str):
    if series == "dwcs":
        return or_(
            CanonicalEvent.series == "dwcs",
            CanonicalEvent.series.startswith("dwcs_"),
        )
    return CanonicalEvent.series == series


def _series_scope(session: Session, series: str | None) -> dict[str, Any] | None:
    if series is None:
        return None
    event_ids = list(
        session.scalars(
            select(CanonicalEvent.id).where(_event_series_clause(series))
        ).all()
    )
    if not event_ids:
        return {
            "event_ids": [],
            "bout_ids": [],
            "fighter_ids": [],
            "review_ids": [],
        }
    bout_ids = list(
        session.scalars(
            select(CanonicalBout.id).where(CanonicalBout.event_id.in_(event_ids))
        ).all()
    )
    fighter_ids = list(
        session.scalars(
            select(BoutParticipant.fighter_id)
            .where(BoutParticipant.bout_id.in_(bout_ids))
            .distinct()
        ).all()
    ) if bout_ids else []
    review_ids = list(
        session.scalars(
            select(IdentityReviewQueue.id).where(
                IdentityReviewQueue.bout_id.in_(bout_ids)
            )
        ).all()
    ) if bout_ids else []
    return {
        "event_ids": event_ids,
        "bout_ids": bout_ids,
        "fighter_ids": fighter_ids,
        "review_ids": review_ids,
    }


def build_identity_audit(
    session: Session, *, series: str | None = None
) -> IdentityAuditReport:
    _assert_series_supported(session, series)
    scope = _series_scope(session, series)
    if scope is None:
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
                    IdentityReviewQueue.rule_id.in_(CONFLICT_RULE_IDS),
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
    else:
        fighter_ids = scope["fighter_ids"]
        bout_ids = scope["bout_ids"]
        review_ids = scope["review_ids"]
        canonical_fighter_count = len(set(fighter_ids))
        exact_espn_mappings = (
            int(
                session.scalar(
                    select(func.count())
                    .select_from(FighterSourceId)
                    .where(
                        FighterSourceId.source == "espn",
                        FighterSourceId.fighter_id.in_(fighter_ids),
                    )
                )
                or 0
            )
            if fighter_ids
            else 0
        )
        pending_reviews = (
            int(
                session.scalar(
                    select(func.count())
                    .select_from(IdentityReviewQueue)
                    .where(
                        IdentityReviewQueue.status == "pending",
                        IdentityReviewQueue.id.in_(review_ids),
                    )
                )
                or 0
            )
            if review_ids
            else 0
        )
        unresolved_conflicts = (
            int(
                session.scalar(
                    select(func.count())
                    .select_from(IdentityReviewQueue)
                    .where(
                        IdentityReviewQueue.status == "pending",
                        IdentityReviewQueue.rule_id.in_(CONFLICT_RULE_IDS),
                        IdentityReviewQueue.id.in_(review_ids),
                    )
                )
                or 0
            )
            if review_ids
            else 0
        )
        upcoming_blocks = (
            int(
                session.scalar(
                    select(func.count())
                    .select_from(IdentityScoringBlock)
                    .where(
                        IdentityScoringBlock.active.is_(True),
                        IdentityScoringBlock.bout_id.in_(bout_ids),
                    )
                )
                or 0
            )
            if bout_ids
            else 0
        )
        if review_ids or bout_ids:
            stmt = select(func.count()).select_from(IdentityMatchEvidence)
            if review_ids and bout_ids:
                stmt = stmt.where(
                    (IdentityMatchEvidence.review_id.in_(review_ids))
                    | (IdentityMatchEvidence.bout_id.in_(bout_ids))
                )
            elif review_ids:
                stmt = stmt.where(IdentityMatchEvidence.review_id.in_(review_ids))
            else:
                stmt = stmt.where(IdentityMatchEvidence.bout_id.in_(bout_ids))
            evidence_rows = int(session.scalar(stmt) or 0)
        else:
            evidence_rows = 0

    adjudicated = evaluate_frozen_adjudicated_fixture()
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
        "denominator_all": adjudicated["denominator_all"],
        "denominator_auto_eligible": adjudicated["denominator_auto_eligible"],
        "auto_true_pos": adjudicated["auto_true_pos"],
        "auto_false_pos": adjudicated["auto_false_pos"],
        "auto_false_neg": adjudicated["auto_false_neg"],
        "precision": adjudicated["precision"],
        "recall": adjudicated["recall"],
        "queued": adjudicated["queued"],
        "queue_rate": adjudicated["queue_rate"],
        "blocked": adjudicated["blocked"],
        "blocked_rate": adjudicated["blocked_rate"],
        "coverage": adjudicated["coverage"],
        "same_name_conflations": adjudicated["same_name_conflations"],
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
        denominator_all=int(adjudicated["denominator_all"]),
        denominator_auto_eligible=int(adjudicated["denominator_auto_eligible"]),
        auto_true_pos=int(adjudicated["auto_true_pos"]),
        auto_false_pos=int(adjudicated["auto_false_pos"]),
        auto_false_neg=int(adjudicated["auto_false_neg"]),
        precision=float(adjudicated["precision"]),
        recall=float(adjudicated["recall"]),
        queued=int(adjudicated["queued"]),
        queue_rate=float(adjudicated["queue_rate"]),
        blocked=int(adjudicated["blocked"]),
        blocked_rate=float(adjudicated["blocked_rate"]),
        coverage=float(adjudicated["coverage"]),
        same_name_conflations=int(adjudicated["same_name_conflations"]),
    )
