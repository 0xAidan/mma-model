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
from mma_model.identity.adjudicated import (
    evaluate_frozen_adjudicated_fixture,
    load_adjudicated_cases,
)
from mma_model.identity.constants import ALLOWED_RESOLVE_SOURCES, RESOLVER_VERSION
from mma_model.sources.policy import load_source_policy

FROZEN_AUDIT_SERIES = frozenset({"dwcs"})
CONFLICT_RULE_IDS = (
    "identity_conflict_queue",
    "ambiguous_identity_queue",
    "same_normalized_name_queue",
)
FIXTURE_METRIC_KEYS = (
    "n",
    "denominator_all",
    "denominator_auto_eligible",
    "auto_true_pos",
    "auto_false_pos",
    "auto_false_neg",
    "precision",
    "recall",
    "queued",
    "queue_rate",
    "blocked",
    "blocked_rate",
    "coverage",
    "same_name_conflations",
    "fixture_status",
    "statistical_confidence_claim",
    "version",
    "case_file_hash",
    "label",
    "case_count",
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
    unscoped_pending: int
    unscoped_approved: int
    unscoped_rejected: int
    unscoped_pending_blocking: bool
    report_hash: str
    config_hash: str
    case_file_hash: str
    allowed_resolve_sources: tuple[str, ...]
    fixture_validation: dict[str, Any]

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
            "unscoped_pending": self.unscoped_pending,
            "unscoped_approved": self.unscoped_approved,
            "unscoped_rejected": self.unscoped_rejected,
            "unscoped_pending_blocking": self.unscoped_pending_blocking,
            "report_hash": self.report_hash,
            "config_hash": self.config_hash,
            "case_file_hash": self.case_file_hash,
            "allowed_resolve_sources": list(self.allowed_resolve_sources),
            "fixture_validation": dict(self.fixture_validation),
        }

    def human_summary(self) -> str:
        fixture = self.fixture_validation
        return (
            f"identity audit series={self.series or '*'} "
            f"fighters={self.canonical_fighter_count} espn={self.exact_espn_mappings} "
            f"pending={self.pending_reviews} conflicts={self.unresolved_conflicts} "
            f"blocks={self.upcoming_blocks} "
            f"unscoped_pending={self.unscoped_pending} "
            f"unscoped_approved={self.unscoped_approved} "
            f"unscoped_rejected={self.unscoped_rejected} "
            f"unscoped_pending_blocking={str(self.unscoped_pending_blocking).lower()} "
            f"fixture_n={fixture.get('n', 0)} "
            f"precision={float(fixture.get('precision') or 0.0):.6f} "
            f"recall={float(fixture.get('recall') or 0.0):.6f} "
            f"fixture_status={fixture.get('fixture_status', 'synthetic_explicit')} "
            f"synthetic no statistical confidence "
            f"hash={self.report_hash[:12]}"
        )


def _config_hash(*, series: str | None, case_file_hash: str, case_version: str) -> str:
    policy = load_source_policy()
    payload = {
        "policy_mode": policy.policy_mode,
        "identity_rules": policy.identity_rules.model_dump(mode="json"),
        "resolver_version": RESOLVER_VERSION,
        "allowed_resolve_sources": sorted(ALLOWED_RESOLVE_SOURCES),
        "adjudicated_cases_version": case_version,
        "adjudicated_cases_hash": case_file_hash,
        "series": series,
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
        }
    bout_ids = list(
        session.scalars(
            select(CanonicalBout.id).where(CanonicalBout.event_id.in_(event_ids))
        ).all()
    )
    fighter_ids = (
        list(
            session.scalars(
                select(BoutParticipant.fighter_id)
                .where(BoutParticipant.bout_id.in_(bout_ids))
                .distinct()
            ).all()
        )
        if bout_ids
        else []
    )
    return {
        "event_ids": event_ids,
        "bout_ids": bout_ids,
        "fighter_ids": fighter_ids,
    }


def _candidate_canonical_ids(review: IdentityReviewQueue) -> list[str]:
    try:
        raw = json.loads(review.candidate_canonical_ids_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if item]


