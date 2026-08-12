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
SYNTHETIC_PATH = ROOT / "tests" / "fixtures" / "spikes" / "stats_source_synthetic_observations.json"
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


@pytest.fixture(scope="module")
def synthetic() -> dict[str, Any]:
    return json.loads(SYNTHETIC_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def sample_bouts() -> list[dict[str, Any]]:
    return [
        {
            "bout_id": "dwcs:bout:espn:1",
            "event_id": "dwcs:event:espn:100",
            "calendar_year": 2023,
            "series_variant": "standard",
            "version_state": "assumed_equal_to_current",
            "occurrence_timestamp": "2023-08-01T00:00:00+00:00",
            "event_night_result": {
                "class": "decisive",
                "winner_display_name": "Alice Alpha",
            },
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
            "occurrence_timestamp": "2024-08-01T00:00:00+00:00",
            "event_night_result": {
                "class": "decisive",
                "winner_display_name": "Cara-Lee Gamma",
            },
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
            "occurrence_timestamp": "2025-08-01T00:00:00+00:00",
            "event_night_result": {
                "class": "decisive",
                "winner_display_name": "Eve Epsilon",
            },
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
            "occurrence_timestamp": "2019-08-01T00:00:00+00:00",
            "event_night_result": {
                "class": "decisive",
                "winner_display_name": "Old One",
            },
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
    top_ids = [row["espn_athlete_id"] for row in sample_a]
    assert "1004" in top_ids
    assert "1003" in top_ids
    method = audit.difficult_identity_selection_method()
    assert "deterministic" in method.lower()
    assert str(audit.DIFFICULT_IDENTITY_SEED) in method


def test_coverage_rate_math_and_unknown_vs_zero(audit: Any) -> None:
    observed = audit.make_rate_metric(numerator=98, denominator=100, status="measured")
    assert observed["rate"] == pytest.approx(0.98)
    unknown = audit.make_rate_metric(
        numerator=None,
        denominator=100,
        status="unknown",
        reason="not_configured",
    )
    assert unknown["rate"] is None
    assert unknown["numerator"] is None
    assert unknown["denominator"] == 100
    assert unknown["numerator"] != 0


def test_event_coverage_uses_unique_events_not_years(audit: Any) -> None:
    metric = audit.compute_event_coverage(
        matched_manifest_event_ids=["e1", "e2", "e2"],
        manifest_event_ids=[f"e{i}" for i in range(1, 31)],
    )
    assert metric["numerator"] == 2
    assert metric["denominator"] == 30
    assert metric["rate"] == pytest.approx(2 / 30)


def test_not_configured_distinct_from_absent_and_auth_failed(audit: Any) -> None:
    assert audit.classify_provider_access(api_key=None, http_status=None, body=None) == (
        "not_configured"
    )
    assert (
        audit.classify_provider_access(
            api_key="k", http_status=401, body={"error": "unauthorized"}
        )
        == "auth_failed"
    )
    assert (
        audit.classify_provider_access(
            api_key="k", http_status=401, body={"error": "tier does not have access"}
        )
        == "entitlement_blocked"
    )
    assert (
        audit.classify_provider_access(
            api_key="k",
            http_status=401,
            body={},
            authenticated_ok_prior=True,
        )
        == "entitlement_blocked"
    )
    assert (
        audit.classify_provider_access(api_key="k", http_status=429, body=None)
        == "quota_exceeded"
    )
    assert (
        audit.classify_observation_status(
            access_status="ok", matched=False, request_failed=False
        )
        == "absent"
    )
    assert (
        audit.classify_observation_status(
            access_status="not_configured", matched=False, request_failed=False
        )
        == "unknown"
    )


def test_dwcs_event_name_matcher_rejects_false_contender(audit: Any) -> None:
    assert audit.is_dwcs_provider_event_name(
        "Dana White's Contender Series: Season 7, Week 1"
    )
    assert audit.is_dwcs_provider_event_name("DWCS Season 8 Week 3")
    assert audit.is_dwcs_provider_event_name("Contender Series Brazil")
    assert not audit.is_dwcs_provider_event_name("FCC 36: Full Contact Contender 36")
    assert not audit.is_dwcs_provider_event_name("UFC Fight Night: Contender vs Prospect")
    assert not audit.is_dwcs_provider_event_name("")


def test_extract_unique_bout_dates(audit: Any, sample_bouts: list[dict[str, Any]]) -> None:
    dates = audit.extract_unique_bout_dates(sample_bouts)
    assert dates == ["2019-08-01", "2023-08-01", "2024-08-01", "2025-08-01"]


def test_retry_after_quota_then_succeeds(audit: Any) -> None:
    calls = {"n": 0}

    def fake_request(
        *,
        path: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> tuple[int, Any, dict[str, str]]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"error": "rate limited"}, {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "0"}
        return (
            200,
            {"data": [{"id": 1, "name": "Dana White's Contender Series"}], "meta": {}},
            {"x-ratelimit-limit": "5", "x-ratelimit-remaining": "4"},
        )

    sleeps: list[float] = []
    status, body, _headers = audit.call_with_quota_retries(
        request_get=fake_request,
        path="/events",
        params={"date": "2024-08-13", "per_page": 100},
        max_retries_on_quota=2,
        sleep_fn=sleeps.append,
        quota_sleep_seconds=1.0,
    )
    assert status == 200
    assert isinstance(body, dict)
    assert len(body.get("data") or []) == 1
    assert calls["n"] == 2
    assert sleeps == [60.0]
    pages = {
        None: {
            "status": 200,
            "body": {
                "data": [{"id": 1, "name": "Dana White's Contender Series Week 1"}],
                "meta": {"next_cursor": 10, "per_page": 1},
            },
            "headers": {"x-ratelimit-limit": "600", "x-ratelimit-remaining": "599"},
        },
        10: {
            "status": 200,
            "body": {
                "data": [{"id": 2, "name": "UFC 300"}],
                "meta": {"next_cursor": None, "per_page": 1},
            },
            "headers": {"x-ratelimit-limit": "600", "x-ratelimit-remaining": "598"},
        },
    }
    calls: list[dict[str, Any]] = []

    def fake_request(
        *,
        path: str,
        params: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> tuple[int, Any, dict[str, str]]:
        params = dict(params or {})
        cursor = params.get("cursor")
        calls.append({"path": path, "cursor": cursor})
        page = pages[cursor]
        return page["status"], page["body"], page["headers"]

    rows, last_status, meta = audit.paginate_balldontlie_get(
        request_get=fake_request,
        path="/events",
        base_params={"year": 2024, "per_page": 1},
        max_pages=5,
        sleep_fn=lambda _s: None,
    )
    assert last_status == 200
    assert [row["id"] for row in rows] == [1, 2]
    assert [call["cursor"] for call in calls] == [None, 10]
    assert meta["page_count"] == 2
    assert meta["truncated"] is False


def test_outcome_agreement_decisive_draw_nc_unmapped_boundaries(audit: Any) -> None:
    decisive_agree = audit.classify_outcome_pair(
        {
            "event_night_result": {
                "class": "decisive",
                "winner_display_name": "Alice Alpha",
            },
            "participants": [
                {"normalized_name": "alice alpha"},
                {"normalized_name": "bob beta"},
            ],
        },
        {
            "fighter1": {"id": 1, "name": "Alice Alpha"},
            "fighter2": {"id": 2, "name": "Bob Beta"},
            "winner": {"id": 1, "name": "Alice Alpha"},
            "status": "completed",
            "result_method": "KO",
        },
    )
    assert decisive_agree["status"] == "agree"
    assert decisive_agree["winner_agree"] is True

    decisive_disagree = audit.classify_outcome_pair(
        {
            "event_night_result": {
                "class": "decisive",
                "winner_display_name": "Alice Alpha",
            }
        },
        {
            "fighter1": {"id": 1, "name": "Alice Alpha"},
            "fighter2": {"id": 2, "name": "Bob Beta"},
            "result_winner_id": 2,
            "status": "completed",
        },
    )
    assert decisive_disagree["status"] == "disagree"
    assert decisive_disagree["winner_agree"] is False

    draw_agree = audit.classify_outcome_pair(
        {"event_night_result": {"class": "draw"}},
        {"status": "completed", "result_method": "Draw", "fighter1": {}, "fighter2": {}},
    )
    assert draw_agree["status"] == "agree"

    nc_agree = audit.classify_outcome_pair(
        {"event_night_result": {"class": "no_contest"}},
        {
            "status": "completed",
            "result_method": "No Contest",
            "fighter1": {},
            "fighter2": {},
        },
    )
    assert nc_agree["status"] == "agree"

    unmapped = audit.classify_outcome_pair(
        {
            "event_night_result": {
                "class": "decisive",
                "winner_display_name": "Alice Alpha",
            }
        },
        {"status": "completed", "fighter1": {"name": "A"}, "fighter2": {"name": "B"}},
    )
    assert unmapped["status"] == "unknown"
    assert unmapped["winner_agree"] is None

    metric = audit.compute_outcome_agreement(
        [decisive_agree, decisive_disagree, draw_agree, nc_agree, unmapped]
    )
    assert metric["denominator_policy"] == "comparable_mapped_pairs_only"
    assert metric["excluded_unknown_count"] == 1
    assert metric["denominator"] == 4
    assert metric["numerator"] == 3
    assert metric["rate"] == pytest.approx(0.75)


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
        monthly_budget_cents=6999,
        budget_cap_cents=10000,
    )
    assert decision["primary"] == "balldontlie"
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
        monthly_budget_cents=6999,
    )
    assert blocked["primary"] is None
    assert blocked["hard_blocker"] is True


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
        monthly_budget_cents=6999,
    )
    assert decision["hard_blocker"] is True
    assert decision["primary"] is None


def test_sportsdataio_adoption_reachable_with_complete_quote(audit: Any) -> None:
    bdl_fail = {
        "event_coverage_rate": None,
        "bout_coverage_rate": None,
        "outcome_agreement_rate": None,
        "required_features_status": "unknown",
        "pit_fitness_status": "unknown",
        "rights_status": "pass",
        "budget_status": "pass",
        "metrics_status": "unknown",
    }
    missing_quote = audit.apply_stats_source_decision_tree(
        balldontlie_gates=bdl_fail,
        api_sports_gates={"access_status": "not_configured"},
        sportsdataio_status="quote_pending",
        sportsdataio_gates={
            "quote_status": "quote_pending",
            "metrics_status": "measured",
            "event_coverage_rate": 1.0,
            "bout_coverage_rate": 1.0,
            "outcome_agreement_rate": 1.0,
            "required_features_status": "pass",
            "pit_fitness_status": "pass",
            "rights_status": "pass",
            "budget_status": "pass",
        },
    )
    assert missing_quote["hard_blocker"] is True
    assert missing_quote["primary"] is None

    adopted = audit.apply_stats_source_decision_tree(
        balldontlie_gates=bdl_fail,
        api_sports_gates={"access_status": "not_configured"},
        sportsdataio_status="complete",
        sportsdataio_gates={
            "quote_status": "complete",
            "metrics_status": "measured",
            "event_coverage_rate": 1.0,
            "bout_coverage_rate": 1.0,
            "outcome_agreement_rate": 1.0,
            "required_features_status": "pass",
            "pit_fitness_status": "pass",
            "rights_status": "pass",
            "budget_status": "pass",
        },
    )
    assert adopted["primary"] == "sportsdataio"
    assert adopted["path"] == "sportsdataio_primary"
    assert adopted["hard_blocker"] is False


def test_combat_registry_adoption_reachable_with_complete_quote(audit: Any) -> None:
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
    adopted = audit.apply_stats_source_decision_tree(
        balldontlie_gates=bdl_fail,
        api_sports_gates={"access_status": "not_configured"},
        sportsdataio_status="quote_pending",
        combat_registry_status="complete",
        combat_registry_gates={
            "quote_status": "complete",
            "metrics_status": "measured",
            "event_coverage_rate": 0.99,
            "bout_coverage_rate": 0.99,
            "outcome_agreement_rate": 0.995,
            "required_features_status": "pass",
            "pit_fitness_status": "pass",
            "rights_status": "pass",
            "budget_status": "pass",
        },
    )
    assert adopted["primary"] == "combat_registry"
    assert adopted["hard_blocker"] is False


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
        monthly_budget_cents=8000,
    )
    assert too_low["api_sports_probe_keep"] is False

    keep = audit.apply_stats_source_decision_tree(
        balldontlie_gates=bdl_fail,
        api_sports_gates={
            "access_status": "ok",
            "non_overlap_rate": 0.10,
            "accuracy_status": "pass",
        },
        monthly_budget_cents=8000,
    )
    assert keep["api_sports_probe_keep"] is True
    assert keep["primary"] is None
    assert keep["hard_blocker"] is True


