"""Polite HTTP client and block-signal tests (DWCS-102 Task 3)."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from mma_model.sources.http_politeness import load_http_politeness

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/sources/http"
POLITE_PATH = ROOT / "config/sources/http_politeness_v1.json"


def test_detect_captcha_interstitial() -> None:
    from mma_model.sources.http.block_signals import detect_block_signal

    html = "<html><body>Please complete the CAPTCHA to continue</body></html>"
    assert detect_block_signal(200, html, False) == "captcha_interstitial"


def test_detect_robots_disallow() -> None:
    from mma_model.sources.http.block_signals import detect_block_signal

    assert detect_block_signal(200, "ok", True) == "robots_disallow"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, "forbidden", "http_403"),
        (429, "slow down", "http_429"),
        (503, "unavailable", "http_503"),
        (200, "cf-browser-verification challenge", "cloudflare_challenge"),
        (200, "Checking your browser…", "cloudflare_challenge"),
        (200, "Access Denied by security policy", "access_denied"),
    ],
)
def test_detect_status_and_body_block_signals(
    status: int, body: str, expected: str
) -> None:
    from mma_model.sources.http.block_signals import detect_block_signal

    assert detect_block_signal(status, body, False) == expected


def test_ok_html_fixture_has_no_block_signal() -> None:
    from mma_model.sources.http.block_signals import detect_block_signal

    html = (FIXTURES / "ok.html").read_text(encoding="utf-8")
    assert detect_block_signal(200, html, False) is None


def _client(tmp_path: Path, **overrides: Any):
    from mma_model.sources.http.polite_client import PoliteHttpClient

    politeness = load_http_politeness(POLITE_PATH)
    return PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        **overrides,
    )


def test_get_text_caches_gzip_content_addressed(tmp_path: Path, monkeypatch) -> None:
    from mma_model.sources.http.polite_client import PoliteHttpClient

    html = (FIXTURES / "ok.html").read_text(encoding="utf-8")
    body = html.encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()

    def fake_send(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, request=request)

    transport = httpx.MockTransport(fake_send)
    politeness = load_http_politeness(POLITE_PATH)
    # Shrink delay for tests.
    host = politeness.hosts["ufcstats.com"]
    object.__setattr__(
        politeness,
        "hosts",
        type(politeness.hosts)(
            {
                **dict(politeness.hosts),
                "ufcstats.com": host.model_copy(update={"min_delay_sec": 0.0}),
            }
        ),
    )
    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=transport,
        robots_disallow=False,
    )
    text, sha = client.get_text("http://www.ufcstats.com/event-details/abc")
    assert text == html
    assert sha == digest
    cached = tmp_path / "cache" / "ufcstats.com" / f"{digest}.gz"
    assert cached.is_file()
    with gzip.open(cached, "rb") as handle:
        assert handle.read() == body

    # Second call hits cache without network.
    calls = {"n": 0}

    def boom(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise AssertionError("network should not be used on cache hit")

    client2 = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(boom),
        robots_disallow=False,
    )
    text2, sha2 = client2.get_text("http://www.ufcstats.com/event-details/abc")
    assert text2 == html
    assert sha2 == digest
    assert calls["n"] == 0


def test_stop_on_403(tmp_path: Path) -> None:
    from mma_model.sources.http.block_signals import SourceBlockedError
    from mma_model.sources.http.polite_client import PoliteHttpClient

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

    def fake_send(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden", request=request)

    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(fake_send),
        robots_disallow=False,
    )
    with pytest.raises(SourceBlockedError, match="http_403") as exc:
        client.get_text("http://www.ufcstats.com/event-details/abc")
    assert exc.value.status_code == 403
    assert exc.value.host == "ufcstats.com"


def test_retry_exhaustion_on_503(tmp_path: Path) -> None:
    from mma_model.sources.http.block_signals import SourceBlockedError
    from mma_model.sources.http.polite_client import PoliteHttpClient

    politeness = load_http_politeness(POLITE_PATH)
    host = politeness.hosts["ufcstats.com"]
    object.__setattr__(
        politeness,
        "hosts",
        type(politeness.hosts)(
            {
                **dict(politeness.hosts),
                "ufcstats.com": host.model_copy(
                    update={
                        "min_delay_sec": 0.0,
                        "max_retries": 2,
                        "backoff_base_sec": 0.001,
                        "backoff_cap_sec": 0.002,
                    }
                ),
            }
        ),
    )
    calls = {"n": 0}

    def fake_send(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="unavailable", request=request)

    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(fake_send),
        robots_disallow=False,
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(SourceBlockedError, match="http_503|retry_exhausted"):
        client.get_text("http://www.ufcstats.com/event-details/abc")
    assert calls["n"] == 3  # initial + 2 retries


def test_url_host_allowlist_rejects_other_host(tmp_path: Path) -> None:
    from mma_model.sources.http.polite_client import PoliteHttpClient, UrlNotAllowedError

    politeness = load_http_politeness(POLITE_PATH)
    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        robots_disallow=False,
    )
    with pytest.raises(UrlNotAllowedError, match="host"):
        client.get_text("https://evil.example/event-details/abc")


def test_url_path_allowlist_rejects_disallowed_path(tmp_path: Path) -> None:
    from mma_model.sources.http.polite_client import PoliteHttpClient, UrlNotAllowedError

    politeness = load_http_politeness(POLITE_PATH)
    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        robots_disallow=False,
    )
    with pytest.raises(UrlNotAllowedError, match="path"):
        client.get_text("http://www.ufcstats.com/admin/secret")


def test_redirect_off_host_rejected(tmp_path: Path) -> None:
    from mma_model.sources.http.polite_client import PoliteHttpClient, UrlNotAllowedError

    politeness = load_http_politeness(POLITE_PATH)
    host = politeness.hosts["ufcstats.com"]
    object.__setattr__(
        politeness,
        "hosts",
        type(politeness.hosts)(
            {
                **dict(politeness.hosts),
                "ufcstats.com": host.model_copy(update={"min_delay_sec": 0.0}),
            }
        ),
    )

    def fake_send(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://login.example/captcha"},
            request=request,
        )

    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(fake_send),
        robots_disallow=False,
    )
    with pytest.raises(UrlNotAllowedError, match="redirect|host"):
        client.get_text("http://www.ufcstats.com/event-details/abc")


def test_cache_hash_corruption_raises(tmp_path: Path) -> None:
    from mma_model.sources.http.polite_client import CacheCorruptionError, PoliteHttpClient

    html = (FIXTURES / "ok.html").read_text(encoding="utf-8")
    body = html.encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    url = "http://www.ufcstats.com/event-details/abc"
    cache_dir = tmp_path / "cache" / "ufcstats.com"
    cache_dir.mkdir(parents=True)
    cache_path = cache_dir / f"{digest}.gz"
    with gzip.open(cache_path, "wb") as handle:
        handle.write(b"tampered-bytes")
    (cache_dir / "url_index.json").write_text(
        json.dumps({url: digest}), encoding="utf-8"
    )

    politeness = load_http_politeness(POLITE_PATH)
    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError())),
        robots_disallow=False,
    )
    with pytest.raises(CacheCorruptionError):
        client.get_text(url)


def test_robots_disallow_stops_before_network(tmp_path: Path) -> None:
    from mma_model.sources.http.block_signals import SourceBlockedError
    from mma_model.sources.http.polite_client import PoliteHttpClient

    politeness = load_http_politeness(POLITE_PATH)
    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError())),
        robots_disallow=True,
    )
    with pytest.raises(SourceBlockedError, match="robots_disallow"):
        client.get_text("http://www.ufcstats.com/event-details/abc")


def test_no_cookies_or_auth_headers(tmp_path: Path) -> None:
    from mma_model.sources.http.polite_client import PoliteHttpClient

    html = b"<html>ok</html>"
    seen: dict[str, Any] = {}

    def fake_send(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["cookies"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=html, request=request)

    politeness = load_http_politeness(POLITE_PATH)
    host = politeness.hosts["ufcstats.com"]
    object.__setattr__(
        politeness,
        "hosts",
        type(politeness.hosts)(
            {
                **dict(politeness.hosts),
                "ufcstats.com": host.model_copy(update={"min_delay_sec": 0.0}),
            }
        ),
    )
    client = PoliteHttpClient(
        host="ufcstats.com",
        politeness=politeness,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(fake_send),
        robots_disallow=False,
    )
    client.get_text("http://www.ufcstats.com/event-details/abc")
    assert seen["cookies"] is None
    assert seen["authorization"] is None
    assert "User-Agent" in {k.title() for k in seen["headers"]} or "user-agent" in seen[
        "headers"
    ]