def _partition_reviews(
    session: Session, scope: dict[str, Any] | None
) -> tuple[list[IdentityReviewQueue], list[IdentityReviewQueue]]:
    reviews = list(session.scalars(select(IdentityReviewQueue)).all())
    if scope is None:
        return reviews, []
    fighter_ids = set(scope["fighter_ids"])
    bout_ids = set(scope["bout_ids"])
    mapping_keys: set[tuple[str, str]] = set()
    if fighter_ids:
        mapping_keys = {
            (str(source), str(external_id))
            for source, external_id in session.execute(
                select(FighterSourceId.source, FighterSourceId.external_id).where(
                    FighterSourceId.fighter_id.in_(fighter_ids)
                )
            ).all()
        }
    scoped: list[IdentityReviewQueue] = []
    unscoped: list[IdentityReviewQueue] = []
    for review in reviews:
        bout_hit = review.bout_id is not None and review.bout_id in bout_ids
        mapped = (review.source, review.external_id) in mapping_keys
        candidate_hit = any(cid in fighter_ids for cid in _candidate_canonical_ids(review))
        if bout_hit or mapped or candidate_hit:
            scoped.append(review)
        else:
            unscoped.append(review)
    return scoped, unscoped


def _status_count(rows: list[IdentityReviewQueue], status: str) -> int:
    return sum(1 for row in rows if row.status == status)


def _review_fingerprint(rows: list[IdentityReviewQueue]) -> list[list[str]]:
    return sorted(
        [row.id, row.status, row.source, row.external_id, row.rule_id]
        for row in rows
    )


def _fixture_validation_payload(adjudicated: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "n": int(adjudicated.get("n") or 0),
        "case_count": int(adjudicated.get("n") or 0),
        "denominator_all": int(adjudicated.get("denominator_all") or 0),
        "denominator_auto_eligible": int(adjudicated.get("denominator_auto_eligible") or 0),
        "auto_true_pos": int(adjudicated.get("auto_true_pos") or 0),
        "auto_false_pos": int(adjudicated.get("auto_false_pos") or 0),
        "auto_false_neg": int(adjudicated.get("auto_false_neg") or 0),
        "precision": float(adjudicated.get("precision") or 0.0),
        "recall": float(adjudicated.get("recall") or 0.0),
        "queued": int(adjudicated.get("queued") or 0),
        "queue_rate": float(adjudicated.get("queue_rate") or 0.0),
        "blocked": int(adjudicated.get("blocked") or 0),
        "blocked_rate": float(adjudicated.get("blocked_rate") or 0.0),
        "coverage": float(adjudicated.get("coverage") or 0.0),
        "same_name_conflations": int(adjudicated.get("same_name_conflations") or 0),
        "fixture_status": str(adjudicated.get("fixture_status") or "synthetic_explicit"),
        "statistical_confidence_claim": False,
        "version": str(adjudicated.get("version") or ""),
        "case_file_hash": str(adjudicated.get("case_file_hash") or ""),
        "label": "synthetic_explicit",
    }
    for key in FIXTURE_METRIC_KEYS:
        if key not in payload:
            raise ValueError(f"fixture_validation missing {key}")
    return payload


