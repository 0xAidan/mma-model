"""Bounded same-host robots.txt redirect retrieval tests (RFC 9309)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mma_model.sources.http_politeness import load_http_politeness

ROOT = Path(__file__).resolve().parents[3]
POLITE_PATH = ROOT / "config/sources/http_politeness_v1.json"
UA = load_http_politeness(POLITE_PATH).user_agent
TARGET = "http://www.ufcstats.com/statistics/events/completed"


def _zero_delay_politeness():
    politeness = load_http_politeness(POLITE_PATH)
    host = politeness.hosts["ufcstats.com"]
    object.__setattr__(
        politeness,
        "hosts",
        type(politeness.hosts)(
            {
                **dict(politeness.hosts),
                "ufcstats.com": host.model_copy(
                    update={"min_delay_sec": 0.0, "max_retries": 0}
                ),
            }
        ),
    )
    return politeness


def _robots_client(tmp_path: Path, handler) -> object:
    from mma_model.sources.http.polite_client import PoliteHttpClient

    return PoliteHttpClient(
        host="ufcstats.com",
        politeness=_zero_delay_politeness(),
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
        robots_disallow=False,
    )


def test_robots_redirect_one_hop_same_host(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if (
            request.url.path == "/robots.txt"
            and request.url.scheme == "http"
            and not request.url.query
        ):
            return httpx.Response(
                302,
                headers={"Location": "http://www.ufcstats.com/robots.txt?canonical=1"},
                request=request,
            )
        return httpx.Response(
            200,
            content=b"User-agent: *\nAllow: /\n",
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    status, body, digest = client.fetch_robots_txt()
    assert status == 200
    assert "Allow: /" in body
    assert len(digest) == 64
    assert len(calls) == 2
    decision = evaluate_robots_access(
        status_code=status,
        body_text=body,
        user_agent=UA,
        target_url=TARGET,
    )
    assert decision.allowed is True


def test_robots_redirect_http_to_https_same_host(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(
                301,
                headers={"Location": "https://www.ufcstats.com/robots.txt"},
                request=request,
            )
        return httpx.Response(
            200, content=b"User-agent: *\nDisallow:\n", request=request
        )

    client = _robots_client(tmp_path, handler)
    status, body, _digest = client.fetch_robots_txt()
    assert status == 200
    assert "User-agent" in body


def test_robots_redirect_chain_of_five_then_ok(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # hop markers h1..h5 then final
        if "h5" in url:
            return httpx.Response(200, content=b"User-agent: *\nAllow: /\n", request=request)
        for idx in range(1, 5):
            if f"h{idx}" in url:
                return httpx.Response(
                    302,
                    headers={
                        "Location": f"http://www.ufcstats.com/robots.txt?h{idx + 1}"
                    },
                    request=request,
                )
        # initial → h1
        return httpx.Response(
            302,
            headers={"Location": "http://www.ufcstats.com/robots.txt?h1"},
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    status, body, _ = client.fetch_robots_txt()
    assert status == 200
    assert "Allow: /" in body


def test_robots_redirect_sixth_hop_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import RobotsRedirectError

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for idx in range(1, 7):
            if f"h{idx}" in url:
                return httpx.Response(
                    302,
                    headers={
                        "Location": f"http://www.ufcstats.com/robots.txt?h{idx + 1}"
                    },
                    request=request,
                )
        return httpx.Response(
            302,
            headers={"Location": "http://www.ufcstats.com/robots.txt?h1"},
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    with pytest.raises(RobotsRedirectError) as excinfo:
        client.fetch_robots_txt()
    assert excinfo.value.reason == "robots_redirect_hop_limit"


def test_robots_redirect_loop_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import RobotsRedirectError

    def handler(request: httpx.Request) -> httpx.Response:
        if "b=1" in str(request.url):
            return httpx.Response(
                302,
                headers={"Location": "http://www.ufcstats.com/robots.txt?a=1"},
                request=request,
            )
        return httpx.Response(
            302,
            headers={"Location": "http://www.ufcstats.com/robots.txt?b=1"},
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    with pytest.raises(RobotsRedirectError) as excinfo:
        client.fetch_robots_txt()
    assert excinfo.value.reason == "robots_redirect_loop"


def test_robots_redirect_relative_location(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" and not request.url.query:
            return httpx.Response(
                302,
                headers={"Location": "/robots.txt?rel=1"},
                request=request,
            )
        return httpx.Response(
            200, content=b"User-agent: *\nAllow: /\n", request=request
        )

    client = _robots_client(tmp_path, handler)
    status, body, _ = client.fetch_robots_txt()
    assert status == 200
    assert "Allow: /" in body


def test_robots_redirect_off_host_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import RobotsRedirectError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/robots.txt"},
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    with pytest.raises(RobotsRedirectError) as excinfo:
        client.fetch_robots_txt()
    assert excinfo.value.reason == "robots_redirect_off_host"


def test_robots_redirect_missing_location_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import RobotsRedirectError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={}, request=request)

    client = _robots_client(tmp_path, handler)
    with pytest.raises(RobotsRedirectError) as excinfo:
        client.fetch_robots_txt()
    assert excinfo.value.reason == "robots_redirect_missing_location"


def test_robots_redirect_malformed_location_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import RobotsRedirectError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "http://["},
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    with pytest.raises(RobotsRedirectError) as excinfo:
        client.fetch_robots_txt()
    assert excinfo.value.reason == "robots_redirect_malformed_location"


def test_robots_redirect_userinfo_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import RobotsRedirectError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://user:pass@www.ufcstats.com/robots.txt"},
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    with pytest.raises(RobotsRedirectError) as excinfo:
        client.fetch_robots_txt()
    assert excinfo.value.reason == "robots_redirect_userinfo"


def test_robots_redirect_non_http_fail_closed(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import RobotsRedirectError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "ftp://www.ufcstats.com/robots.txt"},
            request=request,
        )

    client = _robots_client(tmp_path, handler)
    with pytest.raises(RobotsRedirectError) as excinfo:
        client.fetch_robots_txt()
    assert excinfo.value.reason == "robots_redirect_non_http"


def test_robots_redirect_final_404_allow_all(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "http":
            return httpx.Response(
                302,
                headers={"Location": "https://www.ufcstats.com/robots.txt"},
                request=request,
            )
        return httpx.Response(404, content=b"Not Found", request=request)

    client = _robots_client(tmp_path, handler)
    status, body, _ = client.fetch_robots_txt()
    decision = evaluate_robots_access(
        status_code=status,
        body_text=body,
        user_agent=UA,
        target_url=TARGET,
    )
    assert status == 404
    assert decision.allowed is True
    assert decision.policy_decision == "rfc9309_http_404_410_allow_all"


def test_robots_redirect_final_5xx_disallow(tmp_path: Path) -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    def handler(request: httpx.Request) -> httpx.Response:
        if "next=1" not in str(request.url):
            return httpx.Response(
                302,
                headers={"Location": "http://www.ufcstats.com/robots.txt?next=1"},
                request=request,
            )
        return httpx.Response(503, content=b"unavailable", request=request)

    client = _robots_client(tmp_path, handler)
    status, body, _ = client.fetch_robots_txt()
    decision = evaluate_robots_access(
        status_code=status,
        body_text=body,
        user_agent=UA,
        target_url=TARGET,
    )
    assert status == 503
    assert decision.allowed is False
    assert decision.policy_decision == "rfc9309_http_5xx_temporary_disallow"


def test_page_client_still_rejects_same_host_redirects(tmp_path: Path) -> None:
    """General page client must not start following redirects."""
    from mma_model.sources.http.polite_client import PoliteHttpClient, UrlNotAllowedError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "http://www.ufcstats.com/event-details/other"},
            request=request,
        )

    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=_zero_delay_politeness(),
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(handler),
        robots_disallow=False,
    )
    with pytest.raises(UrlNotAllowedError, match="refusing to follow redirect"):
        client.get_text("http://www.ufcstats.com/event-details/abc")


# Canonical host set for configured host "ufcstats.com": exact {ufcstats.com, www.ufcstats.com}.
_CONFIGURED_HOST = "ufcstats.com"
_CURRENT = "http://www.ufcstats.com/robots.txt?start=1"


@pytest.mark.parametrize(
    ("location", "outcome"),
    [
        # Allowed: exact canonical hosts, case fold, www↔bare, default ports, http↔https.
        ("http://www.ufcstats.com/robots.txt", "allow"),
        ("http://ufcstats.com/robots.txt", "allow"),
        ("http://WWW.UFCSTATS.COM/robots.txt", "allow"),
        ("http://UFCSTATS.COM/robots.txt", "allow"),
        ("https://www.ufcstats.com/robots.txt", "allow"),
        ("https://ufcstats.com/robots.txt", "allow"),
        ("http://www.ufcstats.com:80/robots.txt", "allow"),
        ("http://ufcstats.com:80/robots.txt", "allow"),
        ("https://www.ufcstats.com:443/robots.txt", "allow"),
        ("https://ufcstats.com:443/robots.txt", "allow"),
        ("/robots.txt?rel=1", "allow"),
        # Non-default / wrong-scheme ports.
        ("http://www.ufcstats.com:8080/robots.txt", "robots_redirect_non_default_port"),
        ("https://www.ufcstats.com:8443/robots.txt", "robots_redirect_non_default_port"),
        ("http://www.ufcstats.com:443/robots.txt", "robots_redirect_non_default_port"),
        ("https://www.ufcstats.com:80/robots.txt", "robots_redirect_non_default_port"),
        ("http://www.ufcstats.com:1/robots.txt", "robots_redirect_non_default_port"),
        ("http://www.ufcstats.com:65535/robots.txt", "robots_redirect_non_default_port"),
        # Invalid ports.
        ("http://www.ufcstats.com:abc/robots.txt", "robots_redirect_invalid_port"),
        ("http://www.ufcstats.com:80a/robots.txt", "robots_redirect_invalid_port"),
        ("http://www.ufcstats.com:/robots.txt", "robots_redirect_invalid_port"),
        # Off-host / authority tricks.
        ("https://evil.example/robots.txt", "robots_redirect_off_host"),
        ("http://evil.ufcstats.com/robots.txt", "robots_redirect_off_host"),
        ("http://www.www.ufcstats.com/robots.txt", "robots_redirect_off_host"),
        ("http://notufcstats.com/robots.txt", "robots_redirect_off_host"),
        ("http://ufcstats.com.evil.example/robots.txt", "robots_redirect_off_host"),
        ("http://www.ufcstats.com.evil.example/robots.txt", "robots_redirect_off_host"),
        ("http://127.0.0.1/robots.txt", "robots_redirect_off_host"),
        ("http://[::1]/robots.txt", "robots_redirect_off_host"),
        ("http://[::1]:80/robots.txt", "robots_redirect_off_host"),
        ("http://www.ufcstats.com./robots.txt", "robots_redirect_off_host"),
        ("http://ufcstats.com./robots.txt", "robots_redirect_off_host"),
        ("http://www.ufcstats.com.:80/robots.txt", "robots_redirect_off_host"),
        ("http://www.ufcstats.com%2eevil.com/robots.txt", "robots_redirect_off_host"),
        ("http://www.ufcstats.com%2Eevil.com/robots.txt", "robots_redirect_off_host"),
        ("http://www.xn--ufcstats-r9a.com/robots.txt", "robots_redirect_off_host"),
        # Cyrillic 'с' confusable in place of Latin 'c'.
        ("http://www.uf\u0441stats.com/robots.txt", "robots_redirect_off_host"),
        # Userinfo edge cases.
        ("https://user:pass@www.ufcstats.com/robots.txt", "robots_redirect_userinfo"),
        ("https://user@www.ufcstats.com/robots.txt", "robots_redirect_userinfo"),
        # userinfo@host form: authority is evil.example with userinfo www.ufcstats.com
        ("https://www.ufcstats.com@evil.example/robots.txt", "robots_redirect_userinfo"),
        ("ftp://www.ufcstats.com/robots.txt", "robots_redirect_non_http"),
    ],
)
def test_robots_redirect_trust_boundary_matrix(location: str, outcome: str) -> None:
    from mma_model.sources.http.robots import (
        RobotsRedirectError,
        resolve_robots_redirect_url,
    )

    if outcome == "allow":
        resolved = resolve_robots_redirect_url(
            current_url=_CURRENT,
            location=location,
            configured_host=_CONFIGURED_HOST,
            visited={_CURRENT},
            redirect_count=0,
        )
        assert resolved.startswith(("http://", "https://"))
        # Default ports only: never retain a non-default authority port.
        assert ":8080" not in resolved
        assert ":8443" not in resolved
        return

    with pytest.raises(RobotsRedirectError) as excinfo:
        resolve_robots_redirect_url(
            current_url=_CURRENT,
            location=location,
            configured_host=_CONFIGURED_HOST,
            visited={_CURRENT},
            redirect_count=0,
        )
    assert excinfo.value.reason == outcome


def test_robots_redirect_canonical_host_set_is_narrow() -> None:
    from mma_model.sources.http.robots import allowed_robots_hosts

    assert allowed_robots_hosts("ufcstats.com") == frozenset(
        {"ufcstats.com", "www.ufcstats.com"}
    )
    assert allowed_robots_hosts("www.ufcstats.com") == frozenset(
        {"ufcstats.com", "www.ufcstats.com"}
    )
    assert allowed_robots_hosts("WWW.UFCSTATS.COM") == frozenset(
        {"ufcstats.com", "www.ufcstats.com"}
    )
