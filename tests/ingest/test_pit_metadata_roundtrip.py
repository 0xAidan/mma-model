"""Four-clock PIT / quality metadata round-trip tests (DWCS-102 Task 2)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.provenance import RawObservation
from mma_model.ingest.raw_store import ContentAddressedRawStore
from mma_model.ingest.repository import IngestRepository, ReservedAttributeKeyError
from mma_model.sources.contracts import DetailLevel, SourceObservationRecord
from mma_model.sources.policy import load_source_policy

REPO_ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
PIT_COLUMNS = (
    "source_published_at",
    "proxy_published_at",
    "timestamp_quality",
    "timestamp_quality_source",
    "quality_tier",
    "attributes_json",
)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite often returns naive UTC; normalize for assertion equality."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def _session_factory(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True), engine


@pytest.fixture
def repo_env(tmp_path: Path):
    db_path = tmp_path / "pit.db"
    Session, engine = _session_factory(db_path)
    store = ContentAddressedRawStore(tmp_path / "raw")
    repo = IngestRepository(session_factory=Session, raw_store=store)
    yield {
        "session_factory": Session,
        "engine": engine,
        "store": store,
        "repo": repo,
        "db_path": db_path,
    }
    engine.dispose()


def _put(store: ContentAddressedRawStore, body: bytes) -> str:
    return store.put(body)


def test_round_trip_silver_vs_gold_quality_tier(repo_env) -> None:
    policy = load_source_policy()
    assert "round_trip_silver_vs_gold_quality_tier" in policy.dwcs_102_persistence.required_tests
    repo = repo_env["repo"]
    store = repo_env["store"]
    Session = repo_env["session_factory"]

    observed = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    gold_hash = _put(store, b"gold-payload")
    silver_hash = _put(store, b"silver-payload")
    gold = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-gold",
        entity_kind="bout_stat",
        observed_at=observed,
        source_published_at=datetime(2019, 1, 2, tzinfo=UTC),
        source_updated_at=datetime(2019, 1, 2, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        proxy_published_at=None,
        timestamp_quality="direct_source_timestamp",
        timestamp_quality_source="ufcstats_public",
        quality_tier="gold",
        payload_hash=gold_hash,
        raw_ref=gold_hash,
        detail_level=DetailLevel.VERIFIED,
        attributes={"significant_strikes_landed": 12},
    )
    silver = gold.model_copy(
        update={
            "external_id": "fight-silver",
            "source_published_at": None,
            "proxy_published_at": datetime(2019, 1, 2, tzinfo=UTC),
            "timestamp_quality": "publication_proxy",
            "timestamp_quality_source": "event_completion_plus_delay@1",
            "quality_tier": "silver",
            "payload_hash": silver_hash,
            "raw_ref": silver_hash,
        }
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="test")
    repo.commit_batch(
        run_id=run.id,
        observations=[gold, silver],
        checkpoint_token="t1",
        checkpoint_version="v1",
    )
    with Session() as session:
        rows = {
            row.external_id: row
            for row in session.scalars(select(RawObservation)).all()
        }
    assert rows["fight-gold"].quality_tier == "gold"
    assert rows["fight-gold"].proxy_published_at is None
    assert _as_utc(rows["fight-gold"].source_published_at) == datetime(
        2019, 1, 2, tzinfo=UTC
    )
    assert rows["fight-silver"].quality_tier == "silver"
    assert rows["fight-silver"].timestamp_quality == "publication_proxy"
    assert _as_utc(rows["fight-silver"].proxy_published_at) == datetime(
        2019, 1, 2, tzinfo=UTC
    )
    assert _as_utc(rows["fight-gold"].observed_at) != _as_utc(
        rows["fight-gold"].effective_at
    )


def test_proxy_published_at_persisted_when_timestamp_quality_publication_proxy(
    repo_env,
) -> None:
    repo = repo_env["repo"]
    store = repo_env["store"]
    Session = repo_env["session_factory"]
    digest = _put(store, b"proxy-row")
    obs = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-proxy",
        entity_kind="bout_stat",
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        source_published_at=None,
        source_updated_at=None,
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        proxy_published_at=datetime(2019, 1, 2, tzinfo=UTC),
        timestamp_quality="publication_proxy",
        timestamp_quality_source="event_completion_plus_delay@1",
        quality_tier="silver",
        payload_hash=digest,
        raw_ref=digest,
        detail_level=DetailLevel.VERIFIED,
        attributes={"kd": 1},
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="proxy")
    repo.commit_batch(
        run_id=run.id,
        observations=[obs],
        checkpoint_token="t",
        checkpoint_version="v1",
    )
    with Session() as session:
        row = session.scalars(select(RawObservation)).one()
    assert _as_utc(row.proxy_published_at) == datetime(2019, 1, 2, tzinfo=UTC)
    assert row.timestamp_quality == "publication_proxy"


def test_attributes_json_not_dropped_on_commit_batch(repo_env) -> None:
    repo = repo_env["repo"]
    store = repo_env["store"]
    Session = repo_env["session_factory"]
    digest = _put(store, b"attrs")
    attrs = {"z_key": 1, "a_key": "x", "nested": {"b": 2, "a": 1}}
    obs = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-attrs",
        entity_kind="bout_stat",
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        source_published_at=datetime(2019, 1, 2, tzinfo=UTC),
        source_updated_at=datetime(2019, 1, 2, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        proxy_published_at=None,
        timestamp_quality="direct_source_timestamp",
        timestamp_quality_source="ufcstats_public",
        quality_tier="gold",
        payload_hash=digest,
        raw_ref=digest,
        detail_level=DetailLevel.VERIFIED,
        attributes=attrs,
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="attrs")
    repo.commit_batch(
        run_id=run.id,
        observations=[obs],
        checkpoint_token="t",
        checkpoint_version="v1",
    )
    with Session() as session:
        row = session.scalars(select(RawObservation)).one()
    loaded = json.loads(row.attributes_json)
    assert loaded == attrs
    # Canonical serialization: sorted object keys.
    assert row.attributes_json == json.dumps(attrs, sort_keys=True, separators=(",", ":"))


def test_source_published_at_distinct_from_observed_at(repo_env) -> None:
    repo = repo_env["repo"]
    store = repo_env["store"]
    Session = repo_env["session_factory"]
    digest = _put(store, b"distinct")
    observed = datetime(2026, 8, 12, 15, 0, tzinfo=UTC)
    published = datetime(2019, 1, 2, tzinfo=UTC)
    obs = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-distinct",
        entity_kind="bout_stat",
        observed_at=observed,
        source_published_at=published,
        source_updated_at=published,
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        proxy_published_at=None,
        timestamp_quality="direct_source_timestamp",
        timestamp_quality_source="ufcstats_public",
        quality_tier="gold",
        payload_hash=digest,
        raw_ref=digest,
        detail_level=DetailLevel.VERIFIED,
        attributes={"sig": 3},
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="distinct")
    repo.commit_batch(
        run_id=run.id,
        observations=[obs],
        checkpoint_token="t",
        checkpoint_version="v1",
    )
    with Session() as session:
        row = session.scalars(select(RawObservation)).one()
    assert _as_utc(row.source_published_at) != _as_utc(row.observed_at)
    assert _as_utc(row.source_published_at) == published
    assert _as_utc(row.observed_at) == observed


def test_utc_validation_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="UTC|timezone|tzinfo"):
        SourceObservationRecord(
            source="ufcstats_public",
            stream="fight_details",
            external_id="naive",
            entity_kind="bout_stat",
            observed_at=datetime(2026, 8, 12, 15, 0),  # naive
            effective_at=datetime(2019, 1, 1, tzinfo=UTC),
            timestamp_quality="unknown",
            quality_tier="bronze",
            payload_hash="a" * 64,
            raw_blob_absent=True,
        )


def test_reserved_attribute_collision_rejected_on_commit(repo_env) -> None:
    policy = load_source_policy()
    assert "quality_tier" in policy.observation_metadata.reserved_attribute_keys
    repo = repo_env["repo"]
    store = repo_env["store"]
    digest = _put(store, b"reserved")
    obs = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-reserved",
        entity_kind="bout_stat",
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        timestamp_quality="unknown",
        quality_tier="bronze",
        payload_hash=digest,
        raw_ref=digest,
        detail_level=DetailLevel.PARTIAL,
        attributes={"quality_tier": "gold"},
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="reserved")
    with pytest.raises(ReservedAttributeKeyError, match="quality_tier"):
        repo.commit_batch(
            run_id=run.id,
            observations=[obs],
            checkpoint_token="t",
            checkpoint_version="v1",
        )


def test_verified_rejects_missing_required_metadata(repo_env) -> None:
    repo = repo_env["repo"]
    store = repo_env["store"]
    digest = _put(store, b"verified-missing")
    obs = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-missing-meta",
        entity_kind="bout_stat",
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        timestamp_quality="unknown",
        timestamp_quality_source=None,
        quality_tier="bronze",
        payload_hash=digest,
        raw_ref=digest,
        detail_level=DetailLevel.VERIFIED,
        attributes={"sig": 1},
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="missing")
    with pytest.raises(ValueError, match="required|timestamp_quality_source|verified"):
        repo.commit_batch(
            run_id=run.id,
            observations=[obs],
            checkpoint_token="t",
            checkpoint_version="v1",
        )


def test_raw_ref_blob_invariant_still_enforced(repo_env) -> None:
    from mma_model.ingest.raw_store import PayloadCorruptionError

    repo = repo_env["repo"]
    obs = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="missing-blob",
        entity_kind="bout_stat",
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        timestamp_quality="unknown",
        quality_tier="bronze",
        payload_hash="c" * 64,
        raw_ref="c" * 64,
        detail_level=DetailLevel.PARTIAL,
        attributes={"sig": 1},
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="blob")
    with pytest.raises(PayloadCorruptionError):
        repo.commit_batch(
            run_id=run.id,
            observations=[obs],
            checkpoint_token="t",
            checkpoint_version="v1",
        )


def test_migration_0006_upgrade_downgrade_upgrade_preserves_pre0006_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "mig0006.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "0005_provenance_revisions")

    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO ingest_runs "
            "(id, source, stream, scope, status, started_at, observation_count, created_at) "
            "VALUES ('run-pre', 's', 'st', 'sc', 'succeeded', ?, 1, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO raw_observations "
            "(ingest_run_id, source, stream, scope, checkpoint_version, external_id, "
            "entity_kind, observed_at, effective_at, source_updated_at, payload_hash, "
            "raw_ref, detail_level, schema_version, created_at) "
            "VALUES ('run-pre', 's', 'st', 'sc', 'v1', 'ext-pre', 'bout_stat', "
            "?, ?, ?, ?, ?, 'partial', '1', ?)",
            (now, now, now, "d" * 64, "d" * 64, now),
        )
        conn.commit()
        pre_count = conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0]
        pre_ext = conn.execute(
            "SELECT external_id, payload_hash FROM raw_observations"
        ).fetchone()
    finally:
        conn.close()

    command.upgrade(cfg, "0006_observation_pit_metadata")
    cols = {
        c["name"]
        for c in inspect(create_engine(f"sqlite:///{db_path}")).get_columns(
            "raw_observations"
        )
    }
    for name in PIT_COLUMNS:
        assert name in cols

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0] == pre_count
        row = conn.execute(
            "SELECT external_id, payload_hash, quality_tier, attributes_json "
            "FROM raw_observations WHERE external_id='ext-pre'"
        ).fetchone()
        assert row[0] == pre_ext[0]
        assert row[1] == pre_ext[1]
        assert row[2] is None
        assert row[3] is None
    finally:
        conn.close()

    command.downgrade(cfg, "0005_provenance_revisions")
    cols_down = {
        c["name"]
        for c in inspect(create_engine(f"sqlite:///{db_path}")).get_columns(
            "raw_observations"
        )
    }
    for name in PIT_COLUMNS:
        assert name not in cols_down

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0] == pre_count
        row = conn.execute(
            "SELECT external_id, payload_hash FROM raw_observations WHERE external_id='ext-pre'"
        ).fetchone()
        assert row == pre_ext
    finally:
        conn.close()

    command.upgrade(cfg, "0006_observation_pit_metadata")
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0] == pre_count
        row = conn.execute(
            "SELECT external_id, payload_hash FROM raw_observations WHERE external_id='ext-pre'"
        ).fetchone()
        assert row == pre_ext
    finally:
        conn.close()


def test_correction_append_only_semantics(repo_env) -> None:
    """Same identity with new hash inserts another observation (no overwrite)."""
    repo = repo_env["repo"]
    store = repo_env["store"]
    Session = repo_env["session_factory"]
    h1 = _put(store, b"first")
    h2 = _put(store, b"correction")
    base = dict(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-corr",
        entity_kind="bout_stat",
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        timestamp_quality="unknown",
        quality_tier="bronze",
        detail_level=DetailLevel.PARTIAL,
        attributes={"sig": 1},
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="corr")
    repo.commit_batch(
        run_id=run.id,
        observations=[SourceObservationRecord(**base, payload_hash=h1, raw_ref=h1)],
        checkpoint_token="t1",
        checkpoint_version="v1",
    )
    repo.commit_batch(
        run_id=run.id,
        observations=[
            SourceObservationRecord(
                **{**base, "attributes": {"sig": 2}},
                payload_hash=h2,
                raw_ref=h2,
            )
        ],
        checkpoint_token="t2",
        checkpoint_version="v1",
    )
    with Session() as session:
        rows = session.scalars(
            select(RawObservation).where(RawObservation.external_id == "fight-corr")
        ).all()
    assert len(rows) == 2
    hashes = {r.payload_hash for r in rows}
    assert hashes == {h1, h2}


def test_idempotent_replay_skips_identical(repo_env) -> None:
    repo = repo_env["repo"]
    store = repo_env["store"]
    digest = _put(store, b"same")
    obs = SourceObservationRecord(
        source="ufcstats_public",
        stream="fight_details",
        external_id="fight-idem",
        entity_kind="bout_stat",
        observed_at=datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
        effective_at=datetime(2019, 1, 1, tzinfo=UTC),
        timestamp_quality="unknown",
        quality_tier="bronze",
        payload_hash=digest,
        raw_ref=digest,
        detail_level=DetailLevel.PARTIAL,
        attributes={"sig": 1},
    )
    run = repo.start_run(source="ufcstats_public", stream="fight_details", scope="idem")
    first = repo.commit_batch(
        run_id=run.id,
        observations=[obs],
        checkpoint_token="t1",
        checkpoint_version="v1",
    )
    second = repo.commit_batch(
        run_id=run.id,
        observations=[obs],
        checkpoint_token="t2",
        checkpoint_version="v1",
    )
    assert first.inserted == 1
    assert second.inserted == 0
    assert second.skipped_identical == 1