def build_identity_audit(
    session: Session, *, series: str | None = None
) -> IdentityAuditReport:
    _assert_series_supported(session, series)
    scope = _series_scope(session, series)
    scoped_reviews, unscoped_reviews = _partition_reviews(session, scope)
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
        scoped_state = {
            "fighter_ids": sorted(
                session.scalars(select(CanonicalFighter.id)).all()
            ),
            "espn_external_ids": sorted(
                session.scalars(
                    select(FighterSourceId.external_id).where(
                        FighterSourceId.source == "espn"
                    )
                ).all()
            ),
        }
    else:
        fighter_ids = list(scope["fighter_ids"])
        bout_ids = list(scope["bout_ids"])
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
        scoped_review_ids = [row.id for row in scoped_reviews]
        if scoped_review_ids or bout_ids:
            stmt = select(func.count()).select_from(IdentityMatchEvidence)
            if scoped_review_ids and bout_ids:
                stmt = stmt.where(
                    (IdentityMatchEvidence.review_id.in_(scoped_review_ids))
                    | (IdentityMatchEvidence.bout_id.in_(bout_ids))
                )
            elif scoped_review_ids:
                stmt = stmt.where(IdentityMatchEvidence.review_id.in_(scoped_review_ids))
            else:
                stmt = stmt.where(IdentityMatchEvidence.bout_id.in_(bout_ids))
            evidence_rows = int(session.scalar(stmt) or 0)
        else:
            evidence_rows = 0
        scoped_state = {
            "fighter_ids": sorted(set(fighter_ids)),
            "bout_ids": sorted(set(bout_ids)),
            "espn_external_ids": sorted(
                session.scalars(
                    select(FighterSourceId.external_id).where(
                        FighterSourceId.source == "espn",
                        FighterSourceId.fighter_id.in_(fighter_ids),
                    )
                ).all()
            )
            if fighter_ids
            else [],
        }

    pending_reviews = _status_count(scoped_reviews, "pending")
    unresolved_conflicts = sum(
        1
        for row in scoped_reviews
        if row.status == "pending" and row.rule_id in CONFLICT_RULE_IDS
    )
    unscoped_pending = _status_count(unscoped_reviews, "pending")
    unscoped_approved = _status_count(unscoped_reviews, "approved")
    unscoped_rejected = _status_count(unscoped_reviews, "rejected")
    unscoped_pending_blocking = unscoped_pending > 0

    cases = load_adjudicated_cases()
    adjudicated = evaluate_frozen_adjudicated_fixture()
    fixture_validation = _fixture_validation_payload(adjudicated)
    case_file_hash = str(cases.get("case_file_hash") or fixture_validation["case_file_hash"])
    case_version = str(cases.get("version") or fixture_validation["version"])
    allowed_sources = tuple(sorted(ALLOWED_RESOLVE_SOURCES))
    config_hash = _config_hash(
        series=series, case_file_hash=case_file_hash, case_version=case_version
    )
    unscoped_evidence = (
        int(
            session.scalar(
                select(func.count())
                .select_from(IdentityMatchEvidence)
                .where(
                    IdentityMatchEvidence.review_id.in_(
                        [row.id for row in unscoped_reviews]
                    )
                )
            )
            or 0
        )
        if unscoped_reviews
        else 0
    )
    body = {
        "series": series,
        "resolver_version": RESOLVER_VERSION,
        "allowed_resolve_sources": list(allowed_sources),
        "canonical_fighter_count": canonical_fighter_count,
        "exact_espn_mappings": exact_espn_mappings,
        "pending_reviews": pending_reviews,
        "unresolved_conflicts": unresolved_conflicts,
        "upcoming_blocks": upcoming_blocks,
        "evidence_rows": evidence_rows,
        "unscoped_pending": unscoped_pending,
        "unscoped_approved": unscoped_approved,
        "unscoped_rejected": unscoped_rejected,
        "unscoped_pending_blocking": unscoped_pending_blocking,
        "unscoped_evidence_rows": unscoped_evidence,
        "scoped_reviews": _review_fingerprint(scoped_reviews),
        "unscoped_reviews": _review_fingerprint(unscoped_reviews),
        "scoped_state": scoped_state,
        "config_hash": config_hash,
        "case_file_hash": case_file_hash,
        "adjudicated_cases_version": case_version,
        "fixture_validation": fixture_validation,
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
        unscoped_pending=unscoped_pending,
        unscoped_approved=unscoped_approved,
        unscoped_rejected=unscoped_rejected,
        unscoped_pending_blocking=unscoped_pending_blocking,
        report_hash=report_hash,
        config_hash=config_hash,
        case_file_hash=case_file_hash,
        allowed_resolve_sources=allowed_sources,
        fixture_validation=fixture_validation,
    )