def test_api_sports_non_overlap_synthetic_boundaries(
    audit: Any, synthetic: dict[str, Any]
) -> None:
    measured = audit.measure_api_sports_from_observations(
        provider_history_bouts=synthetic["api_sports_history_bouts"],
        dwcs_bouts=synthetic["manifest_bouts"],
    )
    # 10 history bouts, 1 overlaps DWCS fingerprint => 9/10 = 0.9
    assert measured["non_overlapping_pre_dwcs_bouts"]["numerator"] == 9
    assert measured["non_overlapping_pre_dwcs_bouts"]["denominator"] == 10
    assert measured["non_overlap_rate"] == pytest.approx(0.9)
    # Overlapping row lacks winner mapping => unknown accuracy, not disagreement.
    assert len(measured["overlapping_outcome_pairs"]) == 1
    assert measured["overlapping_outcome_pairs"][0]["status"] == "unknown"
    assert measured["accuracy_status"] == "unknown"

    low = audit.compute_api_sports_non_overlap(
        synthetic["api_sports_history_bouts"][:10],
        {
            audit.bout_fingerprint(bout)
            for bout in synthetic["api_sports_history_bouts"][:10]
            if audit.bout_fingerprint(bout)
        },
    )
    assert low["rate"] == pytest.approx(0.0)

    boundary_keep = audit.apply_stats_source_decision_tree(
        balldontlie_gates={
            "metrics_status": "unknown",
            "event_coverage_rate": None,
            "bout_coverage_rate": None,
            "outcome_agreement_rate": None,
            "required_features_status": "unknown",
            "pit_fitness_status": "unknown",
            "rights_status": "unknown",
            "budget_status": "unknown",
        },
        api_sports_gates={
            "access_status": "ok",
            "non_overlap_rate": 0.10,
            "accuracy_status": "pass",
        },
    )
    assert boundary_keep["api_sports_probe_keep"] is True


