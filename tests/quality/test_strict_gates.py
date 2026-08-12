"""Strict health-gate tests (DWCS-106)."""

from __future__ import annotations

from datetime import datetime, timezone

from mma_model.db.tables.identity import IdentityReviewQueue
from mma_model.quality.coverage import compute_coverage_report
from mma_model.quality.gates import evaluate_strict_gates
from mma_model.sources.policy import load_source_policy
from tests.quality.helpers import make_empty_db


def test_strict_gate_fails_on_unresolved_identity(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        conflicted = report.model_copy(
            update={
                "identity": report.identity.model_copy(
                    update={"scoped_unresolved_conflicts": 1}
                )
            }
        )
        result = evaluate_strict_gates(conflicted, policy)
        assert result.ok is False
        assert result.exit_code == 2
        assert "identity_conflict" in result.blocker_codes
    finally:
        env["engine"].dispose()


def test_licensed_primary_null_is_never_global_blocker(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        assert policy.licensed_audit_status.decision_primary is None
        assert policy.licensed_audit_status.licensed_hard_blocker is True
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        result = evaluate_strict_gates(report, policy)
        assert report.licensed_status.phase1_global_blocker is False
        assert report.licensed_status.licensed_primary_unselected is True
        assert "licensed_primary_unselected" not in result.blocker_codes
        assert "licensed_hard_blocker" not in result.blocker_codes
        assert "licensed_adoption_not_selected" not in result.blocker_codes
        assert "licensed_primary_status" not in result.blocker_codes
        assert "licensed_primary_status" in result.informational_codes
    finally:
        env["engine"].dispose()


def test_zero_denominator_required_live_sample_is_insufficient_not_pass(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        with env["Session"]() as session:
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        result = evaluate_strict_gates(report, policy)
        regional = [row for row in result.gates if row.code == "regional_professional_sample"]
        assert regional
        assert regional[0].status == "insufficient_sample"
        assert regional[0].blocking is True
        assert regional[0].denominator == 0
        assert result.ok is False
        assert result.exit_code == 2
        amateur = [row for row in result.gates if row.code == "regional_amateur_sample"]
        assert amateur[0].status == "insufficient_sample"
        agree = [row for row in result.gates if row.code == "pre_fight_agreement"]
        assert agree[0].status == "insufficient_sample"
        recon = [row for row in result.gates if row.code == "cross_source_reconciliation"]
        assert recon[0].status == "insufficient_sample"
    finally:
        env["engine"].dispose()


def test_unscoped_identity_does_not_block_scoped_dwcs(tmp_path) -> None:
    env = make_empty_db(tmp_path)
    try:
        policy = load_source_policy()
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        with env["Session"]() as session:
            session.add(
                IdentityReviewQueue(
                    status="pending",
                    source="tapology_public",
                    external_id="unscoped-1",
                    display_name="Unrelated Fighter",
                    normalized_name="unrelated fighter",
                    rule_id="identity_conflict_queue",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            report = compute_coverage_report(series="dwcs", session=session, policy=policy)
        assert report.identity.unscoped_pending == 1
        assert report.identity.scoped_unresolved_conflicts == 0
        result = evaluate_strict_gates(report, policy)
        assert "identity_conflict" not in result.blocker_codes
    finally:
        env["engine"].dispose()
