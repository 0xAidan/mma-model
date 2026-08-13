"""Bookmaker adapter audit / fallback report (DWCS-202)."""

from __future__ import annotations

from typing import Any

from mma_model.domain.markets import MarketFamily, MarketMaturity, OutcomeKey
from mma_model.odds.manual_price import MANUAL_SOURCE_LABEL
from mma_model.odds.price_guidance import build_unpriced_price_targets
from mma_model.odds.provider_decision import (
    PINNED_ODDS_DECISION_HASH,
    assert_no_sportsbook_scraper_modules,
    load_odds_decision_contract,
    load_phase0_odds_decision,
)


def run_bookmaker_audit(*, next_dwcs: bool = False) -> dict[str, Any]:
    """Report Phase 0 decision honesty and sportsbook-agnostic fallback readiness.

    Does not invent a licensed bookmaker adapter when Phase 0 did not authorize one.
    """
    decision = load_phase0_odds_decision()
    config = load_odds_decision_contract()
    scraper_ok = True
    scraper_error: str | None = None
    try:
        assert_no_sportsbook_scraper_modules()
    except AssertionError as exc:
        scraper_ok = False
        scraper_error = str(exc)

    sample_rows = build_unpriced_price_targets(
        [
            {
                "market_family": MarketFamily.MONEYLINE,
                "outcome_key": OutcomeKey.FIGHTER_A,
                "maturity": MarketMaturity.QUALIFIED,
                "p50": 0.55,
                "p25": 0.50,
                "gates_pass": True,
            },
            {
                "market_family": MarketFamily.MONEYLINE,
                "outcome_key": OutcomeKey.FIGHTER_B,
                "maturity": MarketMaturity.QUALIFIED,
                "p50": 0.45,
                "p25": 0.40,
                "gates_pass": True,
            },
        ]
    )

    return {
        "ticket": "DWCS-202",
        "licensed_bookmaker_adapter_authorized": (
            decision.licensed_bookmaker_adapter_authorized
        ),
        "automated_bookmaker_adapter": None,
        "decision": {
            "path": decision.path,
            "bet365_dwcs_status": decision.bet365_dwcs_status,
            "rationale": decision.rationale,
            "evidence_path": decision.evidence_path,
            "trial_providers": dict(decision.trial_providers),
            "contract_version": decision.contract_version,
            "content_hash": decision.content_hash,
            "pinned_hash": PINNED_ODDS_DECISION_HASH,
        },
        "config_path": "config/sources/odds.yaml",
        "packaged_contract": "mma_model/odds/odds_decision_v1.yaml",
        "fallback": {
            "sportsbook_agnostic_required": True,
            "manual_observation_source": MANUAL_SOURCE_LABEL,
            "manual_observation_automated": False,
            "exact_ev_requires_observed_price": True,
            "label": config.manual_observation.source_label,
        },
        "prohibited": list(config.prohibited),
        "odds_package_scraper_heuristic": {
            "scope": "mma_model.odds package modules only",
            "hit": not scraper_ok,
            "error": scraper_error,
            "note": (
                "Filename/text heuristics are a scoped package check, not proof "
                "that no scraper exists elsewhere in the repository."
            ),
        },
        "scraper_paths_present": not scraper_ok,
        "next_dwcs": {
            "requested": bool(next_dwcs),
            "note": (
                "Bout matching remains DWCS-203. This audit proves the fallback "
                "path and Phase 0 honesty without inventing licensed adapters."
            ),
        },
        "sample_price_targets": [row.as_dict() for row in sample_rows],
        "product_note": (
            "Exact bookmaker lines are optional enrichment. Sportsbook-agnostic "
            "fair / actionable / strong-value guidance is mandatory. Exact EV "
            "only after a timestamped observed or user_observed price on a "
            "qualified/gates-pass selection. Reference odds are never mislabeled "
            "as Bet365."
        ),
    }
