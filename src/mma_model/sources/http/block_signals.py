"""Block-signal detection for public HTTP adapters (DWCS-102)."""

from __future__ import annotations


class SourceBlockedError(RuntimeError):
    """Raised when a public source must stop due to access/block signals."""

    def __init__(
        self,
        reason: str,
        *,
        host: str,
        status_code: int | None = None,
    ) -> None:
        self.reason = reason
        self.host = host
        self.status_code = status_code
        super().__init__(f"source blocked host={host!r} reason={reason!r} status={status_code}")


def detect_block_signal(
    status_code: int | None,
    body_text: str,
    robots_disallow: bool,
) -> str | None:
    """Return a kill reason string when crawling must stop immediately."""
    if robots_disallow:
        return "robots_disallow"
    if status_code == 403:
        return "http_403"
    if status_code == 429:
        return "http_429"
    if status_code == 503:
        return "http_503"
    lowered = (body_text or "").lower()
    if "captcha" in lowered:
        return "captcha_interstitial"
    if "cf-browser-verification" in lowered or "cf-challenge" in lowered:
        return "cloudflare_challenge"
    if "access denied" in lowered:
        return "access_denied"
    return None
