"""Champion/challenger registry and fixed-spec retrain (DWCS-402)."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from mma_model.cli import main
from mma_model.db.models import Fighter
from mma_model.db.session import create_all_for_tests
from mma_model.db.tables.model_registry import ModelRegistryDecision
from mma_model.evaluation.contract import PINNED_CONTRACT_HASH
from mma_model.jobs.handlers import HandlerRegistry, handle_retrain
from mma_model.jobs.types import DueJob, JobStatus, JobType
from mma_model.modeling.artifacts import PINNED_RIDGE_SPEC_HASH, RIDGE_SPEC_ID
from mma_model.modeling.baselines import TrainReport, run_protocol_train
from mma_model.modeling.promotion import (
    DecisionAction,
    GateVerdict,
    PromotionEvaluateRequiredError,
    PromotionGateError,
)
from mma_model.modeling.registry import (
    PINNED_REGISTRY_HASH,
    load_model_registry,
    promote_candidate,
    register_shadow_challenger,
    retrain_fixed_spec,
    rollback_champion,
    store_artifact_by_digest,
    write_registry_document,
)
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK, EXIT_STRICT_BLOCKERS
from mma_model.quality.models import GateResult


def _session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'registry.db'}", future=True)
    create_all_for_tests(engine)
    factory = sessionmaker(bind=engine, future=True)
    return factory(), engine


def _seed_champion(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    report = run_protocol_train(output_path=artifacts / "seed.json")
    digest, stored = store_artifact_by_digest(
        artifacts_dir=artifacts,
        payload_path=report.artifact.payload_path,
    )
    registry_path = tmp_path / "model_registry.yaml"
    write_registry_document(
        registry_path,
        champion_digest=digest,
        artifact_relpath=str(stored),
    )
    return registry_path, artifacts, digest, stored


def _pass_health_result() -> GateResult:
    return GateResult(
        ok=True,
        exit_code=EXIT_OK,
        blocker_codes=(),
        passed_codes=("test_health",),
        informational_codes=(),
        gates=(),
    )


def _fail_health_result() -> GateResult:
    return GateResult(
        ok=False,
        exit_code=EXIT_STRICT_BLOCKERS,
        blocker_codes=("test_blocker",),
        passed_codes=(),
        informational_codes=(),
        gates=(),
    )


def _gate_kwargs(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "health_result": _pass_health_result(),
        "backtest_ok": True,
        "calibration_ok": True,
    }
    payload.update(overrides)
    return payload


def test_packaged_registry_identity_hash() -> None:
    state = load_model_registry()
    assert state.content_hash == PINNED_REGISTRY_HASH
    assert state.champion.spec_id == RIDGE_SPEC_ID
    assert state.champion.config_hash == PINNED_RIDGE_SPEC_HASH


def test_failed_retrain_leaves_champion_unchanged(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    session, engine = _session(tmp_path)

    def _boom(*, output_path: Path, include_holdout: bool = False) -> TrainReport:
        _ = (output_path, include_holdout)
        raise RuntimeError("injected train failure")

    result = retrain_fixed_spec(
        session,
        registry_path=registry_path,
        artifacts_dir=artifacts,
        train_runner=_boom,  # type: ignore[arg-type]
        **_gate_kwargs(),  # type: ignore[arg-type]
    )
    session.commit()
    assert result.champion_unchanged is True
    assert result.activated is False
    assert result.artifact_digest == digest
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    rows = list(session.scalars(select(ModelRegistryDecision).order_by(ModelRegistryDecision.seq)))
    assert rows
    assert rows[-1].action == DecisionAction.REJECT.value
    engine.dispose()


def test_same_spec_retrain_gate_fail_does_not_activate(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    session, engine = _session(tmp_path)

    result = retrain_fixed_spec(
        session,
        registry_path=registry_path,
        artifacts_dir=artifacts,
        health_result=_fail_health_result(),
        backtest_ok=True,
        calibration_ok=True,
    )
    session.commit()
    assert result.champion_unchanged is True
    assert result.activated is False
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    rows = list(session.scalars(select(ModelRegistryDecision)))
    assert any(row.action == DecisionAction.REJECT.value for row in rows)
    engine.dispose()


def test_missing_backtest_or_calibration_fails_closed(tmp_path: Path) -> None:
    registry_path, artifacts, digest, stored = _seed_champion(tmp_path)
    session, engine = _session(tmp_path)
    with pytest.raises(PromotionGateError):
        promote_candidate(
            session,
            registry_path=registry_path,
            candidate_digest=digest,
            evaluate=True,
            artifacts_dir=artifacts,
            reason="missing backtest",
            artifact_path=stored,
            health_result=_pass_health_result(),
            backtest_ok=None,
            calibration_ok=True,
        )
    with pytest.raises(PromotionGateError):
        promote_candidate(
            session,
            registry_path=registry_path,
            candidate_digest=digest,
            evaluate=True,
            artifacts_dir=artifacts,
            reason="missing calibration",
            artifact_path=stored,
            health_result=_pass_health_result(),
            backtest_ok=True,
            calibration_ok=None,
        )
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    engine.dispose()


def test_promote_without_evaluate_rejected(tmp_path: Path) -> None:
    registry_path, artifacts, digest, stored = _seed_champion(tmp_path)
    report = run_protocol_train(output_path=artifacts / "cand.json")
    cand_digest, cand_path = store_artifact_by_digest(
        artifacts_dir=artifacts,
        payload_path=report.artifact.payload_path,
    )
    session, engine = _session(tmp_path)
    with pytest.raises(PromotionEvaluateRequiredError, match="--evaluate"):
        promote_candidate(
            session,
            registry_path=registry_path,
            candidate_digest=cand_digest,
            evaluate=False,
            artifacts_dir=artifacts,
            reason="should fail",
            artifact_path=cand_path,
            **_gate_kwargs(),  # type: ignore[arg-type]
        )
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    assert stored.is_file()
    engine.dispose()


def test_promote_gate_fail_leaves_champion_and_append_only(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    report = run_protocol_train(output_path=artifacts / "cand2.json")
    cand_digest, cand_path = store_artifact_by_digest(
        artifacts_dir=artifacts,
        payload_path=report.artifact.payload_path,
    )
    session, engine = _session(tmp_path)
    with pytest.raises(PromotionGateError):
        promote_candidate(
            session,
            registry_path=registry_path,
            candidate_digest=cand_digest,
            evaluate=True,
            artifacts_dir=artifacts,
            reason="gate fail",
            artifact_path=cand_path,
            health_result=_fail_health_result(),
            backtest_ok=True,
            calibration_ok=True,
        )
    session.commit()
    with pytest.raises(PromotionGateError):
        promote_candidate(
            session,
            registry_path=registry_path,
            candidate_digest=cand_digest,
            evaluate=True,
            artifacts_dir=artifacts,
            reason="gate fail again",
            artifact_path=cand_path,
            health_result=_fail_health_result(),
            backtest_ok=True,
            calibration_ok=True,
        )
    session.commit()
    rows = list(
        session.scalars(select(ModelRegistryDecision).order_by(ModelRegistryDecision.seq))
    )
    rejects = [row for row in rows if row.action == DecisionAction.REJECT.value]
    assert len(rejects) >= 2
    assert rejects[0].id != rejects[1].id
    assert rejects[0].seq < rejects[1].seq
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    engine.dispose()


def test_successful_promote_records_prior(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    report = run_protocol_train(output_path=artifacts / "cand3.json")
    cand_digest, cand_path = store_artifact_by_digest(
        artifacts_dir=artifacts,
        payload_path=report.artifact.payload_path,
    )
    session, engine = _session(tmp_path)
    when = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    def _pass_gate(**_kwargs: object) -> GateVerdict:
        return GateVerdict(
            ok=True,
            health_ok=True,
            evaluator_hash=PINNED_CONTRACT_HASH,
            details={"source": "injected_health_gate"},
        )

    payload = promote_candidate(
        session,
        registry_path=registry_path,
        candidate_digest=cand_digest,
        evaluate=True,
        artifacts_dir=artifacts,
        reason="gates passed",
        artifact_path=cand_path,
        health_gate=_pass_gate,
        backtest_ok=True,
        calibration_ok=True,
        at=when,
    )
    session.commit()
    assert payload["artifact_digest"] == cand_digest
    assert payload["prior_champion_digest"] == digest
    assert payload["config_hash"] == PINNED_RIDGE_SPEC_HASH
    assert payload["reason"] == "gates passed"
    assert when.isoformat() in payload["at"]
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == cand_digest
    assert reloaded.prior_champion is not None
    assert reloaded.prior_champion.artifact_digest == digest
    row = session.scalars(
        select(ModelRegistryDecision).where(
            ModelRegistryDecision.action == DecisionAction.PROMOTE.value
        )
    ).one()
    assert row.evaluator_hash == PINNED_CONTRACT_HASH
    assert row.prior_champion_digest == digest
    engine.dispose()


def test_rollback_restores_prior_without_db_rollback(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    report = run_protocol_train(output_path=artifacts / "cand4.json")
    cand_digest, cand_path = store_artifact_by_digest(
        artifacts_dir=artifacts,
        payload_path=report.artifact.payload_path,
    )
    session, engine = _session(tmp_path)
    session.add(Fighter(id="keep-me", name="Keep Me"))
    session.commit()
    promote_candidate(
        session,
        registry_path=registry_path,
        candidate_digest=cand_digest,
        evaluate=True,
        artifacts_dir=artifacts,
        reason="promote for rollback test",
        artifact_path=cand_path,
        **_gate_kwargs(),  # type: ignore[arg-type]
    )
    session.commit()
    before_fighters = int(session.scalar(select(func.count()).select_from(Fighter)) or 0)
    payload = rollback_champion(
        session,
        registry_path=registry_path,
        reason="restore prior champion",
    )
    session.commit()
    assert payload["artifact_digest"] == digest
    assert payload["prior_champion_digest"] == cand_digest
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    after_fighters = int(session.scalar(select(func.count()).select_from(Fighter)) or 0)
    assert after_fighters == before_fighters
    assert session.get(Fighter, "keep-me") is not None
    rows = list(
        session.scalars(
            select(ModelRegistryDecision).where(
                ModelRegistryDecision.action == DecisionAction.ROLLBACK.value
            )
        )
    )
    assert len(rows) == 1
    engine.dispose()


def test_new_config_registered_shadow_only(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    session, engine = _session(tmp_path)
    fake = "a" * 64
    register_shadow_challenger(
        session,
        registry_path=registry_path,
        spec_id="joint_v1",
        artifact_digest=fake,
        config_hash="b" * 64,
        reason="new joint challenger",
    )
    session.commit()
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    assert any(row.artifact_digest == fake for row in reloaded.challengers)
    assert all(row.lane.value == "shadow" for row in reloaded.challengers)
    engine.dispose()


def test_retrain_rejects_locked_holdout(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    session, engine = _session(tmp_path)
    result = retrain_fixed_spec(
        session,
        registry_path=registry_path,
        artifacts_dir=artifacts,
        include_holdout=True,
        **_gate_kwargs(),  # type: ignore[arg-type]
    )
    session.commit()
    assert result.champion_unchanged is True
    assert "2025" in result.reason or "holdout" in result.reason.lower()
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest
    engine.dispose()


def test_handle_retrain_failure_keeps_incumbent(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    session, engine = _session(tmp_path)
    handler_registry = HandlerRegistry()
    handler_registry.artifact.digest = digest
    job = DueJob(
        job_type=JobType.RETRAIN,
        idempotency_key="retrain:test",
        dependencies=(),
        event_id="evt-1",
        scope="event",
    )

    def _boom(*, output_path: Path, include_holdout: bool = False) -> TrainReport:
        _ = (output_path, include_holdout)
        raise RuntimeError("job train fail")

    result = handle_retrain(
        session,
        job=job,
        as_of=datetime(2026, 8, 12, tzinfo=UTC),
        events=(),
        context={
            "registry": handler_registry,
            "model_registry_path": registry_path,
            "artifacts_dir": artifacts,
            "train_runner": _boom,
            "health_result": _pass_health_result(),
            "backtest_ok": True,
            "calibration_ok": True,
        },
    )
    session.commit()
    assert result.status == JobStatus.FAILED
    assert result.artifact_digest == digest
    assert result.counts.get("champion_unchanged") is True
    assert handler_registry.artifact.digest == digest
    engine.dispose()


def test_handle_retrain_success_keeps_spec_id(tmp_path: Path) -> None:
    registry_path, artifacts, digest, _stored = _seed_champion(tmp_path)
    session, engine = _session(tmp_path)
    handler_registry = HandlerRegistry()
    handler_registry.artifact.digest = digest
    job = DueJob(
        job_type=JobType.RETRAIN,
        idempotency_key="retrain:ok",
        dependencies=(),
        event_id="evt-1",
        scope="event",
    )
    result = handle_retrain(
        session,
        job=job,
        as_of=datetime(2026, 8, 12, tzinfo=UTC),
        events=(),
        context={
            "registry": handler_registry,
            "model_registry_path": registry_path,
            "artifacts_dir": artifacts,
            "health_result": _pass_health_result(),
            "backtest_ok": True,
            "calibration_ok": True,
        },
    )
    session.commit()
    assert result.status == JobStatus.SUCCESS
    assert result.counts.get("spec_id") == RIDGE_SPEC_ID
    assert result.counts.get("promoted") is False
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.spec_id == RIDGE_SPEC_ID
    engine.dispose()


def test_handle_retrain_without_registry_keeps_champion(tmp_path: Path) -> None:
    session, engine = _session(tmp_path)
    handler_registry = HandlerRegistry()
    handler_registry.artifact.digest = "champion-fixed"
    job = DueJob(
        job_type=JobType.RETRAIN,
        idempotency_key="retrain:seam",
        dependencies=(),
        event_id="evt-1",
        scope="event",
    )
    result = handle_retrain(
        session,
        job=job,
        as_of=datetime(2026, 8, 12, tzinfo=UTC),
        events=(),
        context={"registry": handler_registry},
    )
    assert result.status == JobStatus.SUCCESS
    assert result.counts.get("champion_unchanged") is True
    assert result.counts.get("promoted") is False
    assert handler_registry.artifact.digest == "champion-fixed"
    engine.dispose()


def test_cli_retrain_promote_refuse_live_db(tmp_path: Path) -> None:
    registry_path, artifacts, digest, stored = _seed_champion(tmp_path)
    live = "sqlite:///data/mma.db"
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(
            [
                "model",
                "retrain",
                "--registry",
                str(registry_path),
                "--artifacts-dir",
                str(artifacts),
                "--database-url",
                live,
            ]
        )
    assert code == EXIT_INTERNAL
    assert "refusing live data/mma.db" in out.getvalue() + err.getvalue()

    out2 = io.StringIO()
    with redirect_stdout(out2), redirect_stderr(err):
        code2 = main(
            [
                "model",
                "promote",
                "--candidate",
                digest,
                "--evaluate",
                "--registry",
                str(registry_path),
                "--artifacts-dir",
                str(artifacts),
                "--artifact",
                str(stored),
                "--database-url",
                live,
            ]
        )
    assert code2 == EXIT_INTERNAL
    assert "refusing live data/mma.db" in out2.getvalue()


def test_cli_promote_without_health_evidence_fails(tmp_path: Path) -> None:
    registry_path, artifacts, digest, stored = _seed_champion(tmp_path)
    db = tmp_path / "cli-promote-fail.db"
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(
            [
                "model",
                "promote",
                "--candidate",
                digest,
                "--evaluate",
                "--registry",
                str(registry_path),
                "--artifacts-dir",
                str(artifacts),
                "--artifact",
                str(stored),
                "--database-url",
                f"sqlite:///{db}",
            ]
        )
    assert code == EXIT_INTERNAL
    combined = out.getvalue() + err.getvalue()
    assert "activation gates failed" in combined or "configuration error" in combined
    reloaded = load_model_registry(path=registry_path, enforce_pinned_digest=False)
    assert reloaded.champion.artifact_digest == digest


def test_cli_has_no_health_ok_or_force_flags(tmp_path: Path) -> None:
    registry_path, artifacts, digest, stored = _seed_champion(tmp_path)
    db = tmp_path / "cli-flags.db"
    with pytest.raises(SystemExit) as health_exc:
        main(
            [
                "model",
                "promote",
                "--candidate",
                digest,
                "--evaluate",
                "--registry",
                str(registry_path),
                "--artifacts-dir",
                str(artifacts),
                "--artifact",
                str(stored),
                "--database-url",
                f"sqlite:///{db}",
                "--health-ok",
            ]
        )
    assert health_exc.value.code != EXIT_OK

    with pytest.raises(SystemExit) as force_exc:
        main(
            [
                "model",
                "promote",
                "--candidate",
                digest,
                "--evaluate",
                "--registry",
                str(registry_path),
                "--artifacts-dir",
                str(artifacts),
                "--artifact",
                str(stored),
                "--database-url",
                f"sqlite:///{db}",
                "--force",
            ]
        )
    assert force_exc.value.code != EXIT_OK


def test_cli_promote_requires_evaluate(tmp_path: Path) -> None:
    registry_path, artifacts, digest, stored = _seed_champion(tmp_path)
    db = tmp_path / "cli-promote.db"
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "model",
                "promote",
                "--candidate",
                digest,
                "--registry",
                str(registry_path),
                "--artifacts-dir",
                str(artifacts),
                "--artifact",
                str(stored),
                "--database-url",
                f"sqlite:///{db}",
            ]
        )
    assert excinfo.value.code != EXIT_OK
