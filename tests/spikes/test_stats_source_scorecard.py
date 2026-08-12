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
    # Without independently established auth, entitlement must not be inferred
    # from generic body keywords such as "access" / "tier".
    assert (
        audit.classify_provider_access(
            api_key="k", http_status=401, body={"error": "tier does not have access"}
        )
        == "auth_failed"
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


def test_classify_access_requires_prior_auth_for_entitlement(audit: Any) -> None:
    """Invalid/generic first-call denial is auth_failed, never entitlement."""
    sentinel = "SENTINEL_SDIO_KEY_DO_NOT_LEAK"
    assert (
        audit.classify_provider_access(
            api_key=sentinel,
            http_status=401,
            body={"Message": "Access denied due to invalid subscription key"},
            authenticated_ok_prior=False,
        )
        == "auth_failed"
    )
    assert (
        audit.classify_provider_access(
            api_key=sentinel,
            http_status=403,
            body={"error": "forbidden access"},
            authenticated_ok_prior=False,
        )
        == "auth_failed"
    )
    # Valid auth established, then historical/feed denial => entitlement_blocked.
    assert (
        audit.classify_provider_access(
            api_key=sentinel,
            http_status=401,
            body={
                "Code": 401,
                "Description": "Subscription does not include this historical feed",
            },
            authenticated_ok_prior=True,
        )
        == "entitlement_blocked"
    )
    # Ambiguous post-auth denial fails closed (unknown), never silent entitlement pass.
    ambiguous = audit.classify_provider_access(
        api_key=sentinel,
        http_status=403,
        body={"note": "temporary denial please retry later"},
        authenticated_ok_prior=True,
    )
    assert ambiguous in {"unknown", "auth_failed"}
    assert ambiguous != "entitlement_blocked"
    assert ambiguous != "ok"
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
            "publication_timestamp_status": None,
        }
    )
    assert pit["status"] == "unknown"
    assert pit["latency_ms_p50"] == 20.0
    assert pit["request_cost_units"] == 12
    assert pit["publication_timestamps"] == "unknown"
    assert "publication_timestamps_unproven" in str(pit.get("reason") or "")

    required = audit.evaluate_required_features(
        [{"id": 1, "fighter1": {}, "fighter2": {}, "status": "completed", "date": "2024-01-01"}],
        bout_stat_observations=None,
    )
    assert required["status"] == "unknown"
    assert required["reason"] == "stat_samples_not_probed"


def test_pit_pass_requires_publication_timestamp_proof(audit: Any) -> None:
    """Missing/unknown source-update timestamps can never produce PIT pass."""
    almost = audit.evaluate_pit_fitness(
        {
            "pre_fight_reconstruction_status": "pass",
            "revision_support_status": "pass",
            "publication_timestamp_status": None,
        }
    )
    assert almost["status"] == "unknown"
    assert almost["publication_timestamps"] == "unknown"
    assert almost["status"] != "pass"

    failed_ts = audit.evaluate_pit_fitness(
        {
            "pre_fight_reconstruction_status": "pass",
            "revision_support_status": "pass",
            "publication_timestamp_status": "fail",
        }
    )
    assert failed_ts["status"] == "fail"
    assert failed_ts["publication_timestamps"] == "fail"

    full = audit.evaluate_pit_fitness(
        {
            "pre_fight_reconstruction_status": "pass",
            "revision_support_status": "pass",
            "publication_timestamp_status": "pass",
            "latencies_ms": [5.0],
            "request_count": 1,
        }
    )
    assert full["status"] == "pass"
    assert full["publication_timestamps"] == "pass"


