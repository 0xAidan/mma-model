"""DWCS-202: licensed-adapter gate, manual prices, and actionable price fallback."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select, text
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
from mma_model.odds.bookmaker_keys import is_bet365_bookmaker_key
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
    PriceGuidanceSelectionError,
    build_price_guidance,
    build_unpriced_price_targets,
)
from mma_model.odds.provider_decision import (
    DECISION_PATH_REFERENCE_FALLBACK,
    PINNED_ODDS_DECISION_HASH,
    FrozenStrMapping,
    LicensedBookmakerAdapterError,
    assert_no_sportsbook_scraper_modules,
    licensed_bookmaker_adapter_authorized,
    load_odds_decision_contract,
    load_odds_source_config,
    load_phase0_odds_decision,
    package_decision_resource_path,
    require_licensed_bookmaker_adapter,
    visible_decision_path,
)
from mma_model.odds.store import OddsQuoteStore

OBSERVED = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED_DIGEST_LITERAL = (
    "85e036e1717ba9df41bd31ed7aed1e2fcc1a54747fc0175ce5d53679ac6a1637"
)


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
    assert decision.content_hash == PINNED_DIGEST_LITERAL
    assert licensed_bookmaker_adapter_authorized() is False
    with pytest.raises(LicensedBookmakerAdapterError, match="not authorized"):
        require_licensed_bookmaker_adapter("opticodds")
    with pytest.raises(LicensedBookmakerAdapterError, match="not authorized"):
        require_licensed_bookmaker_adapter("bet365")


def test_packaged_decision_contract_is_authority() -> None:
    assert PINNED_ODDS_DECISION_HASH == PINNED_DIGEST_LITERAL
    packaged = package_decision_resource_path()
    assert packaged.is_file()
    visible = visible_decision_path()
    assert visible.resolve() == packaged.resolve()
    cfg = load_odds_source_config()
    contract = load_odds_decision_contract()
    assert cfg["content_hash"] == contract.content_hash == PINNED_DIGEST_LITERAL
    assert contract.decision.licensed_bookmaker_adapter_authorized is False
    assert contract.manual_observation.source_label == MANUAL_SOURCE_LABEL
    assert "sportsbook_website_scraping" in contract.prohibited


def test_odds_decision_contract_is_deeply_immutable() -> None:
    """Cached authority must reject mutation attempts (typed + compatibility view)."""
    load_odds_decision_contract.cache_clear()
    load_odds_source_config.cache_clear()
    load_phase0_odds_decision.cache_clear()

    contract = load_odds_decision_contract()
    phase0 = load_phase0_odds_decision()
    readonly = load_odds_source_config()

    assert isinstance(contract.trial_providers, FrozenStrMapping)
    assert isinstance(phase0.trial_providers, FrozenStrMapping)

    with pytest.raises((ValidationError, TypeError, AttributeError)):
        contract.content_hash = "0" * 64  # type: ignore[misc]
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        contract.decision.path = "licensed_bet365_primary"  # type: ignore[misc]
    with pytest.raises(TypeError):
        contract.trial_providers["opticodds"] = "pass"  # type: ignore[index]
    with pytest.raises(TypeError):
        phase0.trial_providers["opticodds"] = "pass"  # type: ignore[index]
    with pytest.raises(TypeError):
        readonly["ticket"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        readonly["decision"]["path"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        readonly["trial_providers"]["opticodds"] = "pass"  # type: ignore[index]

    # Authority still matches pin after failed mutations.
    assert load_odds_decision_contract().content_hash == PINNED_DIGEST_LITERAL
    assert load_phase0_odds_decision().trial_providers["opticodds"] == "not_configured"
    assert load_odds_decision_contract().trial_providers["opticodds"] == "not_configured"


def test_odds_package_scraper_heuristic_is_scoped() -> None:
    assert_no_sportsbook_scraper_modules()
    report = run_bookmaker_audit(next_dwcs=False)
    assert report["odds_package_scraper_heuristic"]["scope"].startswith("mma_model.odds")
    assert "not proof" in report["odds_package_scraper_heuristic"]["note"].lower()


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
    assert row.exact_ev is None
    assert row.exact_ev_available is False
    assert row.line_point is None
    assert row.source_label == "sportsbook_agnostic"


def test_unpriced_rejects_invalid_family_outcome_line() -> None:
    with pytest.raises(PriceGuidanceSelectionError, match="not valid"):
        build_price_guidance(
            family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.OVER,
            maturity=MarketMaturity.QUALIFIED,
            p50=0.5,
            p25=0.4,
            gates_pass=True,
            line_point=None,
        )
    with pytest.raises(PriceGuidanceSelectionError, match="line_point"):
        build_price_guidance(
            family=MarketFamily.TOTALS,
            outcome_key=OutcomeKey.OVER,
            maturity=MarketMaturity.QUALIFIED,
            p50=0.5,
            p25=0.4,
            gates_pass=True,
            line_point=None,
        )
    with pytest.raises(PriceGuidanceSelectionError, match="line_point"):
        build_price_guidance(
            family=MarketFamily.TOTALS,
            outcome_key=OutcomeKey.OVER,
            maturity=MarketMaturity.QUALIFIED,
            p50=0.5,
            p25=0.4,
            gates_pass=True,
            line_point=3.5,
        )
    with pytest.raises(PriceGuidanceSelectionError, match="rejects line_point"):
        build_price_guidance(
            family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            maturity=MarketMaturity.QUALIFIED,
            p50=0.5,
            p25=0.4,
            gates_pass=True,
            line_point=1.5,
        )


def test_observation_must_match_guidance_selection_moneyline_totals_exact_round() -> None:
    totals_obs = parse_manual_price_observation(
        {
            "bookmaker_key": "draftkings",
            "region": "us",
            "market_family": "totals",
            "outcome_key": "over",
            "line_point": 2.5,
            "price_decimal": 1.91,
            "observed_at": "2026-08-12T18:00:00Z",
        }
    )
    with pytest.raises(PriceGuidanceSelectionError, match="mismatch"):
        build_price_guidance(
            family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            maturity=MarketMaturity.QUALIFIED,
            p50=0.55,
            p25=0.50,
            gates_pass=True,
            observed=totals_obs,
            line_point=None,
        )

    fighter_b = parse_manual_price_observation(
        {
            "bookmaker_key": "bet365",
            "region": "uk",
            "market_family": "moneyline",
            "outcome_key": "fighter_b",
            "price_decimal": 2.10,
            "observed_at": "2026-08-12T18:00:00Z",
        }
    )
    with pytest.raises(PriceGuidanceSelectionError, match="mismatch"):
        build_price_guidance(
            family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            maturity=MarketMaturity.QUALIFIED,
            p50=0.55,
            p25=0.50,
            gates_pass=True,
            observed=fighter_b,
        )

    ok_totals = build_price_guidance(
        family=MarketFamily.TOTALS,
        outcome_key=OutcomeKey.OVER,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.55,
        p25=0.50,
        gates_pass=True,
        observed=totals_obs,
        line_point=2.5,
        prob_ev_positive=0.9,
    )
    assert ok_totals.exact_ev_available is True
    assert ok_totals.line_point == pytest.approx(2.5)

    round_obs = parse_manual_price_observation(
        {
            "bookmaker_key": "fanduel",
            "region": "us",
            "market_family": "exact_round",
            "outcome_key": "round_2",
            "price_decimal": 5.5,
            "observed_at": "2026-08-12T18:00:00Z",
        }
    )
    with pytest.raises(PriceGuidanceSelectionError, match="mismatch"):
        build_price_guidance(
            family=MarketFamily.EXACT_ROUND,
            outcome_key=OutcomeKey.ROUND_1,
            maturity=MarketMaturity.QUALIFIED,
            p50=0.20,
            p25=0.15,
            gates_pass=True,
            observed=round_obs,
        )
    ok_round = build_price_guidance(
        family=MarketFamily.EXACT_ROUND,
        outcome_key=OutcomeKey.ROUND_2,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.20,
        p25=0.15,
        gates_pass=True,
        observed=round_obs,
        prob_ev_positive=0.9,
    )
    assert ok_round.exact_ev_available is True


def test_exact_ev_only_after_observed_price_on_product_eligible_selection() -> None:
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

    blocked = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.EXPERIMENTAL,
        p50=0.50,
        p25=0.40,
        gates_pass=True,
        observed=observed,
        prob_ev_positive=0.85,
    )
    assert blocked.recommendation.state is RecommendationState.NO_BET
    assert blocked.exact_ev is None
    assert blocked.exact_ev_available is False

    failed_gate = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.50,
        p25=0.40,
        gates_pass=False,
        observed=observed,
        prob_ev_positive=0.85,
    )
    assert failed_gate.recommendation.state is RecommendationState.NO_BET
    assert failed_gate.exact_ev_available is False


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
    assert observed.attempted_provider is None
    assert observed.line_point == pytest.approx(2.5)
    assert observed.observed_at == OBSERVED
    assert observed.as_identity_dict()["attempted_provider"] is None


def test_parser_rejects_price_on_non_available_and_prior_price() -> None:
    with pytest.raises(ValueError, match="must not include price_decimal"):
        parse_manual_price_observation(
            {
                "bookmaker_key": "fanduel",
                "region": "us",
                "market_family": "moneyline",
                "outcome_key": "fighter_b",
                "lifecycle": "locked",
                "price_decimal": 1.80,
                "observed_at": "2026-08-12T18:00:00Z",
            }
        )
    with pytest.raises(ValueError, match="prior_price_decimal is not accepted"):
        parse_manual_price_observation(
            {
                "bookmaker_key": "fanduel",
                "region": "us",
                "market_family": "moneyline",
                "outcome_key": "fighter_b",
                "lifecycle": "locked",
                "prior_price_decimal": 1.80,
                "observed_at": "2026-08-12T18:00:00Z",
            }
        )


def test_locks_and_entitlement_are_explicit_no_forward_fill() -> None:
    locked = parse_manual_price_observation(
        {
            "bookmaker_key": "fanduel",
            "region": "us",
            "market_family": "moneyline",
            "outcome_key": "fighter_b",
            "lifecycle": "locked",
            "observed_at": "2026-08-12T18:00:00Z",
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
    assert guidance.exact_ev_available is False
    assert guidance.line_lifecycle is LineLifecycleState.LOCKED

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


def test_entitlement_failure_persists_attempted_provider(tmp_path: Path) -> None:
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
    assert row.attempted_provider == "sportsgameodds"
    assert row.provider is None
    assert row.price_decimal is None
    assert row.as_identity_dict()["attempted_provider"] == "sportsgameodds"

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
    assert guidance.observed_price is not None
    assert guidance.observed_price.attempted_provider == "sportsgameodds"

    session = _session(tmp_path)
    store = OddsQuoteStore(session)
    result = store.append_manual_prices([row])
    session.commit()
    assert result.inserted == 1
    persisted = session.scalars(select(OddsManualPriceObservation)).one()
    assert persisted.attempted_provider == "sportsgameodds"
    assert persisted.lifecycle == "entitlement_failed"
    assert persisted.price_decimal is None


def test_observed_price_normalizes_non_utc_aware_datetimes() -> None:
    eastern = timezone(timedelta(hours=-4))
    raw = datetime(2026, 8, 12, 14, 0, tzinfo=eastern)
    obs = ObservedPrice(
        source_kind=PriceSourceKind.USER_OBSERVED,
        automated=False,
        provider=None,
        bookmaker_key="bet365",
        bookmaker_title=None,
        region="uk",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        line_point=None,
        price_decimal=2.0,
        lifecycle=LineLifecycleState.AVAILABLE,
        observed_at=raw,
        source_updated_at=raw,
        event_external_id=None,
        settlement_identity=None,
    )
    assert obs.observed_at.tzinfo == UTC
    assert obs.observed_at == datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
    assert obs.source_updated_at == datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
    assert obs.as_identity_dict()["observed_at"].endswith("+00:00")


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
    result2 = store.append_manual_prices([observed])
    session.commit()
    assert result2.deduped == 1
    rows = session.scalars(select(OddsManualPriceObservation)).all()
    assert len(rows) == 1
    with pytest.raises(IntegrityError, match="append-only"):
        rows[0].price_decimal = 9.99
        session.commit()
    session.rollback()


def test_raw_sql_rejects_invalid_manual_selection_and_entitlement(tmp_path: Path) -> None:
    session = _session(tmp_path)
    bind = session.get_bind()
    with pytest.raises(IntegrityError), bind.begin() as conn:
        conn.execute(
            text(
                """
                    INSERT INTO odds_manual_price_observations (
                      dedupe_key, source_kind, automated, bookmaker_key, region,
                      market_family, outcome_key, line_point, price_decimal, lifecycle,
                      attempted_provider, observed_at, created_at
                    ) VALUES (
                      'bad-family', 'user_observed', 0, 'bk', 'us',
                      'moneyline', 'over', NULL, 2.0, 'available',
                      NULL, '2026-08-12T18:00:00+00:00', '2026-08-12T18:00:00+00:00'
                    )
                    """
            )
        )
    with pytest.raises(IntegrityError), bind.begin() as conn:
        conn.execute(
            text(
                """
                    INSERT INTO odds_manual_price_observations (
                      dedupe_key, source_kind, automated, bookmaker_key, region,
                      market_family, outcome_key, line_point, price_decimal, lifecycle,
                      attempted_provider, observed_at, created_at
                    ) VALUES (
                      'bad-line', 'user_observed', 0, 'bk', 'us',
                      'totals', 'over', 3.5, 1.91, 'available',
                      NULL, '2026-08-12T18:00:00+00:00', '2026-08-12T18:00:00+00:00'
                    )
                    """
            )
        )
    with pytest.raises(IntegrityError), bind.begin() as conn:
        conn.execute(
            text(
                """
                    INSERT INTO odds_manual_price_observations (
                      dedupe_key, source_kind, automated, bookmaker_key, region,
                      market_family, outcome_key, line_point, price_decimal, lifecycle,
                      attempted_provider, observed_at, created_at
                    ) VALUES (
                      'entitlement-missing-provider', 'user_observed', 0, 'bk', 'us',
                      'moneyline', 'fighter_a', NULL, NULL, 'entitlement_failed',
                      NULL, '2026-08-12T18:00:00+00:00', '2026-08-12T18:00:00+00:00'
                    )
                    """
            )
        )
    with bind.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO odds_manual_price_observations (
                  dedupe_key, source_kind, automated, bookmaker_key, region,
                  market_family, outcome_key, line_point, price_decimal, lifecycle,
                  attempted_provider, observed_at, created_at
                ) VALUES (
                  'entitlement-ok', 'user_observed', 0, 'bk', 'us',
                  'moneyline', 'fighter_a', NULL, NULL, 'entitlement_failed',
                  'opticodds', '2026-08-12T18:00:00+00:00', '2026-08-12T18:00:00+00:00'
                )
                """
            )
        )


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


