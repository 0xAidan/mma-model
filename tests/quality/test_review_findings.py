"""Adversarial tests for independent DWCS-106 review findings 1-15."""

from __future__ import annotations

import gzip
import hashlib
import json
import random
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from mma_model.db.tables.core import CanonicalFighter, FighterProfileObservation, FighterSourceId
from mma_model.db.tables.history import HistorySourceBout
from mma_model.db.tables.identity import IdentityReviewQueue
from mma_model.db.tables.provenance import RawObservation
from mma_model.dwcs.ids import canonical_bout_id
from mma_model.dwcs.manifest import load_dwcs_bout_manifest
from mma_model.evaluation.contract import load_evaluation_contract
from mma_model.ingest.raw_store import ContentAddressedRawStore
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import report_with_gates
from mma_model.quality.readonly import CoverageDatabaseError, open_readonly_sqlite_engine
from mma_model.quality.schema import (
    CoverageSchemaError,
    load_coverage_schema,
    validate_coverage_json,
)
from mma_model.quality.universe import UniverseContractError, load_universe_contract
from mma_model.sources.policy import load_source_policy
from tests.quality.helpers import add_ingest_run, add_observation, make_empty_db

UTC = timezone.utc
PAST = datetime(2018, 6, 2, tzinfo=UTC)
NOW = datetime(2026, 8, 12, tzinfo=UTC)


def _first_bout_id() -> str:
    return canonical_bout_id(load_dwcs_bout_manifest()[0].espn_competition_id)


def _attrs(result_type: str = "decisive", winner: str = "aaa") -> str:
    return json.dumps({"result_type": result_type, "winner_fighter_id": winner})


def test_manifest_only_is_bronze_not_silver(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session, source="dwcs_manifest")
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="manifest-only",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                quality_tier="silver",
                attributes_json=_attrs(),
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "bronze"
        assert report.core_tiers["bronze"] == 1
        assert report.core_tiers["silver"] == 0
        assert report.core_tiers["missing"] == 439
    finally:
        env["engine"].dispose()


def test_two_independent_proxy_sources_agree_silver(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session)
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="m1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                quality_tier="silver",
                attributes_json=_attrs(),
            )
            add_observation(
                session,
                run.id,
                source="ufcstats_public",
                external_id="u1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                quality_tier="silver",
                attributes_json=_attrs(),
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "silver"
        ufc = next(item for item in report.source_rows if item.source == "ufcstats_public")
        assert ufc.mapped_bouts == 1
        assert ufc.status == "source_killed"
    finally:
        env["engine"].dispose()


def test_independent_disagreement_is_conflict(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session)
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="m1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs("decisive", "aaa"),
            )
            add_observation(
                session,
                run.id,
                source="ufcstats_public",
                external_id="u1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs("draw", ""),
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "conflict"
    finally:
        env["engine"].dispose()


def test_licensed_source_access_status_stays_validation_only_but_facts_can_agree(
    tmp_path,
) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session)
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="m1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            add_observation(
                session,
                run.id,
                source="sportsdataio",
                external_id="lic1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "silver"
        licensed = next(item for item in report.source_rows if item.source == "sportsdataio")
        assert licensed.validation_only is True
        assert licensed.status == "validation_only"
    finally:
        env["engine"].dispose()


