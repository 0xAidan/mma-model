"""Bounded live probe + sanitized audit evidence (DWCS-102)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from mma_model import __version__ as CLIENT_VERSION
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.http.polite_client import PoliteHttpClient
from mma_model.sources.ufcstats_public.parser import SOURCE_UFCSTATS_PUBLIC

PARSER_VERSION = "ufcstats_public_parser@1"
ROBOTS_UNAVAILABLE_POLICY = "fail_closed_unavailable"


def evaluate_robots_policy(
    *,
    robots_status_code: int | None,
    robots_body_text: str,
    target_path: str,
) -> dict[str, Any]:
    """Fail-closed robots policy.

    Missing/unavailable robots (404/5xx/empty) is NOT treated as permission.
    Explicit Disallow matching target_path is a hard stop.
    """
    path = target_path or "/"
    if robots_status_code != 200:
        return {
            "robots_status_code": robots_status_code,
            "policy_decision": ROBOTS_UNAVAILABLE_POLICY,
            "allowed": False,
            "detail": "robots unavailable; fail closed (do not infer permission)",
            "target_path": path,
        }
    disallow_paths: list[str] = []
    for line in robots_body_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lower = stripped.lower()
        if lower.startswith("disallow:"):
            value = stripped.split(":", 1)[1].strip()
            if value:
                disallow_paths.append(value)
    for rule in disallow_paths:
        if rule == "/" or path.startswith(rule):
            return {
                "robots_status_code": robots_status_code,
                "policy_decision": "disallow_match",
                "allowed": False,
                "detail": f"robots Disallow matched {rule!r}",
                "target_path": path,
            }
    return {
        "robots_status_code": robots_status_code,
        "policy_decision": "allow_no_disallow_match",
        "allowed": True,
        "detail": "robots fetched; no disallow match for target path",
        "target_path": path,
    }


def build_sanitized_probe_evidence(
    *,
    host: str,
    path_category: str,
    http_status: int | None,
    block_reason: str | None,
    response_content_hash: str | None,
    observed_at: datetime,
    robots: Mapping[str, Any],
) -> dict[str, Any]:
    """Build redistributable probe metadata with no body/raw HTML."""
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware UTC")
    path = path_category.split("?", 1)[0]
    return {
        "source": SOURCE_UFCSTATS_PUBLIC,
        "host": host,
        "path_category": path,
        "http_status": http_status,
        "block_reason": block_reason,
        "response_content_hash": response_content_hash,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "robots": dict(robots),
        "client_version": CLIENT_VERSION,
        "parser_version": PARSER_VERSION,
        "result": "BLOCKED" if block_reason else "OK",
    }


def not_run_live_probe_evidence() -> dict[str, Any]:
    return {
        "source": SOURCE_UFCSTATS_PUBLIC,
        "result": "NOT_RUN",
        "block_reason": "not_run",
        "detail": "fixture/offline audit; live probe not executed",
        "client_version": CLIENT_VERSION,
        "parser_version": PARSER_VERSION,
    }


def run_bounded_live_probe(
    *,
    client: PoliteHttpClient,
    host: str = "ufcstats.com",
    path_category: str = "/statistics/events/completed",
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """One bounded probe: robots policy then one allowlisted page. Never bypasses blocks."""
    observed = observed_at or datetime.now(timezone.utc)
    robots_url = f"http://www.{host}/robots.txt"
    try:
        robots_status, robots_body, robots_hash = client.fetch_robots_txt()
    except Exception as exc:  # noqa: BLE001 - fail closed
        robots_decision = {
            "robots_url": robots_url,
            "robots_status_code": None,
            "policy_decision": ROBOTS_UNAVAILABLE_POLICY,
            "allowed": False,
            "detail": f"robots fetch failed: {type(exc).__name__}",
            "response_content_hash": None,
            "target_path": path_category,
        }
        return build_sanitized_probe_evidence(
            host=host,
            path_category=path_category,
            http_status=None,
            block_reason="robots_unavailable",
            response_content_hash=None,
            observed_at=observed,
            robots=robots_decision,
        )

    robots_decision = evaluate_robots_policy(
        robots_status_code=robots_status,
        robots_body_text=robots_body,
        target_path=path_category,
    )
    robots_decision = {
        **robots_decision,
        "robots_url": robots_url,
        "response_content_hash": robots_hash,
    }
    if not robots_decision["allowed"]:
        return build_sanitized_probe_evidence(
            host=host,
            path_category=path_category,
            http_status=None,
            block_reason=str(robots_decision["policy_decision"]),
            response_content_hash=None,
            observed_at=observed,
            robots=robots_decision,
        )

    probe_url = f"http://www.{host}{path_category}"
    try:
        _text, digest = client.get_text(probe_url)
        return build_sanitized_probe_evidence(
            host=host,
            path_category=path_category,
            http_status=200,
            block_reason=None,
            response_content_hash=digest,
            observed_at=observed,
            robots=robots_decision,
        )
    except SourceBlockedError as exc:
        return build_sanitized_probe_evidence(
            host=host,
            path_category=path_category,
            http_status=exc.status_code,
            block_reason=exc.reason,
            response_content_hash=None,
            observed_at=observed,
            robots=robots_decision,
        )
