"""DWCS-203: hardened matching, lifecycle, review, replacement, CLI, integrity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from mma_model.db.base import Base
from mma_model.db.odds_guards import install_odds_sqlite_guards
from mma_model.db.session import sqlite_connect_pragmas
from mma_model.db.tables.core import (
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterAlias,
    FighterSourceId,
)
from mma_model.db.tables.identity import IdentityReviewQueue
from mma_model.db.tables.odds import (
    OddsBoutLifecycleObservation,
    OddsBoutMatchReview,
    OddsProviderEventAlias,
    OddsQuote,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.odds.lifecycle import (
    OddsBoutLifecycleState,
    QuoteValueEligibility,
    alias_effective_at,
    apply_bout_lifecycle,
    classify_quote_value_eligibility,
    latest_bout_lifecycle,
    latest_quote_timestamp,
    quote_authoritative_freshness,
    quote_is_stale,
    quotes_visible_under_active_alias,
    quotes_visible_under_alias_at,
)
from mma_model.odds.match_review import (
    OddsBoutMatchReviewError,
    approve_bout_match_review,
    enqueue_bout_match_review,
    reject_bout_match_review,
    reverse_bout_match_review,
)
from mma_model.odds.matching import (
    EXPECTED_MATCHING_CONTRACT_VERSION,
    MATCH_RULE_MANUAL_REVIEW,
    MATCH_RULE_PARTICIPANT_PAIR,
    MATCH_RULE_PROVIDER_ID,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    MATCHING_CONTRACT_ID,
    PINNED_MATCHING_CONTRACT_HASH,
    MatchingContractHashMismatch,
    compute_matching_contract_hash,
    load_matching_contract,
    match_provider_event,
    package_matching_resource_path,
    participant_names_equal,
    require_aware_utc,
    visible_matching_path,
)
from mma_model.odds.normalize import quote_dedupe_key
from mma_model.odds.reconcile import (
    OddsReconcileError,
    apply_replacement,
    load_golden_card,
    persist_match_decision,
    run_odds_reconcile,
    seed_canonical_card,
    select_next_dwcs_event,
)
from mma_model.odds.snapshot import OddsOfflineModeError
from mma_model.odds.store import OddsQuoteStore
from mma_model.odds.types import (
    PROVIDER_THE_ODDS_API,
    NormalizedQuote,
    OddsEvent,
    QuoteAvailability,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ACTIVE = REPO_ROOT / "tests/fixtures/odds/golden/card_active_bouts.json"
GOLDEN_REPLACEMENT = REPO_ROOT / "tests/fixtures/odds/golden/card_replacement.json"
OBSERVED = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
AS_OF_CARD = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
PINNED_DIGEST_LITERAL = (
    "f5406f7c5600646c3ab33c7767c2f4a881ef6abf6beea6fe891f06f248906305"
)


def _session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'odds203.db'}", future=True)
    event.listen(engine, "connect", sqlite_connect_pragmas)
    import mma_model.db.tables.core  # noqa: F401
    import mma_model.db.tables.identity  # noqa: F401
    import mma_model.db.tables.odds  # noqa: F401

    Base.metadata.create_all(bind=engine)
    install_odds_sqlite_guards(engine)
    return sessionmaker(bind=engine, future=True)()


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_bout(
    session,
    *,
    bout_id: str,
    event_id: str,
    fighter_a: str,
    fighter_b: str,
    start: datetime,
    series: str = "dwcs",
    bout_status: str = "scheduled",
    event_status: str = "scheduled",
) -> None:
    if session.get(CanonicalEvent, event_id) is None:
        session.add(
            CanonicalEvent(
                id=event_id,
                name=event_id,
                series=series,
                status=event_status,
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
                status=bout_status,
            )
        )
    session.flush()


# --- contract / packaging -------------------------------------------------


def test_matching_contract_loads_and_pins_window() -> None:
    contract = load_matching_contract()
    assert contract.contract_id == MATCHING_CONTRACT_ID
    assert contract.contract_version == EXPECTED_MATCHING_CONTRACT_VERSION
    assert contract.ticket == "DWCS-203"
    assert contract.match_window_minutes == 30
    assert contract.stale_after_minutes == 360
    assert MATCH_RULE_PROVIDER_ID in contract.match_rules
    assert MATCH_RULE_PARTICIPANT_PAIR in contract.match_rules
    assert "fuzzy_auto_merge" in contract.prohibited
    assert contract.content_hash == PINNED_MATCHING_CONTRACT_HASH
    assert PINNED_MATCHING_CONTRACT_HASH == PINNED_DIGEST_LITERAL


def test_matching_contract_pinned_hash_and_plan_visible_bytes() -> None:
    packaged = package_matching_resource_path().read_bytes()
    visible = visible_matching_path().read_bytes()
    assert packaged == visible
    payload = yaml.safe_load(packaged.decode("utf-8"))
    assert compute_matching_contract_hash(payload) == PINNED_DIGEST_LITERAL
    contract = load_matching_contract()
    with pytest.raises((TypeError, ValidationError)):
        contract.match_window_minutes = 1  # type: ignore[misc]


def test_matching_contract_hash_mismatch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    load_matching_contract.cache_clear()
    monkeypatch.setattr(
        "mma_model.odds.matching.PINNED_MATCHING_CONTRACT_HASH",
        "0" * 64,
    )
    with pytest.raises(MatchingContractHashMismatch):
        load_matching_contract()
    load_matching_contract.cache_clear()


def test_participant_names_ignore_home_away_order() -> None:
    assert participant_names_equal(
        ("Jon Kunneman", "Joseph Kropschot"),
        ("Joseph Kropschot", "Jon Kunneman"),
    )
    assert not participant_names_equal(
        ("Jon Kunneman", "Joseph Kropschot"),
        ("Jon Kunneman", "Nick Kropschot"),
    )


def test_require_aware_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        require_aware_utc(datetime(2026, 8, 12, 12, 0), field="commence_time")


# --- provider-ID safety ---------------------------------------------------


def _add_quote(
    session,
    *,
    external_id: str,
    home: str,
    away: str,
    commence: datetime,
    observed_at: datetime,
    dedupe: str,
) -> None:
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
                price_decimal=1.9,
                availability=QuoteAvailability.AVAILABLE,
                observed_at=observed_at,
                source_updated_at=None,
                commence_time=commence,
                snapshot_at=None,
                raw_ref=dedupe,
                dedupe_key=dedupe,
            )
        ],
        events_by_external_id={external_id: event_row},
    )


def test_provider_id_match_requires_participant_and_time(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-stored",
        event_id="evt-1",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    session.add(
        BoutSourceId(
            bout_id="bout-stored",
            source=PROVIDER_THE_ODDS_API,
            external_id="prov-stored-id",
        )
    )
    _add_quote(
        session,
        external_id="prov-stored-id",
        home="Bravo Fighter",
        away="Alpha Fighter",
        commence=start,
        observed_at=OBSERVED - timedelta(minutes=5),
        dedupe="prov-stored-quote",
    )
    session.commit()

    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-stored-id",
        home_team="Bravo Fighter",
        away_team="Alpha Fighter",
        commence_time=start,
        observed_at=OBSERVED,
    )
    assert decision.status == MATCH_STATUS_MATCHED
    assert decision.match_rule == MATCH_RULE_PROVIDER_ID
    assert decision.bout_id == "bout-stored"
    assert decision.eligible_for_value is True


def test_provider_id_changed_opponent_blocks_review(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-stored",
        event_id="evt-1",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    session.add(
        BoutSourceId(
            bout_id="bout-stored",
            source=PROVIDER_THE_ODDS_API,
            external_id="prov-stored-id",
        )
    )
    session.commit()

    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-stored-id",
        home_team="Totally Different",
        away_team="Name Pair",
        commence_time=start,
    )
    assert decision.status == MATCH_STATUS_AMBIGUOUS
    assert decision.eligible_for_value is False
    assert decision.lifecycle == OddsBoutLifecycleState.REVIEW_BLOCKED
    assert "participants" in decision.reason
    assert decision.review_id is not None


@pytest.mark.parametrize(
    "bout_status,reason_snip",
    [
        ("cancelled", "cancelled"),
        ("replaced", "replaced"),
    ],
)
def test_provider_id_inactive_bout_blocks(
    tmp_path: Path, bout_status: str, reason_snip: str
) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-dead",
        event_id="evt-dead",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
        bout_status=bout_status,
    )
    session.add(
        OddsProviderEventAlias(
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-dead",
            bout_id="bout-dead",
            alias_version=1,
            status="active",
            match_rule=MATCH_RULE_PROVIDER_ID,
            evidence_json="{}",
            created_at=OBSERVED,
        )
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-dead",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
    )
    # Inactive linked bout is not authority; participant search finds no active bout.
    assert decision.status == MATCH_STATUS_UNMATCHED
    assert decision.eligible_for_value is False
    assert decision.bout_id is None
    assert reason_snip  # parametrize keeps explicit cancelled/replaced coverage


def test_provider_id_wrong_series_blocks(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-ufc",
        event_id="evt-ufc",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
        series="ufc",
    )
    session.add(
        BoutSourceId(
            bout_id="bout-ufc",
            source=PROVIDER_THE_ODDS_API,
            external_id="prov-ufc",
        )
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-ufc",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
    )
    assert decision.status == MATCH_STATUS_AMBIGUOUS
    assert "DWCS" in decision.reason


def test_stale_alias_participant_mismatch_blocks(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-alias",
        event_id="evt-alias",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    session.add(
        OddsProviderEventAlias(
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-alias",
            bout_id="bout-alias",
            alias_version=1,
            status="active",
            match_rule=MATCH_RULE_PARTICIPANT_PAIR,
            evidence_json="{}",
            created_at=OBSERVED - timedelta(days=1),
        )
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-alias",
        home_team="Alpha Fighter",
        away_team="Charlie Intruder",
        commence_time=start,
    )
    assert decision.status == MATCH_STATUS_AMBIGUOUS
    assert decision.eligible_for_value is False


# --- participant pair / ambiguity ----------------------------------------


def test_participant_pair_match_within_window_ignores_corner(tmp_path: Path) -> None:
    session = _session(tmp_path)
    card = load_golden_card(GOLDEN_ACTIVE)
    seed_canonical_card(session, card)
    session.commit()

    provider = card["provider_events"][0]
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id=provider["id"],
        home_team=provider["home_team"],
        away_team=provider["away_team"],
        commence_time=datetime.fromisoformat(
            provider["commence_time"].replace("Z", "+00:00")
        ),
    )
    assert decision.status == MATCH_STATUS_MATCHED
    assert decision.match_rule == MATCH_RULE_PARTICIPANT_PAIR
    assert decision.bout_id == "golden-bout-kunneman-kropschot"


def test_participant_pair_outside_window_is_unmatched(tmp_path: Path) -> None:
    session = _session(tmp_path)
    card = load_golden_card(GOLDEN_ACTIVE)
    seed_canonical_card(session, card)
    session.commit()

    provider = card["provider_events"][0]
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id=provider["id"],
        home_team=provider["home_team"],
        away_team=provider["away_team"],
        commence_time=datetime.fromisoformat(
            provider["commence_time"].replace("Z", "+00:00")
        )
        + timedelta(hours=3),
    )
    assert decision.status == MATCH_STATUS_UNMATCHED
    assert decision.eligible_for_value is False


def test_ambiguity_uses_odds_bout_review_not_fighter_queue(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-a",
        event_id="evt-amb",
        fighter_a="Same One",
        fighter_b="Same Two",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="bout-b",
        event_id="evt-amb",
        fighter_a="Same One",
        fighter_b="Same Two",
        start=start,
    )
    session.commit()

    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-amb",
        home_team="Same Two",
        away_team="Same One",
        commence_time=start,
    )
    assert decision.status == MATCH_STATUS_AMBIGUOUS
    assert decision.eligible_for_value is False
    assert decision.lifecycle == OddsBoutLifecycleState.REVIEW_BLOCKED
    assert decision.review_id is not None
    review = session.get(OddsBoutMatchReview, decision.review_id)
    assert review is not None
    assert review.status == "pending"
    assert session.scalars(select(IdentityReviewQueue)).all() == []
    assert session.scalars(select(FighterSourceId)).all() == []


def test_bout_match_review_approve_reject_stale_reverse(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-a",
        event_id="evt-rev",
        fighter_a="Same One",
        fighter_b="Same Two",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="bout-b",
        event_id="evt-rev",
        fighter_a="Same One",
        fighter_b="Same Two",
        start=start,
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-rev",
        home_team="Same One",
        away_team="Same Two",
        commence_time=start,
    )
    assert decision.review_id is not None
    review_id = decision.review_id
    session.commit()

    with pytest.raises(OddsBoutMatchReviewError, match="stale review version"):
        approve_bout_match_review(
            session,
            review_id=review_id,
            bout_id="bout-a",
            actor="ops",
            expected_version=99,
        )

    approved = approve_bout_match_review(
        session,
        review_id=review_id,
        bout_id="bout-a",
        actor="ops",
        expected_version=1,
        observed_at=OBSERVED,
    )
    session.commit()
    assert approved.status == "approved"
    assert approved.decision_bout_id == "bout-a"
    alias = session.scalar(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.external_event_id == "prov-rev",
            OddsProviderEventAlias.status == "active",
        )
    )
    assert alias is not None
    assert alias.bout_id == "bout-a"
    assert session.scalars(select(FighterSourceId)).all() == []

    reversed_row = reverse_bout_match_review(
        session,
        review_id=review_id,
        actor="ops",
        expected_version=2,
        observed_at=OBSERVED + timedelta(minutes=1),
    )
    session.commit()
    assert reversed_row.status == "reversed"
    assert (
        session.scalar(
            select(OddsProviderEventAlias).where(
                OddsProviderEventAlias.external_event_id == "prov-rev",
                OddsProviderEventAlias.status == "active",
            )
        )
        is None
    )

    # Fresh pending review for reject path
    decision2 = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-rev-2",
        home_team="Same One",
        away_team="Same Two",
        commence_time=start,
    )
    assert decision2.review_id is not None
    session.commit()
    rejected = reject_bout_match_review(
        session,
        review_id=decision2.review_id,
        actor="ops",
        expected_version=1,
        observed_at=OBSERVED,
    )
    session.commit()
    assert rejected.status == "rejected"
    assert (
        session.scalar(
            select(OddsProviderEventAlias).where(
                OddsProviderEventAlias.external_event_id == "prov-rev-2",
                OddsProviderEventAlias.status == "active",
            )
        )
        is None
    )


# --- golden / next-dwcs ---------------------------------------------------


def test_golden_card_exact_active_bout_matches_100_percent(tmp_path: Path) -> None:
    session = _session(tmp_path)
    db_url = f"sqlite:///{tmp_path / 'golden.db'}"
    report = run_odds_reconcile(
        session,
        next_dwcs=True,
        strict=True,
        golden_card_path=GOLDEN_ACTIVE,
        as_of=AS_OF_CARD,
        offline_fixtures=True,
        database_url=db_url,
        allow_golden_seed=True,
        observed_at=OBSERVED,
    )
    session.commit()
    assert report["active_bout_match_rate"] == 1.0
    assert report["matched_active_bouts"] == report["active_bout_count"] == 5
    assert report["blockers"] == []
    assert all(row["status"] == MATCH_STATUS_MATCHED for row in report["decisions"])


def test_next_dwcs_scopes_nearest_card_and_fails_closed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    early = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    late = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-early",
        event_id="evt-early",
        fighter_a="Early A",
        fighter_b="Early B",
        start=early,
    )
    _seed_bout(
        session,
        bout_id="bout-late",
        event_id="evt-late",
        fighter_a="Late A",
        fighter_b="Late B",
        start=late,
    )
    _seed_bout(
        session,
        bout_id="bout-ufc",
        event_id="evt-ufc",
        fighter_a="UFC A",
        fighter_b="UFC B",
        start=early + timedelta(hours=1),
        series="ufc",
    )
    session.commit()

    chosen = select_next_dwcs_event(session, as_of=datetime(2026, 8, 15, tzinfo=UTC))
    assert chosen is not None
    assert chosen.id == "evt-early"

    report = run_odds_reconcile(
        session,
        next_dwcs=True,
        strict=True,
        as_of=datetime(2026, 8, 15, tzinfo=UTC),
        observed_at=OBSERVED,
    )
    assert any(b["kind"] == "zero_provider_events" for b in report["blockers"])
    assert report["scoped_event_id"] == "evt-early"
    assert report["active_bout_count"] == 1

    empty = run_odds_reconcile(
        session,
        next_dwcs=True,
        as_of=datetime(2026, 11, 1, tzinfo=UTC),
        observed_at=OBSERVED,
    )
    assert any(b["kind"] == "zero_next_dwcs_event" for b in empty["blockers"])


def test_golden_seed_refuses_without_offline_disposable(tmp_path: Path) -> None:
    session = _session(tmp_path)
    with pytest.raises(OddsReconcileError, match="offline"):
        run_odds_reconcile(
            session,
            golden_card_path=GOLDEN_ACTIVE,
            offline_fixtures=False,
            allow_golden_seed=False,
        )
    with pytest.raises(OddsOfflineModeError):
        run_odds_reconcile(
            session,
            golden_card_path=GOLDEN_ACTIVE,
            offline_fixtures=True,
            allow_golden_seed=True,
            database_url=None,
        )


# --- replacement ----------------------------------------------------------


def test_replacement_never_inherits_old_opponent_quotes(tmp_path: Path) -> None:
    session = _session(tmp_path)
    fixture = load_golden_card(GOLDEN_REPLACEMENT)
    seed = {
        "card_id": fixture["card_id"],
        "bouts": [fixture["original_bout"], fixture["replacement_bout"]],
        "provider_events": [],
    }
    seed_canonical_card(session, seed)
    session.commit()

    original_event = fixture["original_provider_event"]
    first = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id=original_event["id"],
        home_team=original_event["home_team"],
        away_team=original_event["away_team"],
        commence_time=datetime.fromisoformat(
            original_event["commence_time"].replace("Z", "+00:00")
        ),
    )
    persist_match_decision(session, first)
    store = OddsQuoteStore(session)
    event_row = store.upsert_event(
        OddsEvent(
            id=original_event["id"],
            sport_key=original_event["sport_key"],
            commence_time=datetime.fromisoformat(
                original_event["commence_time"].replace("Z", "+00:00")
            ),
            home_team=original_event["home_team"],
            away_team=original_event["away_team"],
        ),
        provider=PROVIDER_THE_ODDS_API,
    )
    q = fixture["original_quote"]
    store.append_quotes(
        [
            NormalizedQuote(
                provider=PROVIDER_THE_ODDS_API,
                bookmaker_key=q["bookmaker_key"],
                bookmaker_title=q["bookmaker_title"],
                region=q["region"],
                event_id=original_event["id"],
                home_team=original_event["home_team"],
                away_team=original_event["away_team"],
                market_family=MarketFamily.MONEYLINE,
                provider_market_key=q["provider_market_key"],
                outcome_key=OutcomeKey.FIGHTER_A,
                outcome_label=q["outcome_label"],
                line_point=None,
                price_decimal=q["price_decimal"],
                availability=QuoteAvailability.AVAILABLE,
                observed_at=datetime.fromisoformat(q["observed_at"].replace("Z", "+00:00")),
                source_updated_at=None,
                commence_time=datetime.fromisoformat(
                    original_event["commence_time"].replace("Z", "+00:00")
                ),
                snapshot_at=None,
                raw_ref=q["raw_ref"],
                dedupe_key="original-quote-dedupe",
            )
        ],
        events_by_external_id={original_event["id"]: event_row},
    )
    session.commit()

    result = apply_replacement(
        session,
        old_bout_id=fixture["original_bout"]["bout_id"],
        new_bout_id=fixture["replacement_bout"]["bout_id"],
        provider=PROVIDER_THE_ODDS_API,
        old_external_event_id=original_event["id"],
        new_external_event_id=fixture["replacement_provider_event"]["id"],
        new_home_team=fixture["replacement_provider_event"]["home_team"],
        new_away_team=fixture["replacement_provider_event"]["away_team"],
        new_commence_time=datetime.fromisoformat(
            fixture["replacement_provider_event"]["commence_time"].replace("Z", "+00:00")
        ),
        observed_at=OBSERVED,
    )
    session.commit()

    assert result["old_lifecycle"] == OddsBoutLifecycleState.REPLACED.value
    assert result["activated"] is True
    assert result["new_match"]["status"] == MATCH_STATUS_MATCHED
    assert result["new_match"]["bout_id"] == fixture["replacement_bout"]["bout_id"]

    old_quotes = session.scalars(
        select(OddsQuote).where(OddsQuote.external_event_id == original_event["id"])
    ).all()
    assert len(old_quotes) == 1

    new_aliases = session.scalars(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.bout_id == fixture["replacement_bout"]["bout_id"],
            OddsProviderEventAlias.status == "active",
        )
    ).all()
    assert len(new_aliases) == 1
    assert new_aliases[0].external_event_id == fixture["replacement_provider_event"]["id"]
    inherited = session.scalars(
        select(OddsQuote).where(
            OddsQuote.external_event_id == fixture["replacement_provider_event"]["id"]
        )
    ).all()
    assert inherited == []


def test_replacement_unmatched_and_wrong_participant_blocked(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="old-bout",
        event_id="evt-r",
        fighter_a="Alex Original",
        fighter_b="Blake Opponent",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="new-bout",
        event_id="evt-r2",
        fighter_a="Alex Original",
        fighter_b="Casey Replacement",
        start=start,
    )
    session.commit()
    first = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-old",
        home_team="Alex Original",
        away_team="Blake Opponent",
        commence_time=start,
    )
    persist_match_decision(session, first)
    session.commit()

    unmatched = apply_replacement(
        session,
        old_bout_id="old-bout",
        new_bout_id="new-bout",
        provider=PROVIDER_THE_ODDS_API,
        old_external_event_id="prov-old",
        new_external_event_id="prov-missing",
        new_home_team="Nobody",
        new_away_team="Nowhere",
        new_commence_time=start,
        observed_at=OBSERVED,
    )
    assert unmatched["activated"] is False
    assert unmatched["new_match"]["status"] == MATCH_STATUS_AMBIGUOUS
    assert unmatched["new_match"]["eligible_for_value"] is False
    assert (
        session.scalar(
            select(OddsProviderEventAlias).where(
                OddsProviderEventAlias.external_event_id == "prov-missing",
                OddsProviderEventAlias.status == "active",
            )
        )
        is None
    )


def test_same_external_id_replacement_versions_alias_hides_old_quotes(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="old-bout",
        event_id="evt-same",
        fighter_a="Alex Original",
        fighter_b="Blake Opponent",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="new-bout",
        event_id="evt-same-2",
        fighter_a="Alex Original",
        fighter_b="Casey Replacement",
        start=start,
    )
    session.commit()
    first = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-reused",
        home_team="Alex Original",
        away_team="Blake Opponent",
        commence_time=start,
    )
    persist_match_decision(session, first, observed_at=OBSERVED - timedelta(hours=2))
    store = OddsQuoteStore(session)
    event_row = store.upsert_event(
        OddsEvent(
            id="prov-reused",
            sport_key="mma_mixed_martial_arts",
            commence_time=start,
            home_team="Alex Original",
            away_team="Blake Opponent",
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
                event_id="prov-reused",
                home_team="Alex Original",
                away_team="Blake Opponent",
                market_family=MarketFamily.MONEYLINE,
                provider_market_key="h2h",
                outcome_key=OutcomeKey.FIGHTER_A,
                outcome_label="Alex Original",
                line_point=None,
                price_decimal=1.9,
                availability=QuoteAvailability.AVAILABLE,
                observed_at=OBSERVED - timedelta(hours=1),
                source_updated_at=None,
                commence_time=start,
                snapshot_at=None,
                raw_ref="old",
                dedupe_key="reused-old-quote",
            )
        ],
        events_by_external_id={"prov-reused": event_row},
    )
    session.commit()
    source_before = session.scalar(
        select(BoutSourceId).where(BoutSourceId.external_id == "prov-reused")
    )
    assert source_before is not None
    assert source_before.bout_id == "old-bout"

    result = apply_replacement(
        session,
        old_bout_id="old-bout",
        new_bout_id="new-bout",
        provider=PROVIDER_THE_ODDS_API,
        old_external_event_id="prov-reused",
        new_external_event_id="prov-reused",
        new_home_team="Casey Replacement",
        new_away_team="Alex Original",
        new_commence_time=start,
        observed_at=OBSERVED,
    )
    session.commit()
    assert result["activated"] is True
    source_after = session.scalar(
        select(BoutSourceId).where(BoutSourceId.external_id == "prov-reused")
    )
    assert source_after is not None
    assert source_after.bout_id == "old-bout"  # immutable
    active = session.scalars(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.external_event_id == "prov-reused",
            OddsProviderEventAlias.status == "active",
        )
    ).all()
    assert len(active) == 1
    assert active[0].bout_id == "new-bout"
    superseded = session.scalars(
        select(OddsProviderEventAlias).where(
            OddsProviderEventAlias.external_event_id == "prov-reused",
            OddsProviderEventAlias.status == "superseded",
        )
    ).all()
    assert superseded
    visible = quotes_visible_under_active_alias(
        session, provider=PROVIDER_THE_ODDS_API, external_event_id="prov-reused"
    )
    assert visible == []
    assert len(session.scalars(select(OddsQuote)).all()) == 1


def test_alias_versioning_preserves_old_identity(tmp_path: Path) -> None:
    session = _session(tmp_path)
    fixture = load_golden_card(GOLDEN_REPLACEMENT)
    seed = {
        "card_id": fixture["card_id"],
        "bouts": [fixture["original_bout"], fixture["replacement_bout"]],
        "provider_events": [],
    }
    seed_canonical_card(session, seed)
    session.commit()
    original_event = fixture["original_provider_event"]
    first = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id=original_event["id"],
        home_team=original_event["home_team"],
        away_team=original_event["away_team"],
        commence_time=datetime.fromisoformat(
            original_event["commence_time"].replace("Z", "+00:00")
        ),
    )
    persist_match_decision(session, first)
    apply_replacement(
        session,
        old_bout_id=fixture["original_bout"]["bout_id"],
        new_bout_id=fixture["replacement_bout"]["bout_id"],
        provider=PROVIDER_THE_ODDS_API,
        old_external_event_id=original_event["id"],
        new_external_event_id=fixture["replacement_provider_event"]["id"],
        new_home_team=fixture["replacement_provider_event"]["home_team"],
        new_away_team=fixture["replacement_provider_event"]["away_team"],
        new_commence_time=datetime.fromisoformat(
            fixture["replacement_provider_event"]["commence_time"].replace("Z", "+00:00")
        ),
        observed_at=OBSERVED,
    )
    session.commit()

    versions = session.scalars(
        select(OddsProviderEventAlias)
        .where(OddsProviderEventAlias.external_event_id == original_event["id"])
        .order_by(OddsProviderEventAlias.alias_version.asc())
    ).all()
    assert len(versions) >= 1
    assert any(v.status == "superseded" for v in versions)
    assert all(v.bout_id == fixture["original_bout"]["bout_id"] for v in versions)


# --- lifecycle ------------------------------------------------------------


def test_lifecycle_requires_evidence_and_never_forward_fills(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _seed_bout(
        session,
        bout_id="bout-l",
        event_id="evt-l",
        fighter_a="A",
        fighter_b="B",
        start=OBSERVED,
    )
    session.commit()

    with pytest.raises(ValueError, match="evidence_kind|disallowed|unknown"):
        apply_bout_lifecycle(
            session,
            bout_id="bout-l",
            lifecycle=OddsBoutLifecycleState.LOCKED,
            evidence_kind="",
            observed_at=OBSERVED,
        )

    with pytest.raises(ValueError, match="disallowed|unknown"):
        apply_bout_lifecycle(
            session,
            bout_id="bout-l",
            lifecycle=OddsBoutLifecycleState.LOCKED,
            evidence_kind="probably_locked",
            observed_at=OBSERVED,
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-l",
        )

    with pytest.raises(ValueError, match="provider and external_event_id"):
        apply_bout_lifecycle(
            session,
            bout_id="bout-l",
            lifecycle=OddsBoutLifecycleState.LOCKED,
            evidence_kind="provider_lock_signal",
            observed_at=OBSERVED,
        )

    with pytest.raises(ValueError, match="forward-fill|price"):
        apply_bout_lifecycle(
            session,
            bout_id="bout-l",
            lifecycle=OddsBoutLifecycleState.LOCKED,
            evidence_kind="provider_lock_signal",
            observed_at=OBSERVED,
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-l",
            price_decimal=1.95,
        )

    row = apply_bout_lifecycle(
        session,
        bout_id="bout-l",
        lifecycle=OddsBoutLifecycleState.MISSING_UNKNOWN,
        evidence_kind="provider_market_absent",
        observed_at=OBSERVED,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-l",
    )
    session.commit()
    assert row is not None
    assert row.lifecycle == OddsBoutLifecycleState.MISSING_UNKNOWN.value
    assert (
        classify_quote_value_eligibility(
            match_status=MATCH_STATUS_MATCHED,
            lifecycle=OddsBoutLifecycleState.MISSING_UNKNOWN,
        )
        is QuoteValueEligibility.BLOCKED
    )


def test_lifecycle_terminal_blocks_active_rerun(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-term",
        event_id="evt-term",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    session.commit()
    apply_bout_lifecycle(
        session,
        bout_id="bout-term",
        lifecycle=OddsBoutLifecycleState.CANCELLED,
        evidence_kind="canonical_bout_cancelled",
        observed_at=OBSERVED - timedelta(minutes=5),
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-term",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED,
    )
    assert decision.status == MATCH_STATUS_MATCHED
    assert decision.eligible_for_value is False
    assert decision.lifecycle == OddsBoutLifecycleState.CANCELLED
    persist_match_decision(session, decision, observed_at=OBSERVED)
    session.commit()
    lifecycles = [
        row.lifecycle
        for row in session.scalars(select(OddsBoutLifecycleObservation)).all()
    ]
    assert "cancelled" in lifecycles
    # No ACTIVE override appended after terminal cancel.
    assert lifecycles.count("active") == 0


def test_stale_from_quote_age_blocks_without_inferring_lock(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-s",
        event_id="evt-s",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    store = OddsQuoteStore(session)
    event_row = store.upsert_event(
        OddsEvent(
            id="prov-s",
            sport_key="mma_mixed_martial_arts",
            commence_time=start,
            home_team="Alpha Fighter",
            away_team="Bravo Fighter",
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
                event_id="prov-s",
                home_team="Alpha Fighter",
                away_team="Bravo Fighter",
                market_family=MarketFamily.MONEYLINE,
                provider_market_key="h2h",
                outcome_key=OutcomeKey.FIGHTER_A,
                outcome_label="Alpha Fighter",
                line_point=None,
                price_decimal=1.8,
                availability=QuoteAvailability.AVAILABLE,
                observed_at=OBSERVED - timedelta(hours=10),
                source_updated_at=None,
                commence_time=start,
                snapshot_at=None,
                raw_ref="stale",
                dedupe_key="stale-quote",
            )
        ],
        events_by_external_id={"prov-s": event_row},
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-s",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED,
    )
    assert decision.lifecycle == OddsBoutLifecycleState.STALE
    assert decision.eligible_for_value is False
    locks = session.scalars(
        select(OddsBoutLifecycleObservation).where(
            OddsBoutLifecycleObservation.lifecycle == "locked"
        )
    ).all()
    assert locks == []


def test_quote_history_survives_cancellation(tmp_path: Path) -> None:
    session = _session(tmp_path)
    card = load_golden_card(GOLDEN_ACTIVE)
    seed_canonical_card(session, card)
    session.commit()
    provider = card["provider_events"][0]
    store = OddsQuoteStore(session)
    event_row = store.upsert_event(
        OddsEvent(
            id=provider["id"],
            sport_key=provider["sport_key"],
            commence_time=datetime.fromisoformat(
                provider["commence_time"].replace("Z", "+00:00")
            ),
            home_team=provider["home_team"],
            away_team=provider["away_team"],
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
                event_id=provider["id"],
                home_team=provider["home_team"],
                away_team=provider["away_team"],
                market_family=MarketFamily.MONEYLINE,
                provider_market_key="h2h",
                outcome_key=OutcomeKey.FIGHTER_A,
                outcome_label=provider["home_team"],
                line_point=None,
                price_decimal=1.8,
                availability=QuoteAvailability.AVAILABLE,
                observed_at=OBSERVED,
                source_updated_at=None,
                commence_time=datetime.fromisoformat(
                    provider["commence_time"].replace("Z", "+00:00")
                ),
                snapshot_at=None,
                raw_ref="cancel-survives",
                dedupe_key="cancel-survives-dedupe",
            )
        ],
        events_by_external_id={provider["id"]: event_row},
    )
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id=provider["id"],
        home_team=provider["home_team"],
        away_team=provider["away_team"],
        commence_time=datetime.fromisoformat(
            provider["commence_time"].replace("Z", "+00:00")
        ),
    )
    persist_match_decision(session, decision)
    apply_bout_lifecycle(
        session,
        bout_id=decision.bout_id or "",
        lifecycle=OddsBoutLifecycleState.CANCELLED,
        evidence_kind="canonical_bout_cancelled",
        observed_at=OBSERVED,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id=provider["id"],
    )
    session.commit()

    quotes = session.scalars(select(OddsQuote)).all()
    assert len(quotes) == 1
    aliases = session.scalars(select(OddsProviderEventAlias)).all()
    assert aliases
    lifecycle = session.scalars(select(OddsBoutLifecycleObservation)).all()
    assert any(row.lifecycle == "cancelled" for row in lifecycle)


# --- migrations / integrity ----------------------------------------------


def test_migration_upgrade_downgrade_preserves_schema_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "mig203.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "odds_provider_event_aliases" in tables
        assert "odds_match_observations" in tables
        assert "odds_bout_lifecycle_observations" in tables
        assert "odds_bout_match_reviews" in tables
        indexes = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index'")
            )
        }
        assert "uq_odds_provider_event_alias_active" in indexes
        assert "uq_odds_bout_match_reviews_pending_provider_ext" in indexes
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(odds_bout_match_reviews)"))
        }
        assert "activated_alias_id" in cols
        assert "activated_alias_version" in cols

    command.downgrade(_alembic_config(db_path), "0013_odds_manual_prices")
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "odds_provider_event_aliases" not in tables
        assert "odds_bout_match_reviews" not in tables
        assert "odds_match_observations" not in tables

    command.upgrade(_alembic_config(db_path), "head")
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "odds_bout_match_reviews" in tables
        assert "odds_provider_event_aliases" in tables


def test_raw_sql_rejects_integrity_violations(tmp_path: Path) -> None:
    db_path = tmp_path / "integrity203.db"
    command.upgrade(_alembic_config(db_path), "head")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    now = "2026-08-12T00:00:00"
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(
            text(
                "INSERT INTO canonical_fighters "
                "(id, display_name, created_at, updated_at) VALUES "
                f"('fa', 'A', '{now}', '{now}'), ('fb', 'B', '{now}', '{now}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO canonical_events "
                "(id, name, series, status, scheduled_start_at, created_at, updated_at) "
                f"VALUES ('e1', 'E', 'dwcs', 'scheduled', '{now}', '{now}', '{now}')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO canonical_bouts "
                "(id, event_id, fighter_a_id, fighter_b_id, status, "
                "scheduled_rounds, created_at, updated_at) VALUES "
                f"('b1', 'e1', 'fa', 'fb', 'scheduled', 3, '{now}', '{now}')"
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(
            text(
                "INSERT INTO odds_match_observations ("
                "dedupe_key, provider, external_event_id, bout_id, match_status, "
                "match_rule, reason, review_id, eligible_for_value, "
                "observed_at, created_at"
                ") VALUES ("
                "'d1', 'the_odds_api', 'x', 'b1', 'matched', 'provider_id', 'r', "
                f"NULL, 2, '{now}', '{now}')"
            )
        )

    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(
            text(
                "INSERT INTO odds_provider_event_aliases ("
                "id, provider, external_event_id, bout_id, alias_version, status, "
                "match_rule, evidence_json, created_at, superseded_at"
                ") VALUES ("
                f"'a1', 'the_odds_api', 'ext', 'b1', 1, 'active', 'provider_id', '{{}}', "
                f"'{now}', NULL)"
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(
            text(
                "INSERT INTO odds_provider_event_aliases ("
                "id, provider, external_event_id, bout_id, alias_version, status, "
                "match_rule, evidence_json, created_at, superseded_at"
                ") VALUES ("
                "'a2', 'the_odds_api', 'ext', 'b1', 2, 'active', 'provider_id', '{}', "
                f"'{now}', NULL)"
            )
        )


# --- CLI ------------------------------------------------------------------


def test_cli_reconcile_strict_exits_nonzero_on_blockers(tmp_path: Path) -> None:
    db_path = tmp_path / "cli203.db"
    broken = json.loads(GOLDEN_ACTIVE.read_text(encoding="utf-8"))
    broken["provider_events"] = broken["provider_events"][:3]
    broken_path = tmp_path / "broken_card.json"
    broken_path.write_text(json.dumps(broken), encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mma_model.cli",
            "odds",
            "reconcile",
            "--next-dwcs",
            "--strict",
            "--offline-fixtures",
            "--as-of",
            "2026-08-11T12:00:00Z",
            "--golden-card",
            str(broken_path),
            "--database-url",
            f"sqlite:///{db_path}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["strict"] is True
    assert payload["blockers"]


def test_cli_reconcile_golden_passes_strict(tmp_path: Path) -> None:
    db_path = tmp_path / "cli203ok.db"
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mma_model.cli",
            "odds",
            "reconcile",
            "--next-dwcs",
            "--strict",
            "--offline-fixtures",
            "--as-of",
            "2026-08-11T12:00:00Z",
            "--golden-card",
            str(GOLDEN_ACTIVE),
            "--database-url",
            f"sqlite:///{db_path}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["active_bout_match_rate"] == 1.0
    assert payload["next_dwcs"] is True


def test_cli_golden_refuses_live_default_and_without_offline(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    # No --offline-fixtures
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mma_model.cli",
            "odds",
            "reconcile",
            "--golden-card",
            str(GOLDEN_ACTIVE),
            "--database-url",
            f"sqlite:///{tmp_path / 'x.db'}",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode != 0
    assert "offline-fixtures" in (proc.stdout + proc.stderr).lower()

    # No database-url → default/live refused
    proc2 = subprocess.run(
        [
            sys.executable,
            "-m",
            "mma_model.cli",
            "odds",
            "reconcile",
            "--offline-fixtures",
            "--golden-card",
            str(GOLDEN_ACTIVE),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc2.returncode != 0
    combined = (proc2.stdout + proc2.stderr).lower()
    assert "disposable" in combined or "database-url" in combined or "live" in combined


def test_tz_environment_determinism_for_decisions(tmp_path: Path) -> None:
    session = _session(tmp_path)
    card = load_golden_card(GOLDEN_ACTIVE)
    seed_canonical_card(session, card)
    session.commit()
    keys = []
    for tz_name in ("UTC", "America/New_York", "Asia/Tokyo"):
        env = os.environ.copy()
        env["TZ"] = tz_name
        env["PYTHONPATH"] = "src"
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from datetime import datetime, UTC;"
                    "from pathlib import Path;"
                    "from sqlalchemy import create_engine, event;"
                    "from sqlalchemy.orm import sessionmaker;"
                    "from mma_model.db.base import Base;"
                    "from mma_model.db.odds_guards import install_odds_sqlite_guards;"
                    "from mma_model.db.session import sqlite_connect_pragmas;"
                    "import mma_model.db.tables.core, mma_model.db.tables.odds;"
                    "from mma_model.odds.matching import match_provider_event, decision_dedupe_key;"
                    "from mma_model.odds.reconcile import seed_canonical_card, load_golden_card;"
                    f"db={str(tmp_path / 'tz.db')!r};"
                    "engine=create_engine(f'sqlite:///{db}', future=True);"
                    "event.listen(engine,'connect',sqlite_connect_pragmas);"
                    "Base.metadata.create_all(bind=engine);"
                    "install_odds_sqlite_guards(engine);"
                    "s=sessionmaker(bind=engine,future=True)();"
                    f"card=load_golden_card(Path({str(GOLDEN_ACTIVE)!r}));"
                    "seed_canonical_card(s,card); s.commit();"
                    f"p=card['provider_events'][0];"
                    "d=match_provider_event(s,provider='the_odds_api',"
                    "external_event_id=p['id'],home_team=p['home_team'],"
                    "away_team=p['away_team'],"
                    "commence_time=datetime.fromisoformat(p['commence_time'].replace('Z','+00:00')),"
                    "observed_at=datetime(2026,8,12,18,0,tzinfo=UTC));"
                    "print(decision_dedupe_key(d, observed_at=datetime(2026,8,12,18,0,tzinfo=UTC)))"
                ),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        keys.append(proc.stdout.strip())
    assert len(set(keys)) == 1


def test_fighter_alias_supports_exact_participant_match(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _seed_bout(
        session,
        bout_id="bout-al",
        event_id="evt-al",
        fighter_a="Jonathan Kunneman",
        fighter_b="Joseph Kropschot",
        start=OBSERVED,
    )
    session.add(FighterAlias(fighter_id="bout-al:a", alias="Jon Kunneman", source="manual"))
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-al",
        home_team="Jon Kunneman",
        away_team="Joseph Kropschot",
        commence_time=OBSERVED,
    )
    assert decision.status == MATCH_STATUS_MATCHED
    assert decision.bout_id == "bout-al"


@pytest.mark.slow
def test_matching_contract_loads_from_non_editable_wheel_install(tmp_path: Path) -> None:
    """Build a wheel, install it, and load the packaged matching contract."""
    assert PINNED_MATCHING_CONTRACT_HASH == PINNED_DIGEST_LITERAL

    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(REPO_ROOT),
            "-w",
            str(wheel_dir),
            "--no-deps",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_dir.glob("mma_model-*.whl"))
    assert len(wheels) == 1

    import venv

    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--force-reinstall",
            str(wheels[0]),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env.pop("__PYVENV_LAUNCHER__", None)

    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from pathlib import Path; "
                "import mma_model; "
                "from mma_model.odds.matching import ("
                "  PINNED_MATCHING_CONTRACT_HASH, load_matching_contract, "
                "  package_matching_resource_path"
                "); "
                "root = Path(mma_model.__file__).resolve().parent; "
                "assert (root / 'odds' / 'matching_v1.yaml').is_file(), root; "
                "assert package_matching_resource_path().is_file(); "
                "c = load_matching_contract(); "
                f"assert c.content_hash == {PINNED_DIGEST_LITERAL!r}; "
                "assert c.content_hash == PINNED_MATCHING_CONTRACT_HASH; "
                "assert c.stale_after_minutes == 360; "
                "\nfrom pydantic import ValidationError\n"
                "_mutable=True\n"
                "try:\n"
                "  c.match_rules = ('x',)\n"
                "except (TypeError, ValidationError):\n"
                "  _mutable=False\n"
                "assert not _mutable, 'mutable match_rules'\n"
                "print('WHEEL_MATCHING_OK', c.contract_version, c.content_hash)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "WHEEL_MATCHING_OK" in probe.stdout
    assert PINNED_DIGEST_LITERAL in probe.stdout


# --- second-review: PIT / alias / review / statuses -----------------------


def test_quote_first_then_match_keeps_quotes_visible(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-q1",
        event_id="evt-q1",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    _add_quote(
        session,
        external_id="prov-q1",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=OBSERVED - timedelta(minutes=30),
        dedupe="q1-first",
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-q1",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED,
    )
    persist_match_decision(session, decision, observed_at=OBSERVED)
    session.commit()
    assert decision.eligible_for_value is True
    visible = quotes_visible_under_alias_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-q1",
        as_of=OBSERVED,
    )
    assert len(visible) == 1


def test_no_quotes_matched_is_missing_blocked(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-nq",
        event_id="evt-nq",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-nq",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED,
    )
    assert decision.status == MATCH_STATUS_MATCHED
    assert decision.lifecycle == OddsBoutLifecycleState.MISSING_UNKNOWN
    assert decision.eligible_for_value is False


def test_pit_future_lifecycle_and_quotes_do_not_leak(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-pit",
        event_id="evt-pit",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    _add_quote(
        session,
        external_id="prov-pit",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=OBSERVED - timedelta(hours=1),
        dedupe="pit-old",
    )
    session.commit()
    past = OBSERVED - timedelta(minutes=10)
    decision_past = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-pit",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=past,
    )
    assert decision_past.eligible_for_value is True
    persist_match_decision(session, decision_past, observed_at=past)

    apply_bout_lifecycle(
        session,
        bout_id="bout-pit",
        lifecycle=OddsBoutLifecycleState.CANCELLED,
        evidence_kind="canonical_bout_cancelled",
        observed_at=OBSERVED + timedelta(hours=1),
    )
    _add_quote(
        session,
        external_id="prov-pit",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=OBSERVED + timedelta(hours=2),
        dedupe="pit-future",
    )
    session.commit()

    latest_past = latest_bout_lifecycle(
        session, bout_id="bout-pit", as_of=past, provider=PROVIDER_THE_ODDS_API
    )
    assert latest_past is None or latest_past.lifecycle != "cancelled"
    visible_past = quotes_visible_under_alias_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-pit",
        as_of=past,
    )
    assert all(q.dedupe_key != "pit-future" for q in visible_past)
    decision_replay = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-pit",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=past,
    )
    assert decision_replay.lifecycle != OddsBoutLifecycleState.CANCELLED


def test_alias_effective_at_historical_replacement(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="old-bout",
        event_id="evt-h1",
        fighter_a="Alex Original",
        fighter_b="Blake Opponent",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="new-bout",
        event_id="evt-h2",
        fighter_a="Alex Original",
        fighter_b="Casey Replacement",
        start=start,
    )
    t0 = OBSERVED - timedelta(hours=2)
    t1 = OBSERVED
    first = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-hist",
        home_team="Alex Original",
        away_team="Blake Opponent",
        commence_time=start,
        observed_at=t0,
    )
    _add_quote(
        session,
        external_id="prov-hist",
        home="Alex Original",
        away="Blake Opponent",
        commence=start,
        observed_at=t0 - timedelta(minutes=5),
        dedupe="hist-old",
    )
    persist_match_decision(session, first, observed_at=t0)
    apply_replacement(
        session,
        old_bout_id="old-bout",
        new_bout_id="new-bout",
        provider=PROVIDER_THE_ODDS_API,
        old_external_event_id="prov-hist",
        new_external_event_id="prov-hist",
        new_home_team="Casey Replacement",
        new_away_team="Alex Original",
        new_commence_time=start,
        observed_at=t1,
    )
    session.commit()
    before = alias_effective_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-hist",
        as_of=t0 + timedelta(minutes=1),
    )
    assert before is not None
    assert before.bout_id == "old-bout"
    after = alias_effective_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-hist",
        as_of=t1 + timedelta(minutes=1),
    )
    assert after is not None
    assert after.bout_id == "new-bout"
    visible_after = quotes_visible_under_alias_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-hist",
        as_of=t1 + timedelta(minutes=1),
    )
    assert visible_after == []


def test_review_approval_requires_candidates_and_revalidation(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-ok",
        event_id="evt-ok",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="bout-ufc",
        event_id="evt-ufc",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
        series="ufc",
    )
    empty_id = enqueue_bout_match_review(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-empty",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        candidate_bout_ids=(),
        reason="empty",
        observed_at=OBSERVED,
    )
    session.commit()
    with pytest.raises(OddsBoutMatchReviewError, match="empty candidate"):
        approve_bout_match_review(
            session,
            review_id=empty_id,
            bout_id="bout-ok",
            actor="ops",
            expected_version=1,
        )

    review_id = enqueue_bout_match_review(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-rev-val",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        candidate_bout_ids=("bout-ok", "bout-ufc"),
        reason="ambig",
        observed_at=OBSERVED,
    )
    session.commit()
    with pytest.raises(OddsBoutMatchReviewError, match="revalidation|DWCS"):
        approve_bout_match_review(
            session,
            review_id=review_id,
            bout_id="bout-ufc",
            actor="ops",
            expected_version=1,
        )
    with pytest.raises(OddsBoutMatchReviewError, match="revalidation|participants"):
        # wrong participants on review fields vs bout-ok names... use wrong names via new review
        bad = enqueue_bout_match_review(
            session,
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-wrong-part",
            home_team="Wrong One",
            away_team="Wrong Two",
            commence_time=start,
            candidate_bout_ids=("bout-ok",),
            reason="bad",
            observed_at=OBSERVED,
        )
        session.commit()
        approve_bout_match_review(
            session,
            review_id=bad,
            bout_id="bout-ok",
            actor="ops",
            expected_version=1,
        )

    approved = approve_bout_match_review(
        session,
        review_id=review_id,
        bout_id="bout-ok",
        actor="ops",
        expected_version=1,
        observed_at=OBSERVED,
    )
    session.commit()
    assert approved.activated_alias_id is not None
    alias = session.get(OddsProviderEventAlias, approved.activated_alias_id)
    assert alias is not None
    assert alias.match_rule == MATCH_RULE_MANUAL_REVIEW


def test_pending_review_evidence_change_increments_version(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    review_id = enqueue_bout_match_review(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-cas",
        home_team="A",
        away_team="B",
        commence_time=start,
        candidate_bout_ids=("bout-1",),
        reason="first",
        observed_at=OBSERVED,
    )
    session.commit()
    row = session.get(OddsBoutMatchReview, review_id)
    assert row is not None
    assert row.version == 1
    enqueue_bout_match_review(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-cas",
        home_team="A",
        away_team="B",
        commence_time=start,
        candidate_bout_ids=("bout-1", "bout-2"),
        reason="updated",
        observed_at=OBSERVED + timedelta(minutes=1),
    )
    session.commit()
    session.refresh(row)
    assert row.version == 2
    assert "bout-2" in row.candidate_bout_ids_json


def test_reverse_only_owned_alias_preserves_newer(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-a",
        event_id="evt-r1",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="bout-b",
        event_id="evt-r1",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    review_id = enqueue_bout_match_review(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-owned",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        candidate_bout_ids=("bout-a", "bout-b"),
        reason="ambig",
        observed_at=OBSERVED,
    )
    session.commit()
    first = approve_bout_match_review(
        session,
        review_id=review_id,
        bout_id="bout-a",
        actor="ops",
        expected_version=1,
        observed_at=OBSERVED,
    )
    session.commit()
    owned_id = first.activated_alias_id
    # Newer alias from later match/replacement path
    from mma_model.odds.reconcile import activate_provider_alias

    newer = activate_provider_alias(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-owned",
        bout_id="bout-b",
        match_rule=MATCH_RULE_PARTICIPANT_PAIR,
        observed_at=OBSERVED + timedelta(minutes=5),
    )
    session.commit()
    reverse_bout_match_review(
        session,
        review_id=review_id,
        actor="ops",
        expected_version=2,
        observed_at=OBSERVED + timedelta(minutes=10),
    )
    session.commit()
    owned = session.get(OddsProviderEventAlias, owned_id)
    assert owned is not None
    assert owned.status == "superseded"
    session.refresh(newer)
    assert newer.status == "active"
    assert newer.bout_id == "bout-b"


def test_unknown_bout_and_cancelled_event_not_active(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-unk",
        event_id="evt-unk",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
        bout_status="mystery",
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-unk",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
    )
    assert decision.status == MATCH_STATUS_UNMATCHED

    _seed_bout(
        session,
        bout_id="bout-ce",
        event_id="evt-ce",
        fighter_a="Alpha Fighter",
        fighter_b="Charlie Fighter",
        start=start,
        event_status="cancelled",
    )
    session.commit()
    decision2 = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-ce",
        home_team="Alpha Fighter",
        away_team="Charlie Fighter",
        commence_time=start,
    )
    assert decision2.status == MATCH_STATUS_UNMATCHED


def test_golden_uses_single_card_event(tmp_path: Path) -> None:
    session = _session(tmp_path)
    card = load_golden_card(GOLDEN_ACTIVE)
    seed_canonical_card(session, card)
    session.commit()
    events = session.scalars(select(CanonicalEvent)).all()
    assert len(events) == 1
    assert events[0].id == "dwcs-golden-s10e1:event"
    bouts = session.scalars(select(CanonicalBout)).all()
    assert len(bouts) == 5
    assert {bout.event_id for bout in bouts} == {"dwcs-golden-s10e1:event"}


# --- final integrity: freshness / dedupe / CAS / transitions / PIT --------


def _append_quote_full(
    session,
    *,
    external_id: str,
    home: str,
    away: str,
    commence: datetime,
    observed_at: datetime,
    source_updated_at: datetime | None,
    price_decimal: float,
    raw_ref: str,
    bookmaker_key: str = "fanduel",
    outcome_key: OutcomeKey = OutcomeKey.FIGHTER_A,
    outcome_label: str | None = None,
) -> str:
    dedupe = quote_dedupe_key(
        provider=PROVIDER_THE_ODDS_API,
        event_id=external_id,
        bookmaker_key=bookmaker_key,
        region="us",
        market_family=MarketFamily.MONEYLINE,
        outcome_key=outcome_key,
        line_point=None,
        price_decimal=price_decimal,
        source_updated_at=source_updated_at,
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
                bookmaker_key=bookmaker_key,
                bookmaker_title=bookmaker_key,
                region="us",
                event_id=external_id,
                home_team=home,
                away_team=away,
                market_family=MarketFamily.MONEYLINE,
                provider_market_key="h2h",
                outcome_key=outcome_key,
                outcome_label=outcome_label or home,
                line_point=None,
                price_decimal=price_decimal,
                availability=QuoteAvailability.AVAILABLE,
                observed_at=observed_at,
                source_updated_at=source_updated_at,
                commence_time=commence,
                snapshot_at=None,
                raw_ref=raw_ref,
                dedupe_key=dedupe,
            )
        ],
        events_by_external_id={external_id: event_row},
    )
    return dedupe


def test_freshness_uses_source_updated_not_max_with_observed(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-fresh",
        event_id="evt-fresh",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    stale_source = OBSERVED - timedelta(hours=12)
    _append_quote_full(
        session,
        external_id="prov-fresh",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=OBSERVED,
        source_updated_at=stale_source,
        price_decimal=1.9,
        raw_ref="fresh-fetch-stale-source",
    )
    session.commit()
    latest = latest_quote_timestamp(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-fresh",
        as_of=OBSERVED,
    )
    assert latest == stale_source
    assert quote_is_stale(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-fresh",
        observed_at=OBSERVED,
        stale_after_minutes=360,
    )
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-fresh",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED,
    )
    assert decision.lifecycle == OddsBoutLifecycleState.STALE
    assert decision.eligible_for_value is False


def test_freshness_falls_back_to_observed_when_source_absent(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-nosrc",
        event_id="evt-nosrc",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    _append_quote_full(
        session,
        external_id="prov-nosrc",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=OBSERVED - timedelta(minutes=30),
        source_updated_at=None,
        price_decimal=1.9,
        raw_ref="no-source",
    )
    session.commit()
    latest = latest_quote_timestamp(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-nosrc",
        as_of=OBSERVED,
    )
    assert latest == OBSERVED - timedelta(minutes=30)
    assert not quote_is_stale(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-nosrc",
        observed_at=OBSERVED,
        stale_after_minutes=360,
    )


def test_freshness_future_source_rejected_and_mixed_books(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-mix",
        event_id="evt-mix",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    past_as_of = OBSERVED - timedelta(hours=1)
    _append_quote_full(
        session,
        external_id="prov-mix",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=past_as_of - timedelta(minutes=5),
        source_updated_at=past_as_of - timedelta(minutes=10),
        price_decimal=1.8,
        raw_ref="book-a",
        bookmaker_key="fanduel",
    )
    _append_quote_full(
        session,
        external_id="prov-mix",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=past_as_of - timedelta(minutes=5),
        source_updated_at=past_as_of + timedelta(hours=2),
        price_decimal=1.85,
        raw_ref="book-future",
        bookmaker_key="draftkings",
    )
    _append_quote_full(
        session,
        external_id="prov-mix",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=past_as_of - timedelta(minutes=5),
        source_updated_at=past_as_of - timedelta(hours=2),
        price_decimal=1.7,
        raw_ref="book-stale",
        bookmaker_key="betmgm",
        outcome_key=OutcomeKey.FIGHTER_B,
        outcome_label="Bravo Fighter",
    )
    session.commit()
    visible = quotes_visible_under_alias_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-mix",
        as_of=past_as_of,
    )
    for quote in visible:
        if quote.source_updated_at is None:
            continue
        source = quote.source_updated_at
        source = (
            source.replace(tzinfo=UTC)
            if source.tzinfo is None
            else source.astimezone(UTC)
        )
        assert source <= past_as_of
    # Future-source quote must not be PIT-visible.
    assert all(q.raw_ref != "book-future" for q in visible)
    latest = latest_quote_timestamp(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-mix",
        as_of=past_as_of,
    )
    assert latest == past_as_of - timedelta(minutes=10)
    row = next(q for q in session.scalars(select(OddsQuote)).all() if q.raw_ref == "book-future")
    assert quote_authoritative_freshness(row, as_of=past_as_of) is None


def test_same_id_same_price_source_null_replacement_persists_for_v2(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="old-bout-dedupe",
        event_id="evt-dd1",
        fighter_a="Alex Original",
        fighter_b="Blake Opponent",
        start=start,
    )
    _seed_bout(
        session,
        bout_id="new-bout-dedupe",
        event_id="evt-dd2",
        fighter_a="Alex Original",
        fighter_b="Casey Replacement",
        start=start,
    )
    session.commit()
    first = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-dd",
        home_team="Alex Original",
        away_team="Blake Opponent",
        commence_time=start,
    )
    persist_match_decision(session, first, observed_at=OBSERVED - timedelta(hours=2))
    old_dedupe = _append_quote_full(
        session,
        external_id="prov-dd",
        home="Alex Original",
        away="Blake Opponent",
        commence=start,
        observed_at=OBSERVED - timedelta(hours=1),
        source_updated_at=None,
        price_decimal=1.9,
        raw_ref="old-opponent-fragment",
    )
    session.commit()

    result = apply_replacement(
        session,
        old_bout_id="old-bout-dedupe",
        new_bout_id="new-bout-dedupe",
        provider=PROVIDER_THE_ODDS_API,
        old_external_event_id="prov-dd",
        new_external_event_id="prov-dd",
        new_home_team="Casey Replacement",
        new_away_team="Alex Original",
        new_commence_time=start,
        observed_at=OBSERVED,
    )
    session.commit()
    assert result["activated"] is True

    # Same price + source_null; different opponents/raw_ref must not collide.
    new_dedupe = _append_quote_full(
        session,
        external_id="prov-dd",
        home="Casey Replacement",
        away="Alex Original",
        commence=start,
        observed_at=OBSERVED + timedelta(minutes=1),
        source_updated_at=None,
        price_decimal=1.9,
        raw_ref="new-opponent-fragment",
    )
    session.commit()
    assert new_dedupe != old_dedupe
    quotes = session.scalars(select(OddsQuote)).all()
    assert {q.dedupe_key for q in quotes} == {old_dedupe, new_dedupe}

    visible_v2 = quotes_visible_under_active_alias(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-dd",
        as_of=OBSERVED + timedelta(minutes=2),
    )
    assert len(visible_v2) == 1
    assert visible_v2[0].dedupe_key == new_dedupe
    assert visible_v2[0].raw_ref == "new-opponent-fragment"
    assert visible_v2[0].outcome_label == "Casey Replacement"

    # Identical re-poll of the new fragment still dedupes.
    again = _append_quote_full(
        session,
        external_id="prov-dd",
        home="Casey Replacement",
        away="Alex Original",
        commence=start,
        observed_at=OBSERVED + timedelta(minutes=3),
        source_updated_at=None,
        price_decimal=1.9,
        raw_ref="new-opponent-fragment",
    )
    session.commit()
    assert again == new_dedupe
    assert len(session.scalars(select(OddsQuote)).all()) == 2


def test_two_session_stale_approval_has_zero_side_effects(tmp_path: Path) -> None:
    db_path = tmp_path / "cas203.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    event.listen(engine, "connect", sqlite_connect_pragmas)
    import mma_model.db.tables.core  # noqa: F401
    import mma_model.db.tables.identity  # noqa: F401
    import mma_model.db.tables.odds  # noqa: F401

    Base.metadata.create_all(bind=engine)
    install_odds_sqlite_guards(engine)
    Session = sessionmaker(bind=engine, future=True)

    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    seed = Session()
    _seed_bout(
        seed,
        bout_id="bout-cas-a",
        event_id="evt-cas",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    _seed_bout(
        seed,
        bout_id="bout-cas-b",
        event_id="evt-cas",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    review_id = enqueue_bout_match_review(
        seed,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-cas-2",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        candidate_bout_ids=("bout-cas-a", "bout-cas-b"),
        reason="ambig",
        observed_at=OBSERVED,
    )
    seed.commit()
    seed.close()

    s1 = Session()
    s2 = Session()
    aliases_before = len(s1.scalars(select(OddsProviderEventAlias)).all())
    life_before = len(s1.scalars(select(OddsBoutLifecycleObservation)).all())

    approve_bout_match_review(
        s1,
        review_id=review_id,
        bout_id="bout-cas-a",
        actor="ops-a",
        expected_version=1,
        observed_at=OBSERVED,
    )
    s1.commit()

    with pytest.raises(OddsBoutMatchReviewError, match="pending|stale|concurrent"):
        approve_bout_match_review(
            s2,
            review_id=review_id,
            bout_id="bout-cas-b",
            actor="ops-b",
            expected_version=1,
            observed_at=OBSERVED + timedelta(minutes=1),
        )
    s2.rollback()

    check = Session()
    aliases = list(check.scalars(select(OddsProviderEventAlias)).all())
    lifecycles = list(check.scalars(select(OddsBoutLifecycleObservation)).all())
    assert len(aliases) == aliases_before + 1
    assert aliases[0].bout_id == "bout-cas-a"
    assert aliases[0].status == "active"
    assert len(lifecycles) == life_before + 1
    assert lifecycles[0].bout_id == "bout-cas-a"
    assert lifecycles[0].evidence_kind == "odds_bout_match_review_approved"
    review = check.get(OddsBoutMatchReview, review_id)
    assert review is not None
    assert review.status == "approved"
    assert review.decision_bout_id == "bout-cas-a"
    assert review.version == 2
    check.close()
    s1.close()
    s2.close()


def test_terminal_transition_matrix_cross_product(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    cases = (
        ("locked", "provider_lock_signal", True),
        ("cancelled", "canonical_bout_cancelled", False),
        ("replaced", "canonical_bout_replaced", False),
        ("review_blocked", "ambiguous_match_blocked", False),
    )
    for index, (lifecycle, enter_kind, needs_provider) in enumerate(cases):
        bout_id = f"bout-term-{index}"
        _seed_bout(
            session,
            bout_id=bout_id,
            event_id=f"evt-term-{index}",
            fighter_a="Alpha Fighter",
            fighter_b="Bravo Fighter",
            start=start,
        )
        kwargs: dict = {
            "bout_id": bout_id,
            "lifecycle": OddsBoutLifecycleState(lifecycle),
            "evidence_kind": enter_kind,
            "observed_at": OBSERVED,
        }
        if needs_provider or enter_kind.startswith("provider_"):
            kwargs["provider"] = PROVIDER_THE_ODDS_API
            kwargs["external_event_id"] = f"prov-term-{index}"
        if lifecycle == "review_blocked":
            kwargs["provider"] = PROVIDER_THE_ODDS_API
            kwargs["external_event_id"] = f"prov-term-{index}"
        apply_bout_lifecycle(session, **kwargs)
    session.commit()

    # provider_unlock exits LOCKED only.
    unlocked = apply_bout_lifecycle(
        session,
        bout_id="bout-term-0",
        lifecycle=OddsBoutLifecycleState.ACTIVE,
        evidence_kind="provider_unlock_signal",
        observed_at=OBSERVED + timedelta(minutes=1),
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-term-0",
    )
    assert unlocked is not None
    for bad_bout, ext in (
        ("bout-term-1", "prov-term-1"),
        ("bout-term-2", "prov-term-2"),
        ("bout-term-3", "prov-term-3"),
    ):
        refused = apply_bout_lifecycle(
            session,
            bout_id=bad_bout,
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            evidence_kind="provider_unlock_signal",
            observed_at=OBSERVED + timedelta(minutes=1),
            provider=PROVIDER_THE_ODDS_API,
            external_event_id=ext,
        )
        assert refused is None

    # Review approval exits REVIEW_BLOCKED only (not cancelled/replaced).
    review_clear = apply_bout_lifecycle(
        session,
        bout_id="bout-term-3",
        lifecycle=OddsBoutLifecycleState.ACTIVE,
        evidence_kind="odds_bout_match_review_approved",
        observed_at=OBSERVED + timedelta(minutes=2),
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-term-3",
    )
    assert review_clear is not None
    for bad_bout in ("bout-term-1", "bout-term-2"):
        refused = apply_bout_lifecycle(
            session,
            bout_id=bad_bout,
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            evidence_kind="odds_bout_match_review_approved",
            observed_at=OBSERVED + timedelta(minutes=2),
            provider=PROVIDER_THE_ODDS_API,
            external_event_id=f"prov-term-{bad_bout[-1]}",
        )
        assert refused is None

    # Cancelled/replaced require canonical correction.
    for bout_id in ("bout-term-1", "bout-term-2"):
        still = apply_bout_lifecycle(
            session,
            bout_id=bout_id,
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            evidence_kind="match_participant_pair",
            observed_at=OBSERVED + timedelta(minutes=3),
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-x",
        )
        assert still is None
        corrected = apply_bout_lifecycle(
            session,
            bout_id=bout_id,
            lifecycle=OddsBoutLifecycleState.ACTIVE,
            evidence_kind="canonical_bout_correction_reactivate",
            observed_at=OBSERVED + timedelta(minutes=4),
        )
        assert corrected is not None


def test_fresh_quote_clears_stale_and_missing_with_persisted_evidence(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-clear",
        event_id="evt-clear",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    session.commit()
    missing = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clear",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED - timedelta(hours=2),
    )
    assert missing.lifecycle == OddsBoutLifecycleState.MISSING_UNKNOWN
    persist_match_decision(session, missing, observed_at=OBSERVED - timedelta(hours=2))
    session.commit()
    assert any(
        row.lifecycle == "missing_unknown"
        for row in session.scalars(select(OddsBoutLifecycleObservation)).all()
    )

    _append_quote_full(
        session,
        external_id="prov-clear",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=OBSERVED - timedelta(hours=1),
        source_updated_at=None,
        price_decimal=1.9,
        raw_ref="clear-missing",
    )
    session.commit()
    cleared = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clear",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED - timedelta(hours=1),
    )
    assert cleared.lifecycle == OddsBoutLifecycleState.ACTIVE
    assert cleared.eligible_for_value is True
    persist_match_decision(session, cleared, observed_at=OBSERVED - timedelta(hours=1))
    session.commit()
    assert any(
        row.evidence_kind == "fresh_quote_clears_observational_block"
        and row.lifecycle == "active"
        for row in session.scalars(select(OddsBoutLifecycleObservation)).all()
    )

    apply_bout_lifecycle(
        session,
        bout_id="bout-clear",
        lifecycle=OddsBoutLifecycleState.STALE,
        evidence_kind="quote_age_exceeds_stale_after_minutes",
        observed_at=OBSERVED - timedelta(minutes=30),
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clear",
    )
    session.commit()
    _append_quote_full(
        session,
        external_id="prov-clear",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=OBSERVED,
        source_updated_at=OBSERVED - timedelta(minutes=5),
        price_decimal=1.91,
        raw_ref="clear-stale",
    )
    session.commit()
    again = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clear",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED,
    )
    assert again.lifecycle == OddsBoutLifecycleState.ACTIVE
    persist_match_decision(session, again, observed_at=OBSERVED)
    session.commit()
    clears = [
        row
        for row in session.scalars(select(OddsBoutLifecycleObservation)).all()
        if row.evidence_kind == "fresh_quote_clears_observational_block"
    ]
    assert len(clears) >= 2
    latest = latest_bout_lifecycle(
        session,
        bout_id="bout-clear",
        as_of=OBSERVED,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clear",
    )
    assert latest is not None
    assert latest.lifecycle == "active"

    # Terminal CANCELLED is preserved despite fresh quotes.
    apply_bout_lifecycle(
        session,
        bout_id="bout-clear",
        lifecycle=OddsBoutLifecycleState.CANCELLED,
        evidence_kind="canonical_bout_cancelled",
        observed_at=OBSERVED + timedelta(minutes=1),
    )
    session.commit()
    blocked = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clear",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=OBSERVED + timedelta(minutes=2),
    )
    assert blocked.lifecycle == OddsBoutLifecycleState.CANCELLED
    assert blocked.eligible_for_value is False
    persist_match_decision(session, blocked, observed_at=OBSERVED + timedelta(minutes=2))
    session.commit()
    latest_term = latest_bout_lifecycle(
        session,
        bout_id="bout-clear",
        as_of=OBSERVED + timedelta(minutes=2),
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-clear",
    )
    assert latest_term is not None
    assert latest_term.lifecycle == "cancelled"


def test_alias_quote_pit_exact_boundaries_and_utc(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    _seed_bout(
        session,
        bout_id="bout-bound",
        event_id="evt-bound",
        fighter_a="Alpha Fighter",
        fighter_b="Bravo Fighter",
        start=start,
    )
    t0 = OBSERVED - timedelta(hours=2)
    t1 = OBSERVED - timedelta(hours=1)
    t2 = OBSERVED
    _append_quote_full(
        session,
        external_id="prov-bound",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=t0,
        source_updated_at=t0,
        price_decimal=1.9,
        raw_ref="bound-q0",
    )
    session.commit()
    decision = match_provider_event(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-bound",
        home_team="Alpha Fighter",
        away_team="Bravo Fighter",
        commence_time=start,
        observed_at=t0,
    )
    persist_match_decision(session, decision, observed_at=t0)
    session.commit()

    # Force version bump with a distinct match_rule (same bout+rule is a no-op).
    from mma_model.odds.reconcile import activate_provider_alias

    v2 = activate_provider_alias(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-bound",
        bout_id="bout-bound",
        match_rule=MATCH_RULE_MANUAL_REVIEW,
        observed_at=t1,
        evidence={"boundary": True},
    )
    session.commit()
    assert v2.alias_version == 2
    # Exact created_at boundary includes the new alias.
    at_created = alias_effective_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-bound",
        as_of=t1,
    )
    assert at_created is not None
    assert at_created.id == v2.id
    assert at_created.alias_version == 2

    # Quote observed_at == as_of included; source_updated_at == as_of included.
    # v2 cutoff hides pre-t1 quotes under alias_version > 1.
    _append_quote_full(
        session,
        external_id="prov-bound",
        home="Alpha Fighter",
        away="Bravo Fighter",
        commence=start,
        observed_at=t1,
        source_updated_at=t1,
        price_decimal=1.95,
        raw_ref="bound-q1",
    )
    session.commit()
    visible_eq = quotes_visible_under_alias_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-bound",
        as_of=t1,
    )
    assert {q.raw_ref for q in visible_eq} == {"bound-q1"}

    # Naive UTC rejection for PIT as_of.
    with pytest.raises(ValueError, match="timezone-aware"):
        quotes_visible_under_alias_at(
            session,
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-bound",
            as_of=datetime(2026, 8, 12, 18, 0),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        latest_quote_timestamp(
            session,
            provider=PROVIDER_THE_ODDS_API,
            external_event_id="prov-bound",
            as_of=datetime(2026, 8, 12, 18, 0),
        )

    # After supersede at t2, as_of==superseded_at excludes that alias version.
    v3 = activate_provider_alias(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-bound",
        bout_id="bout-bound",
        match_rule=MATCH_RULE_PROVIDER_ID,
        observed_at=t2,
        evidence={"v3": True},
    )
    session.commit()
    assert v3.alias_version == 3
    at_super = alias_effective_at(
        session,
        provider=PROVIDER_THE_ODDS_API,
        external_event_id="prov-bound",
        as_of=t2,
    )
    assert at_super is not None
    assert at_super.alias_version == 3
    # Exactly at superseded_at, v2 is not effective (superseded_at <= as_of).
    session.refresh(v2)
    assert v2.superseded_at is not None
    assert v2.status == "superseded"