def test_direct_timestamp_is_gold(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            run = add_ingest_run(session)
            add_observation(
                session,
                run.id,
                source="ufcstats_public",
                external_id="gold-1",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                source_published_at=PAST,
                timestamp_quality="direct_source_timestamp",
                quality_tier="gold",
                attributes_json=_attrs(),
            )
            session.flush()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        row = next(item for item in report.bouts if item.bout_id == bout_id)
        assert row.overall_tier == "gold"
        assert row.timestamp_quality == "direct_source_timestamp"
        ufc = next(item for item in report.source_rows if item.source == "ufcstats_public")
        assert ufc.mapped_bouts == 1
        assert ufc.gold == 1
        assert ufc.status == "source_killed"
    finally:
        env["engine"].dispose()


def test_insertion_order_and_later_gold_are_deterministic(tmp_path) -> None:
    bout_id = _first_bout_id()
    policy = load_source_policy()
    hashes: list[tuple[str, str, str]] = []
    orders = [
        ["proxy", "gold"],
        ["gold", "proxy"],
    ]
    random.shuffle(orders)
    for index, order in enumerate(orders):
        subdir = tmp_path / f"order-{index}"
        subdir.mkdir()
        env = make_empty_db(subdir)
        try:
            with env["Session"]() as session:
                run = add_ingest_run(session)
                for label in order:
                    if label == "proxy":
                        add_observation(
                            session,
                            run.id,
                            source="dwcs_manifest",
                            external_id="proxy",
                            subject_id=bout_id,
                            effective_at=PAST,
                            observed_at=NOW,
                            proxy_published_at=PAST,
                            timestamp_quality="publication_proxy",
                            quality_tier="silver",
                            payload_hash="1" * 64,
                            attributes_json=_attrs(),
                        )
                    else:
                        add_observation(
                            session,
                            run.id,
                            source="ufcstats_public",
                            external_id="gold",
                            subject_id=bout_id,
                            effective_at=PAST,
                            observed_at=NOW,
                            source_published_at=PAST,
                            timestamp_quality="direct_source_timestamp",
                            quality_tier="gold",
                            payload_hash="2" * 64,
                            attributes_json=_attrs(),
                        )
                session.flush()
                report = compute_coverage_report(series="dwcs", session=session, policy=policy)
            row = next(item for item in report.bouts if item.bout_id == bout_id)
            assert row.overall_tier == "gold"
            assert row.timestamp_quality == "direct_source_timestamp"
            hashes.append((report.report_hash, report.db_hash, report.config_hash))
        finally:
            env["engine"].dispose()
    assert hashes[0] == hashes[1]


def test_db_hash_changes_when_identity_or_profile_changes(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            baseline = compute_coverage_report(series="dwcs", session=session, policy=policy)
            fighter_id = str(uuid4())
            session.add(CanonicalFighter(id=fighter_id, display_name="Hash Probe"))
            session.flush()
            session.add(
                FighterSourceId(
                    fighter_id=fighter_id,
                    source="tapology_public",
                    external_id="hash-probe-1",
                )
            )
            session.flush()
            after_id = compute_coverage_report(series="dwcs", session=session, policy=policy)
            session.add(
                FighterProfileObservation(
                    fighter_id=fighter_id,
                    attribute="reach_in",
                    value_num=70.0,
                    source="ufcstats_public",
                    effective_at=PAST,
                    observed_at=NOW,
                )
            )
            session.flush()
            after_profile = compute_coverage_report(series="dwcs", session=session, policy=policy)
            session.rollback()
        assert after_id.db_hash != baseline.db_hash
        assert after_id.report_hash != baseline.report_hash
        assert after_profile.db_hash != after_id.db_hash
        assert after_id.config_hash == baseline.config_hash
    finally:
        env["engine"].dispose()


def test_unmatched_is_not_alias_of_scoped_pending(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            session.add(
                IdentityReviewQueue(
                    status="pending",
                    source="tapology_public",
                    external_id="unscoped-pending-1",
                    display_name="Unscoped",
                    normalized_name="unscoped",
                    rule_id="identity_conflict_queue",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add(
                HistorySourceBout(
                    source="tapology_public",
                    stream="fighter_history",
                    external_bout_id="hist-unmatched-1",
                    fighter_source="tapology_public",
                    fighter_external_id="hist-fighter-1",
                    fighter_name="Hist Fighter",
                    opponent_name="Opp",
                    classification="professional",
                    result="win",
                    observed_at=PAST,
                    effective_at=PAST,
                    payload_hash="a" * 64,
                    identity_status="unresolved",
                    observation_origin="unknown",
                )
            )
            session.commit()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        assert report.identity.unscoped_pending == 1
        assert report.identity.scoped_pending == 0
        assert report.identity.unmatched == 2
        assert report.identity.unmatched_source_identities == 2
        assert report.identity.unmatched != report.identity.scoped_pending
        assert report.identity.upcoming_blocks == 0
    finally:
        env["engine"].dispose()


def test_one_live_row_cannot_make_regional_one_of_one(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            session.add(
                HistorySourceBout(
                    source="tapology_public",
                    stream="fighter_history",
                    external_bout_id="not-in-sample",
                    fighter_source="tapology_public",
                    fighter_external_id="live-1",
                    fighter_name="Live One",
                    opponent_name="Opp",
                    classification="professional",
                    result="win",
                    observed_at=PAST,
                    effective_at=PAST,
                    payload_hash="b" * 64,
                    identity_status="unresolved",
                    observation_origin="live_public",
                )
            )
            session.commit()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        regional = report.regional_live
        assert regional["professional_n"] == 9
        assert regional["professional_found"] == 0
        assert not (regional["professional_found"] == 1 and regional["professional_n"] == 1)
        _, gates = report_with_gates(report, policy)
        pro = next(row for row in gates.gates if row.code == "regional_professional_sample")
        assert pro.status == "fail"
        assert pro.denominator == 9
    finally:
        env["engine"].dispose()


def test_raw_ref_missing_corrupt_dangling_and_unverifiable(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    policy = load_source_policy()
    digest = hashlib.sha256(b"raw-bytes").hexdigest()
    try:
        with env["Session"]() as session:
            run = add_ingest_run(session)
            add_observation(
                session,
                run.id,
                source="ufcstats_public",
                external_id="present-ref",
                subject_id=bout_id,
                effective_at=PAST,
                payload_hash=digest,
                raw_ref=digest,
                timestamp_quality="direct_source_timestamp",
                quality_tier="gold",
            )
            session.flush()
            missing = compute_coverage_report(series="dwcs", session=session, policy=policy)
            assert missing.raw_ref_integrity.unverifiable == 1
            assert missing.raw_ref_integrity.ok is False
            store = ContentAddressedRawStore(tmp_path / "raw-ok")
            store.put(b"raw-bytes")
            present = compute_coverage_report(
                series="dwcs", session=session, policy=policy, raw_store=store
            )
            assert present.raw_ref_integrity.blob_present == 1
            assert present.raw_ref_integrity.ok is True
            empty_store = ContentAddressedRawStore(tmp_path / "raw-empty")
            absent_store = compute_coverage_report(
                series="dwcs", session=session, policy=policy, raw_store=empty_store
            )
            assert absent_store.raw_ref_integrity.missing_blobs == 1
            corrupt_root = tmp_path / "raw-corrupt"
            path = corrupt_root / digest[:2] / f"{digest}.gz"
            path.parent.mkdir(parents=True)
            path.write_bytes(gzip.compress(b"tampered"))
            corrupt_store = ContentAddressedRawStore(corrupt_root)
            corrupt = compute_coverage_report(
                series="dwcs", session=session, policy=policy, raw_store=corrupt_store
            )
            assert corrupt.raw_ref_integrity.corrupt_blobs == 1
            session.add(
                RawObservation(
                    ingest_run_id=run.id,
                    source="ufcstats_public",
                    stream="history",
                    scope="quality-test",
                    checkpoint_version="v1",
                    external_id="dangling",
                    entity_kind="bout_result",
                    observed_at=PAST,
                    effective_at=PAST,
                    timestamp_quality="direct_source_timestamp",
                    quality_tier="gold",
                    payload_hash="f" * 64,
                    raw_ref="0" * 64,
                    subject_id=bout_id,
                    version_kind="event_night",
                    attributes_json=_attrs(),
                )
            )
            session.flush()
            dangling = compute_coverage_report(
                series="dwcs", session=session, policy=policy, raw_store=store
            )
            assert dangling.raw_ref_integrity.dangling_raw_refs == 1
            session.rollback()
    finally:
        env["engine"].dispose()


def test_universe_contracts_fail_closed(monkeypatch) -> None:
    real = load_evaluation_contract()
    tweaked = real.model_copy(
        update={
            "universe": real.universe.model_copy(
                update={"all_dwcs": real.universe.all_dwcs.model_copy(update={"bouts": 439})}
            )
        }
    )
    monkeypatch.setattr("mma_model.quality.universe.load_evaluation_contract", lambda: tweaked)
    with pytest.raises(UniverseContractError):
        load_universe_contract()


def test_readonly_sqlite_rejects_writes(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    env["engine"].dispose()
    engine = open_readonly_sqlite_engine(env["db_url"])
    try:
        with engine.connect() as connection:
            with pytest.raises(Exception):
                connection.execute(
                    text(
                        "INSERT INTO ingest_runs (id, source, stream, scope, status) "
                        "VALUES ('x', 'x', 'x', 'x', 'running')"
                    )
                )
                connection.commit()
    finally:
        engine.dispose()


def test_malformed_database_url_is_configuration_error() -> None:
    with pytest.raises(CoverageDatabaseError):
        open_readonly_sqlite_engine("postgres://localhost/mma")
    with pytest.raises(CoverageDatabaseError):
        open_readonly_sqlite_engine("sqlite://")


def test_schema_rejects_nested_additional_and_unknown_timestamp(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    schema = load_coverage_schema()
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            report, _gates = report_with_gates(
                compute_coverage_report(series="dwcs", session=session, policy=policy),
                policy,
            )
        payload = report.model_dump(mode="json")
        validate_coverage_json(payload, schema)
        payload["source_failures"] = [
            {
                "source": "tapology_public",
                "reason": "http_403",
                "scope": "default",
                "subject": "",
                "host": None,
                "path_category": None,
                "http_status": None,
                "observed_at": None,
                "extra": True,
            }
        ]
        with pytest.raises(CoverageSchemaError, match="additional field"):
            validate_coverage_json(payload, schema)
        payload = report.model_dump(mode="json")
        payload["bouts"][0]["timestamp_quality"] = "made_up"
        with pytest.raises(CoverageSchemaError):
            validate_coverage_json(payload, schema)
        payload = report.model_dump(mode="json")
        payload["regional_live"]["unexpected"] = 1
        with pytest.raises(CoverageSchemaError, match="additional field"):
            validate_coverage_json(payload, schema)
        payload = report.model_dump(mode="json")
        payload["fixture_validation"]["extra"] = True
        with pytest.raises(CoverageSchemaError, match="additional field"):
            validate_coverage_json(payload, schema)
    finally:
        env["engine"].dispose()


def test_future_effective_with_past_proxy_does_not_leak(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    bout_id = _first_bout_id()
    cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    future = datetime(2026, 9, 1, tzinfo=UTC)
    policy = load_source_policy()
    try:
        with env["Session"]() as session:
            run = add_ingest_run(session, source="dwcs_manifest")
            add_observation(
                session,
                run.id,
                source="dwcs_manifest",
                external_id="baseline",
                subject_id=bout_id,
                effective_at=PAST,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            session.flush()
            before = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=cutoff
            )
            add_observation(
                session,
                run.id,
                source="ufcstats_public",
                external_id="future-effective",
                subject_id=bout_id,
                effective_at=future,
                observed_at=NOW,
                proxy_published_at=PAST,
                timestamp_quality="publication_proxy",
                attributes_json=_attrs(),
            )
            session.flush()
            after = compute_coverage_report(
                series="dwcs", session=session, policy=policy, as_of=cutoff
            )
            current = compute_coverage_report(series="dwcs", session=session, policy=policy)
            session.rollback()
        assert before.core_tiers["bronze"] == 1
        assert after.report_hash == before.report_hash
        leaked = next(item for item in current.bouts if item.bout_id == bout_id)
        assert leaked.overall_tier == "silver"
    finally:
        env["engine"].dispose()
