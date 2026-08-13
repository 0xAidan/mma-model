"""DWCS-203: odds event matching, lifecycle, replacements, and reconcile CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select, text
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
)
from mma_model.db.tables.odds import (
    OddsBoutLifecycleObservation,
    OddsProviderEventAlias,
    OddsQuote,
)
from mma_model.domain.markets import MarketFamily, OutcomeKey
from mma_model.odds.lifecycle import (
    OddsBoutLifecycleState,
    QuoteValueEligibility,
    apply_bout_lifecycle,
    classify_quote_value_eligibility,
)
from mma_model.odds.matching import (
    MATCH_RULE_PARTICIPANT_PAIR,
    MATCH_RULE_PROVIDER_ID,
    MATCH_STATUS_AMBIGUOUS,
    MATCH_STATUS_MATCHED,
    MATCH_STATUS_UNMATCHED,
    load_matching_contract,
    match_provider_event,
    participant_names_equal,
)
from mma_model.odds.reconcile import (
    apply_replacement,
    load_golden_card,
    persist_match_decision,
    run_odds_reconcile,
    seed_canonical_card,
)
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


def test_matching_contract_loads_and_pins_window() -> None:
    contract = load_matching_contract()
    assert contract.contract_id == "dwcs_odds_matching"
    assert contract.ticket == "DWCS-203"
    assert contract.match_window_minutes == 30
    assert MATCH_RULE_PROVIDER_ID in contract.match_rules
    assert MATCH_RULE_PARTICIPANT_PAIR in contract.match_rules
    assert "fuzzy_auto_merge" in contract.prohibited


def test_participant_names_ignore_home_away_order() -> None:
    assert participant_names_equal(
        ("Jon Kunneman", "Joseph Kropschot"),
        ("Joseph Kropschot", "Jon Kunneman"),
    )
    assert not participant_names_equal(
        ("Jon Kunneman", "Joseph Kropschot"),
        ("Jon Kunneman", "Nick Kropschot"),
    )


def test_provider_id_match_beats_participant_pair(tmp_path: Path) -> None:
    session = _session(tmp_path)
    event_row = CanonicalEvent(id="evt-1", name="Card", series="dwcs", status="scheduled")
    fa = CanonicalFighter(id="f-a", display_name="Alpha Fighter")
    fb = CanonicalFighter(id="f-b", display_name="Bravo Fighter")
    session.add_all([event_row, fa, fb])
    session.flush()
    bout = CanonicalBout(
        id="bout-stored",
        event_id="evt-1",
        fighter_a_id="f-a",
        fighter_b_id="f-b",
        status="scheduled",
    )
    session.add(bout)
    session.flush()
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
        commence_time=OBSERVED,
    )
    assert decision.status == MATCH_STATUS_MATCHED
    assert decision.match_rule == MATCH_RULE_PROVIDER_ID
    assert decision.bout_id == "bout-stored"
    assert decision.eligible_for_value is True


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


def test_ambiguity_blocks_value_and_routes_review(tmp_path: Path) -> None:
    session = _session(tmp_path)
    start = datetime(2026, 8, 12, 1, 0, tzinfo=UTC)
    event_row = CanonicalEvent(
        id="evt-amb",
        name="Amb",
        series="dwcs",
        status="scheduled",
        scheduled_start_at=start,
    )
    fa = CanonicalFighter(id="fa", display_name="Same One")
    fb = CanonicalFighter(id="fb", display_name="Same Two")
    session.add_all([event_row, fa, fb])
    session.flush()
    for bout_id in ("bout-a", "bout-b"):
        session.add(
            CanonicalBout(
                id=bout_id,
                event_id="evt-amb",
                fighter_a_id="fa",
                fighter_b_id="fb",
                status="scheduled",
            )
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


def test_golden_card_exact_active_bout_matches_100_percent(tmp_path: Path) -> None:
    session = _session(tmp_path)
    report = run_odds_reconcile(
        session,
        next_dwcs=True,
        strict=True,
        golden_card_path=GOLDEN_ACTIVE,
    )
    session.commit()
    assert report["active_bout_match_rate"] == 1.0
    assert report["matched_active_bouts"] == report["active_bout_count"] == 5
    assert report["blockers"] == []
    assert all(row["status"] == MATCH_STATUS_MATCHED for row in report["decisions"])


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

    old_elig = classify_quote_value_eligibility(
        match_status=MATCH_STATUS_MATCHED,
        lifecycle=OddsBoutLifecycleState.REPLACED,
    )
    assert old_elig is QuoteValueEligibility.BLOCKED


def test_lifecycle_requires_evidence_and_never_forward_fills(tmp_path: Path) -> None:
    session = _session(tmp_path)
    event_row = CanonicalEvent(id="evt-l", name="L", series="dwcs", status="scheduled")
    fa = CanonicalFighter(id="fa", display_name="A")
    fb = CanonicalFighter(id="fb", display_name="B")
    session.add_all([event_row, fa, fb])
    session.flush()
    bout = CanonicalBout(
        id="bout-l",
        event_id="evt-l",
        fighter_a_id="fa",
        fighter_b_id="fb",
        status="scheduled",
    )
    session.add(bout)
    session.commit()

    with pytest.raises(ValueError, match="evidence"):
        apply_bout_lifecycle(
            session,
            bout_id="bout-l",
            lifecycle=OddsBoutLifecycleState.LOCKED,
            evidence_kind="",
            observed_at=OBSERVED,
        )

    with pytest.raises(ValueError, match="forward-fill|price"):
        apply_bout_lifecycle(
            session,
            bout_id="bout-l",
            lifecycle=OddsBoutLifecycleState.LOCKED,
            evidence_kind="provider_lock_signal",
            observed_at=OBSERVED,
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
    assert row.lifecycle == OddsBoutLifecycleState.MISSING_UNKNOWN.value
    assert row.price_decimal is None
    assert (
        classify_quote_value_eligibility(
            match_status=MATCH_STATUS_MATCHED,
            lifecycle=OddsBoutLifecycleState.MISSING_UNKNOWN,
        )
        is QuoteValueEligibility.BLOCKED
    )


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


def test_stale_blocks_without_inferring_lock(tmp_path: Path) -> None:
    session = _session(tmp_path)
    event_row = CanonicalEvent(id="evt-s", name="S", series="dwcs", status="scheduled")
    fa = CanonicalFighter(id="fa", display_name="A")
    fb = CanonicalFighter(id="fb", display_name="B")
    session.add_all([event_row, fa, fb])
    session.flush()
    bout = CanonicalBout(
        id="bout-s",
        event_id="evt-s",
        fighter_a_id="fa",
        fighter_b_id="fb",
        status="scheduled",
    )
    session.add(bout)
    session.commit()
    apply_bout_lifecycle(
        session,
        bout_id="bout-s",
        lifecycle=OddsBoutLifecycleState.STALE,
        evidence_kind="quote_age_exceeds_stale_after_minutes",
        observed_at=OBSERVED,
        detail="last_observed_at=2026-08-12T10:00:00Z",
    )
    session.commit()
    assert (
        classify_quote_value_eligibility(
            match_status=MATCH_STATUS_MATCHED,
            lifecycle=OddsBoutLifecycleState.STALE,
        )
        is QuoteValueEligibility.BLOCKED
    )
    locks = session.scalars(
        select(OddsBoutLifecycleObservation).where(
            OddsBoutLifecycleObservation.lifecycle == "locked"
        )
    ).all()
    assert locks == []


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


def test_migration_creates_matching_tables(tmp_path: Path) -> None:
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
        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(odds_provider_event_aliases)"))
        }
        assert {"provider", "external_event_id", "bout_id", "alias_version", "status"} <= cols


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


def test_fighter_alias_supports_exact_participant_match(tmp_path: Path) -> None:
    session = _session(tmp_path)
    event_row = CanonicalEvent(
        id="evt-al",
        name="Alias card",
        series="dwcs",
        status="scheduled",
        scheduled_start_at=OBSERVED,
    )
    fa = CanonicalFighter(id="fa", display_name="Jonathan Kunneman")
    fb = CanonicalFighter(id="fb", display_name="Joseph Kropschot")
    session.add_all([event_row, fa, fb])
    session.flush()
    bout = CanonicalBout(
        id="bout-al",
        event_id="evt-al",
        fighter_a_id="fa",
        fighter_b_id="fb",
        status="scheduled",
    )
    session.add(bout)
    session.flush()
    session.add(FighterAlias(fighter_id="fa", alias="Jon Kunneman", source="manual"))
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