def test_api_sports_overlapping_outcome_pairs_agree_disagree_unmapped_no_overlap(
    audit: Any, synthetic: dict[str, Any]
) -> None:
    cases = synthetic["api_sports_overlap_cases"]
    manifest = synthetic["manifest_bouts"]

    agree = audit.measure_api_sports_from_observations(
        provider_history_bouts=cases["agree"]["provider_history_bouts"],
        dwcs_bouts=manifest,
    )
    assert agree["non_overlap_rate"] == pytest.approx(0.5)
    assert len(agree["overlapping_outcome_pairs"]) == 1
    assert agree["overlapping_outcome_pairs"][0]["status"] == "agree"
    assert agree["accuracy_status"] == "pass"
    assert agree["accuracy"]["outcome_agreement"]["numerator"] == 1
    assert agree["accuracy"]["outcome_agreement"]["denominator"] == 1
    assert agree["accuracy"]["outcome_agreement"]["excluded_unknown_count"] == 0

    disagree = audit.measure_api_sports_from_observations(
        provider_history_bouts=cases["disagree"]["provider_history_bouts"],
        dwcs_bouts=manifest,
    )
    assert disagree["overlapping_outcome_pairs"][0]["status"] == "disagree"
    assert disagree["accuracy_status"] == "fail"
    assert disagree["accuracy"]["outcome_agreement"]["numerator"] == 0
    assert disagree["accuracy"]["outcome_agreement"]["denominator"] == 1

    unmapped = audit.measure_api_sports_from_observations(
        provider_history_bouts=cases["unmapped"]["provider_history_bouts"],
        dwcs_bouts=manifest,
    )
    assert unmapped["overlapping_outcome_pairs"][0]["status"] == "unknown"
    assert unmapped["accuracy"]["outcome_agreement"]["excluded_unknown_count"] == 1
    assert unmapped["accuracy"]["outcome_agreement"]["denominator"] == 0
    assert unmapped["accuracy_status"] == "unknown"

    no_overlap = audit.measure_api_sports_from_observations(
        provider_history_bouts=cases["no_overlap"]["provider_history_bouts"],
        dwcs_bouts=manifest,
    )
    assert no_overlap["overlapping_outcome_pairs"] == []
    assert no_overlap["non_overlap_rate"] == pytest.approx(1.0)
    assert no_overlap["accuracy_status"] == "unknown"
    assert no_overlap["accuracy"]["outcome_agreement"]["denominator"] == 0

    # Direct builder: ambiguous fingerprint match stays unknown, not disagree.
    ambiguous = audit.build_api_sports_overlapping_outcome_pairs(
        [
            {
                "date": "2024-08-13",
                "fighter1": {"name": "Alice Alpha"},
                "fighter2": {"name": "Bob Beta"},
                "winner": {"name": "Alice Alpha"},
            }
        ],
        [
            manifest[0],
            {
                **manifest[0],
                "bout_id": "dwcs:bout:espn:dup",
                "event_night_result": {
                    "class": "decisive",
                    "winner_display_name": "Bob Beta",
                },
            },
        ],
    )
    assert len(ambiguous) == 1
    assert ambiguous[0]["status"] == "unknown"
    assert ambiguous[0]["reason"] == "ambiguous_manifest_fingerprint_match"


