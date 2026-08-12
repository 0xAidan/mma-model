"""RFC 9309 robots.txt response handling and directive evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import ParseResult, unquote, urljoin, urlparse, urlunparse

MAX_ROBOTS_REDIRECTS = 5
_DEFAULT_PORT_BY_SCHEME: dict[str, int] = {"http": 80, "https": 443}
# Narrow ASCII DNS hostname: labels of [a-z0-9-], no trailing dot, no IP, no IDN.
_ASCII_DNS_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+$"
)
_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class RobotsRedirectError(ValueError):
    """Fail-closed robots redirect policy violation with a typed reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


@dataclass(frozen=True)
class RobotsAccessDecision:
    allowed: bool
    policy_decision: str
    robots_status_code: int | None
    detail: str
    target_path: str
    matched_user_agent_group: str | None = None
    standard: str = "RFC9309"

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "policy_decision": self.policy_decision,
            "robots_status_code": self.robots_status_code,
            "detail": self.detail,
            "target_path": self.target_path,
            "matched_user_agent_group": self.matched_user_agent_group,
            "standard": self.standard,
        }


@dataclass(frozen=True)
class _Rule:
    path: str
    allow: bool


@dataclass(frozen=True)
class _AgentGroup:
    agents: tuple[str, ...]
    rules: tuple[_Rule, ...]


def _path_of(target_url: str) -> str:
    parsed = urlparse(target_url)
    path = unquote(parsed.path or "/")
    if not path.startswith("/"):
        path = "/" + path
    return path


def allowed_robots_hosts(configured_host: str) -> frozenset[str]:
    """Exact canonical ASCII host set for robots redirects.

    Narrow configured↔www rule only:
    - Take the configured host, ASCII-lowercase.
    - If it begins with a single ``www.`` label, the bare remainder is the apex.
    - Otherwise the configured host is the apex and ``www.{apex}`` is added.
    - No other prefixes, trailing dots, IDN, IPs, or multi-``www`` forms.
    """
    host = configured_host.strip().lower()
    if not _is_exact_ascii_dns_hostname(host):
        raise RobotsRedirectError(
            "robots_redirect_off_host",
            f"configured robots host is not a canonical ASCII DNS name: {configured_host!r}",
        )
    if host.startswith("www."):
        apex = host[4:]
        if not _is_exact_ascii_dns_hostname(apex) or apex.startswith("www."):
            raise RobotsRedirectError(
                "robots_redirect_off_host",
                f"configured robots host www canonicalization failed: {configured_host!r}",
            )
    else:
        apex = host
    return frozenset({apex, f"www.{apex}"})


def _is_exact_ascii_dns_hostname(hostname: str) -> bool:
    """True only for a strict ASCII DNS hostname (no IP/IDN/trailing-dot/%)."""
    if not hostname or "%" in hostname or hostname.endswith("."):
        return False
    if any(ord(ch) > 127 for ch in hostname):
        return False
    if "[" in hostname or "]" in hostname:
        return False
    lowered = hostname.lower()
    if "xn--" in lowered:
        return False
    if _IPV4_RE.fullmatch(lowered):
        return False
    return _ASCII_DNS_HOST_RE.fullmatch(lowered) is not None


