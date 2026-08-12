"""Provenance / ingest repository behavior tests (DWCS-101).

All work uses disposable temp DBs and temp raw-store roots — never live data/.
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import BoutResultVersion, CanonicalBout, CanonicalEvent, CanonicalFighter
from mma_model.db.tables.provenance import IngestRun, RawObservation, SourceCheckpoint
from mma_model.ingest.raw_store import ContentAddressedRawStore, PayloadCorruptionError
from mma_model.ingest.repository import IngestRepository
from mma_model.sources.contracts import DetailLevel, SourceObservationRecord

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVENANCE_TABLES = {"ingest_runs", "raw_observations", "source_checkpoints"}
UTC = timezone.utc


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return cfg


def _engine(db_path: Path):
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _attach_sqlite_listeners(engine)
    return engine


def _session_factory(db_path: Path):
    engine = _engine(db_path)
    create_all_for_tests(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True), engine


def _ts(hour: int = 12, minute: int = 0) -> datetime:
    return datetime(2024, 6, 1, hour, minute, tzinfo=UTC)


def _payload(body: bytes) -> tuple[bytes, str]:
    digest = hashlib.sha256(body).hexdigest()
    return body, digest


def _obs(
    *,
    external_id: str = "ext-1",
    payload_hash: str,
    raw_ref: str | None = None,
    detail_level: DetailLevel = DetailLevel.PARTIAL,
    version_kind: str | None = None,
    entity_kind: str = "bout_result",
    source: str = "testsource",
    stream: str = "results",
    observed_at: datetime | None = None,
    effective_at: datetime | None = None,
    source_updated_at: datetime | None = None,
    subject_id: str | None = None,
    attributes: dict | None = None,
) -> SourceObservationRecord:
    return SourceObservationRecord(
        source=source,
        stream=stream,
        external_id=external_id,
        entity_kind=entity_kind,
        observed_at=observed_at or _ts(12),
        effective_at=effective_at or _ts(10),
        source_updated_at=source_updated_at or _ts(11),
        payload_hash=payload_hash,
        raw_ref=raw_ref or payload_hash,
        detail_level=detail_level,
        version_kind=version_kind,
        schema_version="1",
        subject_id=subject_id,
        attributes=attributes or {},
    )


@pytest.fixture
def db_env(tmp_path: Path):
    db_path = tmp_path / "ingest.db"
    Session, engine = _session_factory(db_path)
    store = ContentAddressedRawStore(tmp_path / "raw")
    repo = IngestRepository(session_factory=Session, raw_store=store)
    yield {"session_factory": Session, "engine": engine, "store": store, "repo": repo, "db_path": db_path}
    engine.dispose()


def _seed_bout(session_factory) -> str:
    Session = session_factory
    with Session() as session:
        session.add_all(
            [
                CanonicalFighter(id="f-a", display_name="A"),
                CanonicalFighter(id="f-b", display_name="B"),
                CanonicalEvent(id="e-1", name="Test Card", status="completed"),
            ]
        )
        session.flush()
        session.add(
            CanonicalBout(
                id="bout-1",
                event_id="e-1",
                fighter_a_id="f-a",
                fighter_b_id="f-b",
                status="completed",
            )
        )
        session.commit()
    return "bout-1"


def test_migration_adds_provenance_tables_and_downgrade_removes_only_them(tmp_path: Path) -> None:
    db_path = tmp_path / "mig.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    names = set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())
    assert PROVENANCE_TABLES.issubset(names)

    # Seed a provenance row and a prior canonical row so downgrade ownership is clear.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO canonical_fighters (id, display_name, created_at, updated_at) "
            "VALUES ('cf1', 'Keep Me', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO ingest_runs "
            "(id, source, stream, scope, status, started_at, observation_count, created_at) "
            "VALUES ('run1', 's', 'st', 'sc', 'succeeded', ?, 0, ?)",
            (now, now),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM canonical_fighters").fetchone()[0] == 1
    finally:
        conn.close()

    command.downgrade(cfg, "0003_legacy_import")
    names_after = set(inspect(create_engine(f"sqlite:///{db_path}")).get_table_names())
    assert PROVENANCE_TABLES.isdisjoint(names_after)
    assert "canonical_fighters" in names_after
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM canonical_fighters").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM canonical_fighters").fetchone()[0] == "cf1"
    finally:
        conn.close()


def test_identical_replay_is_noop(db_env) -> None:
    repo: IngestRepository = db_env["repo"]
    store: ContentAddressedRawStore = db_env["store"]
    Session = db_env["session_factory"]
    bout_id = _seed_bout(Session)
    body, digest = _payload(b'{"winner":"f-a","method":"KO"}')
    store.put(body)
    run = repo.start_run(source="testsource", stream="results", scope="profile:default")
    obs = _obs(
        payload_hash=digest,
        version_kind="event_night",
        detail_level=DetailLevel.VERIFIED,
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-a",
            "result_type": "win",
            "method": "KO",
            "ending_round": 1,
            "time_str": "1:00",
        },
    )
    first = repo.commit_batch(
        run_id=run.id,
        observations=[obs],
        checkpoint_token="page:1",
        checkpoint_version="v1",
    )
    second = repo.commit_batch(
        run_id=run.id,
        observations=[obs],
        checkpoint_token="page:1",
        checkpoint_version="v1",
    )
    assert first.inserted == 1
    assert second.inserted == 0
    assert second.skipped_identical == 1
    with Session() as session:
        rows = session.scalars(select(RawObservation)).all()
        assert len(rows) == 1
        results = session.scalars(select(BoutResultVersion)).all()
        assert len(results) == 1


def test_correction_appends_version_not_overwrite_event_night(db_env) -> None:
    repo: IngestRepository = db_env["repo"]
    store: ContentAddressedRawStore = db_env["store"]
    Session = db_env["session_factory"]
    bout_id = _seed_bout(Session)
    body1, h1 = _payload(b'{"winner":"f-a","method":"KO"}')
    body2, h2 = _payload(b'{"winner":"f-b","method":"Decision"}')
    store.put(body1)
    store.put(body2)
    run = repo.start_run(source="testsource", stream="results", scope="profile:default")
    night = _obs(
        payload_hash=h1,
        version_kind="event_night",
        detail_level=DetailLevel.VERIFIED,
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-a",
            "result_type": "win",
            "method": "KO",
            "ending_round": 1,
            "time_str": "1:00",
        },
    )
    correction = _obs(
        payload_hash=h2,
        version_kind="current",
        detail_level=DetailLevel.VERIFIED,
        observed_at=_ts(15),
        source_updated_at=_ts(14),
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-b",
            "result_type": "win",
            "method": "Decision",
            "ending_round": 3,
            "time_str": "5:00",
        },
    )
    repo.commit_batch(run_id=run.id, observations=[night], checkpoint_token="p1", checkpoint_version="v1")
    repo.commit_batch(
        run_id=run.id, observations=[correction], checkpoint_token="p2", checkpoint_version="v1"
    )
    with Session() as session:
        raw_rows = session.scalars(select(RawObservation).order_by(RawObservation.id)).all()
        assert len(raw_rows) == 2
        assert {r.payload_hash for r in raw_rows} == {h1, h2}
        versions = {
            v.version_kind: v for v in session.scalars(select(BoutResultVersion)).all()
        }
        assert versions["event_night"].winner_fighter_id == "f-a"
        assert versions["event_night"].method == "KO"
        assert versions["current"].winner_fighter_id == "f-b"
        assert versions["current"].method == "Decision"


def test_verified_detail_never_downgraded(db_env) -> None:
    repo: IngestRepository = db_env["repo"]
    store: ContentAddressedRawStore = db_env["store"]
    Session = db_env["session_factory"]
    bout_id = _seed_bout(Session)
    body_v, hv = _payload(b'{"detail":"verified","winner":"f-a"}')
    body_s, hs = _payload(b'{"detail":"summary","winner":"f-a"}')
    store.put(body_v)
    store.put(body_s)
    run = repo.start_run(source="testsource", stream="results", scope="profile:default")
    verified = _obs(
        payload_hash=hv,
        version_kind="current",
        detail_level=DetailLevel.VERIFIED,
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-a",
            "result_type": "win",
            "method": "KO/TKO",
            "ending_round": 2,
            "time_str": "3:10",
        },
    )
    summary = _obs(
        payload_hash=hs,
        version_kind="current",
        detail_level=DetailLevel.SUMMARY,
        observed_at=_ts(16),
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-a",
            "result_type": "win",
            "method": None,
            "ending_round": None,
            "time_str": None,
        },
    )
    repo.commit_batch(
        run_id=run.id, observations=[verified], checkpoint_token="a", checkpoint_version="v1"
    )
    result = repo.commit_batch(
        run_id=run.id, observations=[summary], checkpoint_token="b", checkpoint_version="v1"
    )
    assert result.skipped_downgrade == 1
    with Session() as session:
        # Correction raw row still appended for provenance.
        assert session.scalars(select(RawObservation)).all().__len__() == 2
        current = session.scalars(
            select(BoutResultVersion).where(BoutResultVersion.version_kind == "current")
        ).one()
        assert current.method == "KO/TKO"
        assert current.ending_round == 2
        assert current.time_str == "3:10"


def test_failed_batch_retains_earlier_committed_progress(db_env) -> None:
    repo: IngestRepository = db_env["repo"]
    store: ContentAddressedRawStore = db_env["store"]
    Session = db_env["session_factory"]
    bout_id = _seed_bout(Session)
    body1, h1 = _payload(b'{"batch":1}')
    store.put(body1)
    run = repo.start_run(source="testsource", stream="results", scope="profile:default")
    ok = _obs(
        payload_hash=h1,
        version_kind="event_night",
        detail_level=DetailLevel.VERIFIED,
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-a",
            "result_type": "win",
            "method": "SUB",
            "ending_round": 1,
            "time_str": "2:00",
        },
    )
    repo.commit_batch(run_id=run.id, observations=[ok], checkpoint_token="ok", checkpoint_version="v1")

    bad = _obs(
        payload_hash="0" * 64,
        version_kind="current",
        detail_level=DetailLevel.VERIFIED,
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-b",
            "result_type": "win",
            "method": "DEC",
            "ending_round": 3,
            "time_str": "5:00",
        },
    )
    with pytest.raises(PayloadCorruptionError):
        repo.commit_batch(
            run_id=run.id,
            observations=[bad],
            checkpoint_token="bad",
            checkpoint_version="v1",
            require_raw_present=True,
        )

    with Session() as session:
        cp = session.scalars(select(SourceCheckpoint)).one()
        assert cp.cursor_token == "ok"
        assert session.scalars(select(RawObservation)).all().__len__() == 1
        assert session.scalars(select(BoutResultVersion)).all().__len__() == 1


def test_checkpoint_scope_cannot_collide_across_profiles_or_sources(db_env) -> None:
    repo: IngestRepository = db_env["repo"]
    Session = db_env["session_factory"]
    run_a = repo.start_run(source="src-a", stream="events", scope="profile:quick")
    run_b = repo.start_run(source="src-a", stream="events", scope="profile:full")
    run_c = repo.start_run(source="src-b", stream="events", scope="profile:quick")
    empty_obs: list[SourceObservationRecord] = []
    repo.commit_batch(run_id=run_a.id, observations=empty_obs, checkpoint_token="a", checkpoint_version="v1")
    repo.commit_batch(run_id=run_b.id, observations=empty_obs, checkpoint_token="b", checkpoint_version="v1")
    repo.commit_batch(run_id=run_c.id, observations=empty_obs, checkpoint_token="c", checkpoint_version="v1")
    with Session() as session:
        rows = session.scalars(select(SourceCheckpoint)).all()
        assert len(rows) == 3
        keys = {(r.source, r.stream, r.scope, r.version) for r in rows}
        assert keys == {
            ("src-a", "events", "profile:quick", "v1"),
            ("src-a", "events", "profile:full", "v1"),
            ("src-b", "events", "profile:quick", "v1"),
        }
        tokens = {r.cursor_token for r in rows}
        assert tokens == {"a", "b", "c"}


def test_raw_store_atomic_idempotent_and_hash_verification(tmp_path: Path) -> None:
    store = ContentAddressedRawStore(tmp_path / "raw")
    body = b'{"hello":"world"}'
    digest = hashlib.sha256(body).hexdigest()
    path1 = store.put(body)
    path2 = store.put(body)
    assert path1 == path2
    assert store.path_for(digest).is_file()
    assert store.get(digest) == body
    # Corrupt on-disk bytes while keeping the hash key → verification fails.
    target = store.path_for(digest)
    target.write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(PayloadCorruptionError):
        store.get(digest)
    with pytest.raises(PayloadCorruptionError):
        store.verify(digest)


def test_concurrent_same_hash_writes_are_safe(tmp_path: Path) -> None:
    store = ContentAddressedRawStore(tmp_path / "raw")
    body = b"x" * 10_000
    digest = hashlib.sha256(body).hexdigest()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            store.put(body)
        except BaseException as exc:  # noqa: BLE001 - collect any failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.get(digest) == body


def test_transaction_boundary_per_batch_not_whole_run(db_env) -> None:
    """Each commit_batch is its own transaction; run status update is separate."""
    repo: IngestRepository = db_env["repo"]
    store: ContentAddressedRawStore = db_env["store"]
    Session = db_env["session_factory"]
    bout_id = _seed_bout(Session)
    body, digest = _payload(b'{"ok":true}')
    store.put(body)
    run = repo.start_run(source="testsource", stream="results", scope="profile:default")
    obs = _obs(
        payload_hash=digest,
        version_kind="event_night",
        detail_level=DetailLevel.PARTIAL,
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-a",
            "result_type": "win",
            "method": "KO",
            "ending_round": 1,
            "time_str": "0:45",
        },
    )
    repo.commit_batch(run_id=run.id, observations=[obs], checkpoint_token="1", checkpoint_version="v1")
    # Simulate crash before finish_run: DB must still show committed batch + running status.
    with Session() as session:
        assert session.get(IngestRun, run.id).status == "running"
        assert session.scalars(select(RawObservation)).all().__len__() == 1
        assert session.scalars(select(SourceCheckpoint)).one().cursor_token == "1"
    repo.finish_run(run.id, status="succeeded")
    with Session() as session:
        assert session.get(IngestRun, run.id).status == "succeeded"


def test_provenance_uniqueness_enforced(db_env) -> None:
    Session = db_env["session_factory"]
    engine = db_env["engine"]
    now = datetime.now(UTC)
    with Session() as session:
        session.add(
            IngestRun(
                id="run-x",
                source="s",
                stream="st",
                scope="sc",
                status="running",
                started_at=now,
                observation_count=0,
            )
        )
        session.commit()
        session.add(
            RawObservation(
                ingest_run_id="run-x",
                source="s",
                stream="st",
                external_id="e1",
                entity_kind="event",
                observed_at=now,
                effective_at=now,
                source_updated_at=now,
                payload_hash="a" * 64,
                raw_ref="a" * 64,
                detail_level="partial",
                schema_version="1",
            )
        )
        session.commit()
    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO raw_observations "
                    "(ingest_run_id, source, stream, external_id, entity_kind, "
                    "observed_at, effective_at, source_updated_at, payload_hash, raw_ref, "
                    "detail_level, schema_version, created_at) "
                    "VALUES ('run-x','s','st','e1','event',:now,:now,:now,:h,:h,'partial','1',:now)"
                ),
                {"now": now.isoformat(), "h": "a" * 64},
            )


def test_event_night_result_not_overwritten_by_same_kind_correction(db_env) -> None:
    repo: IngestRepository = db_env["repo"]
    store: ContentAddressedRawStore = db_env["store"]
    Session = db_env["session_factory"]
    bout_id = _seed_bout(Session)
    b1, h1 = _payload(b'{"night":1}')
    b2, h2 = _payload(b'{"night":2}')
    store.put(b1)
    store.put(b2)
    run = repo.start_run(source="testsource", stream="results", scope="profile:default")
    first = _obs(
        payload_hash=h1,
        version_kind="event_night",
        detail_level=DetailLevel.VERIFIED,
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-a",
            "result_type": "win",
            "method": "KO",
            "ending_round": 1,
            "time_str": "1:00",
        },
    )
    second = _obs(
        payload_hash=h2,
        version_kind="event_night",
        detail_level=DetailLevel.VERIFIED,
        observed_at=_ts(18),
        subject_id=bout_id,
        attributes={
            "fighter_a_id": "f-a",
            "fighter_b_id": "f-b",
            "winner_fighter_id": "f-b",
            "result_type": "win",
            "method": "DEC",
            "ending_round": 3,
            "time_str": "5:00",
        },
    )
    repo.commit_batch(run_id=run.id, observations=[first], checkpoint_token="1", checkpoint_version="v1")
    out = repo.commit_batch(
        run_id=run.id, observations=[second], checkpoint_token="2", checkpoint_version="v1"
    )
    assert out.skipped_preserve_version == 1
    with Session() as session:
        assert session.scalars(select(RawObservation)).all().__len__() == 2
        night = session.scalars(
            select(BoutResultVersion).where(BoutResultVersion.version_kind == "event_night")
        ).one()
        assert night.winner_fighter_id == "f-a"
        assert night.method == "KO"
