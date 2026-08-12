"""RFC 9309 robots.txt response handling and directive evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse


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


def _select_group(
    groups: list[_AgentGroup], user_agent: str
) -> tuple[_AgentGroup | None, str | None]:
    product = _product_token(user_agent)
    ua_l = user_agent.lower()
    star: _AgentGroup | None = None
    for group in groups:
        for agent in group.agents:
            if agent == "*":
                star = group
                continue
            if ua_l.startswith(agent) or product == agent:
                return group, agent
    if star is not None:
        return star, "*"
    return None, None


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
) -> RobotsAccessDecision:
    """Evaluate robots access using RFC 9309 status semantics + focused parsing.

    Status handling (RFC 9309 §2.3.1):
    - 2xx: parse directives for the configured UA (with ``*`` fallback)
    - 401/403: complete disallow
    - 404/410: treat as no robots file → allow all
    - other 4xx (unavailable file): allow all
    - 5xx / network unreachable: temporary complete disallow (fail closed)

    Parsing: select one User-agent group (exact product token, else ``*``);
    do not union unrelated agents; longest-path match with Allow-on-ties.
    """
    path = _path_of(target_url)

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
        group, matched = _select_group(groups, user_agent)
        if group is None:
            return RobotsAccessDecision(
                allowed=True,
                policy_decision="rfc9309_parsed_allow",
                robots_status_code=status_code,
                detail="RFC 9309: no matching User-agent group; default allow",
                target_path=path,
                matched_user_agent_group=None,
            )
        allowed = _allowed_by_rules(group.rules, path)
        return RobotsAccessDecision(
            allowed=allowed,
            policy_decision=(
                "rfc9309_parsed_allow" if allowed else "rfc9309_parsed_disallow"
            ),
            robots_status_code=status_code,
            detail=(
                "RFC 9309: parsed robots directives for configured UA "
                f"(group={matched!r})"
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