def normalize_robots_url(url: str) -> str:
    """Normalize a robots URL for loop detection (no fragment; lower host).

    Default scheme ports are omitted from the authority; non-default ports are
    preserved only for loop-keying of rejected URLs (callers must validate first).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    netloc = host
    try:
        port = parsed.port
    except ValueError:
        port = None
    default = _DEFAULT_PORT_BY_SCHEME.get(scheme)
    if port is not None and port != default:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _validate_robots_redirect_port(*, scheme: str, parsed: ParseResult) -> None:
    default = _DEFAULT_PORT_BY_SCHEME[scheme]
    netloc = parsed.netloc or ""
    # Empty or non-numeric port suffixes are invalid (urlparse may raise or drop).
    if ":" in netloc and not netloc.startswith("["):
        port_text = netloc.rsplit(":", 1)[1]
        if port_text == "" or not port_text.isdigit():
            raise RobotsRedirectError(
                "robots_redirect_invalid_port",
                f"robots redirect invalid port in authority: {netloc!r}",
            )
    try:
        port = parsed.port
    except ValueError as exc:
        raise RobotsRedirectError(
            "robots_redirect_invalid_port",
            f"robots redirect invalid port: {exc}",
        ) from exc
    if port is not None and port != default:
        raise RobotsRedirectError(
            "robots_redirect_non_default_port",
            f"robots redirect port {port} not default {default} for {scheme}",
        )


def resolve_robots_redirect_url(
    *,
    current_url: str,
    location: str | None,
    configured_host: str,
    visited: set[str],
    redirect_count: int,
) -> str:
    """Resolve one robots redirect hop under the bounded same-host policy.

    Allows at most ``MAX_ROBOTS_REDIRECTS`` hops, only to the exact canonical
    host set (configured apex + single ``www.`` twin) over http/https with
    default ports only (implicit/explicit 80 or 443). Relative ``Location``
    values are resolved against ``current_url``.
    """
    if redirect_count >= MAX_ROBOTS_REDIRECTS:
        raise RobotsRedirectError(
            "robots_redirect_hop_limit",
            f"robots redirect exceeded {MAX_ROBOTS_REDIRECTS} hops",
        )
    if location is None or not str(location).strip():
        raise RobotsRedirectError(
            "robots_redirect_missing_location",
            "robots redirect missing Location header",
        )

    raw_location = str(location).strip()
    try:
        joined = urljoin(current_url, raw_location)
        parsed = urlparse(joined)
    except ValueError as exc:
        raise RobotsRedirectError(
            "robots_redirect_malformed_location",
            f"robots redirect malformed Location: {raw_location!r}",
        ) from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _DEFAULT_PORT_BY_SCHEME:
        raise RobotsRedirectError(
            "robots_redirect_non_http",
            f"robots redirect scheme not http(s): {parsed.scheme!r}",
        )
    if parsed.username is not None or parsed.password is not None:
        raise RobotsRedirectError(
            "robots_redirect_userinfo",
            "robots redirect must not include userinfo",
        )
    netloc = parsed.netloc or ""
    if "@" in netloc:
        raise RobotsRedirectError(
            "robots_redirect_userinfo",
            "robots redirect must not include userinfo",
        )
    if netloc.startswith("["):
        raise RobotsRedirectError(
            "robots_redirect_off_host",
            "robots redirect IPv6 literals are not allowed",
        )

    hostname = parsed.hostname or ""
    if not hostname:
        raise RobotsRedirectError(
            "robots_redirect_malformed_location",
            f"robots redirect malformed Location: {raw_location!r}",
        )
    if not _is_exact_ascii_dns_hostname(hostname):
        raise RobotsRedirectError(
            "robots_redirect_off_host",
            f"robots redirect host not canonical ASCII DNS: {hostname!r}",
        )
    canonical_host = hostname.lower()
    if canonical_host not in allowed_robots_hosts(configured_host):
        raise RobotsRedirectError(
            "robots_redirect_off_host",
            f"robots redirect host not allowed: {canonical_host!r}",
        )

    _validate_robots_redirect_port(scheme=scheme, parsed=parsed)

    # Rebuild with lowercase host and omitted default port (no open redirect tricks).
    path = parsed.path or "/"
    normalized = urlunparse((scheme, canonical_host, path, "", parsed.query, ""))
    if normalized in visited:
        raise RobotsRedirectError(
            "robots_redirect_loop",
            f"robots redirect loop detected at {normalized!r}",
        )
    return normalized


def _product_token(user_agent: str) -> str:
    token = user_agent.strip().split()[0] if user_agent.strip() else "*"
    return token.split("/")[0].lower()


def _parse_groups(body_text: str) -> list[_AgentGroup]:
    """Parse robots groups without unioning unrelated User-agent blocks."""
    groups: list[_AgentGroup] = []
    current_agents: list[str] = []
    current_rules: list[_Rule] = []
    saw_rules = False

    def _flush() -> None:
        nonlocal current_agents, current_rules, saw_rules
        if current_agents:
            groups.append(
                _AgentGroup(agents=tuple(current_agents), rules=tuple(current_rules))
            )
        current_agents = []
        current_rules = []
        saw_rules = False

    for raw in body_text.splitlines():
        # Ignore BOM / NUL for resilience; keep text parseable.
        line = raw.replace("\x00", "").strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        field, value = line.split(":", 1)
        field_l = field.strip().lower()
        value = value.strip()
        if field_l == "user-agent":
            agent = value.lower()
            if not agent:
                continue
            if saw_rules:
                _flush()
            current_agents.append(agent)
            continue
        if field_l in {"allow", "disallow"}:
            if not current_agents:
                # Orphan rules apply to '*' per common practice.
                current_agents = ["*"]
            # Empty Disallow means allow all; empty Allow is ignored.
            if field_l == "disallow" and value == "":
                saw_rules = True
                continue
            if field_l == "allow" and value == "":
                saw_rules = True
                continue
            current_rules.append(_Rule(path=value, allow=(field_l == "allow")))
            saw_rules = True
            continue
        # Unknown fields ignored (Crawl-delay etc. not used here).
    _flush()
    return groups


def _agent_matches_specific(agent: str, user_agent: str, product: str) -> bool:
    """True when agent is a non-wildcard token matching the configured UA."""
    if agent == "*":
        return False
    ua_l = user_agent.lower()
    return ua_l.startswith(agent) or product == agent


def _collect_matching_rules(
    groups: list[_AgentGroup], user_agent: str
) -> tuple[tuple[_Rule, ...], str | None]:
    """Merge rules from all groups for the most-specific matching UA token.

    RFC 9309 / common robots practice: when several record-groups share the same
    matching User-agent token, their rules are combined. Wildcard ``*`` groups
    are used only when no specific group matches the configured UA.
    """
    product = _product_token(user_agent)
    specific_rules: list[_Rule] = []
    star_rules: list[_Rule] = []
    matched_specific: str | None = None

    for group in groups:
        group_is_specific = False
        group_is_star = False
        for agent in group.agents:
            if agent == "*":
                group_is_star = True
                continue
            if _agent_matches_specific(agent, user_agent, product):
                group_is_specific = True
                # Prefer the product token label when present; else first match.
                if matched_specific is None or agent == product:
                    matched_specific = agent
        if group_is_specific:
            specific_rules.extend(group.rules)
        elif group_is_star:
            # Only collect '*' rules for fallback; never mix with specific.
            star_rules.extend(group.rules)

    if matched_specific is not None:
        return tuple(specific_rules), matched_specific
    if star_rules:
        return tuple(star_rules), "*"
    return (), None


def _path_matches(rule_path: str, request_path: str) -> bool:
    if rule_path == "":
        return False
    # Trailing "$" end-anchor is uncommon; support prefix match (RFC/Google style).
    if rule_path.endswith("$"):
        return request_path == rule_path[:-1]
    return request_path.startswith(rule_path)


def _allowed_by_rules(rules: tuple[_Rule, ...], request_path: str) -> bool:
    """Longest-path match; Allow wins on equal specificity."""
    matches = [rule for rule in rules if _path_matches(rule.path, request_path)]
    if not matches:
        return True
    best_len = max(len(rule.path) for rule in matches)
    best = [rule for rule in matches if len(rule.path) == best_len]
    # Equal length: Allow precedence.
    if any(rule.allow for rule in best):
        return True
    return False


def evaluate_robots_access(
    *,
    status_code: int | None,
    body_text: str,
    user_agent: str,
    target_url: str,
    network_error: str | None = None,
    redirect_error: str | None = None,
) -> RobotsAccessDecision:
    """Evaluate robots access using RFC 9309 status semantics + focused parsing.

    Status handling (RFC 9309 §2.3.1) applies to the **final** response after
    bounded same-host redirect retrieval:
    - 2xx: parse directives for the configured UA (with ``*`` fallback)
    - 401/403: complete disallow
    - 404/410: treat as no robots file → allow all
    - other 4xx (unavailable file): allow all
    - 5xx / network unreachable: temporary complete disallow (fail closed)
    - redirect policy violations: fail closed with typed ``robots_redirect_*``

    Parsing: merge all groups matching the most-specific configured UA token
    (``*`` only if none match); do not union unrelated agents; longest-path
    match with Allow-on-ties across the merged rule set.
    """
    path = _path_of(target_url)

    if redirect_error:
        return RobotsAccessDecision(
            allowed=False,
            policy_decision=redirect_error,
            robots_status_code=status_code,
            detail=f"robots redirect policy fail-closed: {redirect_error}",
            target_path=path,
        )

    if network_error:
        return RobotsAccessDecision(
            allowed=False,
            policy_decision="rfc9309_network_unreachable_temporary_disallow",
            robots_status_code=status_code,
            detail=f"robots fetch network error: {network_error}",
            target_path=path,
        )

    if status_code is None:
        return RobotsAccessDecision(
            allowed=False,
            policy_decision="rfc9309_network_unreachable_temporary_disallow",
            robots_status_code=None,
            detail="robots status missing; temporary complete disallow",
            target_path=path,
        )

    if 500 <= status_code <= 599:
        return RobotsAccessDecision(
            allowed=False,
            policy_decision="rfc9309_http_5xx_temporary_disallow",
            robots_status_code=status_code,
            detail="RFC 9309: 5xx robots response → temporary complete disallow",
            target_path=path,
        )

    if status_code in {401, 403}:
        return RobotsAccessDecision(
            allowed=False,
            policy_decision="rfc9309_http_401_403_complete_disallow",
            robots_status_code=status_code,
            detail="RFC 9309: 401/403 robots response → complete disallow",
            target_path=path,
        )

    if status_code in {404, 410} or (400 <= status_code <= 499):
        label = (
            "rfc9309_http_404_410_allow_all"
            if status_code in {404, 410}
            else "rfc9309_http_4xx_unavailable_allow_all"
        )
        return RobotsAccessDecision(
            allowed=True,
            policy_decision=label,
            robots_status_code=status_code,
            detail=(
                "RFC 9309: unavailable robots file (4xx) → proceed with no crawl "
                "restrictions from robots.txt"
            ),
            target_path=path,
        )

    if not (200 <= status_code <= 299):
        return RobotsAccessDecision(
            allowed=False,
            policy_decision="rfc9309_unexpected_status_fail_closed",
            robots_status_code=status_code,
            detail=f"unexpected robots status {status_code}; fail closed",
            target_path=path,
        )

    try:
        groups = _parse_groups(body_text)
        rules, matched = _collect_matching_rules(groups, user_agent)
        if matched is None:
            return RobotsAccessDecision(
                allowed=True,
                policy_decision="rfc9309_parsed_allow",
                robots_status_code=status_code,
                detail="RFC 9309: no matching User-agent group; default allow",
                target_path=path,
                matched_user_agent_group=None,
            )
        allowed = _allowed_by_rules(rules, path)
        return RobotsAccessDecision(
            allowed=allowed,
            policy_decision=(
                "rfc9309_parsed_allow" if allowed else "rfc9309_parsed_disallow"
            ),
            robots_status_code=status_code,
            detail=(
                "RFC 9309: parsed robots directives for configured UA "
                f"(group={matched!r}, merged_rules={len(rules)})"
            ),
            target_path=path,
            matched_user_agent_group=matched,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on parse blow-ups
        return RobotsAccessDecision(
            allowed=False,
            policy_decision="rfc9309_parse_error_fail_closed",
            robots_status_code=status_code,
            detail=f"robots parse error: {type(exc).__name__}: {exc}",
            target_path=path,
        )
