"""RFC 9309 robots status/parsing tests (DWCS-102 re-review)."""

from __future__ import annotations

from mma_model.sources.http_politeness import load_http_politeness

UA = load_http_politeness().user_agent


def test_robots_404_means_access_allowed_not_bypass() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    decision = evaluate_robots_access(
        status_code=404,
        body_text="Not Found",
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.policy_decision == "rfc9309_http_404_410_allow_all"
    assert decision.robots_status_code == 404


def test_robots_410_means_access_allowed() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    decision = evaluate_robots_access(
        status_code=410,
        body_text="Gone",
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.policy_decision == "rfc9309_http_404_410_allow_all"


def test_robots_5xx_temporary_complete_disallow() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    decision = evaluate_robots_access(
        status_code=503,
        body_text="unavailable",
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is False
    assert decision.policy_decision == "rfc9309_http_5xx_temporary_disallow"


def test_robots_network_unreachable_fail_closed() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    decision = evaluate_robots_access(
        status_code=None,
        body_text="",
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
        network_error="ConnectError",
    )
    assert decision.allowed is False
    assert decision.policy_decision == "rfc9309_network_unreachable_temporary_disallow"


def test_robots_unrelated_badbot_disallow_does_not_apply() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: BadBot
Disallow: /

User-agent: *
Allow: /
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.policy_decision == "rfc9309_parsed_allow"


def test_robots_wildcard_fallback_applies_when_no_exact_agent() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: *
Disallow: /statistics/
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is False
    assert decision.policy_decision == "rfc9309_parsed_disallow"


def test_robots_exact_configured_agent_group_selected() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: *
Disallow: /statistics/

User-agent: mma-model-research
Allow: /statistics/events/
Disallow: /admin/
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.matched_user_agent_group in {"mma-model-research", "mma-model-research/0.1"}


def test_robots_allow_vs_disallow_longest_path_precedence() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: *
Disallow: /statistics/
Allow: /statistics/events/completed
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True

    body2 = """User-agent: *
Allow: /statistics/events/
Disallow: /statistics/events/completed
"""
    decision2 = evaluate_robots_access(
        status_code=200,
        body_text=body2,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision2.allowed is False


def test_robots_empty_rules_allow_all() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    decision = evaluate_robots_access(
        status_code=200,
        body_text="\n# comment only\n",
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.policy_decision == "rfc9309_parsed_allow"


def test_robots_malformed_file_fail_closed_or_parse_safe() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    decision = evaluate_robots_access(
        status_code=200,
        body_text="this is not robots\x00\xff totally broken {{{",
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    # Malformed 2xx must not raise; either allow via empty parse or explicit parse_error disallow.
    assert decision.policy_decision in {
        "rfc9309_parsed_allow",
        "rfc9309_parse_error_fail_closed",
    }
    assert isinstance(decision.allowed, bool)


def test_robots_401_403_complete_disallow() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    for code in (401, 403):
        decision = evaluate_robots_access(
            status_code=code,
            body_text="denied",
            user_agent=UA,
            target_url="http://www.ufcstats.com/statistics/events/completed",
        )
        assert decision.allowed is False
        assert decision.policy_decision == "rfc9309_http_401_403_complete_disallow"


def test_robots_repeated_same_ua_groups_merged_allow_then_disallow() -> None:
    """Later same-UA Disallow must not be skipped when an earlier group matched first."""
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: mma-model-research
Allow: /

User-agent: mma-model-research
Disallow: /statistics/
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is False
    assert decision.policy_decision == "rfc9309_parsed_disallow"
    assert decision.matched_user_agent_group == "mma-model-research"


def test_robots_repeated_same_ua_groups_merged_disallow_then_allow() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: mma-model-research
Disallow: /statistics/

User-agent: mma-model-research
Allow: /statistics/events/completed
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.policy_decision == "rfc9309_parsed_allow"


def test_robots_multiple_wildcard_groups_merged() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body_allow_then_disallow = """User-agent: *
Allow: /

User-agent: *
Disallow: /statistics/
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body_allow_then_disallow,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is False
    assert decision.matched_user_agent_group == "*"

    # Reverse order: last-group-wins would incorrectly Allow:/; merge + longest path must Disallow.
    body_disallow_then_allow = """User-agent: *
Disallow: /statistics/

User-agent: *
Allow: /
"""
    decision2 = evaluate_robots_access(
        status_code=200,
        body_text=body_disallow_then_allow,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision2.allowed is False
    assert decision2.matched_user_agent_group == "*"


def test_robots_specific_group_ignores_wildcard_even_if_more_restrictive() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: *
Disallow: /

User-agent: mma-model-research
Allow: /statistics/
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.matched_user_agent_group == "mma-model-research"

    body_reverse = """User-agent: mma-model-research
Allow: /statistics/

User-agent: *
Disallow: /
"""
    decision2 = evaluate_robots_access(
        status_code=200,
        body_text=body_reverse,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision2.allowed is True
    assert decision2.matched_user_agent_group == "mma-model-research"


def test_robots_mixed_casing_user_agent_groups_merged() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: MMA-Model-Research
Disallow: /statistics/

User-agent: mma-model-research
Allow: /statistics/events/completed
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.matched_user_agent_group == "mma-model-research"


def test_robots_empty_same_ua_group_then_rules_merged() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: mma-model-research

User-agent: mma-model-research
Disallow: /statistics/
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is False
    assert decision.policy_decision == "rfc9309_parsed_disallow"


def test_robots_contradictory_equal_length_allow_wins_across_merged_groups() -> None:
    from mma_model.sources.http.robots import evaluate_robots_access

    body = """User-agent: mma-model-research
Disallow: /statistics/events/

User-agent: mma-model-research
Allow: /statistics/events/
"""
    decision = evaluate_robots_access(
        status_code=200,
        body_text=body,
        user_agent=UA,
        target_url="http://www.ufcstats.com/statistics/events/completed",
    )
    assert decision.allowed is True
    assert decision.policy_decision == "rfc9309_parsed_allow"