def test_sampled_stats_cannot_produce_global_required_features_pass(audit: Any) -> None:
    """Regression: provider_fights[:3] / 6 rows must never yield universe pass."""
    matched = [
        {
            "id": i,
            "fighter1": {"name": "A"},
            "fighter2": {"name": "B"},
            "status": "completed",
            "date": "2024-08-13",
        }
        for i in range(1, 150)
    ]
    # Only three fights probed — mirrors the defective live sample size.
    tiny_obs = [
        {
            "fight_id": i,
            "status": "present",
            "fields": {
                "significant_strikes_landed": True,
                "takedowns_landed": True,
                "control_time_seconds": True,
            },
        }
        for i in (1, 2, 3)
    ]
    required = audit.evaluate_required_features(
        matched,
        bout_stat_observations=tiny_obs,
    )
    assert required["status"] != "pass"
    assert required["status"] == "unknown"
    assert required["reason"] == "stat_probe_incomplete"
    assert required["stat_fields"]["denominator"] == 149
    assert required["stat_fields"]["probed"] == 3
    for field in audit.REQUIRED_STAT_FIELDS:
        metric = required["stat_fields"]["fields"][field]
        assert metric["denominator"] == 149
        assert metric["numerator"] == 3
        assert metric["rate"] == pytest.approx(3 / 149)
    # Fight fields are separate and may still clear on the matched universe.
    assert required["fight_fields"]["denominator"] == 149
    assert required["fight_fields"]["status"] == "pass"


