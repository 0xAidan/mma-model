"""Replay frozen DWCS-307 policy over DWCS-306 evidence or the protocol fixture.

Replay never refits models and never tunes thresholds. Missing p25, P(EV>0),
or authoritative quotes become honest price-targets / no-bets.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from mma_model.backtest.gates import EvidenceTamperError
from mma_model.backtest.report import verify_evidence_payload
from mma_model.domain.markets import MarketFamily, MarketMaturity, OutcomeKey
from mma_model.domain.quote_eligibility import QUOTE_ELIGIBILITY_DECISION_VERSION
from mma_model.evaluation.contract import EvaluationContract, load_evaluation_contract
from mma_model.recommend.policy import (
    PRODUCTION_BOOTSTRAP_REFITS,
    ProbabilitySemantics,
    QuoteEvidence,
    QuoteSourceKind,
    RecommendationPolicy,
    SelectionCandidate,
    canonical_selection_id,
    load_recommendation_policy,
)
from mma_model.recommend.selector import select_recommendations

PROTOCOL_GENERATED_AT: Final = datetime(2024, 8, 13, 1, 0, tzinfo=UTC)
HASH_A: Final = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HASH_B: Final = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
HASH_C: Final = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
HASH_D: Final = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"


class RecommendReplayError(ValueError):
    """Replay cannot proceed (tamper, missing input, or contract failure)."""


def _aware(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RecommendReplayError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)
    text = str(value)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise RecommendReplayError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _eligible_quote(
    *,
    offered: float,
    observed_at: datetime,
    cutoff: datetime,
    stale: bool = False,
    suspended: bool = False,
    locked: bool = False,
    replaced: bool = False,
    ambiguous: bool = False,
    eligible: bool = True,
    lifecycle: str = "active",
    availability: str = "available",
    include_decision: bool = True,
) -> QuoteEvidence:
    identity = None
    version = None
    evaluated = None
    if include_decision:
        identity = "qe_v1:" + HASH_A
        version = QUOTE_ELIGIBILITY_DECISION_VERSION
        evaluated = cutoff
    return QuoteEvidence(
        offered_decimal=offered,
        source_kind=QuoteSourceKind.AUTOMATIC,
        observed_at=observed_at,
        cutoff=cutoff,
        bookmaker_key="protocol_book",
        region="us",
        eligibility_decision_identity=identity,
        eligibility_decision_version=version,
        eligibility_evaluated_at=evaluated,
        eligible=eligible,
        availability=availability,
        lifecycle=lifecycle,
        freshness_at=observed_at,
        stale=stale,
        suspended=suspended,
        locked=locked,
        replaced=replaced,
        ambiguous=ambiguous,
    )


def _candidate(
    *,
    event_id: str,
    bout_id: str,
    family: MarketFamily,
    outcome: OutcomeKey,
    p50: float,
    p25: float | None,
    policy: RecommendationPolicy,
    quote: QuoteEvidence | None = None,
    line_point: float | None = None,
    bootstrap_successful_count: int = PRODUCTION_BOOTSTRAP_REFITS,
    prob_ev_positive: float | None = 0.80,
    identity_resolved: bool = True,
    canonical_match: bool = True,
    ambiguous: bool = False,
    replacement: bool = False,
    data_quality_pass: bool = True,
    model_qualified: bool = True,
    calibrated: bool = True,
    market_maturity: MarketMaturity | None = None,
    probability_semantics: ProbabilitySemantics = ProbabilitySemantics.EXHAUSTIVE,
    p_win_unconditional: float | None = None,
    p_void: float | None = None,
    production_uncertainty: bool = True,
) -> SelectionCandidate:
    maturity = (
        policy.maturity_for(family) if market_maturity is None else market_maturity
    )
    return SelectionCandidate(
        event_id=event_id,
        bout_id=bout_id,
        selection_id=canonical_selection_id(
            event_id=event_id,
            bout_id=bout_id,
            family=family,
            outcome=outcome,
            line_point=line_point,
        ),
        family=family,
        outcome=outcome,
        line_point=line_point,
        p50=p50,
        p25=p25,
        probability_semantics=probability_semantics,
        bootstrap_successful_count=bootstrap_successful_count,
        bootstrap_seed=307001,
        estimator_hash=HASH_A,
        calibration_hash=HASH_B,
        data_hash=HASH_C,
        config_hash=HASH_D,
        identity_resolved=identity_resolved,
        canonical_match=canonical_match,
        ambiguous=ambiguous,
        replacement=replacement,
        data_quality_pass=data_quality_pass,
        model_qualified=model_qualified,
        calibrated=calibrated,
        market_maturity=maturity,
        p_win_unconditional=p_win_unconditional,
        p_void=p_void,
        evaluation_contract_hash=policy.evaluation_contract_hash,
        quote=quote,
        prob_ev_positive=prob_ev_positive,
        production_uncertainty=production_uncertainty,
    )


def protocol_candidates(policy: RecommendationPolicy) -> tuple[SelectionCandidate, ...]:
    """Deterministic fixture covering the frozen policy cases."""
    cutoff = PROTOCOL_GENERATED_AT
    on_time = datetime(2024, 8, 13, 0, 30, tzinfo=UTC)
    late = datetime(2024, 8, 13, 1, 30, tzinfo=UTC)
    # p50=0.50, p25=0.40 → actionable 2.5; p25 EV at 2.5 is exactly 0.
    rows = [
        _candidate(
            event_id="card-confirmed",
            bout_id="bout-confirmed",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.60, observed_at=on_time, cutoff=cutoff),
            prob_ev_positive=0.80,
        ),
        _candidate(
            event_id="card-tie",
            bout_id="bout-tie",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.60, observed_at=on_time, cutoff=cutoff),
            prob_ev_positive=0.80,
        ),
        _candidate(
            event_id="card-tie",
            bout_id="bout-tie",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_B,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.60, observed_at=on_time, cutoff=cutoff),
            prob_ev_positive=0.80,
        ),
        _candidate(
            event_id="card-unpriced",
            bout_id="bout-unpriced",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.55,
            p25=0.50,
            policy=policy,
            quote=None,
            prob_ev_positive=None,
        ),
        _candidate(
            event_id="card-unpriced",
            bout_id="bout-unpriced-two",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.60,
            p25=0.52,
            policy=policy,
            quote=None,
            prob_ev_positive=None,
        ),
        _candidate(
            event_id="card-unpriced",
            bout_id="bout-unpriced-two",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_B,
            p50=0.40,
            p25=0.35,
            policy=policy,
            quote=None,
            prob_ev_positive=None,
        ),
        _candidate(
            event_id="card-stale",
            bout_id="bout-stale",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(
                offered=2.80, observed_at=on_time, cutoff=cutoff, stale=True, lifecycle="stale"
            ),
            prob_ev_positive=0.90,
        ),
        _candidate(
            event_id="card-stale",
            bout_id="bout-post-cutoff",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.80, observed_at=late, cutoff=cutoff),
            prob_ev_positive=0.90,
        ),
        _candidate(
            event_id="card-ambiguous",
            bout_id="bout-ambiguous",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.80, observed_at=on_time, cutoff=cutoff),
            ambiguous=True,
            prob_ev_positive=0.90,
        ),
        _candidate(
            event_id="card-boundary",
            bout_id="bout-below",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.49, observed_at=on_time, cutoff=cutoff),
            prob_ev_positive=0.90,
        ),
        _candidate(
            event_id="card-boundary",
            bout_id="bout-exact-boundary",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.50, observed_at=on_time, cutoff=cutoff),
            prob_ev_positive=0.70,
        ),
        _candidate(
            event_id="card-nobet",
            bout_id="bout-gates",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.80,
            p25=0.70,
            policy=policy,
            quote=_eligible_quote(offered=5.00, observed_at=on_time, cutoff=cutoff),
            identity_resolved=False,
            prob_ev_positive=0.99,
        ),
        _candidate(
            event_id="card-nobet",
            bout_id="bout-experimental",
            family=MarketFamily.METHOD,
            outcome=OutcomeKey.KO_TKO,
            p50=0.30,
            p25=0.25,
            policy=policy,
            quote=_eligible_quote(offered=4.00, observed_at=on_time, cutoff=cutoff),
            market_maturity=MarketMaturity.EXPERIMENTAL,
            prob_ev_positive=0.90,
        ),
        _candidate(
            event_id="card-void",
            bout_id="bout-void",
            family=MarketFamily.MONEYLINE,
            outcome=OutcomeKey.FIGHTER_A,
            p50=0.50,
            p25=0.40,
            policy=policy,
            quote=_eligible_quote(offered=2.60, observed_at=on_time, cutoff=cutoff),
            probability_semantics=ProbabilitySemantics.CONDITIONAL_NONVOID,
            p_win_unconditional=0.48,
            p_void=0.04,
            prob_ev_positive=0.80,
        ),
    ]
    return tuple(rows)


def _parse_family(value: object) -> MarketFamily | None:
    try:
        return MarketFamily(str(value))
    except ValueError:
        return None


def _parse_outcome(value: object) -> OutcomeKey | None:
    try:
        return OutcomeKey(str(value))
    except ValueError:
        return None


def _quote_from_priced_row(
    row: Mapping[str, Any],
    *,
    cutoff: datetime,
) -> QuoteEvidence | None:
    offered = row.get("offered_decimal") or row.get("price_decimal")
    if offered is None:
        return None
    observed_raw = row.get("observed_at")
    if observed_raw is None:
        return None
    try:
        observed_at = _aware(observed_raw, field="observed_at")
        offered_value = float(offered)
    except (TypeError, ValueError, RecommendReplayError):
        return None
    eligible = bool(row.get("eligible", False))
    identity = row.get("eligibility_decision_identity")
    version = row.get("eligibility_decision_version")
    evaluated_raw = row.get("eligibility_evaluated_at") or row.get("evaluated_at")
    evaluated = None
    if evaluated_raw is not None:
        try:
            evaluated = _aware(evaluated_raw, field="eligibility_evaluated_at")
        except RecommendReplayError:
            evaluated = None
    lifecycle = str(row.get("lifecycle") or "unresolved")
    availability = str(row.get("availability") or "unknown")
    return QuoteEvidence(
        offered_decimal=offered_value,
        source_kind=(
            QuoteSourceKind.USER_OBSERVED
            if str(row.get("source_kind") or "") == QuoteSourceKind.USER_OBSERVED.value
            else QuoteSourceKind.AUTOMATIC
        ),
        observed_at=observed_at,
        cutoff=cutoff,
        bookmaker_key=None if row.get("bookmaker_key") is None else str(row.get("bookmaker_key")),
        region=None if row.get("region") is None else str(row.get("region")),
        eligibility_decision_identity=None if identity is None else str(identity),
        eligibility_decision_version=None if version is None else str(version),
        eligibility_evaluated_at=evaluated,
        eligible=eligible,
        availability=availability,
        lifecycle=lifecycle,
        freshness_at=observed_at,
        stale=lifecycle == "stale" or bool(row.get("stale", False)),
        suspended=availability == "suspended",
        locked=lifecycle == "locked",
        replaced=lifecycle == "replaced" or bool(row.get("is_replacement", False)),
        ambiguous=bool(row.get("is_ambiguous", False)),
    )


def candidates_from_backtest_payload(
    payload: Mapping[str, Any],
    policy: RecommendationPolicy,
) -> list[SelectionCandidate | dict[str, Any]]:
    """Map DWCS-306 attempts into recommendation candidates without inventing confidence."""
    hashes = payload.get("hashes") if isinstance(payload.get("hashes"), Mapping) else {}
    data_hash = str(hashes.get("data") or HASH_C)
    config_hash = str(hashes.get("config") or HASH_D)
    bootstrap = payload.get("bootstrap") if isinstance(payload.get("bootstrap"), Mapping) else {}
    n_replicates = bootstrap.get("n_replicates")
    try:
        refits = int(n_replicates) if n_replicates is not None else None
    except (TypeError, ValueError):
        refits = None
    rows: list[SelectionCandidate | dict[str, Any]] = []
    attempts = payload.get("attempts")
    if not isinstance(attempts, list):
        raise RecommendReplayError("backtest evidence attempts must be a list")
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            rows.append(
                {
                    "event_id": "",
                    "bout_id": "",
                    "family": "moneyline",
                    "outcome": "fighter_a",
                }
            )
            continue
        event_id = str(attempt.get("event_id") or "")
        bout_id = str(attempt.get("bout_id") or "")
        cutoff_raw = attempt.get("cutoff")
        try:
            cutoff = (
                _aware(cutoff_raw, field="cutoff")
                if cutoff_raw is not None
                else PROTOCOL_GENERATED_AT
            )
        except RecommendReplayError:
            cutoff = PROTOCOL_GENERATED_AT
        prediction = attempt.get("prediction")
        if not isinstance(prediction, Mapping):
            rows.append(
                {
                    "event_id": event_id,
                    "bout_id": bout_id,
                    "family": "moneyline",
                    "outcome": "fighter_a",
                    "p50": 0.5,
                    "estimator_hash": "not-a-hash",
                    "data_hash": data_hash,
                    "config_hash": config_hash,
                    "identity_resolved": False,
                    "data_quality_pass": False,
                    "model_qualified": False,
                    "calibrated": False,
                }
            )
            continue
        priced = attempt.get("priced_rows") if isinstance(attempt.get("priced_rows"), list) else []
        priced_by_key = {}
        for item in priced:
            if not isinstance(item, Mapping):
                continue
            key = (
                str(item.get("family") or item.get("market_family") or ""),
                str(item.get("outcome_key") or ""),
                item.get("line_point"),
            )
            priced_by_key[key] = item
        markets = prediction.get("markets")
        if not isinstance(markets, list) or not markets:
            rows.append(
                {
                    "event_id": event_id,
                    "bout_id": bout_id,
                    "family": "moneyline",
                    "outcome": "fighter_a",
                    "p50": prediction.get("p50"),
                    "p25": prediction.get("p25"),
                    "estimator_hash": prediction.get("estimator_hash"),
                    "calibration_hash": prediction.get("calibrator_hash"),
                    "data_hash": data_hash,
                    "config_hash": config_hash,
                    "identity_resolved": True,
                    "model_qualified": True,
                    "calibrated": prediction.get("calibrator_hash") is not None,
                    "bootstrap_successful_count": refits,
                }
            )
            continue
        estimator_hash = prediction.get("estimator_hash")
        calibration_hash = prediction.get("calibrator_hash")
        for market in markets:
            if not isinstance(market, Mapping):
                continue
            family = _parse_family(market.get("family"))
            outcome = _parse_outcome(market.get("outcome_key"))
            raw = {
                "event_id": event_id,
                "bout_id": bout_id,
                "family": None if family is None else family.value,
                "outcome": None if outcome is None else outcome.value,
                "line_point": market.get("line_point"),
                "p50": market.get("p50"),
                "p25": market.get("p25"),
                "estimator_hash": estimator_hash,
                "calibration_hash": calibration_hash,
                "data_hash": data_hash,
                "config_hash": config_hash,
                "identity_resolved": True,
                "canonical_match": True,
                "ambiguous": False,
                "replacement": False,
                "data_quality_pass": True,
                "model_qualified": True,
                "calibrated": calibration_hash is not None,
                "bootstrap_successful_count": refits,
                "prob_ev_positive": market.get("prob_ev_positive"),
                "evaluation_contract_hash": payload.get("hashes", {}).get("contract")
                if isinstance(payload.get("hashes"), Mapping)
                else None,
            }
            if family is None or outcome is None:
                rows.append(raw)
                continue
            key = (family.value, outcome.value, market.get("line_point"))
            priced_row = priced_by_key.get(key)
            quote = (
                None
                if priced_row is None
                else _quote_from_priced_row(priced_row, cutoff=cutoff)
            )
            semantics = ProbabilitySemantics.EXHAUSTIVE
            void_family = family in {
                MarketFamily.MONEYLINE,
                MarketFamily.METHOD,
                MarketFamily.FIGHTER_BY_METHOD,
            }
            if void_family and (
                market.get("p25_conditional") or market.get("draw_probability")
            ):
                semantics = ProbabilitySemantics.CONDITIONAL_NONVOID
            try:
                rows.append(
                    SelectionCandidate(
                        event_id=event_id,
                        bout_id=bout_id,
                        selection_id=canonical_selection_id(
                            event_id=event_id,
                            bout_id=bout_id,
                            family=family,
                            outcome=outcome,
                            line_point=None
                            if market.get("line_point") is None
                            else float(market["line_point"]),
                        ),
                        family=family,
                        outcome=outcome,
                        line_point=(
                            None
                            if market.get("line_point") is None
                            else float(market["line_point"])
                        ),
                        p50=float(market["p50"]),
                        p25=None if market.get("p25") is None else float(market["p25"]),
                        probability_semantics=semantics,
                        bootstrap_successful_count=refits,
                        bootstrap_seed=None,
                        estimator_hash=str(estimator_hash),
                        calibration_hash=(
                            None if calibration_hash is None else str(calibration_hash)
                        ),
                        data_hash=data_hash,
                        config_hash=config_hash,
                        identity_resolved=True,
                        canonical_match=True,
                        ambiguous=False,
                        replacement=False,
                        data_quality_pass=True,
                        model_qualified=True,
                        calibrated=calibration_hash is not None,
                        market_maturity=policy.maturity_for(family),
                        p_win_unconditional=None,
                        p_void=(
                            None
                            if market.get("draw_probability") is None
                            else float(market["draw_probability"])
                        ),
                        evaluation_contract_hash=(
                            str(payload["hashes"]["contract"])
                            if isinstance(payload.get("hashes"), Mapping)
                            and payload["hashes"].get("contract")
                            else None
                        ),
                        quote=quote,
                        prob_ev_positive=(
                            None
                            if market.get("prob_ev_positive") is None
                            else float(market["prob_ev_positive"])
                        ),
                        production_uncertainty=refits == PRODUCTION_BOOTSTRAP_REFITS,
                    )
                )
            except (TypeError, ValueError, KeyError):
                rows.append(raw)
    if not rows:
        rows.append({"event_id": "empty", "bout_id": "empty"})
    return rows


def load_backtest_evidence(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecommendReplayError(f"unable to read backtest evidence: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecommendReplayError("backtest evidence root must be an object")
    try:
        verify_evidence_payload(payload)
    except EvidenceTamperError as exc:
        raise RecommendReplayError(str(exc)) from exc
    return payload


def execute_recommend_replay(
    *,
    contract_path: Path | None = None,
    backtest_json: Path | None = None,
    fixture: str | None = None,
    policy: RecommendationPolicy | None = None,
    contract: EvaluationContract | None = None,
) -> dict[str, Any]:
    """Apply frozen policy to protocol candidates or verified DWCS-306 evidence."""
    if fixture == "protocol" and backtest_json is not None:
        raise RecommendReplayError("pass --fixture protocol or --backtest-json, not both")
    if fixture not in (None, "protocol"):
        raise RecommendReplayError(f"unsupported fixture: {fixture!r}")
    if fixture is None and backtest_json is None:
        raise RecommendReplayError("pass --fixture protocol or --backtest-json")
    resolved_contract = (
        contract if contract is not None else load_evaluation_contract(path=contract_path)
    )
    resolved_policy = (
        policy
        if policy is not None
        else load_recommendation_policy(contract=resolved_contract)
    )
    source_hash: str | None = None
    if fixture == "protocol":
        candidates: Sequence[Any] = protocol_candidates(resolved_policy)
    else:
        payload = load_backtest_evidence(Path(backtest_json))
        source_hash = str(payload.get("content_hash") or "")
        candidates = candidates_from_backtest_payload(payload, resolved_policy)
    report = select_recommendations(
        candidates, resolved_policy, source_backtest_hash=source_hash
    )
    return report.as_dict()


__all__ = [
    "RecommendReplayError",
    "candidates_from_backtest_payload",
    "execute_recommend_replay",
    "load_backtest_evidence",
    "protocol_candidates",
]
