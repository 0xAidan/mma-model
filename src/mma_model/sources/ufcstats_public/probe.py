"""Bounded live probe + sanitized audit evidence (DWCS-102)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from mma_model import __version__ as CLIENT_VERSION
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.http.polite_client import PoliteHttpClient
from mma_model.sources.http.robots import RobotsAccessDecision, evaluate_robots_access
from mma_model.sources.http_politeness import load_http_politeness
from mma_model.sources.ufcstats_public.parser import SOURCE_UFCSTATS_PUBLIC

PARSER_VERSION = "ufcstats_public_parser@1"


def evaluate_robots_policy(
    *,
    robots_status_code: int | None,
    robots_body_text: str,
    target_path: str,
    user_agent: str | None = None,
    network_error: str | None = None,
    host: str = "www.ufcstats.com",
) -> dict[str, Any]:
    """RFC 9309 robots policy for the configured identifiable UA.

    Redirect policy for fetches remains same-host/bounded in PoliteHttpClient.
    """
    ua = user_agent or load_http_politeness().user_agent
    path = target_path if target_path.startswith("/") else f"/{target_path}"
    target_url = f"http://{host}{path}"
    decision: RobotsAccessDecision = evaluate_robots_access(
        status_code=robots_status_code,
        body_text=robots_body_text,
        user_agent=ua,
        target_url=target_url,
        network_error=network_error,
    )
    return decision.as_dict()


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
    ua = client.politeness.user_agent
    try:
        robots_status, robots_body, robots_hash = client.fetch_robots_txt()
        network_error = None
    except Exception as exc:  # noqa: BLE001 - fail closed per RFC 9309
        robots_decision = evaluate_robots_policy(
            robots_status_code=None,
            robots_body_text="",
            target_path=path_category,
            user_agent=ua,
            network_error=type(exc).__name__,
            host=f"www.{host}",
        )
        robots_decision = {
            **robots_decision,
            "robots_url": robots_url,
            "response_content_hash": None,
        }
        return build_sanitized_probe_evidence(
            host=host,
            path_category=path_category,
            http_status=None,
            block_reason=str(robots_decision["policy_decision"]),
            response_content_hash=None,
            observed_at=observed,
            robots=robots_decision,
        )

    robots_decision = evaluate_robots_policy(
        robots_status_code=robots_status,
        robots_body_text=robots_body,
        target_path=path_category,
        user_agent=ua,
        network_error=network_error,
        host=f"www.{host}",
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
