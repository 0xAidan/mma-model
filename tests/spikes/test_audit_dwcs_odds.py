"""Tests for DWCS-000 live odds audit spike (no live network)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "spikes" / "audit_dwcs_odds.py"
SENTINEL_API_KEY = "SENTINEL_ODDS_API_KEY_DO_NOT_LEAK"


def _load_audit_module() -> Any:
    spec = importlib.util.spec_from_file_location("audit_dwcs_odds", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit() -> Any:
    if not SCRIPT_PATH.is_file():
        pytest.fail(f"missing audit script: {SCRIPT_PATH}")
    return _load_audit_module()


def test_normalize_fighter_name_strips_noise(audit: Any) -> None:
    assert audit.normalize_fighter_name("  Jon  Jones ") == "jon jones"
    assert audit.normalize_fighter_name("José Aldo") == "jose aldo"


def test_match_bout_to_event_by_participants_and_time(audit: Any) -> None:
    bout = {
        "bout_id": "b1",
        "fighter_a": "Jane Doe",
        "fighter_b": "John Smith",
        "scheduled_start": "2026-08-12T00:00:00Z",
    }
    events = [
        {
            "id": "evt-u",
            "home_team": "Someone Else",
            "away_team": "Another Person",
            "commence_time": "2026-08-12T00:00:00Z",
        },
        {
            "id": "evt-match",
            "home_team": "John Smith",
            "away_team": "Jane Doe",
            "commence_time": "2026-08-12T00:05:00Z",
        },
    ]
    matched = audit.match_bout_to_event(bout, events, max_delta_minutes=30)
    assert matched is not None
    assert matched["id"] == "evt-match"


def test_classify_official_bouts_present_absent_unresolved(audit: Any) -> None:
    official = [
        {
            "bout_id": "present-1",
            "fighter_a": "A One",
            "fighter_b": "B One",
            "scheduled_start": "2026-08-12T00:00:00Z",
        },
        {
            "bout_id": "absent-1",
            "fighter_a": "Missing Fighter",
            "fighter_b": "Also Missing",
            "scheduled_start": "2026-08-12T00:00:00Z",
        },
        {
            "bout_id": "unresolved-1",
            "fighter_a": "Ambiguous A",
            "fighter_b": "Ambiguous B",
            "scheduled_start": "2026-08-12T00:00:00Z",
        },
    ]
    events = [
        {
            "id": "e1",
            "home_team": "A One",
            "away_team": "B One",
            "commence_time": "2026-08-12T00:00:00Z",
        },
        {
            "id": "e2a",
            "home_team": "Ambiguous A",
            "away_team": "Ambiguous B",
            "commence_time": "2026-08-12T00:00:00Z",
        },
        {
            "id": "e2b",
            "home_team": "Ambiguous B",
            "away_team": "Ambiguous A",
            "commence_time": "2026-08-12T00:10:00Z",
        },
    ]
    classified = audit.classify_official_bouts(official, events, max_delta_minutes=30)
    by_id = {row["bout_id"]: row["status"] for row in classified}
    assert by_id["present-1"] == "present"
    assert by_id["absent-1"] == "absent"
    assert by_id["unresolved-1"] == "unresolved"


def test_bookmaker_market_presence_separates_absent_from_request_failed(audit: Any) -> None:
    discovery = {
        "status": "ok",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [{"key": "h2h", "last_update": "2026-08-11T12:00:00Z"}],
            }
        ],
    }
    failed = {"status": "request_failed", "error": "timeout", "bookmakers": []}
    matrix_ok = audit.bookmaker_market_presence(
        discovery,
        bookmaker_keys=["draftkings", "bet365"],
        market_keys=["h2h", "totals"],
    )
    matrix_fail = audit.bookmaker_market_presence(
        failed,
        bookmaker_keys=["bet365"],
        market_keys=["h2h"],
    )
    assert matrix_ok["draftkings"]["h2h"] == "present"
    assert matrix_ok["draftkings"]["totals"] == "absent"
    assert matrix_ok["bet365"]["h2h"] == "absent"
    assert matrix_fail["bet365"]["h2h"] == "request_failed"


def test_redact_summary_removes_secrets_and_prices(audit: Any) -> None:
    raw = {
        "provider": "the_odds_api",
        "api_key": "secret-live-key",
        "request_url": "https://api.the-odds-api.com/v4/sports/mma/odds?apiKey=secret-live-key",
        "authorization": "Bearer abc123",
        "events": [
            {
                "id": "e1",
                "home_team": "A",
                "away_team": "B",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "A", "price": -150},
                                    {"name": "B", "price": 130},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "manual_bet365_samples": [
            {"bout_id": "b1", "displayed_price": -120, "region": "uk"}
        ],
    }
    redacted = audit.redact_summary(raw)
    blob = json.dumps(redacted)
    assert "secret-live-key" not in blob
    assert "abc123" not in blob
    assert "api_key" not in redacted
    assert "authorization" not in redacted
    outcomes = redacted["events"][0]["bookmakers"][0]["markets"][0]["outcomes"]
    assert all("price" not in outcome for outcome in outcomes)
    assert "displayed_price" not in redacted["manual_bet365_samples"][0]
    assert "apiKey=[REDACTED]" in blob or "apiKey=%5BREDACTED%5D" in blob
    assert "secret-live-key" not in redacted["request_url"]


def test_pass_fail_matrix_and_decision_gate(audit: Any) -> None:
    evidence = {
        "providers": {
            "the_odds_api": {
                "dwcs_events_found": 1,
                "bet365_present_on_dwcs": False,
                "markets_observed": ["h2h"],
                "dwcs_market_discovery_status": "ok",
                "quota": {
                    "x-requests-remaining": "480",
                    "x-requests-used": "20",
                    "x-requests-last": "1",
                },
                "timestamps_documented": True,
                "lock_events_supported": None,
                "historical_replay_supported": None,
                "rights_notes": None,
                "monthly_quote_usd": None,
            },
            "opticodds": {
                "status": "not_configured",
                "bet365_present_on_dwcs": None,
            },
        },
        "manual_bet365_samples": [],
        "bout_classifications": [
            {"bout_id": "1", "status": "present"},
            {"bout_id": "2", "status": "absent"},
        ],
    }
    matrix = audit.build_pass_fail_matrix(evidence)
    for key in (
        "moneyline",
        "totals",
        "method",
        "round",
        "lock_events",
        "historical_replay",
        "rights",
        "monthly_quote",
    ):
        assert key in matrix
        assert matrix[key]["status"] in {"pass", "fail", "blocked", "unknown"}
        assert "evidence" in matrix[key]

    assert matrix["moneyline"]["status"] == "pass"
    assert matrix["lock_events"]["status"] == "unknown"
    assert matrix["historical_replay"]["status"] == "unknown"
    assert matrix["rights"]["status"] == "unknown"
    assert matrix["monthly_quote"]["status"] == "unknown"

    decision = audit.decide_provider_path(evidence, matrix)
    assert decision["bet365_dwcs_status"] in {
        "scoped_absent",
        "present",
        "unresolved",
        "request_failed",
    }
    assert decision["bet365_dwcs_status"] != "absent"
    assert decision["path"] in {
        "licensed_bet365_primary",
        "the_odds_api_reference_fallback",
        "hard_blocker",
    }
    # Without Bet365 evidence on DWCS, must not claim licensed Bet365 primary.
    assert decision["path"] != "licensed_bet365_primary"
    assert decision["bet365_dwcs_status"] != "present"


def test_moneyline_requires_h2h_on_reconciled_dwcs(audit: Any) -> None:
    """Regression: reconciled DWCS events alone must not pass moneyline."""
    reconciled_only = {
        "providers": {
            "the_odds_api": {
                "dwcs_events_found": 3,
                "markets_observed": [],
                "dwcs_market_discovery_status": "ok",
                "bet365_present_on_dwcs": None,
            }
        },
        "manual_bet365_samples": [],
        "bout_classifications": [{"bout_id": "1", "status": "present"}],
    }
    matrix = audit.build_pass_fail_matrix(reconciled_only)
    assert matrix["moneyline"]["status"] != "pass"
    assert matrix["moneyline"]["status"] == "fail"

    with_h2h = {
        "providers": {
            "the_odds_api": {
                "dwcs_events_found": 1,
                "markets_observed": ["h2h"],
                "dwcs_market_discovery_status": "ok",
                "bet365_present_on_dwcs": False,
            }
        },
        "manual_bet365_samples": [],
        "bout_classifications": [{"bout_id": "1", "status": "present"}],
    }
    assert audit.build_pass_fail_matrix(with_h2h)["moneyline"]["status"] == "pass"

    not_captured = {
        "providers": {
            "the_odds_api": {
                "dwcs_events_found": 0,
                "markets_observed": [],
                "dwcs_market_discovery_status": "not_run",
                "bet365_present_on_dwcs": None,
            }
        },
        "manual_bet365_samples": [],
        "bout_classifications": [],
    }
    assert audit.build_pass_fail_matrix(not_captured)["moneyline"]["status"] == "unknown"


def test_request_failed_never_counts_as_bet365_absence(audit: Any) -> None:
    official = [
        {
            "bout_id": "bout-1",
            "fighter_a": "Alpha One",
            "fighter_b": "Beta Two",
            "scheduled_start": "2026-08-12T00:00:00Z",
        }
    ]
    events = [
        {
            "id": "evt-1",
            "home_team": "Alpha One",
            "away_team": "Beta Two",
            "commence_time": "2026-08-12T00:00:00Z",
        }
    ]
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-1h",
        official_bouts=official,
        provider_events=events,
        markets_by_event={
            "evt-1": {
                "status": "request_failed",
                "error": "timeout",
                "bookmakers": [],
                "headers": {},
                "schema_keys": [],
            }
        },
        regions="uk",
        bookmaker_keys=["bet365", "fanduel"],
        market_keys=["h2h"],
        manual_bet365_samples=[],
        vendor_notes={"opticodds": {"status": "not_configured"}},
        events_list_meta={
            "headers": {"x-requests-remaining": "10", "x-requests-used": "1", "x-requests-last": "0"},
            "schema_keys": ["id", "home_team", "away_team", "commence_time"],
        },
        redact=True,
    )
    assert summary["providers"]["the_odds_api"]["bet365_present_on_dwcs"] is None
    assert summary["decision"]["bet365_dwcs_status"] in {"unresolved", "request_failed"}
    assert summary["decision"]["bet365_dwcs_status"] != "absent"
    assert summary["providers"]["opticodds"]["bet365_present_on_dwcs"] is None


def test_manual_bet365_samples_max_five(audit: Any, tmp_path: Path) -> None:
    samples = [{"bout_id": f"b{i}", "region": "uk"} for i in range(6)]
    with pytest.raises(ValueError, match="at most 5"):
        audit.validate_manual_bet365_samples(samples)
    assert len(audit.validate_manual_bet365_samples(samples[:5])) == 5

    samples_path = tmp_path / "samples.json"
    bouts_path = tmp_path / "bouts.json"
    out_path = tmp_path / "out.json"
    samples_path.write_text(json.dumps(samples), encoding="utf-8")
    bouts_path.write_text("[]", encoding="utf-8")
    code = audit.main(
        [
            "--official-bouts",
            str(bouts_path),
            "--manual-bet365-samples",
            str(samples_path),
            "--out",
            str(out_path),
        ]
    )
    assert code == 2
    assert not out_path.exists()


def test_events_list_meta_preserved_in_summary(audit: Any) -> None:
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-6h",
        official_bouts=[],
        provider_events=[],
        markets_by_event={},
        regions="us",
        bookmaker_keys=["bet365"],
        market_keys=["h2h"],
        manual_bet365_samples=[],
        vendor_notes={},
        events_list_meta={
            "headers": {
                "x-requests-remaining": "500",
                "x-requests-used": "0",
                "x-requests-last": "0",
            },
            "schema_keys": ["id", "commence_time", "home_team", "away_team"],
        },
        redact=True,
    )
    assert summary["events_list"]["headers"]["x-requests-remaining"] == "500"
    assert "commence_time" in summary["events_list"]["schema_keys"]
    assert summary["providers"]["the_odds_api"]["quota"]["x-requests-remaining"] == "500"
    assert summary["quota_fields_documented"] is True


def test_generated_summary_does_not_hardcode_unobserved_claims(audit: Any) -> None:
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-6h",
        official_bouts=[
            {
                "bout_id": "bout-1",
                "fighter_a": "Alpha One",
                "fighter_b": "Beta Two",
                "scheduled_start": "2026-08-12T00:00:00Z",
            }
        ],
        provider_events=[
            {
                "id": "evt-1",
                "home_team": "Alpha One",
                "away_team": "Beta Two",
                "commence_time": "2026-08-12T00:00:00Z",
            }
        ],
        markets_by_event={
            "evt-1": {
                "status": "ok",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "markets": [{"key": "h2h", "last_update": "2026-08-11T18:00:00Z"}],
                    }
                ],
                "headers": {
                    "x-requests-remaining": "499",
                    "x-requests-used": "1",
                    "x-requests-last": "1",
                },
                "schema_keys": ["id", "bookmakers"],
            }
        },
        regions="us,uk,eu",
        bookmaker_keys=["fanduel", "bet365"],
        market_keys=["h2h", "totals", "method", "round"],
        manual_bet365_samples=[],
        vendor_notes={"opticodds": {"status": "not_configured"}},
        events_list_meta={"headers": {}, "schema_keys": ["id"]},
        redact=True,
    )
    provider = summary["providers"]["the_odds_api"]
    assert provider.get("historical_replay_supported") is None
    assert provider.get("monthly_quote_usd") is None
    assert provider.get("rights_notes") in (None, "")
    assert provider.get("lock_events_supported") is None
    assert summary["lock_fields_documented"] is False
    assert summary["pass_fail_matrix"]["historical_replay"]["status"] == "unknown"
    assert summary["pass_fail_matrix"]["monthly_quote"]["status"] == "unknown"
    assert summary["pass_fail_matrix"]["rights"]["status"] == "unknown"
    # Non-DWCS market observation alone must not invent DWCS h2h; here evt-1 is DWCS.
    assert "h2h" in provider["markets_observed"]
    assert summary["pass_fail_matrix"]["moneyline"]["status"] == "pass"


def test_not_run_artifact_is_blocked_without_fabricated_evidence(audit: Any) -> None:
    artifact = audit.build_not_run_artifact(
        block_reason="ODDS_API_KEY unavailable; live odds audit not executed"
    )
    assert artifact["ticket"] == "DWCS-000"
    assert artifact["run_status"] == "not_run"
    assert artifact["status"] == "blocked"
    assert artifact["events"] == []
    assert artifact["bout_classifications"] == []
    assert artifact["providers"] == {}
    assert artifact["quota_fields_documented"] is False
    assert artifact["timestamp_fields_documented"] is False
    assert artifact["lock_fields_documented"] is False
    assert artifact["decision"]["observed"] is False
    assert artifact["decision"]["bet365_dwcs_status"] == "unresolved"
    assert artifact["decision"]["path"] == "hard_blocker"
    for row in artifact["pass_fail_matrix"].values():
        assert row["status"] in {"unknown", "blocked"}
    blob = json.dumps(artifact)
    assert "x-requests-remaining" not in blob
    assert "draftkings" not in blob
    assert '"price"' not in blob
    assert "ODDS_API_KEY/ODDS_API_KEY" not in artifact["block_reason"]


def test_missing_api_key_reason_avoids_duplicated_env_name(audit: Any) -> None:
    default_reason = audit.missing_api_key_reason("ODDS_API_KEY")
    assert default_reason == "ODDS_API_KEY unavailable; live odds audit not executed"
    assert "ODDS_API_KEY/ODDS_API_KEY" not in default_reason
    custom = audit.missing_api_key_reason("CUSTOM_ODDS_KEY")
    assert "CUSTOM_ODDS_KEY" in custom
    assert "ODDS_API_KEY" in custom
    assert "CUSTOM_ODDS_KEY/ODDS_API_KEY" not in custom
    assert "ODDS_API_KEY/ODDS_API_KEY" not in custom


def _assert_events_request_failed_artifact(artifact: dict[str, Any]) -> None:
    assert artifact["run_status"] == "request_failed"
    assert artifact["status"] == "request_failed"
    assert artifact["events"] == []
    assert artifact["bout_classifications"] == []
    assert SENTINEL_API_KEY not in json.dumps(artifact)
    provider = artifact["providers"]["the_odds_api"]
    assert provider["status"] == "request_failed"
    assert provider["bet365_present_on_dwcs"] is None
    assert provider.get("bet365_query_status") == "request_failed"
    assert artifact["decision"]["observed"] is False
    assert artifact["decision"]["bet365_dwcs_status"] in {"request_failed", "unresolved"}
    assert artifact["decision"]["path"] == "hard_blocker"
    assert artifact["decision"]["path"] != "licensed_bet365_primary"
    assert artifact["decision"]["path"] != "the_odds_api_reference_fallback"
    for row in artifact["pass_fail_matrix"].values():
        assert row["status"] in {"unknown", "blocked"}


def test_run_audit_events_http_failure_is_request_failed_not_absence(audit: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("apiKey") == SENTINEL_API_KEY
        return httpx.Response(401, json={"message": "Unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    artifact = audit.run_audit(
        api_key=SENTINEL_API_KEY,
        sport="mma_mixed_martial_arts",
        regions="us",
        official_bouts=[
            {
                "bout_id": "bout-1",
                "fighter_a": "Alpha One",
                "fighter_b": "Beta Two",
                "scheduled_start": "2026-08-12T00:00:00Z",
            }
        ],
        snapshot_label="T-6h",
        bookmaker_keys=["bet365", "fanduel"],
        market_keys=["h2h", "totals"],
        manual_bet365_samples=[],
        vendor_notes={},
        redact=True,
        max_events_for_markets=2,
        client=client,
    )
    client.close()
    _assert_events_request_failed_artifact(artifact)
    assert SENTINEL_API_KEY not in str(artifact.get("failure_reason", ""))


def test_run_audit_events_malformed_payload_is_request_failed(audit: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a-list"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    artifact = audit.run_audit(
        api_key=SENTINEL_API_KEY,
        sport="mma_mixed_martial_arts",
        regions="us",
        official_bouts=[],
        snapshot_label="T-1h",
        bookmaker_keys=["bet365"],
        market_keys=["h2h"],
        manual_bet365_samples=[],
        vendor_notes={},
        redact=True,
        max_events_for_markets=1,
        client=client,
    )
    client.close()
    _assert_events_request_failed_artifact(artifact)


def test_run_audit_events_timeout_is_request_failed(audit: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("connect timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    artifact = audit.run_audit(
        api_key=SENTINEL_API_KEY,
        sport="mma_mixed_martial_arts",
        regions="us",
        official_bouts=[],
        snapshot_label="T-10m",
        bookmaker_keys=["bet365"],
        market_keys=["h2h"],
        manual_bet365_samples=[],
        vendor_notes={},
        redact=True,
        max_events_for_markets=1,
        client=client,
    )
    client.close()
    _assert_events_request_failed_artifact(artifact)


def test_cli_events_failure_writes_redacted_artifact_without_traceback(
    audit: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"message": f"upstream failed for key={SENTINEL_API_KEY}"},
        )

    real_client = audit.httpx.Client

    def fake_client(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(audit.httpx, "Client", fake_client)
    monkeypatch.setenv("ODDS_API_KEY", SENTINEL_API_KEY)
    monkeypatch.setattr(
        audit,
        "get_settings",
        lambda: type("S", (), {"odds_api_key": SENTINEL_API_KEY})(),
    )

    bouts_path = tmp_path / "bouts.json"
    out_path = tmp_path / "odds-coverage-summary.json"
    bouts_path.write_text(
        json.dumps(
            [
                {
                    "bout_id": "bout-1",
                    "fighter_a": "Alpha One",
                    "fighter_b": "Beta Two",
                    "scheduled_start": "2026-08-12T00:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    code = audit.main(
        [
            "--official-bouts",
            str(bouts_path),
            "--redact",
            "--out",
            str(out_path),
            "--snapshot-label",
            "T-6h",
        ]
    )
    captured = capsys.readouterr()
    assert code != 0
    assert out_path.is_file()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert SENTINEL_API_KEY not in captured.err
    assert SENTINEL_API_KEY not in captured.out
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    _assert_events_request_failed_artifact(artifact)
    assert SENTINEL_API_KEY not in out_path.read_text(encoding="utf-8")


def test_build_coverage_summary_schema(audit: Any) -> None:
    official = [
        {
            "bout_id": "bout-1",
            "fighter_a": "Alpha One",
            "fighter_b": "Beta Two",
            "scheduled_start": "2026-08-12T00:00:00Z",
        }
    ]
    events_payload = [
        {
            "id": "evt-1",
            "home_team": "Alpha One",
            "away_team": "Beta Two",
            "commence_time": "2026-08-12T00:00:00Z",
        },
        {
            "id": "evt-non-dwcs",
            "home_team": "Other A",
            "away_team": "Other B",
            "commence_time": "2026-08-20T00:00:00Z",
        },
    ]
    markets_by_event = {
        "evt-1": {
            "status": "ok",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [{"key": "h2h", "last_update": "2026-08-11T18:00:00Z"}],
                }
            ],
            "headers": {
                "x-requests-remaining": "499",
                "x-requests-used": "1",
                "x-requests-last": "1",
            },
            "schema_keys": ["id", "sport_key", "commence_time", "bookmakers"],
        },
        "evt-non-dwcs": {
            "status": "ok",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "markets": [{"key": "totals", "last_update": "2026-08-11T18:00:00Z"}],
                }
            ],
            "headers": {},
            "schema_keys": ["id", "bookmakers"],
        },
    }
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-6h",
        official_bouts=official,
        provider_events=events_payload,
        markets_by_event=markets_by_event,
        regions="us,uk,eu",
        bookmaker_keys=["fanduel", "bet365"],
        market_keys=["h2h", "totals", "method", "round"],
        manual_bet365_samples=[],
        vendor_notes={"opticodds": {"status": "not_configured"}},
        events_list_meta={
            "headers": {"x-requests-remaining": "500", "x-requests-used": "0", "x-requests-last": "0"},
            "schema_keys": ["id", "home_team", "away_team", "commence_time"],
        },
        redact=True,
    )
    assert summary["ticket"] == "DWCS-000"
    assert summary["sport"] == "mma_mixed_martial_arts"
    assert summary["snapshot_label"] == "T-6h"
    assert summary["quota_fields_documented"] is True
    assert summary["timestamp_fields_documented"] is True
    assert summary["lock_fields_documented"] is False
    assert "pass_fail_matrix" in summary
    assert "decision" in summary
    assert summary["events_list"]["schema_keys"]
    # totals only seen on a non-DWCS event must not count as DWCS market evidence
    assert "totals" not in summary["providers"]["the_odds_api"]["markets_observed"]
    assert "h2h" in summary["providers"]["the_odds_api"]["markets_observed"]
    assert all(
        row["status"] in {"present", "absent", "unresolved"}
        for row in summary["bout_classifications"]
    )
    blob = json.dumps(summary)
    assert '"price"' not in blob
    assert "apiKey" not in blob
    assert "api_key" not in blob


def test_bet365_au_alias_presence_is_recognized(audit: Any) -> None:
    """Catalog identity bet365_au must count as Bet365 presence on DWCS."""
    official = [
        {
            "bout_id": "bout-1",
            "fighter_a": "Alpha One",
            "fighter_b": "Beta Two",
            "scheduled_start": "2026-08-12T00:00:00Z",
        }
    ]
    events = [
        {
            "id": "evt-1",
            "home_team": "Alpha One",
            "away_team": "Beta Two",
            "commence_time": "2026-08-12T00:00:00Z",
        }
    ]
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-1h",
        official_bouts=official,
        provider_events=events,
        markets_by_event={
            "evt-1": {
                "status": "ok",
                "bookmakers": [
                    {
                        "key": "bet365_au",
                        "title": "Bet365 AU",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-08-11T17:55:00Z",
                                "outcomes": [
                                    {"name": "Alpha One", "price": -110},
                                    {"name": "Beta Two", "price": -110},
                                ],
                            }
                        ],
                    }
                ],
                "headers": {
                    "x-requests-remaining": "490",
                    "x-requests-used": "10",
                    "x-requests-last": "1",
                },
                "schema_keys": ["id", "bookmakers"],
            }
        },
        regions="au",
        bookmaker_keys=["bet365", "bet365_au", "fanduel"],
        market_keys=["h2h", "totals"],
        manual_bet365_samples=[],
        vendor_notes={},
        bet365_aliases=["bet365", "bet365_au"],
        redact=True,
    )
    provider = summary["providers"]["the_odds_api"]
    assert provider["bet365_present_on_dwcs"] is True
    assert provider["bet365_query_status"] == "ok"
    assert summary["decision"]["bet365_dwcs_status"] == "present"
    assert summary["events"][0]["market_discovery"]["presence"]["bet365_au"]["h2h"] == (
        "present"
    )
    blob = json.dumps(summary)
    assert '"price"' not in blob
    assert "-110" not in blob


def test_scoped_absence_is_not_universal_bet365_absence(audit: Any) -> None:
    """Absence for queried keys/regions must stay scoped, never universal."""
    official = [
        {
            "bout_id": "bout-1",
            "fighter_a": "Alpha One",
            "fighter_b": "Beta Two",
            "scheduled_start": "2026-08-12T00:00:00Z",
        }
    ]
    events = [
        {
            "id": "evt-1",
            "home_team": "Alpha One",
            "away_team": "Beta Two",
            "commence_time": "2026-08-12T00:00:00Z",
        }
    ]
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-3h",
        official_bouts=official,
        provider_events=events,
        markets_by_event={
            "evt-1": {
                "status": "ok",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "markets": [{"key": "h2h", "last_update": "2026-08-11T18:00:00Z"}],
                    }
                ],
                "headers": {},
                "schema_keys": ["id", "bookmakers"],
            }
        },
        regions="us,uk,eu",
        bookmaker_keys=["bet365", "fanduel"],
        market_keys=["h2h"],
        manual_bet365_samples=[],
        vendor_notes={},
        bet365_aliases=["bet365", "bet365_au"],
        redact=True,
    )
    provider = summary["providers"]["the_odds_api"]
    scope = provider["bet365_observation_scope"]
    assert scope["provider"] == "the_odds_api"
    assert scope["regions"] == "us,uk,eu"
    assert "bet365" in scope["bookmaker_keys_queried"]
    assert "bet365_au" not in scope["bookmaker_keys_queried"]
    assert provider["bet365_present_on_dwcs"] is False
    assert summary["decision"]["bet365_dwcs_status"] == "scoped_absent"
    assert summary["decision"]["bet365_dwcs_status"] != "absent"
    rationale = summary["decision"]["rationale"].lower()
    assert "scoped" in rationale
    assert "not universal" in rationale
    assert summary["decision"]["path"] == "the_odds_api_reference_fallback"


def test_request_failure_remains_distinct_from_scoped_absence(audit: Any) -> None:
    official = [
        {
            "bout_id": "bout-1",
            "fighter_a": "Alpha One",
            "fighter_b": "Beta Two",
            "scheduled_start": "2026-08-12T00:00:00Z",
        }
    ]
    events = [
        {
            "id": "evt-1",
            "home_team": "Alpha One",
            "away_team": "Beta Two",
            "commence_time": "2026-08-12T00:00:00Z",
        }
    ]
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-1h",
        official_bouts=official,
        provider_events=events,
        markets_by_event={
            "evt-1": {
                "status": "request_failed",
                "error": "timeout",
                "bookmakers": [],
                "headers": {},
                "schema_keys": [],
            }
        },
        regions="au",
        bookmaker_keys=["bet365_au"],
        market_keys=["h2h"],
        manual_bet365_samples=[],
        vendor_notes={},
        bet365_aliases=["bet365", "bet365_au"],
        redact=True,
    )
    assert summary["providers"]["the_odds_api"]["bet365_present_on_dwcs"] is None
    assert summary["providers"]["the_odds_api"]["bet365_query_status"] == "request_failed"
    assert summary["decision"]["bet365_dwcs_status"] == "request_failed"
    assert summary["decision"]["bet365_dwcs_status"] != "scoped_absent"
    assert summary["decision"]["bet365_dwcs_status"] != "absent"


def test_sanitized_market_last_update_timestamps_are_retained(audit: Any) -> None:
    official = [
        {
            "bout_id": "bout-1",
            "fighter_a": "Alpha One",
            "fighter_b": "Beta Two",
            "scheduled_start": "2026-08-12T00:00:00Z",
        }
    ]
    events = [
        {
            "id": "evt-1",
            "home_team": "Alpha One",
            "away_team": "Beta Two",
            "commence_time": "2026-08-12T00:00:00Z",
        }
    ]
    summary = audit.build_coverage_summary(
        sport="mma_mixed_martial_arts",
        provider="the_odds_api",
        captured_at="2026-08-11T18:00:00Z",
        snapshot_label="T-1h",
        official_bouts=official,
        provider_events=events,
        markets_by_event={
            "evt-1": {
                "status": "ok",
                "bookmakers": [
                    {
                        "key": "bet365_au",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-08-11T17:40:00Z",
                                "outcomes": [{"name": "Alpha One", "price": 1.91}],
                            }
                        ],
                    },
                    {
                        "key": "fanduel",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-08-11T17:41:00Z",
                                "outcomes": [{"name": "Beta Two", "price": 2.10}],
                            }
                        ],
                    },
                ],
                "headers": {},
                "schema_keys": ["id", "bookmakers"],
            }
        },
        regions="au",
        bookmaker_keys=["bet365_au", "fanduel"],
        market_keys=["h2h"],
        manual_bet365_samples=[],
        vendor_notes={},
        bet365_aliases=["bet365_au"],
        redact=True,
    )
    timestamps = summary["events"][0]["market_discovery"]["market_timestamps"]
    by_book = {row["bookmaker_key"]: row for row in timestamps}
    assert by_book["bet365_au"]["market_key"] == "h2h"
    assert by_book["bet365_au"]["last_update"] == "2026-08-11T17:40:00Z"
    assert by_book["fanduel"]["last_update"] == "2026-08-11T17:41:00Z"
    assert summary["timestamp_fields_documented"] is True
    blob = json.dumps(summary)
    assert "1.91" not in blob
    assert "2.10" not in blob
    assert '"price"' not in blob
    assert all("outcomes" not in row for row in timestamps)
    assert '"outcomes"' not in blob


def test_default_bet365_aliases_include_bet365_au(audit: Any) -> None:
    assert "bet365_au" in audit.DEFAULT_BET365_ALIASES
    assert "bet365" in audit.DEFAULT_BET365_ALIASES
    assert "au" in audit.DEFAULT_REGIONS.split(",")
    assert "bet365_au" in audit.DEFAULT_BOOKMAKERS