def test_rights_and_budget_gates_decimal_safe(audit: Any) -> None:
    rights_ok = audit.evaluate_rights_gate(
        {
            "storage_allowed": True,
            "modeling_allowed": True,
            "source": "written_terms",
            "citation": "https://balldontlie.io/terms.html",
        }
    )
    assert rights_ok["status"] == "pass"

    budget = audit.evaluate_budget_gate(
        recurring_monthly_cents=6999,
        cap_cents=10000,
        components_cents={"the_odds_api": 3000, "balldontlie_goat": 3999},
    )
    assert budget["status"] == "pass"
    assert budget["recurring_monthly"]["usd_cents"] == 6999
    assert budget["recurring_monthly"]["usd"] == "69.99"
    assert budget["components"]["balldontlie_goat"]["usd"] == "39.99"
    assert audit.usd_to_cents(30) + audit.usd_to_cents("39.99") == 6999

    over = audit.evaluate_budget_gate(
        recurring_monthly_cents=12000,
        cap_cents=10000,
        components_cents={"sportsdataio": 12000},
    )
    assert over["status"] == "fail"


def test_pit_and_required_features_unknown_not_auto_fail(audit: Any) -> None:
    pit = audit.evaluate_pit_fitness(
        {
            "latencies_ms": [10.0, 20.0, 30.0],
            "request_count": 12,
            "pre_fight_reconstruction_status": None,
            "revision_support_status": None,
        }
    )
    assert pit["status"] == "unknown"
    assert pit["latency_ms_p50"] == 20.0
    assert pit["request_cost_units"] == 12

    required = audit.evaluate_required_features(
        [{"id": 1, "fighter1": {}, "fighter2": {}, "status": "completed", "date": "2024-01-01"}],
        sample_stats=None,
    )
    assert required["status"] == "unknown"
    assert required["reason"] == "stat_samples_not_probed"


