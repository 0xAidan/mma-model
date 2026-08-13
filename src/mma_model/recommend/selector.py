"""Bout-level confirmed-value selection and global ranking (DWCS-307).

Evaluate every selection first. At most one confirmed pick per bout, ranked
by descending p25 EV then the frozen market-priority / identity tie-break.
Unpriced qualified rows become a watchlist; quoted failures stay no-bet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from mma_model.domain.markets import RecommendationState
from mma_model.quality.schema import sha256_canonical
from mma_model.recommend.policy import (
    NoBetReason,
    RecommendationPolicy,
    SelectionCandidate,
    SelectionDecision,
    coerce_candidate,
    evaluate_selection,
    malformed_no_bet,
)


def _line_sort(line_point: float | None) -> tuple[int, str]:
    if line_point is None:
        return (0, "")
    return (1, f"{float(line_point):.10f}")


def _family_index(policy: RecommendationPolicy, decision: SelectionDecision) -> int:
    if decision.family is None:
        return len(policy.market_priority) + 1
    return policy.market_priority_index(decision.family)


def _identity_key(decision: SelectionDecision) -> tuple[str, str, str, str, tuple[int, str]]:
    outcome = "" if decision.outcome is None else decision.outcome.value
    return (
        decision.event_id,
        decision.bout_id,
        outcome,
        decision.selection_id,
        _line_sort(decision.line_point),
    )


def confirmed_sort_key(
    decision: SelectionDecision, policy: RecommendationPolicy
) -> tuple[Any, ...]:
    ev = decision.p25_ev
    ev_key = float("-inf") if ev is None else float(ev)
    return (
        -ev_key,
        _family_index(policy, decision),
        *_identity_key(decision),
    )


def watchlist_sort_key(
    decision: SelectionDecision, policy: RecommendationPolicy
) -> tuple[Any, ...]:
    maturity = 0
    if decision.family is not None:
        configured = policy.maturity_for(decision.family)
        maturity = 0 if configured.value == "qualified" else 1
    confidence = float("-inf") if decision.p50 is None else -float(decision.p50)
    return (
        maturity,
        _family_index(policy, decision),
        confidence,
        *_identity_key(decision),
    )


def no_bet_sort_key(decision: SelectionDecision) -> tuple[str, str, str]:
    return (decision.event_id, decision.bout_id, decision.selection_id)


def _candidate_fingerprint(candidate: SelectionCandidate) -> str:
    quote = candidate.quote
    quote_payload: dict[str, Any] | None = None
    if quote is not None:
        quote_payload = {
            "bookmaker_key": quote.bookmaker_key,
            "eligible": quote.eligible,
            "observed_at": quote.observed_at.isoformat(),
            "offered_decimal": quote.offered_decimal,
            "source_kind": quote.source_kind.value,
        }
    return sha256_canonical(
        {
            "bootstrap_seed": candidate.bootstrap_seed,
            "bootstrap_successful_count": candidate.bootstrap_successful_count,
            "calibration_hash": candidate.calibration_hash,
            "config_hash": candidate.config_hash,
            "data_hash": candidate.data_hash,
            "estimator_hash": candidate.estimator_hash,
            "p25": candidate.p25,
            "p50": candidate.p50,
            "p_void": candidate.p_void,
            "p_win_unconditional": candidate.p_win_unconditional,
            "prob_ev_positive": candidate.prob_ev_positive,
            "probability_semantics": candidate.probability_semantics.value,
            "quote": quote_payload,
        }
    )


def _dedupe_candidates(candidates: Sequence[SelectionCandidate]) -> list[SelectionCandidate]:
    grouped: dict[str, list[SelectionCandidate]] = {}
    for item in candidates:
        grouped.setdefault(item.selection_id, []).append(item)
    out: list[SelectionCandidate] = []
    for selection_id in sorted(grouped):
        ordered = sorted(
            grouped[selection_id],
            key=lambda item: (
                _candidate_fingerprint(item),
                item.event_id,
                item.bout_id,
                item.selection_id,
            ),
        )
        fingerprints = {_candidate_fingerprint(item) for item in ordered}
        if len(fingerprints) == 1:
            out.append(ordered[0])
            continue
        out.append(replace(ordered[0], ambiguous=True))
    return out


def _bout_key(decision: SelectionDecision) -> tuple[str, str]:
    return (decision.event_id, decision.bout_id)


@dataclass(frozen=True)
class RecommendationReport:
    confirmed_value: tuple[SelectionDecision, ...]
    price_target_watchlist: tuple[SelectionDecision, ...]
    no_bet: tuple[SelectionDecision, ...]
    policy_hash: str
    contract_hash: str
    source_backtest_hash: str | None
    content_hash: str
    counts: Mapping[str, int]
    reason_taxonomy: Mapping[str, int]
    priced_policy: Mapping[str, Any]
    unpriced_target_coverage: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "confirmed_value": [row.as_dict() for row in self.confirmed_value],
            "content_hash": self.content_hash,
            "contract_hash": self.contract_hash,
            "counts": dict(self.counts),
            "no_bet": [row.as_dict() for row in self.no_bet],
            "policy_hash": self.policy_hash,
            "price_target_watchlist": [row.as_dict() for row in self.price_target_watchlist],
            "priced_policy": dict(self.priced_policy),
            "reason_taxonomy": dict(self.reason_taxonomy),
            "source_backtest_hash": self.source_backtest_hash,
            "ticket": "DWCS-307",
            "unpriced_target_coverage": dict(self.unpriced_target_coverage),
        }


def _mark_lower_ranked(
    decision: SelectionDecision, policy: RecommendationPolicy
) -> SelectionDecision:
    reasons = (*decision.reasons, NoBetReason.LOWER_RANKED_ELIGIBLE_SELECTION)
    return replace(
        decision,
        classification=RecommendationState.NO_BET,
        reasons=reasons,
        primary_reason=policy.primary_reason(reasons),
        confirmed_rank=None,
        primary_price_target=False,
    )


def _mark_secondary(decision: SelectionDecision, rank: int) -> SelectionDecision:
    reasons = (*decision.reasons, NoBetReason.SECONDARY_PRICE_TARGET)
    return replace(
        decision,
        reasons=reasons,
        primary_reason=NoBetReason.SECONDARY_PRICE_TARGET,
        primary_price_target=False,
        watchlist_rank=rank,
    )


def _select_bout(
    decisions: Sequence[SelectionDecision],
    policy: RecommendationPolicy,
) -> tuple[SelectionDecision, ...]:
    ordered = tuple(
        sorted(decisions, key=lambda row: (row.selection_id, row.event_id, row.bout_id))
    )
    confirmed = [
        row
        for row in ordered
        if row.classification is RecommendationState.CONFIRMED_VALUE
    ]
    if confirmed:
        ranked = sorted(confirmed, key=lambda row: confirmed_sort_key(row, policy))
        winner = replace(ranked[0], confirmed_rank=1, primary_price_target=False)
        demoted = [_mark_lower_ranked(row, policy) for row in ranked[1:]]
        rest = [
            row
            for row in ordered
            if row.classification is not RecommendationState.CONFIRMED_VALUE
        ]
        # Quoted failures stay no_bet; unpriced rows on a confirmed bout stay
        # watchlist-eligible only if they were price_target. A confirmed bout
        # still emits those unpriced targets as secondary watchlist rows.
        return (winner, *demoted, *rest)

    targets = [
        row for row in ordered if row.classification is RecommendationState.PRICE_TARGET
    ]
    rest = [
        row for row in ordered if row.classification is not RecommendationState.PRICE_TARGET
    ]
    if not targets and not rest:
        first = ordered[0]
        return (
            malformed_no_bet(
                event_id=first.event_id,
                bout_id=first.bout_id,
                selection_id=f"{first.event_id}/{first.bout_id}/bout",
                policy=policy,
                detail="bout produced no selections",
            ),
        )
    if not targets:
        return tuple(rest)
    ranked_targets = sorted(targets, key=lambda row: watchlist_sort_key(row, policy))
    primary = replace(ranked_targets[0], primary_price_target=True, watchlist_rank=1)
    secondary = [
        _mark_secondary(row, rank=index)
        for index, row in enumerate(ranked_targets[1:], start=2)
    ]
    return (primary, *secondary, *rest)


def _taxonomy(decisions: Sequence[SelectionDecision]) -> dict[str, int]:
    counts: dict[str, int] = {reason.value: 0 for reason in NoBetReason}
    for row in decisions:
        for reason in row.reasons:
            counts[reason.value] = counts[reason.value] + 1
        if row.primary_reason is not None and row.classification is RecommendationState.NO_BET:
            key = f"primary:{row.primary_reason.value}"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _priced_policy_block(decisions: Sequence[SelectionDecision]) -> dict[str, Any]:
    confirmed = [
        row for row in decisions if row.classification is RecommendationState.CONFIRMED_VALUE
    ]
    quoted_no_bet = [
        row
        for row in decisions
        if row.classification is RecommendationState.NO_BET and row.offered_decimal is not None
    ]
    return {
        "confirmed_count": len(confirmed),
        "quoted_no_bet_count": len(quoted_no_bet),
        "mean_p25_ev": (
            None
            if not confirmed
            else sum(float(row.p25_ev or 0.0) for row in confirmed) / len(confirmed)
        ),
        "profit": None,
        "roi": None,
        "clv": None,
        "note": "priced policy metrics exclude unpriced targets; no staking",
    }


def _unpriced_coverage_block(decisions: Sequence[SelectionDecision]) -> dict[str, Any]:
    targets = [
        row for row in decisions if row.classification is RecommendationState.PRICE_TARGET
    ]
    primary = [row for row in targets if row.primary_price_target]
    missing_thresholds = [row for row in targets if row.thresholds is None]
    return {
        "primary_count": len(primary),
        "secondary_count": len(targets) - len(primary),
        "target_count": len(targets),
        "rows_missing_thresholds": len(missing_thresholds),
        "profit": None,
        "roi": None,
        "clv": None,
        "unpriced_target_is_not_best_available_market": True,
    }


def attach_report_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in payload.items() if key != "content_hash"}
    digest = sha256_canonical(body)
    attached = dict(body)
    attached["content_hash"] = digest
    return attached


def select_recommendations(
    candidates: Sequence[SelectionCandidate | SelectionDecision | Mapping[str, Any]],
    policy: RecommendationPolicy,
    *,
    source_backtest_hash: str | None = None,
) -> RecommendationReport:
    """Evaluate every input, then apply the frozen one-pick policy."""
    coerced_candidates: list[SelectionCandidate] = []
    evaluated: list[SelectionDecision] = []
    for item in candidates:
        if isinstance(item, SelectionDecision):
            evaluated.append(item)
            continue
        coerced = coerce_candidate(item, policy)
        if isinstance(coerced, SelectionDecision):
            evaluated.append(coerced)
            continue
        coerced_candidates.append(coerced)
    for candidate in _dedupe_candidates(coerced_candidates):
        evaluated.append(evaluate_selection(candidate, policy))

    by_bout: dict[tuple[str, str], list[SelectionDecision]] = {}
    for row in evaluated:
        by_bout.setdefault(_bout_key(row), []).append(row)

    selected: list[SelectionDecision] = []
    for key in sorted(by_bout):
        selected.extend(_select_bout(by_bout[key], policy))

    confirmed = tuple(
        replace(row, confirmed_rank=index)
        for index, row in enumerate(
            sorted(
                [
                    row
                    for row in selected
                    if row.classification is RecommendationState.CONFIRMED_VALUE
                ],
                key=lambda row: confirmed_sort_key(row, policy),
            ),
            start=1,
        )
    )
    watchlist = tuple(
        replace(row, watchlist_rank=index)
        for index, row in enumerate(
            sorted(
                [
                    row
                    for row in selected
                    if row.classification is RecommendationState.PRICE_TARGET
                ],
                key=lambda row: watchlist_sort_key(row, policy),
            ),
            start=1,
        )
    )
    no_bet = tuple(
        sorted(
            [row for row in selected if row.classification is RecommendationState.NO_BET],
            key=no_bet_sort_key,
        )
    )
    combined = (*confirmed, *watchlist, *no_bet)
    counts = {
        "confirmed_value": len(confirmed),
        "no_bet": len(no_bet),
        "price_target": len(watchlist),
        "selections": len(combined),
        "bouts": len(by_bout),
    }
    draft = {
        "confirmed_value": [row.as_dict() for row in confirmed],
        "contract_hash": policy.evaluation_contract_hash,
        "counts": counts,
        "no_bet": [row.as_dict() for row in no_bet],
        "policy_hash": policy.content_hash,
        "price_target_watchlist": [row.as_dict() for row in watchlist],
        "priced_policy": _priced_policy_block(combined),
        "reason_taxonomy": _taxonomy(combined),
        "source_backtest_hash": source_backtest_hash,
        "ticket": "DWCS-307",
        "unpriced_target_coverage": _unpriced_coverage_block(combined),
    }
    attached = attach_report_hash(draft)
    return RecommendationReport(
        confirmed_value=confirmed,
        price_target_watchlist=watchlist,
        no_bet=no_bet,
        policy_hash=policy.content_hash,
        contract_hash=policy.evaluation_contract_hash,
        source_backtest_hash=source_backtest_hash,
        content_hash=str(attached["content_hash"]),
        counts=counts,
        reason_taxonomy=attached["reason_taxonomy"],
        priced_policy=attached["priced_policy"],
        unpriced_target_coverage=attached["unpriced_target_coverage"],
    )
