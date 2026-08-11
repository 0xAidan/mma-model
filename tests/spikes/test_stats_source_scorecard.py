"""Tests for DWCS-003 licensed stats/identity source audit (no live network)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "spikes" / "audit_stats_sources.py"
SENTINEL_API_KEY = "SENTINEL_BALLDONTLIE_KEY_DO_NOT_LEAK"
PROHIBITED_HOST_FRAGMENTS = (
    "tapology.com",
    "sherdog.com",
    "fightmatrix.com",
    "ufcstats.com",
    "bet365.com",
)


def _load_audit_module() -> Any:
    spec = importlib.util.spec_from_file_location("audit_stats_sources", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit() -> Any:
    if not SCRIPT_PATH.is_file():
        pytest.fail(f"missing audit script: {SCRIPT_PATH}")
    return _load_audit_module()


@pytest.fixture
def sample_bouts() -> list[dict[str, Any]]:
    return [
        {
            "bout_id": "dwcs:bout:espn:1",
            "event_id": "dwcs:event:espn:100",
            "calendar_year": 2023,
            "series_variant": "standard",
            "version_state": "assumed_equal_to_current",
            "event_night_result": {"class": "decisive", "winner_normalized": "alice alpha"},
            "current_result": {"class": "decisive", "winner_normalized": "alice alpha"},
            "participants": [
                {
                    "display_name": "Alice Alpha",
                    "normalized_name": "alice alpha",
                    "espn_athlete_id": "1001",
                    "current_winner_flag": True,
                },
                {
                    "display_name": "Bob Beta",
                    "normalized_name": "bob beta",
                    "espn_athlete_id": "1002",
                    "current_winner_flag": False,
                },
            ],
        },
        {
            "bout_id": "dwcs:bout:espn:2",
            "event_id": "dwcs:event:espn:100",
            "calendar_year": 2024,
            "series_variant": "standard",
            "version_state": "reversed_to_no_contest",
            "event_night_result": {"class": "decisive", "winner_normalized": "cara-lee gamma"},
            "current_result": {"class": "no_contest", "winner_normalized": None},
            "participants": [
                {
                    "display_name": "Cara-Lee Gamma",
                    "normalized_name": "cara lee gamma",
                    "espn_athlete_id": "1003",
                    "current_winner_flag": False,
                },
                {
                    "display_name": "José Delta",
                    "normalized_name": "jose delta",
                    "espn_athlete_id": "1004",
                    "current_winner_flag": False,
                },
            ],
        },
        {
            "bout_id": "dwcs:bout:espn:3",
            "event_id": "dwcs:event:espn:101",
            "calendar_year": 2025,
            "series_variant": "standard",
            "version_state": "assumed_equal_to_current",
            "event_night_result": {"class": "decisive", "winner_normalized": "eve epsilon"},
            "current_result": {"class": "decisive", "winner_normalized": "eve epsilon"},
            "participants": [
                {
                    "display_name": "Eve Epsilon",
                    "normalized_name": "eve epsilon",
                    "espn_athlete_id": "1005",
                    "current_winner_flag": True,
                },
                {
                    "display_name": "Frank Zeta Jr.",
                    "normalized_name": "frank zeta jr",
                    "espn_athlete_id": "1006",
                    "current_winner_flag": False,
                },
            ],
        },
        {
            "bout_id": "dwcs:bout:espn:old",
            "event_id": "dwcs:event:espn:50",
            "calendar_year": 2019,
            "series_variant": "standard",
            "version_state": "assumed_equal_to_current",
            "event_night_result": {"class": "decisive", "winner_normalized": "old one"},
            "current_result": {"class": "decisive", "winner_normalized": "old one"},
            "participants": [
                {
                    "display_name": "Old One",
                    "normalized_name": "old one",
                    "espn_athlete_id": "9001",
                    "current_winner_flag": True,
                },
                {
                    "display_name": "Old Two",
                    "normalized_name": "old two",
                    "espn_athlete_id": "9002",
                    "current_winner_flag": False,
                },
            ],
        },
    ]


def test_schema_version_and_required_top_level_keys(audit: Any) -> None:
    schema = audit.SCORECARD_SCHEMA_KEYS
    for key in (
        "schema_version",
        "ticket",
        "captured_at",
        "capture_mode",
        "manifest",
        "audit_universes",
        "providers",
        "decision",
        "prohibited_sources",
        "evidence_timestamps",
    ):
        assert key in schema


def test_filter_manifest_years(audit: Any, sample_bouts: list[dict[str, Any]]) -> None:
    filtered = audit.filter_bouts_by_year(sample_bouts, 2023, 2025)
    assert len(filtered) == 3
    assert all(2023 <= int(b["calendar_year"]) <= 2025 for b in filtered)


def test_extract_entrants_unique_by_athlete_id(
    audit: Any, sample_bouts: list[dict[str, Any]]
) -> None:
    filtered = audit.filter_bouts_by_year(sample_bouts, 2023, 2025)
    entrants = audit.extract_entrants(filtered)
    assert len(entrants) == 6
    ids = {e["espn_athlete_id"] for e in entrants}
    assert ids == {"1001", "1002", "1003", "1004", "1005", "1006"}


def test_difficult_identity_sample_is_deterministic_and_documented(
    audit: Any, sample_bouts: list[dict[str, Any]]
) -> None:
    filtered = audit.filter_bouts_by_year(sample_bouts, 2023, 2025)
    entrants = audit.extract_entrants(filtered)
    sample_a = audit.select_difficult_identity_sample(entrants, size=3)
    sample_b = audit.select_difficult_identity_sample(entrants, size=3)
    assert sample_a == sample_b
    assert len(sample_a) == 3
    # Unicode / hyphenated / multi-token should rank ahead of plain names.
    top_ids = [row["espn_athlete_id"] for row in sample_a]
    assert "1004" in top_ids  # José
    assert "1003" in top_ids  # Cara-Lee / three tokens
    method = audit.difficult_identity_selection_method()
    assert "deterministic" in method.lower()
    assert str(audit.DIFFICULT_IDENTITY_SEED) in method


def test_coverage_rate_math_and_unknown_vs_zero(audit: Any) -> None:
    observed = audit.make_rate_metric(numerator=98, denominator=100, status="measured")
    assert observed["rate"] == pytest.approx(0.98)
    assert observed["numerator"] == 98
    assert observed["denominator"] == 100

    unknown = audit.make_rate_metric(
        numerator=None,
        denominator=100,
        status="unknown",
        reason="not_configured",
    )
    assert unknown["rate"] is None
    assert unknown["numerator"] is None
    assert unknown["denominator"] == 100
    assert unknown["status"] == "unknown"
    assert unknown["reason"] == "not_configured"
    # Missing credentials must never be encoded as zero coverage.
    assert unknown["numerator"] != 0


def test_not_configured_distinct_from_absent_and_auth_failed(audit: Any) -> None:
    not_cfg = audit.classify_provider_access(api_key=None, http_status=None, body=None)
    assert not_cfg == "not_configured"

    auth = audit.classify_provider_access(
        api_key="k", http_status=401, body={"error": "unauthorized"}
    )
    assert auth == "auth_failed"

    entitlement = audit.classify_provider_access(
        api_key="k", http_status=401, body={"error": "tier does not have access"}
    )
    assert entitlement == "entitlement_blocked"

    absent = audit.classify_observation_status(
        access_status="ok", matched=False, request_failed=False
    )
    assert absent == "absent"

    blocked = audit.classify_observation_status(
        access_status="not_configured", matched=False, request_failed=False
    )
    assert blocked == "unknown"


def test_decision_thresholds_balldontlie_boundary(audit: Any) -> None:
    gates_pass = {
        "event_coverage_rate": 0.98,
        "bout_coverage_rate": 0.98,
        "outcome_agreement_rate": 0.99,
        "required_features_status": "pass",
        "pit_fitness_status": "pass",
        "rights_status": "pass",
        "budget_status": "pass",
        "metrics_status": "measured",
    }
    decision = audit.apply_stats_source_decision_tree(
        balldontlie_gates=gates_pass,
        api_sports_gates={
            "access_status": "not_configured",
            "non_overlap_rate": None,
            "accuracy_status": "unknown",
        },
        sportsdataio_status="quote_pending",
        combat_registry_status="quote_pending",
        monthly_budget_usd=69.99,
        budget_cap_usd=100.0,
    )
    assert decision["primary"] == "balldontlie"
    assert decision["path"] == "balldontlie_primary"
    assert decision["hard_blocker"] is False

    just_below = dict(gates_pass)
    just_below["event_coverage_rate"] = 0.979999
    blocked = audit.apply_stats_source_decision_tree(
        balldontlie_gates=just_below,
        api_sports_gates={
            "access_status": "not_configured",
            "non_overlap_rate": None,
            "accuracy_status": "unknown",
        },
        sportsdataio_status="quote_pending",
        combat_registry_status="quote_pending",
        monthly_budget_usd=69.99,
        budget_cap_usd=100.0,
    )
    assert blocked["primary"] is None
    assert blocked["hard_blocker"] is True
    assert blocked["path"] == "hard_blocker"


def test_decision_outcome_agreement_boundary(audit: Any) -> None:
    gates = {
        "event_coverage_rate": 1.0,
        "bout_coverage_rate": 1.0,
        "outcome_agreement_rate": 0.989999,
        "required_features_status": "pass",
        "pit_fitness_status": "pass",
        "rights_status": "pass",
        "budget_status": "pass",
        "metrics_status": "measured",
    }
    decision = audit.apply_stats_source_decision_tree(
        balldontlie_gates=gates,
        api_sports_gates={
            "access_status": "ok",
            "non_overlap_rate": 0.20,
            "accuracy_status": "pass",
        },
        sportsdataio_status="quote_pending",
        combat_registry_status="quote_pending",
        monthly_budget_usd=69.99,
        budget_cap_usd=100.0,
    )
    assert decision["hard_blocker"] is True
    assert decision["primary"] is None


def test_api_sports_requires_non_overlap_and_accuracy(audit: Any) -> None:
    bdl_fail = {
        "event_coverage_rate": 0.5,
        "bout_coverage_rate": 0.5,
        "outcome_agreement_rate": 0.5,
        "required_features_status": "fail",
        "pit_fitness_status": "fail",
        "rights_status": "pass",
        "budget_status": "pass",
        "metrics_status": "measured",
    }
    too_low = audit.apply_stats_source_decision_tree(
        balldontlie_gates=bdl_fail,
        api_sports_gates={
            "access_status": "ok",
            "non_overlap_rate": 0.099,
            "accuracy_status": "pass",
        },
        sportsdataio_status="quote_pending",
        combat_registry_status="quote_pending",
        monthly_budget_usd=80.0,
        budget_cap_usd=100.0,
    )
    assert too_low["api_sports_probe_keep"] is False

    keep = audit.apply_stats_source_decision_tree(
        balldontlie_gates=bdl_fail,
        api_sports_gates={
            "access_status": "ok",
            "non_overlap_rate": 0.10,
            "accuracy_status": "pass",
        },
        sportsdataio_status="quote_pending",
        combat_registry_status="quote_pending",
        monthly_budget_usd=80.0,
        budget_cap_usd=100.0,
    )
    assert keep["api_sports_probe_keep"] is True
    # API-Sports is enrichment probe only; never silent primary when BDL fails.
    assert keep["primary"] is None
    assert keep["hard_blocker"] is True


def test_rights_and_budget_gates(audit: Any) -> None:
    rights_ok = audit.evaluate_rights_gate(
        {
            "storage_allowed": True,
            "modeling_allowed": True,
            "source": "written_terms",
            "citation": "https://balldontlie.io/terms.html",
        }
    )
    assert rights_ok["status"] == "pass"

    rights_unknown = audit.evaluate_rights_gate(
        {
            "storage_allowed": None,
            "modeling_allowed": None,
            "source": "no_written_response",
            "citation": None,
        }
    )
    assert rights_unknown["status"] == "unknown"

    budget = audit.evaluate_budget_gate(
        recurring_monthly_usd=69.99,
        cap_usd=100.0,
        components={"the_odds_api": 30.0, "balldontlie_goat": 39.99},
    )
    assert budget["status"] == "pass"
    over = audit.evaluate_budget_gate(
        recurring_monthly_usd=120.0,
        cap_usd=100.0,
        components={"sportsdataio": 120.0},
    )
    assert over["status"] == "fail"


def test_redact_removes_secrets_and_full_payloads(audit: Any) -> None:
    raw = {
        "api_key": SENTINEL_API_KEY,
        "authorization": f"Bearer {SENTINEL_API_KEY}",
        "request_url": f"https://api.balldontlie.io/mma/v1/fights?apiKey={SENTINEL_API_KEY}",
        "providers": {
            "balldontlie": {
                "status": "ok",
                "raw_payload": {"data": [{"id": 1, "secret_field": "x"}] * 5},
                "sample_fight": {"fighter1": {"name": "A"}, "result_method": "KO"},
            }
        },
    }
    redacted = audit.redact_scorecard(raw)
    blob = json.dumps(redacted)
    assert SENTINEL_API_KEY not in blob
    assert "api_key" not in redacted
    assert "authorization" not in redacted
    assert "raw_payload" not in json.dumps(redacted["providers"])
    assert "sample_fight" not in json.dumps(redacted["providers"])


def test_no_prohibited_fallback_in_decision(audit: Any) -> None:
    decision = audit.apply_stats_source_decision_tree(
        balldontlie_gates={
            "event_coverage_rate": None,
            "bout_coverage_rate": None,
            "outcome_agreement_rate": None,
            "required_features_status": "unknown",
            "pit_fitness_status": "unknown",
            "rights_status": "pass",
            "budget_status": "unknown",
            "metrics_status": "unknown",
        },
        api_sports_gates={
            "access_status": "not_configured",
            "non_overlap_rate": None,
            "accuracy_status": "unknown",
        },
        sportsdataio_status="quote_pending",
        combat_registry_status="quote_pending",
        monthly_budget_usd=30.0,
        budget_cap_usd=100.0,
    )
    assert decision["hard_blocker"] is True
    assert decision["prohibited_scraping_selected"] is False
    fallbacks = [row["source"] for row in decision["ranked_lawful_fallbacks"]]
    for banned in audit.PROHIBITED_PRODUCTION_SOURCES:
        assert banned not in fallbacks


def test_vendor_checklist_marks_unanswered_as_blockers(audit: Any) -> None:
    checklist = audit.build_vendor_request_checklist("sportsdataio")
    assert checklist["status"] == "quote_pending"
    unanswered = [item for item in checklist["items"] if item["status"] == "unanswered"]
    assert unanswered
    assert all(item.get("blocker") is True for item in unanswered)


def test_build_scorecard_offline_is_repeatable(
    audit: Any, sample_bouts: list[dict[str, Any]], tmp_path: Path
) -> None:
    capture = "2026-08-11T21:00:00+00:00"
    score_a = audit.build_scorecard(
        bouts=sample_bouts,
        captured_at=capture,
        capture_mode="fixtures",
        balldontlie_key=None,
        api_sports_key=None,
        vendor_notes={},
        live_observations=None,
    )
    score_b = audit.build_scorecard(
        bouts=sample_bouts,
        captured_at=capture,
        capture_mode="fixtures",
        balldontlie_key=None,
        api_sports_key=None,
        vendor_notes={},
        live_observations=None,
    )
    assert score_a == score_b
    assert score_a["providers"]["balldontlie"]["access_status"] == "not_configured"
    assert score_a["providers"]["api_sports"]["access_status"] == "not_configured"
    assert score_a["decision"]["hard_blocker"] is True
    assert score_a["decision"]["primary"] is None
    # Unknown metrics retain denominator and null numerator.
    event_metric = score_a["providers"]["balldontlie"]["metrics"]["event_coverage"]
    assert event_metric["denominator"] >= 1
    assert event_metric["numerator"] is None
    assert event_metric["status"] == "unknown"

    out = tmp_path / "scorecard.json"
    audit.write_scorecard(score_a, out, redact=True)
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded["captured_at"] == capture
    assert SENTINEL_API_KEY not in out.read_text(encoding="utf-8")


def test_live_measurement_claim_requires_measured_status(audit: Any) -> None:
    score = audit.build_scorecard(
        bouts=[],
        captured_at="2026-08-11T21:00:00+00:00",
        capture_mode="fixtures",
        balldontlie_key=None,
        api_sports_key=None,
        vendor_notes={},
        live_observations=None,
    )
    assert score["capture_mode"] == "fixtures"
    assert score["live_measurements_claimed"] is False
    assert score["providers"]["balldontlie"]["metrics"]["event_coverage"]["status"] != (
        "measured"
    )


def test_match_provider_bout_by_participants_and_date(audit: Any) -> None:
    bout = {
        "bout_id": "b1",
        "occurrence_timestamp": "2024-08-13T00:00:00+00:00",
        "participants": [
            {"normalized_name": "jane doe", "display_name": "Jane Doe"},
            {"normalized_name": "john smith", "display_name": "John Smith"},
        ],
        "event_night_result": {"class": "decisive", "winner_normalized": "jane doe"},
    }
    fights = [
        {
            "id": 9,
            "date": "2024-08-13",
            "fighter1": {"name": "John Smith"},
            "fighter2": {"name": "Jane Doe"},
            "status": "completed",
            "result_winner_id": 1,
        }
    ]
    matched = audit.match_bout_to_provider_fight(bout, fights)
    assert matched is not None
    assert matched["id"] == 9


def test_outcome_agreement_counts(audit: Any) -> None:
    pairs = [
        {"manifest_class": "decisive", "provider_class": "decisive", "winner_agree": True},
        {"manifest_class": "decisive", "provider_class": "decisive", "winner_agree": False},
        {"manifest_class": "no_contest", "provider_class": "no_contest", "winner_agree": True},
    ]
    metric = audit.compute_outcome_agreement(pairs)
    assert metric["denominator"] == 3
    assert metric["numerator"] == 2
    assert metric["rate"] == pytest.approx(2 / 3)


def test_committed_scorecard_sanitized_and_schema_valid(audit: Any) -> None:
    path = ROOT / "output" / "research" / "stats-source-scorecard.json"
    if not path.is_file():
        pytest.skip("committed scorecard not generated yet")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in audit.SCORECARD_SCHEMA_KEYS:
        assert key in payload
    blob = path.read_text(encoding="utf-8")
    assert SENTINEL_API_KEY not in blob
    assert not re.search(r"(?i)(api[_-]?key|authorization)\s*[\"']?\s*[:=]\s*[\"'][^\"']+", blob)
    for host in PROHIBITED_HOST_FRAGMENTS:
        # Citations mentioning prohibition are fine; request URLs are not.
        assert f"https://{host}" not in blob
        assert f"http://{host}" not in blob
    assert payload["decision"]["prohibited_scraping_selected"] is False
    if payload["capture_mode"] == "fixtures":
        assert payload["live_measurements_claimed"] is False


def test_decision_doc_exists_with_citations() -> None:
    path = ROOT / "docs" / "research" / "stats-source-decision.md"
    if not path.is_file():
        pytest.skip("decision doc not written yet")
    text = path.read_text(encoding="utf-8")
    assert "DWCS-003" in text
    assert "balldontlie.io/terms" in text
    assert "hard" in text.lower() and "blocker" in text.lower()
    assert "Tapology" in text or "tapology" in text
    assert "handoff" in text.lower()


def test_cli_help_mentions_manifest_and_out(audit: Any) -> None:
    parser = audit.build_arg_parser()
    help_text = parser.format_help()
    assert "--manifest" in help_text
    assert "--out" in help_text
    assert "--capture-time" in help_text
    assert "--redact" in help_text