@pytest.mark.parametrize(
    "bookmaker_key",
    ["bet365", "bet365_au", "BET365", "Bet365_AU", " bet365 "],
)
def test_reference_rejects_bet365_aliases_while_fallback_active(
    bookmaker_key: str,
) -> None:
    with pytest.raises(ValueError, match="Bet365 aliases"):
        ObservedPrice.from_reference_quote(
            provider="the_odds_api",
            bookmaker_key=bookmaker_key,
            bookmaker_title="Bet365",
            region="uk",
            market_family=MarketFamily.MONEYLINE,
            outcome_key=OutcomeKey.FIGHTER_A,
            price_decimal=1.91,
            observed_at=OBSERVED,
            event_external_id="ref-bet365",
        )


def test_bet365fake_is_not_bet365_alias_and_not_claimed() -> None:
    assert is_bet365_bookmaker_key("bet365fake") is False
    reference = ObservedPrice.from_reference_quote(
        provider="the_odds_api",
        bookmaker_key="bet365fake",
        bookmaker_title="Not Bet365",
        region="us",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        price_decimal=1.90,
        observed_at=OBSERVED,
        event_external_id="ref-fake",
    )
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
    assert guidance.claims_bet365 is False
    assert guidance.source_label == "the_odds_api"


@pytest.mark.parametrize("bookmaker_key", ["bet365", "bet365_au", "BET365"])
def test_manual_user_observed_bet365_may_claim_label(bookmaker_key: str) -> None:
    observed = ObservedPrice(
        source_kind=PriceSourceKind.USER_OBSERVED,
        automated=False,
        provider=None,
        bookmaker_key=bookmaker_key,
        bookmaker_title="Bet365",
        region="uk",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        line_point=None,
        price_decimal=1.91,
        lifecycle=LineLifecycleState.AVAILABLE,
        observed_at=OBSERVED,
        source_updated_at=None,
        event_external_id="manual-bet365",
        settlement_identity=None,
    )
    guidance = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.55,
        p25=0.50,
        gates_pass=True,
        observed=observed,
        prob_ev_positive=0.80,
    )
    assert guidance.source_label == MANUAL_SOURCE_LABEL
    assert guidance.automated_line is False
    assert guidance.claims_bet365 is True


