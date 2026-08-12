"""Polite, fail-closed HTTP client with content-addressed cache (DWCS-102)."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from mma_model.sources.http.block_signals import SourceBlockedError, detect_block_signal
from mma_model.sources.http_politeness import HttpPolitenessConfig, HostPoliteness


class UrlNotAllowedError(ValueError):
    """Raised when a URL host/path is outside the configured allowlist."""


class CacheCorruptionError(RuntimeError):
    """Raised when on-disk HTTP cache bytes do not match the expected hash."""


SleepFn = Callable[[float], None]


class PoliteHttpClient:
    """Single-host polite GET client: delay, UA, cache, block-signal stop."""

    def __init__(
        self,
        *,
        host: str,
        politeness: HttpPolitenessConfig,
        cache_dir: Path,
        transport: httpx.BaseTransport | None = None,
        robots_disallow: bool = False,
        sleep_fn: SleepFn | None = None,
        timeout_sec: float = 60.0,
    ) -> None:
        if host not in politeness.hosts:
            raise UrlNotAllowedError(f"host {host!r} not in politeness config")
        self.host = host
        self.politeness = politeness
        self.host_cfg: HostPoliteness = politeness.hosts[host]
        self.cache_dir = Path(cache_dir) / host
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.robots_disallow = robots_disallow
        self._sleep = sleep_fn or time.sleep
        self._last_request_at = 0.0
        self._client = httpx.Client(
            transport=transport,
            timeout=timeout_sec,
            headers={
                "User-Agent": politeness.user_agent,
                "From": politeness.contact,
            },
            follow_redirects=False,
            cookies=httpx.Cookies(),
        )

    def close(self) -> None:
        self._client.close()

    def get_text(self, url: str) -> tuple[str, str]:
        """Return ``(text, sha256_hex)`` with politeness, cache, and block stops."""
        self._assert_url_allowed(url)
        if self.robots_disallow:
            raise SourceBlockedError(
                "robots_disallow", host=self.host, status_code=None
            )

        cached = self._read_url_cache(url)
        if cached is not None:
            return cached

        attempts = self.host_cfg.max_retries + 1
        last_status: int | None = None
        last_body = ""
        for attempt in range(attempts):
            self._wait_delay()
            response = self._client.get(url)
            last_status = response.status_code
            if response.is_redirect:
                location = response.headers.get("location") or ""
                self._assert_redirect_allowed(location)
                # Same-host redirects are not followed automatically in this client.
                raise UrlNotAllowedError(
                    f"refusing to follow redirect for {url!r} -> {location!r}"
                )

            body_bytes = response.content
            last_body = body_bytes.decode("utf-8", errors="replace")

            if response.status_code in {429, 503} and attempt < attempts - 1:
                delay = min(
                    self.host_cfg.backoff_cap_sec,
                    self.host_cfg.backoff_base_sec * (2**attempt),
                )
                self._sleep(delay)
                continue

            reason = detect_block_signal(
                response.status_code, last_body, robots_disallow=False
            )
            if reason is not None:
                if (
                    reason in {"http_429", "http_503"}
                    and attempt >= attempts - 1
                ):
                    raise SourceBlockedError(
                        f"{reason}:retry_exhausted",
                        host=self.host,
                        status_code=response.status_code,
                    )
                raise SourceBlockedError(
                    reason, host=self.host, status_code=response.status_code
                )

            if response.status_code != 200:
                raise SourceBlockedError(
                    f"http_{response.status_code}",
                    host=self.host,
                    status_code=response.status_code,
                )

            digest = hashlib.sha256(body_bytes).hexdigest()
            self._write_cache(url, digest, body_bytes)
            return last_body, digest

        raise SourceBlockedError(
            f"http_{last_status}:retry_exhausted",
            host=self.host,
            status_code=last_status,
        )

    def _wait_delay(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.host_cfg.min_delay_sec - elapsed
        if remaining > 0:
            self._sleep(remaining)
        self._last_request_at = time.monotonic()

    def _assert_url_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        netloc = (parsed.hostname or "").lower()
        if netloc != self.host and not netloc.endswith("." + self.host):
            # Allow www.<host> only when configured host is bare domain.
            if not (netloc == f"www.{self.host}"):
                raise UrlNotAllowedError(f"url host not allowed: {netloc!r}")
        path = parsed.path or "/"
        prefixes = self.host_cfg.allowed_path_prefixes
        if prefixes and not any(path.startswith(prefix) for prefix in prefixes):
            raise UrlNotAllowedError(f"url path not allowed: {path!r}")

    def _assert_redirect_allowed(self, location: str) -> None:
        if not location:
            raise UrlNotAllowedError("redirect missing location")
        parsed = urlparse(location)
        if not parsed.scheme:
            # Relative redirect — same host.
            return
        netloc = (parsed.hostname or "").lower()
        if netloc != self.host and netloc != f"www.{self.host}":
            raise UrlNotAllowedError(f"redirect host not allowed: {netloc!r}")

    def _index_path(self) -> Path:
        return self.cache_dir / "url_index.json"

    def _load_index(self) -> dict[str, str]:
        path = self._index_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}

    def _save_index(self, index: dict[str, str]) -> None:
        path = self._index_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _cache_path(self, digest: str) -> Path:
        return self.cache_dir / f"{digest}.gz"

    def _read_url_cache(self, url: str) -> tuple[str, str] | None:
        index = self._load_index()
        digest = index.get(url)
        if not digest:
            return None
        path = self._cache_path(digest)
        if not path.is_file():
            return None
        try:
            with gzip.open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            raise CacheCorruptionError(f"unreadable cache for {digest}") from exc
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise CacheCorruptionError(
                f"content hash mismatch for {digest}: got {actual}"
            )
        return data.decode("utf-8", errors="replace"), digest

    def _write_cache(self, url: str, digest: str, body: bytes) -> None:
        target = self._cache_path(digest)
        if not target.exists():
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{digest}.", suffix=".tmp", dir=self.cache_dir
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as gz:
                        gz.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, target)
            except Exception:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                raise
        index = self._load_index()
        index[url] = digest
        self._save_index(index)