def test_difficult_identity_probe_summary_partition(audit: Any) -> None:
    summary = audit.summarize_difficult_identity_probe(
        [
            {"status": "hit"},
            {"status": "hit"},
            {"status": "miss"},
            {"status": "unknown"},
        ],
        expected_size=50,
    )
    assert summary["hit"] == 2
    assert summary["miss"] == 1
    assert summary["unknown"] == 1
    assert summary["probed"] == 4
    assert summary["expected_size"] == 50


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
    assert "raw_payload" not in json.dumps(redacted["providers"])


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
        monthly_budget_cents=3000,
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


def test_synthetic_measured_path_metric_math(
    audit: Any, synthetic: dict[str, Any]
) -> None:
    measured = audit.measure_balldontlie_from_observations(
        bouts=synthetic["manifest_bouts"],
        provider_fights=synthetic["provider_fights"],
        difficult_identity_results=synthetic["difficult_identity_results"],
        sample_stats=[
            {
                "significant_strikes_landed": 10,
                "takedowns_landed": 1,
                "control_time_seconds": 30,
            }
        ],
        latencies_ms=[11.0, 22.0, 33.0],
        request_count=9,
        pre_fight_reconstruction_status="pass",
        revision_support_status="pass",
        field_null_rates={"status": "measured", "fields": {"result_time": 0.0}},
        years_with_any_provider_dwcs_named_events=2,
    )
    assert measured["event_coverage"]["numerator"] == 2
    assert measured["event_coverage"]["denominator"] == 2
    assert measured["bout_coverage"]["numerator"] == 3
    assert measured["bout_coverage"]["denominator"] == 3
    assert measured["outcome_agreement"]["numerator"] == 3
    assert measured["outcome_agreement"]["denominator"] == 3
    assert measured["difficult_identity_coverage"]["hit"] == 1
    assert measured["difficult_identity_coverage"]["miss"] == 1
    assert measured["difficult_identity_coverage"]["unknown"] == 1
    assert measured["required_features"]["status"] == "pass"
    assert measured["pit_fitness"]["status"] == "pass"
    assert measured["year_diagnostics"]["years_with_any_provider_dwcs_named_events"] == 2
    # Year diagnostics are informational and must not drive event_coverage.
    assert "never used as event_coverage" in measured["year_diagnostics"]["note"]
    assert "years_with_any_provider_dwcs_named_events" in measured["year_diagnostics"]


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
    assert score_a["decision"]["hard_blocker"] is True
    assert score_a["decision"]["primary"] is None
    assert score_a["live_measurements_claimed"] is False
    event_metric = score_a["providers"]["balldontlie"]["metrics"]["event_coverage"]
    assert event_metric["denominator"] >= 1
    assert event_metric["numerator"] is None
    assert event_metric["status"] == "unknown"
    assert score_a["budget_context"]["recurring_monthly"]["usd"] == "69.99"

    out = tmp_path / "scorecard.json"
    audit.write_scorecard(score_a, out, redact=True)
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


