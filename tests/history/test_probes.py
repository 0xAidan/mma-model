"""Robots, probe sanitization, and regional HTTP client hygiene (DWCS-105)."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from mma_model.history.probe import (
    build_sanitized_probe_evidence,
    evaluate_robots_policy,
    not_run_live_probe_evidence,
    run_bounded_live_probe,
)
from mma_model.sources.http.polite_client import (
    CacheCorruptionError,
    ForbiddenHeaderError,
    PoliteHttpClient,
)
from mma_model.sources.http_politeness import load_http_politeness

UTC = timezone.utc
POLITE = Path(__file__).resolve().parents[2] / "config/sources/http_politeness_v1.json"


def test_robots_404_is_not_a_block() -> None:
    decision = evaluate_robots_policy(
        robots_status_code=404,
        robots_body_text="",
        target_path="/fightcenter/fighters/x",
        host="www.tapology.com",
    )
    assert decision["allowed"] is True


def test_robots_5xx_fails_closed() -> None:
    decision = evaluate_robots_policy(
        robots_status_code=503,
        robots_body_text="unavailable",
        target_path="/fightcenter/fighters/x",
        host="www.tapology.com",
    )
    assert decision["allowed"] is False


def test_robots_network_fails_closed() -> None:
    decision = evaluate_robots_policy(
        robots_status_code=None,
        robots_body_text="",
        target_path="/fighter/x",
        host="www.sherdog.com",
        network_error="ConnectError",
    )
    assert decision["allowed"] is False


def test_probe_evidence_has_no_raw_body() -> None:
    evidence = build_sanitized_probe_evidence(
        source="tapology_public",
        host="tapology.com",
        path_category="/rankings/?secret=1",
        http_status=200,
        block_reason=None,
        response_content_hash="b" * 64,
        observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        robots={"allowed": True, "policy_decision": "robots_absent_4xx"},
    )
    blob = str(evidence)
    assert "secret=1" not in blob
    assert evidence["path_category"] == "/rankings/"
    assert "<html" not in blob
    offline = not_run_live_probe_evidence("sherdog_public")
    assert offline["result"] == "NOT_RUN"


def test_tapology_client_rejects_cookie_headers(tmp_path: Path) -> None:
    politeness = load_http_politeness(POLITE)
    with pytest.raises(ForbiddenHeaderError):
        PoliteHttpClient(
            host="tapology.com",
            politeness=politeness,
            cache_dir=tmp_path / "cache",
            extra_headers={"Cookie": "session=abc"},
            sleep_fn=lambda _: None,
        )


def test_sequential_requests_drop_set_cookie(tmp_path: Path) -> None:
    politeness = load_http_politeness(POLITE)
    seen_cookie = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookie.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            text="<html>ok</html>",
            headers={"Set-Cookie": "session=abc"},
            request=request,
        )

    client = PoliteHttpClient(
        host="tapology.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    )
    try:
        client.get_text("http://www.tapology.com/rankings/")
        client.get_text("http://www.tapology.com/rankings/")
        assert client.cookie_jar_empty()
        assert all(value in {None, ""} for value in seen_cookie)
    finally:
        client.close()


def test_http_redirect_is_typed_kill_not_followed(tmp_path: Path) -> None:
    politeness = load_http_politeness(POLITE)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url.path).endswith("/robots.txt"):
            return httpx.Response(
                200, text="User-agent: *\nAllow: /\n", request=request
            )
        return httpx.Response(
            302,
            headers={"Location": "https://www.tapology.com/rankings/"},
            request=request,
        )

    client = PoliteHttpClient(
        host="tapology.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    )
    try:
        evidence = run_bounded_live_probe(
            client=client,
            source="tapology_public",
            host="tapology.com",
            path_category="/rankings/",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
        assert evidence["result"] == "BLOCKED"
        assert evidence["block_reason"] == "http_redirect_refused"
    finally:
        client.close()


def test_cache_corruption_hard_fails(tmp_path: Path) -> None:
    politeness = load_http_politeness(POLITE)
    body = b"<html>ok</html>"
    digest = hashlib.sha256(body).hexdigest()
    url = "http://www.tapology.com/rankings/"
    cache_dir = tmp_path / "cache" / "tapology.com"
    cache_dir.mkdir(parents=True)
    with gzip.open(cache_dir / f"{digest}.gz", "wb") as handle:
        handle.write(b"tampered-bytes")
    (cache_dir / "url_index.json").write_text(json.dumps({url: digest}), encoding="utf-8")
    client = PoliteHttpClient(
        host="tapology.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError())),
        sleep_fn=lambda _: None,
    )
    try:
        with pytest.raises(CacheCorruptionError):
            client.get_text(url)
    finally:
        client.close()
