"""Login/paywall/block detection for public regional pages (no auth bypass)."""

from __future__ import annotations

from mma_model.history.constants import LOGIN_WALL_MARKERS, PAYWALL_MARKERS
from mma_model.sources.http.block_signals import SourceBlockedError, detect_block_signal
from mma_model.sources.http.polite_client import PoliteHttpClient, UrlNotAllowedError


def detect_login_wall(html: str) -> str | None:
    """Return a typed kill reason when a page is an auth/login portal."""
    lowered = (html or "").lower()
    if 'data-access="login_required"' in lowered:
        return "login_wall"
    if "app.combatreg.com" in lowered and ("sign in" in lowered or "log in" in lowered):
        return "login_wall"
    for marker in LOGIN_WALL_MARKERS:
        if marker in lowered:
            return "login_wall"
    if "<input" in lowered and 'type="password"' in lowered:
        return "login_wall"
    return None


def detect_paywall(html: str) -> str | None:
    lowered = (html or "").lower()
    for marker in PAYWALL_MARKERS:
        if marker in lowered:
            return "paywall"
    return None


def detect_access_kill(html: str) -> str | None:
    """Hard-stop reason for login, paywall, CAPTCHA, or Cloudflare HTML."""
    login = detect_login_wall(html)
    if login:
        return login
    paywall = detect_paywall(html)
    if paywall:
        return paywall
    return detect_block_signal(200, html, False)


def public_get_text(
    client: PoliteHttpClient,
    url: str,
    *,
    host: str,
) -> tuple[str, str]:
    """Fetch an allowlisted public page; redirects are a typed kill, not followed."""
    try:
        return client.get_text(url)
    except UrlNotAllowedError as exc:
        raise SourceBlockedError("http_redirect_refused", host=host) from exc