def test_manual_bet365fake_does_not_claim_bet365() -> None:
    observed = ObservedPrice(
        source_kind=PriceSourceKind.USER_OBSERVED,
        automated=False,
        provider=None,
        bookmaker_key="bet365fake",
        bookmaker_title="Fake",
        region="us",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        line_point=None,
        price_decimal=1.91,
        lifecycle=LineLifecycleState.AVAILABLE,
        observed_at=OBSERVED,
        source_updated_at=None,
        event_external_id="manual-fake",
        settlement_identity=None,
    )
    guidance = build_price_guidance(
        family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        maturity=MarketMaturity.QUALIFIED,
        p50=0.55,
        p25=0.50,
        gates_pass=True,
        observed=observed,
        prob_ev_positive=0.80,
    )
    assert guidance.claims_bet365 is False


def test_bookmaker_audit_next_dwcs_reports_fallback_path() -> None:
    report = run_bookmaker_audit(next_dwcs=True)
    assert report["ticket"] == "DWCS-202"
    assert report["licensed_bookmaker_adapter_authorized"] is False
    assert report["decision"]["path"] == DECISION_PATH_REFERENCE_FALLBACK
    assert report["decision"]["content_hash"] == PINNED_DIGEST_LITERAL
    assert report["automated_bookmaker_adapter"] is None
    assert report["sample_price_targets"][0]["exact_ev"] is None


