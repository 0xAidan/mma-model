"""DWCS-202: licensed-adapter gate, manual prices, and actionable price fallback."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from mma_model.db.base import Base
from mma_model.db.odds_guards import install_odds_sqlite_guards
from mma_model.db.session import sqlite_connect_pragmas
from mma_model.db.tables.odds import OddsManualPriceObservation
from mma_model.domain.markets import (
    MarketFamily,
    MarketMaturity,
    OutcomeKey,
    RecommendationState,
)
from mma_model.odds.bookmaker_audit import run_bookmaker_audit
from mma_model.odds.manual_price import (
    MANUAL_SOURCE_LABEL,
    EntitlementFailure,
    LineLifecycleState,
    ObservedPrice,
    PriceSourceKind,
    compute_exact_ev,
    parse_manual_price_observation,
)
from mma_model.odds.price_guidance import (
    build_price_guidance,
    build_unpriced_price_targets,
)
from mma_model.odds.provider_decision import (
    DECISION_PATH_REFERENCE_FALLBACK,
    LicensedBookmakerAdapterError,
    assert_no_sportsbook_scraper_modules,
    licensed_bookmaker_adapter_authorized,
    load_odds_source_config,
    load_phase0_odds_decision,
    require_licensed_bookmaker_adapter,
)
from mma_model.odds.store import OddsQuoteStore

OBSERVED = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


def _session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'odds202.db'}", future=True)
    event.listen(engine, "connect", sqlite_connect_pragmas)
    import mma_model.db.tables.odds  # noqa: F401

    Base.metadata.create_all(bind=engine)
    install_odds_sqlite_guards(engine)
    return sessionmaker(bind=engine, future=True)()


def test_phase0_decision_rejects_licensed_bookmaker_adapter() -> None:
    decision = load_phase0_odds_decision()
    assert decision.path == DECISION_PATH_REFERENCE_FALLBACK
    assert decision.bet365_dwcs_status == "scoped_absent"
    assert decision.licensed_bookmaker_adapter_authorized is False
    assert licensed_bookmaker_adapter_authorized() is False
    with pytest.raises(LicensedBookmakerAdapterError, match="not authorized"):
        require_licensed_bookmaker_adapter("opticodds")
    with pytest.raises(LicensedBookmakerAdapterError, match="not authorized"):
        require_licensed_bookmaker_adapter("bet365")


def test_odds_yaml_matches_committed_phase0_evidence() -> None:
    cfg = load_odds_source_config()
    decision = load_phase0_odds_decision()
    assert cfg["decision"]["path"] == decision.path
    assert cfg["decision"]["licensed_bookmaker_adapter_authorized"] is False
    assert cfg["manual_observation"]["source_label"] == MANUAL_SOURCE_LABEL
    assert cfg["manual_observation"]["automated"] is False
    assert "sportsbook_website_scraping" in cfg["prohibited"]
    assert cfg["reference_odds"]["never_label_as_bet365"] is True


def test_no_sportsbook_scraper_paths_in_odds_package() -> None:
    assert_no_sportsbook_scraper_modules()


def test_unpriced_qualified_selection_emits_fair_actionable_strong() -> None:
    rows = build_unpriced_price_targets(
        [
            {
                "market_family": MarketFamily.MONEYLINE,
                "outcome_key": OutcomeKey.FIGHTER_A,
                "maturity": MarketMaturity.QUALIFIED,
                "p50": 0.55,
                "p25": 0.50,
                "gates_pass": True,
            }
        ]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.recommendation.state is RecommendationState.PRICE_TARGET
    assert row.thresholds is not None
    assert row.thresholds.fair_decimal == pytest.approx(1.0 / 0.55)
    assert row.thresholds.actionable_decimal > row.thresholds.fair_decimal
    assert row.thresholds.strong_value_decimal >= row.thresholds.actionable_decimal
    assert row.exact_ev is None
    assert row.exact_ev_available is False
    assert row.automated_line is False
    assert row.source_label == "sportsbook_agnostic"
    assert "non-automated" in row.fallback_label.lower() or "agnostic" in row.fallback_label.lower()


def test_exact_ev_only_after_observed_price() -> None:
    unpriced = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.50,
        p25=0.40,
        gates_pass=True,
        observed=None,
    )
    assert unpriced.exact_ev is None
    assert unpriced.exact_ev_available is False

    observed = parse_manual_price_observation(
        {
            "bookmaker_key": "bet365",
            "region": "uk",
            "market_family": "moneyline",
            "outcome_key": "fighter_a",
            "price_decimal": 2.20,
            "observed_at": "2026-08-12T18:00:00Z",
        }
    )
    priced = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.50,
        p25=0.40,
        gates_pass=True,
        observed=observed,
        prob_ev_positive=0.85,
    )
    assert priced.exact_ev_available is True
    assert priced.exact_ev == pytest.approx(compute_exact_ev(0.50, 2.20))
    assert priced.source_label == MANUAL_SOURCE_LABEL
    assert priced.automated_line is False
    assert priced.observed_price is not None
    assert priced.observed_price.source_kind is PriceSourceKind.USER_OBSERVED


def test_manual_price_maps_canonical_identity_fields() -> None:
    observed = parse_manual_price_observation(
        {
            "bookmaker_key": "draftkings",
            "bookmaker_title": "DraftKings",
            "region": "us",
            "market_family": "totals",
            "outcome_key": "over",
            "line_point": 2.5,
            "price_decimal": 1.91,
            "observed_at": "2026-08-12T18:00:00Z",
            "source_updated_at": "2026-08-12T17:55:00Z",
            "event_external_id": "evt-123",
            "settlement_identity": "totals:over:2.5",
        }
    )
    assert observed.source_kind is PriceSourceKind.USER_OBSERVED
    assert observed.automated is False
    assert observed.provider is None
    assert observed.bookmaker_key == "draftkings"
    assert observed.region == "us"
    assert observed.market_family is MarketFamily.TOTALS
    assert observed.outcome_key is OutcomeKey.OVER
    assert observed.line_point == pytest.approx(2.5)
    assert observed.lifecycle is LineLifecycleState.AVAILABLE
    assert observed.observed_at == OBSERVED
    assert observed.settlement_identity == "totals:over:2.5"
    assert observed.as_identity_dict()["source_kind"] == MANUAL_SOURCE_LABEL


def test_locks_removals_and_entitlement_are_explicit_no_forward_fill() -> None:
    locked = parse_manual_price_observation(
        {
            "bookmaker_key": "fanduel",
            "region": "us",
            "market_family": "moneyline",
            "outcome_key": "fighter_b",
            "lifecycle": "locked",
            "observed_at": "2026-08-12T18:00:00Z",
            "prior_price_decimal": 1.80,
        }
    )
    assert locked.lifecycle is LineLifecycleState.LOCKED
    assert locked.price_decimal is None
    guidance = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_B,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.45,
        p25=0.40,
        gates_pass=True,
        observed=locked,
    )
    assert guidance.exact_ev is None
    assert guidance.exact_ev_available is False
    assert guidance.line_lifecycle is LineLifecycleState.LOCKED
    assert guidance.thresholds is not None  # targets still published

    removed = parse_manual_price_observation(
        {
            "bookmaker_key": "fanduel",
            "region": "us",
            "market_family": "moneyline",
            "outcome_key": "fighter_b",
            "lifecycle": "removed",
            "observed_at": "2026-08-12T18:05:00Z",
        }
    )
    assert removed.lifecycle is LineLifecycleState.REMOVED
    assert removed.price_decimal is None

    with pytest.raises(EntitlementFailure, match="entitlement"):
        ObservedPrice.entitlement_failed(
            provider="opticodds",
            bookmaker_key="bet365",
            region="us",
            market_family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            observed_at=OBSERVED,
            detail="Phase 0 did not authorize licensed OpticOdds adapter",
        )


def test_entitlement_failure_row_is_explicit_without_price() -> None:
    row = ObservedPrice.record_entitlement_failure(
        provider="sportsgameodds",
        bookmaker_key="bet365",
        region="us",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        observed_at=OBSERVED,
        detail="provider not configured / not Phase-0 authorized",
    )
    assert row.lifecycle is LineLifecycleState.ENTITLEMENT_FAILED
    assert row.price_decimal is None
    assert row.automated is False
    guidance = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.50,
        p25=0.45,
        gates_pass=True,
        observed=row,
    )
    assert guidance.exact_ev is None
    assert guidance.line_lifecycle is LineLifecycleState.ENTITLEMENT_FAILED
    assert guidance.recommendation.state is RecommendationState.PRICE_TARGET


def test_manual_price_persists_append_only(tmp_path: Path) -> None:
    session = _session(tmp_path)
    store = OddsQuoteStore(session)
    observed = parse_manual_price_observation(
        {
            "bookmaker_key": "bet365",
            "region": "uk",
            "market_family": "moneyline",
            "outcome_key": "fighter_a",
            "price_decimal": 2.05,
            "observed_at": "2026-08-12T18:00:00Z",
            "event_external_id": "manual-evt-1",
        }
    )
    result = store.append_manual_prices([observed])
    session.commit()
    assert result.inserted == 1
    assert result.deduped == 0

    result2 = store.append_manual_prices([observed])
    session.commit()
    assert result2.inserted == 0
    assert result2.deduped == 1

    rows = session.scalars(select(OddsManualPriceObservation)).all()
    assert len(rows) == 1
    assert rows[0].source_kind == MANUAL_SOURCE_LABEL
    assert rows[0].automated == 0
    assert rows[0].price_decimal == pytest.approx(2.05)

    with pytest.raises(IntegrityError, match="append-only"):
        rows[0].price_decimal = 9.99
        session.commit()
    session.rollback()


def test_reference_quotes_never_mislabeled_bet365_in_guidance() -> None:
    reference = ObservedPrice.from_reference_quote(
        provider="the_odds_api",
        bookmaker_key="fanduel",
        bookmaker_title="FanDuel",
        region="us",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        price_decimal=1.95,
        observed_at=OBSERVED,
        event_external_id="ref-1",
    )
    assert reference.source_kind is PriceSourceKind.REFERENCE_PROVIDER
    assert reference.bookmaker_key == "fanduel"
    assert "bet365" not in reference.as_identity_dict()["bookmaker_key"]
    guidance = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.52,
        p25=0.48,
        gates_pass=True,
        observed=reference,
        prob_ev_positive=0.80,
    )
    assert guidance.source_label == "the_odds_api"
    assert guidance.claims_bet365 is False


def test_bookmaker_audit_next_dwcs_reports_fallback_path() -> None:
    report = run_bookmaker_audit(next_dwcs=True)
    assert report["ticket"] == "DWCS-202"
    assert report["licensed_bookmaker_adapter_authorized"] is False
    assert report["decision"]["path"] == DECISION_PATH_REFERENCE_FALLBACK
    assert report["automated_bookmaker_adapter"] is None
    assert report["fallback"]["sportsbook_agnostic_required"] is True
    assert report["fallback"]["manual_observation_source"] == MANUAL_SOURCE_LABEL
    assert report["scraper_paths_present"] is False
    assert report["next_dwcs"]["requested"] is True
    # Demo unpriced guidance proves core path without inventing book lines.
    assert report["sample_price_targets"]
    sample = report["sample_price_targets"][0]
    assert sample["exact_ev"] is None
    assert "fair_decimal" in sample["thresholds"]


def test_committed_phase0_artifact_is_evidence_source() -> None:
    path = REPO_ROOT / "output" / "research" / "odds-coverage-summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["decision"]["path"] == DECISION_PATH_REFERENCE_FALLBACK
    assert payload["providers"]["opticodds"]["status"] == "not_configured"
    assert payload["providers"]["sportsgameodds"]["status"] == "not_configured"
    assert payload["providers"]["sportsdataio"]["status"] == "not_configured"
    assert payload["pass_fail_matrix"]["lock_events"]["status"] in {"unknown", "fail"}
