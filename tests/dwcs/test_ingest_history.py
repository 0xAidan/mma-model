"""DWCS-103 manifest-first history ingest tests (disposable temp DB only)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import (
    BoutResultVersion,
    BoutSourceId,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    EventSourceId,
    FighterSourceId,
)
from mma_model.db.tables.provenance import RawObservation, SourceCheckpoint
from mma_model.dwcs.classification import (
    BoutCategory,
    ClassificationError,
    classify_bout,
    classify_event_cancellation,
    classify_mismatch_gap,
)
from mma_model.dwcs.duration import DurationStatus, derive_elapsed_seconds
from mma_model.dwcs.ids import canonical_bout_id, canonical_event_id, canonical_fighter_id
from mma_model.dwcs.ingest import detect_provider_disagreement, sync_dwcs_history
from mma_model.dwcs.manifest import (
    ManifestValidationError,
    load_dwcs_bout_manifest,
    load_dwcs_event_manifest,
    load_dwcs_mismatch_ledger,
    validate_expected_universe,
)
from mma_model.dwcs.winners import WinnerValidationError, resolve_version_winner
from mma_model.ingest.raw_store import ContentAddressedRawStore
from mma_model.ingest.repository import IngestRepository, NestedBatchTransactionError
from mma_model.sources.policy import SourceId, load_source_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = REPO_ROOT / "data" / "manifests" / "dwcs_events_v1.jsonl"
BOUTS_PATH = REPO_ROOT / "data" / "manifests" / "dwcs_bouts_v1.jsonl"
MISMATCHES_PATH = REPO_ROOT / "data" / "manifests" / "dwcs_mismatches_v1.json"
UTC = timezone.utc
FIXED_OBSERVED = datetime(2026, 8, 12, 16, 0, 0, tzinfo=UTC)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def _session_env(tmp_path: Path):
    db_path = tmp_path / "dwcs103.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    store = ContentAddressedRawStore(tmp_path / "raw")
    repo = IngestRepository(session_factory=Session, raw_store=store)
    return {
        "db_path": db_path,
        "engine": engine,
        "Session": Session,
        "store": store,
        "repo": repo,
    }


@pytest.fixture
def env(tmp_path: Path):
    ctx = _session_env(tmp_path)
    yield ctx
    ctx["engine"].dispose()


def test_manifest_has_440_bouts() -> None:
    rows = load_dwcs_bout_manifest(BOUTS_PATH)
    assert len(rows) == 440


def test_manifest_has_89_events_and_expected_universe() -> None:
    events = load_dwcs_event_manifest(EVENTS_PATH)
    bouts = load_dwcs_bout_manifest(BOUTS_PATH)
    assert len(events) == 89
    expected = validate_expected_universe(events=events, bouts=bouts)
    assert expected["cards"]["all"] == 89
    assert expected["bouts"]["all"] == 440
    assert expected["cards"]["standard"] == 86
    assert expected["cards"]["brazil"] == 3
    assert expected["bouts"]["standard"] == 425
    assert expected["bouts"]["brazil"] == 15
    assert expected["event_night_results"] == {
        "decisive": 438,
        "draw": 1,
        "no_contest": 1,
    }
    assert expected["current_results"] == {
        "decisive": 431,
        "draw": 1,
        "no_contest": 8,
    }


def test_mismatch_ledger_loaded_and_categorized() -> None:
    ledger = load_dwcs_mismatch_ledger(MISMATCHES_PATH)
    assert ledger.ok is True
    assert ledger.mismatch_count == 0
    assert len(ledger.open_gaps) >= 1
    cats = [classify_mismatch_gap(gap) for gap in ledger.open_gaps]
    assert all(c is BoutCategory.MISMATCH_LEDGER_GAP for c in cats)
    paths = {str(g.get("path")) for g in ledger.open_gaps}
    assert "ufc_ufcstats_ids" in paths
    assert "full_cancellation_replacement_ledger" in paths


def test_standard_brazil_and_result_contract_counts() -> None:
    bouts = load_dwcs_bout_manifest(BOUTS_PATH)
    cats = [
        classify_bout(b.model_dump(by_alias=True), provider_blocked=True) for b in bouts
    ]
    assert sum(1 for c in cats if c.category is BoutCategory.COMPLETED_STANDARD) == 425
    assert sum(1 for c in cats if c.category is BoutCategory.COMPLETED_BRAZIL) == 15
    assert sum(1 for c in cats if c.event_night_result.value == "decisive") == 438
    assert sum(1 for c in cats if c.event_night_result.value == "draw") == 1
    assert sum(1 for c in cats if c.event_night_result.value == "no_contest") == 1
    assert sum(1 for c in cats if c.current_result.value == "decisive") == 431
    assert sum(1 for c in cats if c.current_result.value == "draw") == 1
    assert sum(1 for c in cats if c.current_result.value == "no_contest") == 8
    assert all(
        c.provider_enrichment.value in {"blocked", "unmapped", "resolved"} for c in cats
    )
    assert all(c.provider_enrichment.value == "blocked" for c in cats)


def test_every_bout_row_ends_in_exactly_one_category() -> None:
    bouts = load_dwcs_bout_manifest(BOUTS_PATH)
    seen: list[BoutCategory] = []
    for bout in bouts:
        classification = classify_bout(bout.model_dump(by_alias=True))
        assert isinstance(classification.category, BoutCategory)
        seen.append(classification.category)
    assert len(seen) == 440
    assert set(seen) <= {
        BoutCategory.COMPLETED_STANDARD,
        BoutCategory.COMPLETED_BRAZIL,
        BoutCategory.CANCELLED,
        BoutCategory.REPLACEMENT,
    }


def test_cancelled_replacement_classification() -> None:
    assert (
        classify_event_cancellation({"kind": "cancellation"}) is BoutCategory.CANCELLED
    )
    assert (
        classify_event_cancellation({"kind": "cancelled"}) is BoutCategory.CANCELLED
    )
    assert (
        classify_event_cancellation({"kind": "canceled"}) is BoutCategory.CANCELLED
    )
    assert (
        classify_event_cancellation({"kind": "replacement"})
        is BoutCategory.REPLACEMENT
    )
    with pytest.raises(ClassificationError, match="unknown cancellation kind"):
        classify_event_cancellation({"kind": "mystery"})
    # Builder mismatch-ledger shape may carry kind=cancellation.
    assert (
        classify_mismatch_gap({"kind": "cancellation", "espn_event_id": "x"})
        is BoutCategory.CANCELLED
    )
    assert (
        classify_mismatch_gap(
            {"path": "full_cancellation_replacement_ledger", "severity": "incomplete_not_done"}
        )
        is BoutCategory.MISMATCH_LEDGER_GAP
    )


def test_malformed_unknown_enum_fail_closed() -> None:
    row = load_dwcs_bout_manifest(BOUTS_PATH)[0].model_dump(by_alias=True)
    row["series_variant"] = "europe"
    with pytest.raises(ClassificationError):
        classify_bout(row)
    row = load_dwcs_bout_manifest(BOUTS_PATH)[0].model_dump(by_alias=True)
    row["event_night_result"] = {"class": "dq"}
    with pytest.raises(ClassificationError):
        classify_bout(row)


def test_duplicate_manifest_rows_rejected(tmp_path: Path) -> None:
    line = BOUTS_PATH.read_text(encoding="utf-8").splitlines()[0]
    dup = tmp_path / "dup_bouts.jsonl"
    dup.write_text(line + "\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ManifestValidationError, match="duplicate"):
        load_dwcs_bout_manifest(dup)


def test_deterministic_uuid_and_order() -> None:
    assert canonical_event_id("400961602") == canonical_event_id("400961602")
    assert canonical_bout_id("237190") == canonical_bout_id("237190")
    assert canonical_fighter_id("3122040") == canonical_fighter_id("3122040")
    assert canonical_event_id("1") != canonical_event_id("2")
    bouts = load_dwcs_bout_manifest(BOUTS_PATH)
    ordered = sorted(bouts, key=lambda b: (b.calendar_year, b.espn_competition_id))
    assert [b.espn_competition_id for b in ordered[:3]] == sorted(
        b.espn_competition_id for b in ordered[:3]
    )


def test_exact_espn_ids_on_sync(env) -> None:
    report = sync_dwcs_history(
        through_year=2017,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
        provider_blocked=True,
    )
    assert report.cards == 8
    assert report.bouts == 40
    with env["Session"]() as session:
        event_ids = {
            row.external_id
            for row in session.scalars(
                select(EventSourceId).where(EventSourceId.source == "espn")
            )
        }
        bout_ids = {
            row.external_id
            for row in session.scalars(
                select(BoutSourceId).where(BoutSourceId.source == "espn")
            )
        }
        fighter_ids = {
            row.external_id
            for row in session.scalars(
                select(FighterSourceId).where(FighterSourceId.source == "espn")
            )
        }
    events = [
        e for e in load_dwcs_event_manifest(EVENTS_PATH) if e.calendar_year <= 2017
    ]
    bouts = [b for b in load_dwcs_bout_manifest(BOUTS_PATH) if b.calendar_year <= 2017]
    assert event_ids == {e.espn_event_id for e in events}
    assert bout_ids == {b.espn_competition_id for b in bouts}
    expected_fighters = {
        p.espn_athlete_id for b in bouts for p in b.participants
    }
    assert fighter_ids == expected_fighters


def test_full_universe_sync_counts(env) -> None:
    report = sync_dwcs_history(
        through_year=2025,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
        provider_blocked=True,
    )
    assert report.cards == 89
    assert report.bouts == 440
    assert report.canonical_events == 89
    assert report.canonical_bouts == 440
    assert report.event_night_results == {
        "decisive": 438,
        "draw": 1,
        "no_contest": 1,
    }
    assert report.current_results == {
        "decisive": 431,
        "draw": 1,
        "no_contest": 8,
    }
    assert report.series_variants == {"standard": 425, "brazil": 15}
    assert report.provider_enrichment.get("blocked") == 440
    assert report.categories["completed_standard"] == 425
    assert report.categories["completed_brazil"] == 15
    assert report.categories["mismatch_ledger_gap"] == 3
    # 440 bouts * 2 version kinds
    assert report.result_versions == 880
    with env["Session"]() as session:
        fighters = session.scalars(select(CanonicalFighter)).all()
        assert len(fighters) == report.canonical_fighters
        # Distinct fighters constraint: no bout with a==b
        for bout in session.scalars(select(CanonicalBout)):
            assert bout.fighter_a_id != bout.fighter_b_id


def test_idempotent_rerun_is_noop(env) -> None:
    first = sync_dwcs_history(
        through_year=2025,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
    )
    second = sync_dwcs_history(
        through_year=2025,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
    )
    assert first.canonical_bouts == 440
    assert second.canonical_bouts == 440
    assert second.inserted_observations == 0
    assert second.skipped_identical == first.inserted_observations
    with env["Session"]() as session:
        assert len(list(session.scalars(select(CanonicalBout)))) == 440
        assert len(list(session.scalars(select(BoutResultVersion)))) == 880
        assert len(list(session.scalars(select(CanonicalEvent)))) == 89


def test_event_night_current_correction_preservation(env) -> None:
    sync_dwcs_history(
        through_year=2025,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
    )
    reversed_id = canonical_bout_id("237192")
    with env["Session"]() as session:
        night = session.scalars(
            select(BoutResultVersion).where(
                BoutResultVersion.bout_id == reversed_id,
                BoutResultVersion.version_kind == "event_night",
            )
        ).one()
        current = session.scalars(
            select(BoutResultVersion).where(
                BoutResultVersion.bout_id == reversed_id,
                BoutResultVersion.version_kind == "current",
            )
        ).one()
        assert night.result_type == "decisive"
        assert night.winner_fighter_id is not None
        assert current.result_type == "no_contest"
        assert current.winner_fighter_id is None
        assert night.observed_at.replace(tzinfo=UTC) == FIXED_OBSERVED
        assert current.observed_at.replace(tzinfo=UTC) == FIXED_OBSERVED
        # Acquisition clock must not be backdated to event night.
        assert night.observed_at.replace(tzinfo=UTC) > night.effective_at.replace(
            tzinfo=UTC
        )
        assert night.raw_observation_id is not None
        assert current.raw_observation_id is not None
        assert night.raw_observation_id != current.raw_observation_id
        night_obs = session.get(RawObservation, night.raw_observation_id)
        current_obs = session.get(RawObservation, current.raw_observation_id)
        assert night_obs is not None
        assert current_obs is not None
        assert night_obs.timestamp_quality == "publication_proxy"
        assert night_obs.proxy_published_at is not None
        assert current_obs.timestamp_quality == "unknown"
        assert current_obs.proxy_published_at is None


def test_duration_boundary_and_invalid_values() -> None:
    ok = derive_elapsed_seconds(ending_round=1, time_str="0:00", scheduled_rounds=3)
    assert ok.status is DurationStatus.VALID
    assert ok.elapsed_seconds == 0
    assert ok.allows_verified_detail is True

    full = derive_elapsed_seconds(ending_round=3, time_str="5:00", scheduled_rounds=3)
    assert full.status is DurationStatus.VALID
    assert full.elapsed_seconds == 900

    mid = derive_elapsed_seconds(ending_round=2, time_str="3:10", scheduled_rounds=3)
    assert mid.elapsed_seconds == 300 + 190

    missing = derive_elapsed_seconds(
        ending_round=None, time_str=None, scheduled_rounds=3
    )
    assert missing.status is DurationStatus.MISSING
    assert missing.allows_verified_detail is False

    over_round = derive_elapsed_seconds(
        ending_round=4, time_str="1:00", scheduled_rounds=3
    )
    assert over_round.status is DurationStatus.INVALID
    assert over_round.allows_verified_detail is False

    bad_time = derive_elapsed_seconds(
        ending_round=1, time_str="5:01", scheduled_rounds=3
    )
    assert bad_time.status is DurationStatus.INVALID

    malformed = derive_elapsed_seconds(
        ending_round=1, time_str="1:60", scheduled_rounds=3
    )
    assert malformed.status is DurationStatus.INVALID


def test_participant_disagreement_conflict_does_not_overwrite(env) -> None:
    bout = next(
        b for b in load_dwcs_bout_manifest(BOUTS_PATH) if b.espn_competition_id == "237190"
    )
    evidence = detect_provider_disagreement(
        row=bout,
        provider_participant_espn_ids=["999", "888"],
        provider_result_class="decisive",
    )
    assert evidence is not None
    assert "participant_disagreement" in evidence

    report = sync_dwcs_history(
        through_year=2017,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
        provider_overlays={
            "237190": {
                "participant_espn_ids": ["999", "888"],
                "result_class": "draw",
            }
        },
    )
    assert report.conflicts == 1
    bout_uuid = canonical_bout_id("237190")
    with env["Session"]() as session:
        current = session.scalars(
            select(BoutResultVersion).where(
                BoutResultVersion.bout_id == bout_uuid,
                BoutResultVersion.version_kind == "current",
            )
        ).one()
        assert current.result_type == "decisive"
        conflicts = [
            row
            for row in session.scalars(select(RawObservation))
            if row.quality_tier == "conflict"
        ]
        assert len(conflicts) == 1
        assert conflicts[0].entity_kind == "conflict"


def test_through_year_filtering(env) -> None:
    report = sync_dwcs_history(
        through_year=2018,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
    )
    assert report.cards == 8 + 11
    assert report.bouts == 40 + 55
    assert report.series_variants["brazil"] == 15


def test_transaction_failure_after_earlier_batch_preserves_prior(env) -> None:
    report = sync_dwcs_history(
        through_year=2017,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
        fail_after_batches=2,
    )
    assert report.batches_committed == 2
    assert report.batches_failed == 1
    with env["Session"]() as session:
        assert len(list(session.scalars(select(CanonicalEvent)))) == 2
        assert len(list(session.scalars(select(CanonicalBout)))) >= 1


def _batch_event_ids_2017() -> list[str]:
    events = sorted(
        [e for e in load_dwcs_event_manifest(EVENTS_PATH) if e.calendar_year == 2017],
        key=lambda e: e.espn_event_id,
    )
    return [e.espn_event_id for e in events]


def _counts(session_factory) -> dict[str, int]:
    with session_factory() as session:
        return {
            "events": len(list(session.scalars(select(CanonicalEvent)))),
            "bouts": len(list(session.scalars(select(CanonicalBout)))),
            "results": len(list(session.scalars(select(BoutResultVersion)))),
            "raw": len(list(session.scalars(select(RawObservation)))),
            "checkpoints": len(list(session.scalars(select(SourceCheckpoint)))),
        }


@pytest.mark.parametrize(
    "fail_at",
    ["after_canonical", "after_raw_observations", "during_result_versions"],
)
def test_batch_atomicity_failure_rolls_back_only_failed_batch(env, fail_at: str) -> None:
    event_ids = _batch_event_ids_2017()
    assert len(event_ids) >= 2
    # Succeed batch 1, fail batch 2 at the requested phase.
    report = sync_dwcs_history(
        through_year=2017,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
        fail_on_batch=2,
        fail_at=fail_at,
    )
    assert report.batches_committed == 1
    assert report.batches_failed == 1
    counts = _counts(env["Session"])
    # Prior batch preserved: first 2017 event only.
    first_id = event_ids[0]
    second_id = event_ids[1]
    with env["Session"]() as session:
        first_uuid = canonical_event_id(first_id)
        second_uuid = canonical_event_id(second_id)
        assert session.get(CanonicalEvent, first_uuid) is not None
        assert session.get(CanonicalEvent, second_uuid) is None
        first_bouts = [
            b
            for b in session.scalars(select(CanonicalBout)).all()
            if b.event_id == first_uuid
        ]
        assert len(first_bouts) >= 1
        assert counts["events"] == 1
        # Failed batch left zero partial rows / checkpoint token for event 2.
        tokens = [
            c.cursor_token for c in session.scalars(select(SourceCheckpoint)).all()
        ]
        assert f"event:{second_id}" not in tokens
        assert f"event:{first_id}" in tokens
        second_bout_ids = {
            canonical_bout_id(b.espn_competition_id)
            for b in load_dwcs_bout_manifest(BOUTS_PATH)
            if b.espn_event_id == second_id
        }
        for bout_id in second_bout_ids:
            assert session.get(CanonicalBout, bout_id) is None
            assert (
                list(
                    session.scalars(
                        select(BoutResultVersion).where(
                            BoutResultVersion.bout_id == bout_id
                        )
                    )
                )
                == []
            )


def test_nested_commit_batch_prohibited_during_owned_session(env) -> None:
    repo: IngestRepository = env["repo"]
    with env["Session"]() as session:
        repo.begin_owned_batch(session)
        with pytest.raises(NestedBatchTransactionError):
            repo.commit_batch(
                run_id="missing",
                observations=[],
                checkpoint_token="x",
                checkpoint_version="v1",
            )
        repo.end_owned_batch(session)


def test_winner_validation_event_night_and_current_rules() -> None:
    participants = [
        {"espn_athlete_id": "1", "current_winner_flag": True},
        {"espn_athlete_id": "2", "current_winner_flag": False},
    ]
    ids = {"1": "f1", "2": "f2"}
    ok_night = resolve_version_winner(
        version_kind="event_night",
        result_class="decisive",
        winner_espn_athlete_id="1",
        participants=participants,
        fighter_id_by_espn=ids,
    )
    assert ok_night.winner_fighter_id == "f1"
    ok_current = resolve_version_winner(
        version_kind="current",
        result_class="decisive",
        winner_espn_athlete_id=None,
        participants=participants,
        fighter_id_by_espn=ids,
    )
    assert ok_current.source == "current_winner_flag"
    assert ok_current.winner_fighter_id == "f1"

    with pytest.raises(WinnerValidationError) as nonpart:
        resolve_version_winner(
            version_kind="event_night",
            result_class="decisive",
            winner_espn_athlete_id="9",
            participants=participants,
            fighter_id_by_espn=ids,
        )
    assert nonpart.value.evidence["reason"] == "nonparticipant_winner"

    with pytest.raises(WinnerValidationError) as missing:
        resolve_version_winner(
            version_kind="event_night",
            result_class="decisive",
            winner_espn_athlete_id=None,
            participants=participants,
            fighter_id_by_espn=ids,
        )
    assert missing.value.evidence["reason"] == "missing_decisive_winner"

    with pytest.raises(WinnerValidationError) as dup:
        resolve_version_winner(
            version_kind="current",
            result_class="decisive",
            winner_espn_athlete_id="1",
            participants=[
                {"espn_athlete_id": "1", "current_winner_flag": True},
                {"espn_athlete_id": "2", "current_winner_flag": True},
            ],
            fighter_id_by_espn=ids,
        )
    assert dup.value.evidence["reason"] == "duplicate_winner_flag"

    with pytest.raises(WinnerValidationError) as contrad:
        resolve_version_winner(
            version_kind="current",
            result_class="decisive",
            winner_espn_athlete_id="1",
            participants=[
                {"espn_athlete_id": "1", "current_winner_flag": False},
                {"espn_athlete_id": "2", "current_winner_flag": True},
            ],
            fighter_id_by_espn=ids,
        )
    assert contrad.value.evidence["reason"] == "flag_winner_contradiction"

    nc = resolve_version_winner(
        version_kind="current",
        result_class="no_contest",
        winner_espn_athlete_id=None,
        participants=[
            {"espn_athlete_id": "1", "current_winner_flag": False},
            {"espn_athlete_id": "2", "current_winner_flag": False},
        ],
        fighter_id_by_espn=ids,
    )
    assert nc.winner_fighter_id is None
    with pytest.raises(WinnerValidationError) as nc_bad:
        resolve_version_winner(
            version_kind="current",
            result_class="draw",
            winner_espn_athlete_id="1",
            participants=participants,
            fighter_id_by_espn=ids,
        )
    assert nc_bad.value.evidence["reason"] == "non_decisive_has_winner"


def test_dry_run_nonmutation(env) -> None:
    report = sync_dwcs_history(
        through_year=2025,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=True,
        observed_at=FIXED_OBSERVED,
    )
    assert report.dry_run is True
    assert report.cards == 89
    assert report.bouts == 440
    with env["Session"]() as session:
        assert list(session.scalars(select(CanonicalEvent))) == []
        assert list(session.scalars(select(CanonicalBout))) == []
        assert list(session.scalars(select(RawObservation))) == []


def test_no_network_cli_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mma_model.cli as cli

    def _boom(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("network attempted")

    monkeypatch.setattr(cli, "UfcstatsPublicClient", _boom)
    monkeypatch.setattr(cli, "run_bounded_live_probe", _boom)

    raw = tmp_path / "raw"
    db = tmp_path / "cli.db"
    code = cli.main(
        [
            "dwcs",
            "sync-history",
            "--through",
            "2025",
            "--database-url",
            f"sqlite:///{db}",
            "--raw-store",
            str(raw),
            "--dry-run",
        ]
    )
    assert code == 0
    assert not db.exists()


def test_cli_real_temp_db_two_reruns(tmp_path: Path) -> None:
    from mma_model.cli import main

    raw = tmp_path / "raw"
    db = tmp_path / "cli-real.db"
    url = f"sqlite:///{db}"
    args = [
        "dwcs",
        "sync-history",
        "--through",
        "2025",
        "--database-url",
        url,
        "--raw-store",
        str(raw),
        "--json",
    ]
    assert main(args) == 0
    assert main(args) == 0
    conn = sqlite3.connect(db)
    try:
        events = conn.execute("select count(*) from canonical_events").fetchone()[0]
        bouts = conn.execute("select count(*) from canonical_bouts").fetchone()[0]
        results = conn.execute("select count(*) from bout_result_versions").fetchone()[0]
    finally:
        conn.close()
    assert events == 89
    assert bouts == 440
    assert results == 880


def test_full_migration_up_down_compatibility(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    names = set(engine.dialect.get_table_names(engine.connect()) if False else [])
    engine.dispose()
    # Prefer inspector for dialect portability.
    from sqlalchemy import inspect

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "canonical_bouts" in names
    assert "bout_result_versions" in names
    assert "raw_observations" in names


def test_dwcs_manifest_source_policy_role() -> None:
    policy = load_source_policy()
    assert SourceId.DWCS_MANIFEST.value in policy.source_ids
    assert policy.roles["dwcs_manifest"].role == (
        "frozen_internal_universe_and_result_seed"
    )
    assert "dwcs_manifest" not in policy.deterministic_fallback_order


def test_manifest_facts_never_use_explicit_missing_quality(env) -> None:
    sync_dwcs_history(
        through_year=2017,
        repo=env["repo"],
        session_factory=env["Session"],
        dry_run=False,
        observed_at=FIXED_OBSERVED,
    )
    with env["Session"]() as session:
        rows = list(
            session.scalars(
                select(RawObservation).where(
                    RawObservation.source == SourceId.DWCS_MANIFEST.value
                )
            )
        )
        assert rows
        assert all(row.quality_tier != "missing" for row in rows)
        assert all(row.quality_tier in {"silver", "bronze", "conflict"} for row in rows)
