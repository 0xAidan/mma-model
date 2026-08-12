"""Sanitized live-probe / audit summary contract tests (DWCS-102 review fixes)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
UTC = timezone.utc


def test_robots_404_is_rfc9309_allow_all_not_bypass() -> None:
    from mma_model.sources.ufcstats_public.probe import evaluate_robots_policy

    decision = evaluate_robots_policy(
        robots_status_code=404,
        robots_body_text="Not Found",
        target_path="/statistics/events/completed",
    )
    assert decision["policy_decision"] == "rfc9309_http_404_410_allow_all"
    assert decision["allowed"] is True
    assert decision["robots_status_code"] == 404
    assert decision["standard"] == "RFC9309"


def test_robots_5xx_is_rfc9309_temporary_disallow() -> None:
    from mma_model.sources.ufcstats_public.probe import evaluate_robots_policy

    decision = evaluate_robots_policy(
        robots_status_code=503,
        robots_body_text="unavailable",
        target_path="/statistics/events/completed",
    )
    assert decision["policy_decision"] == "rfc9309_http_5xx_temporary_disallow"
    assert decision["allowed"] is False


def test_build_sanitized_probe_evidence_has_required_fields_no_body() -> None:
    from mma_model.sources.ufcstats_public.probe import build_sanitized_probe_evidence

    observed = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
    evidence = build_sanitized_probe_evidence(
        host="ufcstats.com",
        path_category="/statistics/events/completed",
        http_status=403,
        block_reason="cloudflare_challenge",
        response_content_hash="a" * 64,
        observed_at=observed,
        robots={
            "robots_url": "http://www.ufcstats.com/robots.txt",
            "robots_status_code": 404,
            "policy_decision": "rfc9309_http_404_410_allow_all",
            "allowed": True,
            "standard": "RFC9309",
        },
    )
    required = {
        "host",
        "path_category",
        "http_status",
        "block_reason",
        "response_content_hash",
        "observed_at",
        "robots",
        "client_version",
        "parser_version",
    }
    assert required.issubset(evidence.keys())
    blob = json.dumps(evidence)
    assert "<html" not in blob.lower()
    assert "checking your browser" not in blob.lower()
    assert "?" not in evidence["path_category"]


def test_cli_summary_includes_probe_evidence_deterministically(tmp_path: Path) -> None:
    from mma_model.cli import main

    out = tmp_path / "summary.json"
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    code = main(
        [
            "source",
            "audit",
            "ufcstats-public",
            "--years",
            "2017:2017",
            "--fixture-root",
            str(fixtures),
            "--summary-out",
            str(out),
        ]
    )
    assert code == 0
    summary = json.loads(out.read_text(encoding="utf-8"))
    assert summary["source"] == "ufcstats_public"
    assert summary["events_total"] >= 1
    assert "live_probe" in summary
    # Fixture mode must mark live probe as not run / unverified, never invent Cloudflare.
    assert summary["live_probe"]["result"] in {"NOT_RUN", "UNVERIFIED"}
    assert summary["live_probe"].get("block_reason") in {None, "not_run", "unverified"}
