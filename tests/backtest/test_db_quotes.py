"""Real DWCS-201/203 tables: eligibility, closing, replacement, event-night facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import mma_model.db.tables.core  # noqa: F401
import mma_model.db.tables.identity  # noqa: F401
import mma_model.db.tables.odds  # noqa: F401
from mma_model.backtest.engine import (
    PrecomputedScorer,
    facts_from_snapshot,
    join_quote,
    quote_candidate_from_loaded,
    run_walk_forward,
)
from mma_model.backtest.quotes import load_quotes_at_cutoff, select_closing_row
from mma_model.db.base import Base
from mma_model.db.odds_guards import install_odds_sqlite_guards
from mma_model.db.session import sqlite_connect_pragmas
from mma_model.db.tables.core import (
    BoutResultVersion,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.features.snapshot import snapshot_from_session
from mma_model.odds.lifecycle import QuoteBlockReason
from mma_model.odds.matching import (
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    match_provider_event,
)
from mma_model.odds.normalize import quote_dedupe_key
from mma_model.odds.reconcile import apply_replacement, persist_match_decision
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.types import (
    PROVIDER_THE_ODDS_API,
    NormalizedQuote,
    OddsEvent,
    QuoteAvailability,
)
from mma_model.value.evidence import PriceObservationRole
from tests.backtest.helpers import (
    CONTRACT,
    decisive_facts,
    later_dev_card,
    make_prediction,
    make_score,
    two_bout_dev_card,
)

START = datetime(2017, 7, 11, 19, 0, tzinfo=UTC)
CUTOFF = START - timedelta(minutes=60)


def _session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'odds306.db'}", future=True)
    event.listen(engine, "connect", sqlite_connect_pragmas)
    Base.metadata.create_all(bind=engine)
    install_odds_sqlite_guards(engine)
    return sessionmaker(bind=engine, future=True)()


def _seed_bout(
    session,
    *,
    bout_id: str,
    event_id: str,
    fighter_a: str,
    fighter_b: str,
    start: datetime,
) -> None:
    if session.get(CanonicalEvent, event_id) is None:
        session.add(
            CanonicalEvent(
                id=event_id,
                name=event_id,
                series="dwcs",
                status="scheduled",
                scheduled_start_at=start,
            )
        )
    fa_id = f"{bout_id}:a"
    fb_id = f"{bout_id}:b"
    if session.get(CanonicalFighter, fa_id) is None:
        session.add(CanonicalFighter(id=fa_id, display_name=fighter_a))
    if session.get(CanonicalFighter, fb_id) is None:
        session.add(CanonicalFighter(id=fb_id, display_name=fighter_b))
    session.flush()
    if session.get(CanonicalBout, bout_id) is None:
        session.add(
            CanonicalBout(
                id=bout_id,
                event_id=event_id,
                fighter_a_id=fa_id,
                fighter_b_id=fb_id,
                status="scheduled",
                scheduled_rounds=3,
            )
        )
    session.flush()


def _append_quote(
    session,
    *,
    external_id: str,
    home: str,
    away: str,
    commence: datetime,
    observed_at: datetime,
    price_decimal: float,
    raw_ref: str,
) -> None:
    dedupe = quote_dedupe_key(
        provider=PROVIDER_THE_ODDS_API,
        event_id=external_id,
        bookmaker_key="fanduel",
        region="us",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=OutcomeKey.FIGHTER_A,
        line_point=None,
        price_decimal=price_decimal,
        source_updated_at=observed_at,
        commence_time=commence,
        snapshot_at=None,
        raw_ref=raw_ref,
        home_team=home,
        away_team=away,
    )
    store = OddsQuoteStore(session)
    event_row = store.upsert_event(
        OddsEvent(
            id=external_id,
            sport_key="mma_mixed_martial_arts",
            commence_time=commence,
            home_team=home,
            away_team=away,
        ),
        provider=PROVIDER_THE_ODDS_API,
    )
    store.append_quotes(
        [
            NormalizedQuote(
                provider=PROVIDER_THE_ODDS_API,
                bookmaker_key="fanduel",
                bookmaker_title="FanDuel",
                region="us",
                event_id=external_id,
                home_team=home,
                away_team=away,
                market_family=MarketFamily.MONEYLINE,
                provider_market_key="h2h",
                outcome_key=OutcomeKey.FIGHTER_A,
                outcome_label=home,
                line_point=None,
                price_decimal=price_decimal,
                availability=QuoteAvailability.AVAILABLE,
                observed_at=observed_at,
                source_updated_at=observed_at,
                commence_time=commence,
                snapshot_at=None,
                raw_ref=raw_ref,
                dedupe_key=dedupe,
            )
        ],
        events_by_external_id={external_id: event_row},
    )


def test_db_pre_cutoff_quote_survives_later_post_cutoff_and_has_real_close(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    _seed_bout(
        session,
        bout_id="bout-open",
        event_id="evt-open",
        fighter_a="Alpha One",
        fighter_b="Bravo Two",
        start=START,
    )
    session.commit()
    _append_quote(
        session,
        external_id="prov-open",
        home="Alpha One",
        away="Bravo Two",
        commence=START,
        observed_at=CUTOFF - timedelta(minutes=30),
        price_decimal=2.10,
        raw_ref="open-pre",
    )
    _append_quote(
        session,
        external_id="prov-open",
        home="Alpha One",
        away="Bravo Two",
        commence=START,
        observed_at=CUTOFF + timedelta(minutes=5),
        price_decimal=1.85,
        raw_ref="close-post-cutoff",
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-open",
        home_team="Alpha One",
        away_team="Bravo Two",
        commence_time=START,
    )
    assert decision.status == MATCH_STATUS_MATCHED
    persist_match_decision(session, decision, observed_at=CUTOFF - timedelta(minutes=10))
    session.add(
        BoutResultVersion(
            bout_id="bout-open",
            version_kind="event_night",
            revision=1,
            fighter_a_id="bout-open:a",
            fighter_b_id="bout-open:b",
            winner_fighter_id="bout-open:a",
            result_type="decisive",
            method="U-DEC",
            ending_round=3,
            time_str="5:00",
            effective_at=START,
            observed_at=START,
            provenance_status="unknown",
        )
    )
    session.commit()

    rows = load_quotes_at_cutoff(
        session,
        bout_ids=("bout-open",),
        cutoff=CUTOFF,
        event_start=START,
    )
    eligible = [row for row in rows if row.eligible]
    assert eligible
    assert all(float(row.quote.price_decimal) == 2.10 for row in eligible)
    assert eligible[0].later_ignored >= 1
    assert eligible[0].eligibility.decision_identity
    assert eligible[0].eligibility.decision_version
    assert eligible[0].eligibility.evaluated_at == CUTOFF
    opening = max(eligible, key=lambda row: (row.quote.observed_at, int(row.quote.id or 0)))
    closing = select_closing_row(session, opening=opening, event_start=START)
    assert closing is not None
    assert closing.eligible is True
    assert int(closing.quote.id or 0) != int(opening.quote.id or 0)
    assert float(closing.quote.price_decimal) == 1.85
    candidate = quote_candidate_from_loaded(opening, closing=closing)
    assert candidate.historical_evidence is True
    assert candidate.fixture_provenance is False
    joined = join_quote(
        (candidate,),
        bout_id="bout-open",
        family=MarketFamily.MONEYLINE.value,
        outcome_key=OutcomeKey.FIGHTER_A.value,
        line_point=None,
        cutoff=CUTOFF,
    )
    assert joined.priced is True
    assert joined.quote is not None
    assert joined.quote.price_decimal == 2.10
    snapshot = snapshot_from_session(session)
    facts = facts_from_snapshot(snapshot, "bout-open")
    assert facts is not None
    assert facts.pending is False
    assert facts.winner_side == "a"
    assert facts.result_class == "decisive"


def test_db_replacement_and_ambiguous_quotes_are_blocked(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _seed_bout(
        session,
        bout_id="old-bout",
        event_id="evt-r",
        fighter_a="Alex Original",
        fighter_b="Blake Opponent",
        start=START,
    )
    _seed_bout(
        session,
        bout_id="new-bout",
        event_id="evt-r2",
        fighter_a="Alex Original",
        fighter_b="Casey Replacement",
        start=START,
    )
    session.commit()
    first = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-old",
        home_team="Alex Original",
        away_team="Blake Opponent",
        commence_time=START,
    )
    persist_match_decision(session, first, observed_at=CUTOFF - timedelta(hours=3))
    _append_quote(
        session,
        external_id="prov-old",
        home="Alex Original",
        away="Blake Opponent",
        commence=START,
        observed_at=CUTOFF - timedelta(minutes=20),
        price_decimal=2.05,
        raw_ref="old-q",
    )
    session.commit()
    apply_replacement(
        session,
        old_bout_id="old-bout",
        new_bout_id="new-bout",
        provider=PROVIDER_THE_ODDS_API,
        old_external_event_id="prov-old",
        new_external_event_id="prov-new",
        new_home_team="Alex Original",
        new_away_team="Casey Replacement",
        new_commence_time=START,
        observed_at=CUTOFF - timedelta(minutes=5),
    )
    session.commit()
    replaced = load_quotes_at_cutoff(
        session,
        bout_ids=("old-bout",),
        cutoff=CUTOFF,
        event_start=START,
    )
    assert replaced == () or all(not row.eligible for row in replaced)

    _seed_bout(
        session,
        bout_id="amb-a",
        event_id="evt-amb",
        fighter_a="Same One",
        fighter_b="Same Two",
        start=START,
    )
    _seed_bout(
        session,
        bout_id="amb-b",
        event_id="evt-amb",
        fighter_a="Same One",
        fighter_b="Same Two",
        start=START,
    )
    session.commit()
    amb = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-amb",
        home_team="Same Two",
        away_team="Same One",
        commence_time=START,
    )
    assert amb.status == MATCH_STATUS_AMBIGUOUS
    persist_match_decision(session, amb, observed_at=CUTOFF - timedelta(hours=1))
    _append_quote(
        session,
        external_id="prov-amb",
        home="Same Two",
        away="Same One",
        commence=START,
        observed_at=CUTOFF - timedelta(minutes=15),
        price_decimal=1.91,
        raw_ref="amb-q",
    )
    session.commit()
    amb_rows = load_quotes_at_cutoff(
        session,
        bout_ids=("amb-a", "amb-b"),
        cutoff=CUTOFF,
        event_start=START,
    )
    assert all(not row.eligible for row in amb_rows)
    if amb_rows:
        assert amb_rows[0].eligibility_reason != QuoteBlockReason.NONE.value


def test_db_closing_quote_produces_clv_through_walk_forward(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _seed_bout(
        session,
        bout_id="2017-a",
        event_id="dev-2017",
        fighter_a="Alpha One",
        fighter_b="Bravo Two",
        start=START,
    )
    session.commit()
    _append_quote(
        session,
        external_id="prov-clv",
        home="Alpha One",
        away="Bravo Two",
        commence=START,
        observed_at=CUTOFF - timedelta(minutes=30),
        price_decimal=2.20,
        raw_ref="open-clv",
    )
    _append_quote(
        session,
        external_id="prov-clv",
        home="Alpha One",
        away="Bravo Two",
        commence=START,
        observed_at=CUTOFF + timedelta(minutes=10),
        price_decimal=1.90,
        raw_ref="close-clv",
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clv",
        home_team="Alpha One",
        away_team="Bravo Two",
        commence_time=START,
    )
    assert decision.status == MATCH_STATUS_MATCHED
    persist_match_decision(session, decision, observed_at=CUTOFF - timedelta(minutes=10))
    session.commit()
    rows = load_quotes_at_cutoff(
        session,
        bout_ids=("2017-a",),
        cutoff=CUTOFF,
        event_start=START,
    )
    eligible = [row for row in rows if row.eligible]
    assert eligible
    opening = max(eligible, key=lambda row: (row.quote.observed_at, int(row.quote.id or 0)))
    closing = select_closing_row(session, opening=opening, event_start=START)
    assert closing is not None
    assert closing.quote_evidence is not None
    assert closing.quote_evidence.price_role is PriceObservationRole.CLOSING
    assert opening.quote_evidence is not None
    assert opening.quote_evidence.price_role is PriceObservationRole.OPENING
    candidate = quote_candidate_from_loaded(opening, closing=closing)
    payload = run_walk_forward(
        contract=CONTRACT,
        cards=(two_bout_dev_card(), later_dev_card()),
        scorer=PrecomputedScorer(
            {
                "dev-2017": make_score(
                    "dev-2017",
                    (
                        make_prediction(
                            "2017-a",
                            "dev-2017",
                            p_a=0.62,
                            p25=0.55,
                            estimator_hash="e",
                        ),
                    ),
                    estimator_hash="e",
                )
            }
        ),
        quotes=(candidate,),
        settlement_facts={"2017-a": decisive_facts("a")},
        require_target_cards=False,
        bootstrap_replicates=6,
    )
    priced = next(
        item
        for row in payload["attempts"]
        if row["bout_id"] == "2017-a"
        for item in row["priced_rows"]
        if item["outcome_key"] == "fighter_a"
    )
    assert priced["probability_clv"] is not None
    assert priced["closing_ev"] is not None
    assert priced.get("reason") in {None, "none", "ok"} or priced["available"] is True