def test_match_bout_to_event_by_participants_and_date(audit: Any) -> None:
    bout = {
        "bout_id": "b1",
        "occurrence_timestamp": "2024-08-13T00:00:00+00:00",
        "participants": [
            {"normalized_name": "jane doe", "display_name": "Jane Doe"},
            {"normalized_name": "john smith", "display_name": "John Smith"},
        ],
        "event_night_result": {"class": "decisive", "winner_display_name": "Jane Doe"},
    }
    fights = [
        {
            "id": 9,
            "date": "2024-08-13",
            "fighter1": {"name": "John Smith"},
            "fighter2": {"name": "Jane Doe"},
            "status": "completed",
            "winner": {"name": "Jane Doe"},
        }
    ]
    matched = audit.match_bout_to_provider_fight(bout, fights)
    assert matched is not None
    assert matched["id"] == 9


def test_committed_scorecard_sanitized_and_schema_valid(audit: Any) -> None:
    """Lock the exact DWCS-003 refresh evidence snapshot committed in this PR.

    Adoption-path decision logic is covered separately by
    ``test_decision_thresholds_balldontlie_boundary`` (synthetic gates), not by
    relaxing this artifact regression.
    """
    path = ROOT / "output" / "research" / "stats-source-scorecard.json"
    if not path.is_file():
        pytest.skip("committed scorecard not generated yet")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in audit.SCORECARD_SCHEMA_KEYS:
        assert key in payload
    blob = path.read_text(encoding="utf-8")
    assert SENTINEL_API_KEY not in blob
    assert not re.search(
        r"(?i)(api[_-]?key|authorization)\s*[\"']?\s*[:=]\s*[\"'][^\"']+", blob
    )
    for host in PROHIBITED_HOST_FRAGMENTS:
        assert f"https://{host}" not in blob
        assert f"http://{host}" not in blob
    assert payload["decision"]["prohibited_scraping_selected"] is False
    assert payload["capture_mode"] == "live"
    assert payload["live_measurements_claimed"] is False
    assert payload["budget_context"]["recurring_monthly"]["usd_cents"] == 6999

    # Exact hard-blocker evidence for this PR (not an adopted-or-blocked union).
    assert payload["decision"]["primary"] is None
    assert payload["decision"]["hard_blocker"] is True
    assert payload["decision"]["path"] == "hard_blocker"

    balldontlie = payload["providers"]["balldontlie"]
    assert balldontlie["access_status"] == "entitlement_blocked"
    assert balldontlie["metrics_status"] == "blocked"
    assert balldontlie["error"] == "fights_endpoint_entitlement_blocked"

    metrics = balldontlie["metrics"]
    event_metric = metrics["event_coverage"]
    bout_metric = metrics["bout_coverage"]
    outcome_metric = metrics["outcome_agreement"]
    assert event_metric["denominator"] == 30
    assert event_metric["numerator"] is None
    assert event_metric["rate"] is None
    assert event_metric["status"] == "unknown"
    assert event_metric["reason"] == "entitlement_blocked"
    assert bout_metric["denominator"] == 149
    assert bout_metric["numerator"] is None
    assert bout_metric["rate"] is None
    assert bout_metric["status"] == "unknown"
    assert bout_metric["reason"] == "entitlement_blocked"
    assert outcome_metric["numerator"] is None
    assert outcome_metric["rate"] is None
    assert outcome_metric["status"] == "unknown"
    assert outcome_metric["reason"] == "entitlement_blocked"
    assert metrics["required_features"]["status"] == "unknown"
    assert metrics["pit_fitness"]["status"] == "unknown"
    assert metrics["stat_coverage"]["numerator"] is None
    assert metrics["stat_coverage"]["status"] == "unknown"

    gates = payload["decision"]["gates"]["balldontlie"]
    assert gates["technical_pass"] is False
    assert gates["rights_status"] == "pass"
    assert gates["budget_status"] == "pass"
    assert gates["adopt"] is False


def test_decision_doc_exists_with_citations() -> None:
    path = ROOT / "docs" / "research" / "stats-source-decision.md"
    if not path.is_file():
        pytest.skip("decision doc not written yet")
    text = path.read_text(encoding="utf-8")
    assert "DWCS-003" in text
    assert "balldontlie.io/terms" in text
    assert "hard" in text.lower() and "blocker" in text.lower()
    assert "Phase 0" in text
    assert "executable measurement path" in text.lower() or "measurement path" in text
    assert "Tapology" in text or "tapology" in text
    assert "handoff" in text.lower()
    assert "not measured provider coverage" in text.lower() or (
        "not invented" in text.lower()
    )


def test_cli_help_mentions_manifest_and_out(audit: Any) -> None:
    parser = audit.build_arg_parser()
    help_text = parser.format_help()
    assert "--manifest" in help_text
    assert "--out" in help_text
    assert "--capture-time" in help_text
    assert "--redact" in help_text