def test_committed_phase0_artifact_cross_checks_packaged_contract() -> None:
    path = REPO_ROOT / "output" / "research" / "odds-coverage-summary.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    decision = load_phase0_odds_decision()
    assert payload["decision"]["path"] == decision.path
    assert payload["providers"]["opticodds"]["status"] == decision.trial_providers[
        "opticodds"
    ]


def test_cli_price_guidance_line_point_and_mismatch(tmp_path: Path) -> None:
    from mma_model.cli import main

    obs = tmp_path / "obs.json"
    obs.write_text(
        json.dumps(
            {
                "bookmaker_key": "draftkings",
                "region": "us",
                "market_family": "totals",
                "outcome_key": "over",
                "line_point": 2.5,
                "price_decimal": 1.91,
                "observed_at": "2026-08-12T18:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    # totals without --line-point
    assert (
        main(
            [
                "odds",
                "price-guidance",
                "--family",
                "totals",
                "--outcome",
                "over",
                "--p50",
                "0.55",
                "--p25",
                "0.5",
            ]
        )
        == 2
    )
    # moneyline rejects --line-point
    assert (
        main(
            [
                "odds",
                "price-guidance",
                "--family",
                "moneyline",
                "--outcome",
                "fighter_a",
                "--line-point",
                "1.5",
                "--p50",
                "0.55",
                "--p25",
                "0.5",
            ]
        )
        == 2
    )
    # observation mismatch vs guidance selection
    assert (
        main(
            [
                "odds",
                "price-guidance",
                "--family",
                "moneyline",
                "--outcome",
                "fighter_a",
                "--p50",
                "0.55",
                "--p25",
                "0.5",
                "--observation-json",
                str(obs),
            ]
        )
        == 2
    )
    # matching totals path
    assert (
        main(
            [
                "odds",
                "price-guidance",
                "--family",
                "totals",
                "--outcome",
                "over",
                "--line-point",
                "2.5",
                "--p50",
                "0.55",
                "--p25",
                "0.5",
                "--observation-json",
                str(obs),
                "--prob-ev-positive",
                "0.9",
            ]
        )
        == 0
    )


def test_cli_record_manual_rejects_locked_with_price(tmp_path: Path, capsys) -> None:
    from mma_model.cli import main

    obs = tmp_path / "locked.json"
    obs.write_text(
        json.dumps(
            {
                "bookmaker_key": "fanduel",
                "region": "us",
                "market_family": "moneyline",
                "outcome_key": "fighter_a",
                "lifecycle": "locked",
                "price_decimal": 1.9,
                "observed_at": "2026-08-12T18:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "odds",
            "record-manual-price",
            "--observation-json",
            str(obs),
            "--database-url",
            f"sqlite:///{tmp_path / 'cli-manual.db'}",
        ]
    )
    assert code == 2
    assert "price_decimal" in capsys.readouterr().out
