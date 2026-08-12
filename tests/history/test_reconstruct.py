"""Point-in-time reconstruction tests (DWCS-105)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from mma_model.db.tables.core import CanonicalFighter
from mma_model.db.tables.history import HistorySourceBout
from mma_model.history.reconstruct import persist_reconstruction, reconstruct_pre_fight_record

UTC = timezone.utc
CUTOFF = datetime(2024, 1, 1, tzinfo=UTC)


def _add_fighter(session, fighter_id: str = "f-alex") -> str:
    session.add(CanonicalFighter(id=fighter_id, display_name="Alex Sample"))
    session.flush()
    return fighter_id


def _bout(session, **overrides) -> HistorySourceBout:
    row = HistorySourceBout(
        id=str(uuid.uuid4()),
        source=overrides.get("source", "tapology_public"),
        stream="fighter_history",
        external_bout_id=overrides["external_bout_id"],
        fighter_source="tapology_public",
        fighter_external_id="tap-100",
        fighter_name="Alex Sample",
        fighter_canonical_id=overrides["fighter_id"],
        opponent_name=overrides.get("opponent_name", "Opp"),
        opponent_external_id=overrides.get("opponent_external_id"),
        event_name=overrides.get("event_name", "Card"),
        event_date=overrides.get("event_date"),
        classification=overrides.get("classification", "professional"),
        regulated_us=overrides.get("regulated_us", "unknown"),
        result=overrides.get("result", "win"),
        method=overrides.get("method"),
        ending_round=overrides.get("ending_round", 1),
        time_str=overrides.get("time_str", "1:00"),
        elapsed_seconds=overrides.get("elapsed_seconds", 60),
        scheduled_rounds=overrides.get("scheduled_rounds", 3),
        version_kind=overrides.get("version_kind", "event_night"),
        revision=overrides.get("revision", 1),
        bout_status=overrides.get("bout_status", "completed"),
        quality_tier=overrides.get("quality_tier", "silver"),
        timestamp_quality=overrides.get("timestamp_quality", "publication_proxy"),
        observed_at=overrides.get("observed_at", datetime(2023, 12, 1, tzinfo=UTC)),
        effective_at=overrides.get("effective_at"),
        source_published_at=overrides.get("source_published_at"),
        proxy_published_at=overrides.get("proxy_published_at"),
        payload_hash=overrides.get("payload_hash", "a" * 64),
        identity_status=overrides.get("identity_status", "linked"),
        is_current_record=overrides.get("is_current_record", 0),
        left_truncated=overrides.get("left_truncated", 0),
        missing_reason=overrides.get("missing_reason"),
    )
    session.add(row)
    return row


def test_future_bout_does_not_change_prior_record(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="past",
            event_date=datetime(2022, 1, 1).date(),
            effective_at=datetime(2022, 1, 1, tzinfo=UTC),
            result="win",
        )
        session.commit()
        before = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="future",
            event_date=datetime(2025, 1, 1).date(),
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            observed_at=datetime(2025, 1, 2, tzinfo=UTC),
            result="win",
        )
        session.commit()
        after = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        assert before == after
        assert before.wins == 1
        stored = persist_reconstruction(session, before)
        session.commit()
        again = persist_reconstruction(session, after)
        assert stored.payload_hash == again.payload_hash


def test_later_correction_hidden_before_adjudication(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="rev",
            event_date=datetime(2023, 11, 1).date(),
            effective_at=datetime(2023, 11, 1, tzinfo=UTC),
            result="win",
            version_kind="event_night",
            revision=1,
        )
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="rev",
            event_date=datetime(2023, 11, 1).date(),
            effective_at=datetime(2024, 6, 1, tzinfo=UTC),
            source_published_at=datetime(2024, 6, 1, tzinfo=UTC),
            observed_at=datetime(2024, 6, 2, tzinfo=UTC),
            result="nc",
            version_kind="current",
            revision=2,
        )
        session.commit()
        early = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        late = reconstruct_pre_fight_record(
            fighter_id=fid, cutoff=datetime(2024, 7, 1, tzinfo=UTC), session=session
        )
        assert early.wins == 1
        assert early.no_contests == 0
        assert late.wins == 0
        assert late.no_contests == 1


def test_unknown_class_not_coerced_to_zero_pro_or_am(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="unk",
            event_date=datetime(2022, 1, 1).date(),
            effective_at=datetime(2022, 1, 1, tzinfo=UTC),
            classification="unknown",
            result="win",
        )
        session.commit()
        record = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        assert record.wins == 1
        assert record.professional_bouts == 0
        assert record.amateur_bouts == 0
        assert record.unknown_class_bouts == 1


def test_unresolved_identity_excluded_and_counted(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="ok",
            event_date=datetime(2022, 1, 1).date(),
            effective_at=datetime(2022, 1, 1, tzinfo=UTC),
            identity_status="linked",
        )
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="queued",
            event_date=datetime(2022, 2, 1).date(),
            effective_at=datetime(2022, 2, 1, tzinfo=UTC),
            identity_status="queued",
        )
        session.commit()
        record = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        assert record.wins == 1
        assert record.blocked_identity_excluded == 1


def test_current_mutable_record_ignored(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="current",
            event_date=datetime(2022, 1, 1).date(),
            effective_at=datetime(2022, 1, 1, tzinfo=UTC),
            is_current_record=1,
            result="win",
        )
        session.commit()
        record = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        assert record.wins == 0
        assert record.used_current_record is False


def test_proxy_after_cutoff_excludes_bout(history_env) -> None:
    Session = history_env["Session"]
    with Session() as session:
        fid = _add_fighter(session)
        _bout(
            session,
            fighter_id=fid,
            external_bout_id="proxy-late",
            event_date=datetime(2023, 12, 31).date(),
            effective_at=datetime(2023, 12, 31, tzinfo=UTC),
            proxy_published_at=datetime(2024, 1, 2, tzinfo=UTC),
            observed_at=datetime(2023, 12, 31, tzinfo=UTC),
        )
        session.commit()
        record = reconstruct_pre_fight_record(fighter_id=fid, cutoff=CUTOFF, session=session)
        assert record.wins == 0