def test_required_features_pass_needs_full_universe_stat_coverage(audit: Any) -> None:
    matched = [
        {
            "id": i,
            "fighter1": {"name": "A"},
            "fighter2": {"name": "B"},
            "status": "completed",
            "date": "2024-08-13",
        }
        for i in range(1, 4)
    ]
    complete = [
        {
            "fight_id": i,
            "status": "present",
            "fields": {
                "significant_strikes_landed": True,
                "takedowns_landed": True,
                "control_time_seconds": True,
            },
        }
        for i in (1, 2, 3)
    ]
    required = audit.evaluate_required_features(
        matched,
        bout_stat_observations=complete,
    )
    assert required["status"] == "pass"
    assert required["coverage_min"] == audit.REQUIRED_FEATURE_COVERAGE_MIN
    for field in audit.REQUIRED_STAT_FIELDS:
        assert required["stat_fields"]["fields"][field]["numerator"] == 3
        assert required["stat_fields"]["fields"][field]["denominator"] == 3
        assert required["stat_fields"]["fields"][field]["rate"] == pytest.approx(1.0)

    incomplete_coverage = [
        {
            "fight_id": 1,
            "status": "present",
            "fields": {
                "significant_strikes_landed": True,
                "takedowns_landed": True,
                "control_time_seconds": True,
            },
        },
        {
            "fight_id": 2,
            "status": "absent",
            "fields": {
                "significant_strikes_landed": False,
                "takedowns_landed": False,
                "control_time_seconds": False,
            },
        },
        {
            "fight_id": 3,
            "status": "present",
            "fields": {
                "significant_strikes_landed": True,
                "takedowns_landed": False,
                "control_time_seconds": True,
            },
        },
    ]
    failed = audit.evaluate_required_features(
        matched,
        bout_stat_observations=incomplete_coverage,
    )
    assert failed["status"] == "fail"
    assert failed["stat_fields"]["fields"]["takedowns_landed"]["numerator"] == 1
    assert failed["stat_fields"]["fields"]["takedowns_landed"]["denominator"] == 3


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
    # Full matched-universe bout_stat_observations (3/3) required for a pass.
    bout_stat_observations = []
    for fight in synthetic["provider_fights"]:
        bout_stat_observations.append(
            {
                "fight_id": fight["id"],
                "status": "present",
                "fields": {
                    "significant_strikes_landed": True,
                    "takedowns_landed": True,
                    "control_time_seconds": True,
                },
            }
        )
    measured = audit.measure_balldontlie_from_observations(
        bouts=synthetic["manifest_bouts"],
        provider_fights=synthetic["provider_fights"],
        difficult_identity_results=synthetic["difficult_identity_results"],
        bout_stat_observations=bout_stat_observations,
        latencies_ms=[11.0, 22.0, 33.0],
        request_count=9,
        pre_fight_reconstruction_status="pass",
        revision_support_status="pass",
        publication_timestamp_status="pass",
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
    assert measured["required_features"]["stat_fields"]["probed"] == 3
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
    """Lock SportsDataIO entitlement refresh + preserved BALLDONTLIE history.

    BALLDONTLIE remains measured (coverage/outcome pass; required_features fail on
    control_time_seconds; PIT unknown). SportsDataIO auth succeeds but 2023–2024
    schedule seasons are entitlement-blocked, so full-universe technical gates stay
    unknown/blocked — not scored as zero coverage. Rights/budget remain
    quote-pending/unknown. Primary stays null.
    """
    path = ROOT / "output" / "research" / "stats-source-scorecard.json"
    if not path.is_file():
        pytest.skip("committed scorecard not generated yet")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in audit.SCORECARD_SCHEMA_KEYS:
        assert key in payload
    blob = path.read_text(encoding="utf-8")
    assert SENTINEL_API_KEY not in blob
    assert "Ocp-Apim-Subscription-Key" not in blob
    assert not re.search(
        r"(?i)(api[_-]?key|authorization)\s*[\"']?\s*[:=]\s*[\"'][^\"']+", blob
    )
    for host in PROHIBITED_HOST_FRAGMENTS:
        assert f"https://{host}" not in blob
        assert f"http://{host}" not in blob
    assert payload["decision"]["prohibited_scraping_selected"] is False
    assert payload["capture_mode"] == "live"
    assert payload["live_measurements_claimed"] is True
    assert payload["captured_at"] == "2026-08-12T14:30:00+00:00"
    assert payload["budget_context"]["recurring_monthly"]["usd_cents"] == 6999

    # Exact hard-blocker evidence for this PR (not an adopted-or-blocked union).
    assert payload["decision"]["primary"] is None
    assert payload["decision"]["hard_blocker"] is True
    assert payload["decision"]["path"] == "hard_blocker"

    balldontlie = payload["providers"]["balldontlie"]
    assert balldontlie["access_status"] == "ok"
    assert balldontlie["metrics_status"] == "measured"
    assert balldontlie["error"] is None
    assert balldontlie["rate_limit_limit_header"] == "600"

    metrics = balldontlie["metrics"]
    event_metric = metrics["event_coverage"]
    bout_metric = metrics["bout_coverage"]
    outcome_metric = metrics["outcome_agreement"]
    assert event_metric["denominator"] == 30
    assert event_metric["numerator"] == 30
    assert event_metric["rate"] == pytest.approx(1.0)
    assert event_metric["status"] == "measured"
    assert bout_metric["denominator"] == 149
    assert bout_metric["numerator"] == 149
    assert bout_metric["rate"] == pytest.approx(1.0)
    assert bout_metric["status"] == "measured"
    assert outcome_metric["denominator"] == 149
    assert outcome_metric["numerator"] == 149
    assert outcome_metric["rate"] == pytest.approx(1.0)
    assert outcome_metric["status"] == "measured"

    required = metrics["required_features"]
    assert required["status"] == "fail"
    assert required["reason"] == "required_feature_coverage_below_min"
    assert required["coverage_min"] == audit.REQUIRED_FEATURE_COVERAGE_MIN
    assert required["fight_fields"]["status"] == "pass"
    assert required["fight_fields"]["denominator"] == 149
    assert required["stat_fields"]["status"] == "fail"
    assert required["stat_fields"]["denominator"] == 149
    assert required["stat_fields"]["probed"] == 149
    assert required["stat_fields"]["fields"]["significant_strikes_landed"] == {
        "denominator": 149,
        "numerator": 149,
        "rate": 1.0,
        "status": "measured",
    }
    assert required["stat_fields"]["fields"]["takedowns_landed"] == {
        "denominator": 149,
        "numerator": 149,
        "rate": 1.0,
        "status": "measured",
    }
    control = required["stat_fields"]["fields"]["control_time_seconds"]
    assert control["denominator"] == 149
    assert control["numerator"] == 98
    assert control["rate"] == pytest.approx(98 / 149)
    assert control["status"] == "measured"
    assert control["rate"] < audit.REQUIRED_FEATURE_COVERAGE_MIN

    assert metrics["pit_fitness"]["status"] == "unknown"
    assert "pre_fight_reconstruction_unproven" in str(
        metrics["pit_fitness"].get("reason") or ""
    )
    assert "revision_support_unproven" in str(
        metrics["pit_fitness"].get("reason") or ""
    )
    assert "publication_timestamps_unproven" in str(
        metrics["pit_fitness"].get("reason") or ""
    )
    assert metrics["pit_fitness"]["publication_timestamps"] == "unknown"
    identity = metrics["difficult_identity_coverage"]
    assert identity["status"] == "measured"
    assert identity["hit"] == 50
    assert identity["miss"] == 0
    assert identity["unknown"] == 0

    gates = payload["decision"]["gates"]["balldontlie"]
    assert gates["event_coverage_rate"] == pytest.approx(1.0)
    assert gates["bout_coverage_rate"] == pytest.approx(1.0)
    assert gates["outcome_agreement_rate"] == pytest.approx(1.0)
    assert gates["required_features_status"] == "fail"
    assert gates["pit_fitness_status"] == "unknown"
    assert gates["technical_pass"] is False
    assert gates["rights_status"] == "pass"
    assert gates["budget_status"] == "pass"
    assert gates["adopt"] is False

    sdio = payload["providers"]["sportsdataio"]
    assert sdio["access_status"] == "entitlement_blocked"
    assert sdio["error"] == "historical_season_entitlement_blocked"
    assert sdio["metrics_status"] == "blocked"
    classification = sdio["access_classification"]
    assert classification["auth"] == "ok"
    assert classification["subscription_entitlement"] == "historical_seasons_blocked"
    assert classification["quota"] == "ok"
    assert classification["schema"] == "ok_on_accessible_endpoints"
    assert classification["rights"] == "unknown"
    assert classification["quote"] == "quote_pending"
    assert sdio["season_access"] == {
        "2023": "entitlement_blocked",
        "2024": "entitlement_blocked",
        "2025": "ok",
    }
    assert sdio["probe_notes"]["auth_mode"] == "subscription_key_header_only"
    diag = sdio["accessible_season_diagnostics"]
    assert diag["seasons_ok"] == [2025]
    assert diag["seasons_entitlement_blocked"] == [2023, 2024]
    assert diag["full_event_denominator"] == 30
    assert diag["full_bout_denominator"] == 149
    assert diag["matched_bout_count"] == 49
    assert diag["global_feature_pass_allowed"] is False
    assert sdio["metrics"]["event_coverage"]["numerator"] is None
    assert sdio["metrics"]["bout_coverage"]["numerator"] is None
    assert sdio["metrics"]["required_features"]["status"] == "unknown"
    assert sdio["metrics"]["pit_fitness"]["status"] == "unknown"
    assert sdio["metrics"]["pit_fitness"]["publication_timestamps"] == "unknown"
    assert "publication_timestamps_unproven" in str(
        sdio["metrics"]["pit_fitness"].get("reason") or ""
    )
    assert sdio["metrics"]["difficult_identity_coverage"]["hit"] == 50
    assert sdio["rights"]["status"] == "unknown"
    assert sdio["documented_public"]["status"] == "quote_pending"
    sdio_gates = payload["decision"]["gates"]["sportsdataio"]
    assert sdio_gates["adopt"] is False
    assert sdio_gates["quote_status"] == "quote_pending"

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
    assert "--sportsdataio-key-env" in help_text
    assert "--prior-scorecard" in help_text


def test_normalize_sportsdataio_fight_maps_common_contract(audit: Any) -> None:
    raw = {
        "FightId": 9904,
        "Status": "Final",
        "ResultType": "KO/TKO",
        "ResultRound": 2,
        "ResultClock": 45,
        "Rounds": 3,
        "WinnerId": 11,
        "Fighters": [
            {
                "FighterId": 11,
                "FirstName": "Alice",
                "LastName": "Alpha",
                "Winner": True,
                "PreFightWins": 5,
                "PreFightLosses": 1,
                "PreFightDraws": 0,
            },
            {
                "FighterId": 12,
                "FirstName": "Bob",
                "LastName": "Beta",
                "Winner": False,
                "PreFightWins": 4,
                "PreFightLosses": 2,
                "PreFightDraws": 0,
            },
        ],
    }
    normalized = audit.normalize_sportsdataio_fight(raw, event_date="2025-09-09")
    assert normalized["id"] == 9904
    assert normalized["date"] == "2025-09-09"
    assert normalized["status"] == "Final"
    assert normalized["result_method"] == "KO/TKO"
    assert normalized["result_round"] == 2
    assert normalized["result_clock"] == 45
    assert normalized["rounds"] == 3
    assert normalized["fighter1"]["name"] == "Alice Alpha"
    assert normalized["fighter2"]["name"] == "Bob Beta"
    assert normalized["winner_id"] == 11
    assert normalized["pre_fight_records_present"] is True


def test_summarize_sportsdataio_fight_stats_aliases(audit: Any) -> None:
    rows = [
        {
            "FighterId": 11,
            "SigStrikesLanded": 12.0,
            "TakedownsLanded": 1.0,
            "TimeInControl": 30.0,
        },
        {
            "FighterId": 12,
            "SigStrikesLanded": 8.0,
            "TakedownsLanded": 0.0,
            "TimeInControl": 10.0,
        },
    ]
    fields = audit.summarize_sportsdataio_fight_stats(rows)
    assert fields == {
        "significant_strikes_landed": True,
        "takedowns_landed": True,
        "control_time_seconds": True,
    }


def test_sportsdataio_401_after_auth_is_entitlement_blocked(audit: Any) -> None:
    access = audit.classify_provider_access(
        api_key="SENTINEL_SDIO_KEY",
        http_status=401,
        body={"Code": 401, "Description": "Subscription does not include this feed"},
        authenticated_ok_prior=True,
    )
    assert access == "entitlement_blocked"


def test_sportsdataio_request_uses_header_auth_only(audit: Any) -> None:
    """Official least-exposing auth: header only; never put key in URL/query."""
    sentinel = "SENTINEL_SDIO_KEY_DO_NOT_LEAK"
    request = audit.build_sportsdataio_get_request(
        path="/scores/json/Leagues",
        api_key=sentinel,
    )
    assert request["method"] == "GET"
    assert request["url"] == "https://api.sportsdata.io/v3/mma/scores/json/Leagues"
    assert "key=" not in request["url"].lower()
    assert request.get("params") in ({}, None)
    assert request["headers"]["Ocp-Apim-Subscription-Key"] == sentinel
    assert sentinel not in json.dumps(
        {k: v for k, v in request.items() if k != "headers"}
    )

    redacted = audit.redact_sportsdataio_request(request)
    blob = json.dumps(redacted)
    assert sentinel not in blob
    assert "key=" not in blob.lower()
    assert redacted["headers"]["Ocp-Apim-Subscription-Key"] == "[REDACTED]"
    # Error/log shapes must also redact.
    leaky = {
        "error": f"request failed for key={sentinel}",
        "request_url": f"{request['url']}?key={sentinel}",
    }
    cleaned = audit.redact_scorecard(leaky)
    cleaned_blob = json.dumps(cleaned)
    assert sentinel not in cleaned_blob
    assert "key=[REDACTED]" in cleaned_blob or "[REDACTED]" in cleaned_blob


def test_sportsdataio_partial_season_cannot_pass_global_features(audit: Any) -> None:
    """Accessible-season sample must never clear the full-universe feature gate."""
    result = audit.evaluate_sportsdataio_universe_gates(
        audit_season_access={
            2023: "entitlement_blocked",
            2024: "entitlement_blocked",
            2025: "ok",
        },
        accessible_matched_fights=[{"id": 1}, {"id": 2}],
        accessible_stat_observations=[
            {
                "fight_id": "1",
                "status": "present",
                "fields": {
                    "significant_strikes_landed": True,
                    "takedowns_landed": True,
                    "control_time_seconds": True,
                },
            },
            {
                "fight_id": "2",
                "status": "present",
                "fields": {
                    "significant_strikes_landed": True,
                    "takedowns_landed": True,
                    "control_time_seconds": True,
                },
            },
        ],
        full_event_denominator=30,
        full_bout_denominator=149,
    )
    assert result["access_status"] == "entitlement_blocked"
    assert result["metrics_status"] == "blocked"
    assert result.get("full_universe_measurable") is False
    assert result["required_features"]["status"] in {"unknown", "blocked"}
    assert result["required_features"]["status"] != "pass"
    assert result["event_coverage"]["status"] == "unknown"
    assert result["event_coverage"]["numerator"] is None
    assert result["bout_coverage"]["numerator"] is None
    assert "entitlement" in str(result["required_features"].get("reason") or "").lower() or (
        "entitlement" in str(result.get("error") or "").lower()
    )
    diag = result["accessible_season_diagnostics"]
    assert diag["seasons_ok"] == [2025]
    assert diag["seasons_entitlement_blocked"] == [2023, 2024]
    assert diag["matched_bout_count"] == 2
    assert diag["global_feature_pass_allowed"] is False


def test_sportsdataio_universe_gates_fail_closed_unless_all_seasons_ok(
    audit: Any,
) -> None:
    """Partial/unknown/missing season scope must never set full_universe_measurable."""
    required = tuple(audit.SPORTSDATAIO_AUDIT_SEASONS)
    assert required == (2023, 2024, 2025)
    # Exhaustive contract shared with the live probe.
    assert set(audit.SPORTSDATAIO_SEASON_ACCESS_STATUSES) == {
        "ok",
        "auth_failed",
        "entitlement_blocked",
        "quota_exceeded",
        "request_failed",
        "not_configured",
        "unknown",
    }

    unknown_partial = audit.evaluate_sportsdataio_universe_gates(
        audit_season_access={2023: "unknown", 2024: "ok", 2025: "ok"},
        accessible_matched_fights=[{"id": 1}],
        accessible_stat_observations=None,
        full_event_denominator=30,
        full_bout_denominator=149,
    )
    assert unknown_partial.get("full_universe_measurable") is False
    assert unknown_partial["metrics_status"] == "blocked"
    assert unknown_partial["access_status"] == "unknown"
    assert unknown_partial["event_coverage"]["numerator"] is None
    assert unknown_partial["bout_coverage"]["numerator"] is None
    assert "unknown" in str(unknown_partial.get("error") or "").lower() or (
        "incomplete" in str(unknown_partial.get("error") or "").lower()
    )

    missing_season = audit.evaluate_sportsdataio_universe_gates(
        audit_season_access={2024: "ok", 2025: "ok"},
        accessible_matched_fights=[],
        accessible_stat_observations=None,
        full_event_denominator=30,
        full_bout_denominator=149,
    )
    assert missing_season.get("full_universe_measurable") is False
    assert missing_season["metrics_status"] == "blocked"
    assert missing_season["access_status"] == "unknown"
    assert missing_season["event_coverage"]["numerator"] is None
    assert "missing" in str(missing_season.get("error") or "").lower()
    assert 2023 in (
        missing_season.get("accessible_season_diagnostics", {}).get("seasons_missing")
        or []
    )

    unrecognized = audit.evaluate_sportsdataio_universe_gates(
        audit_season_access={
            2023: "weird_future_status",
            2024: "ok",
            2025: "ok",
        },
        accessible_matched_fights=[],
        accessible_stat_observations=None,
        full_event_denominator=30,
        full_bout_denominator=149,
    )
    assert unrecognized.get("full_universe_measurable") is False
    assert unrecognized["metrics_status"] == "blocked"
    assert unrecognized["access_status"] == "unknown"
    assert unrecognized["event_coverage"]["numerator"] is None
    assert "unrecognized" in str(unrecognized.get("error") or "").lower()

    all_ok = audit.evaluate_sportsdataio_universe_gates(
        audit_season_access={2023: "ok", 2024: "ok", 2025: "ok"},
        accessible_matched_fights=[{"id": 1}],
        accessible_stat_observations=None,
        full_event_denominator=30,
        full_bout_denominator=149,
    )
    assert all_ok.get("full_universe_measurable") is True
    assert all_ok["access_status"] == "ok"
    assert all_ok["metrics_status"] == "pending_full_measurement"
    assert all_ok.get("error") is None


def test_sportsdataio_key_absent_is_not_zero_coverage(
    audit: Any, sample_bouts: list[dict[str, Any]]
) -> None:
    scorecard = audit.build_scorecard(
        bouts=sample_bouts,
        captured_at="2026-08-12T14:30:00+00:00",
        capture_mode="fixtures",
        balldontlie_key=None,
        api_sports_key=None,
        sportsdataio_key=None,
        vendor_notes={},
        live_observations=None,
    )
    sdio = scorecard["providers"]["sportsdataio"]
    assert sdio["access_status"] == "not_configured"
    metrics = sdio.get("metrics") or {}
    event = metrics.get("event_coverage") or {}
    assert event.get("numerator") is None
    assert event.get("status") == "unknown"
    assert scorecard["decision"]["primary"] is None


def test_sportsdataio_complete_quote_still_needs_technical_pass(audit: Any) -> None:
    bdl_fail = {
        "event_coverage_rate": 1.0,
        "bout_coverage_rate": 1.0,
        "outcome_agreement_rate": 1.0,
        "required_features_status": "fail",
        "pit_fitness_status": "unknown",
        "rights_status": "pass",
        "budget_status": "pass",
        "metrics_status": "measured",
    }
    blocked = audit.apply_stats_source_decision_tree(
        balldontlie_gates=bdl_fail,
        api_sports_gates={"access_status": "not_configured"},
        sportsdataio_status="complete",
        sportsdataio_gates={
            "quote_status": "complete",
            "metrics_status": "blocked",
            "event_coverage_rate": None,
            "bout_coverage_rate": None,
            "outcome_agreement_rate": None,
            "required_features_status": "unknown",
            "pit_fitness_status": "unknown",
            "rights_status": "unknown",
            "budget_status": "unknown",
        },
    )
    assert blocked["primary"] is None
    assert blocked["hard_blocker"] is True


def test_preserve_prior_balldontlie_when_key_absent(
    audit: Any, sample_bouts: list[dict[str, Any]]
) -> None:
    prior_live = {
        "balldontlie": {
            "access_status": "ok",
            "error": None,
            "event_coverage": {
                "numerator": 30,
                "denominator": 30,
                "rate": 1.0,
                "status": "measured",
                "reason": None,
            },
            "bout_coverage": {
                "numerator": 149,
                "denominator": 149,
                "rate": 1.0,
                "status": "measured",
                "reason": None,
            },
            "outcome_agreement": {
                "numerator": 149,
                "denominator": 149,
                "rate": 1.0,
                "status": "measured",
                "reason": None,
                "excluded_unknown_count": 0,
                "denominator_policy": "comparable_mapped_pairs_only",
            },
            "difficult_identity_coverage": {
                "status": "measured",
                "expected_size": 50,
                "probed": 50,
                "hit": 50,
                "miss": 0,
                "unknown": 0,
                "hit_rate": 1.0,
                "reason": None,
                "denominator_policy": "hit_miss_unknown_partition_of_probed_sample",
            },
            "profile_coverage": {
                "numerator": 50,
                "denominator": 50,
                "rate": 1.0,
                "status": "measured",
                "reason": None,
            },
            "stat_coverage": {
                "numerator": 149,
                "denominator": 149,
                "rate": 1.0,
                "status": "measured",
                "reason": None,
            },
            "required_features": {
                "status": "fail",
                "reason": "required_feature_coverage_below_min",
                "coverage_min": 0.98,
                "fight_fields": {"status": "pass", "denominator": 149, "fields": {}},
                "stat_fields": {
                    "status": "fail",
                    "denominator": 149,
                    "probed": 149,
                    "fields": {},
                },
                "missing_fight_fields": [],
                "missing_stat_fields": ["control_time_seconds"],
            },
            "pit_fitness": {
                "status": "unknown",
                "reason": "pre_fight_reconstruction_unproven,revision_support_unproven",
                "pre_fight_reconstruction": "unknown",
                "revision_support": "unknown",
                "latency_ms_p50": 1.0,
                "request_cost_units": 1,
                "field_null_rates": {"status": "unknown", "reason": "x", "fields": {}},
            },
            "year_diagnostics": {
                "years_with_any_provider_dwcs_named_events": 3,
                "manifest_calendar_years": [2023, 2024, 2025],
                "note": "Year diagnostics are informational only and are never used as event_coverage numerator/denominator.",
            },
            "rate_limit_limit_header": "600",
            "preserved_from_prior_scorecard": True,
        }
    }
    scorecard = audit.build_scorecard(
        bouts=sample_bouts,
        captured_at="2026-08-12T14:30:00+00:00",
        capture_mode="live",
        balldontlie_key=None,
        api_sports_key=None,
        sportsdataio_key=None,
        vendor_notes={},
        live_observations=prior_live,
    )
    bdl = scorecard["providers"]["balldontlie"]
    assert bdl["access_status"] == "ok"
    assert bdl["metrics_status"] == "measured"
    assert bdl["metrics"]["event_coverage"]["numerator"] == 30
    assert bdl["metrics"]["required_features"]["status"] == "fail"
    assert scorecard["decision"]["primary"] is None
