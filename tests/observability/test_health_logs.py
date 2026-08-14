"""DWCS-403 observability tests: redaction, health, LKG publish, retries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mma_model.cli import main
from mma_model.jobs.types import (
    DEFAULT_MAX_TRANSIENT_ATTEMPTS,
    NON_RETRYABLE_ERRORS,
    JobErrorClass,
)
from mma_model.observability.errors import (
    BoundedRetryPolicy,
    is_non_retryable,
    is_retryable,
)
from mma_model.observability.health import (
    HEALTH_COMPONENT_NAMES,
    HealthSeverity,
    HealthStatus,
    build_health_report,
    dumps_health,
    make_component,
    severity_for,
    validate_health_json,
)
from mma_model.observability.logging import (
    build_log_record,
    format_log_line,
    log_event,
    redact_secrets,
)
from mma_model.observability.publish_guard import (
    FilesystemPublishPointer,
    PublishValidationError,
)
from mma_model.quality.constants import EXIT_OK, EXIT_STRICT_BLOCKERS

FAKE_API_KEY = "sk_live_test_odds_key_ABC123XYZ789"
FAKE_BEARER = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.fake.signature"
FAKE_PASSWORD = "s3cret-passw0rd"
FAKE_ODDS_ENV = f"THE_ODDS_API_KEY={FAKE_API_KEY}"
FAKE_SQLITE_URL = f"sqlite://user:{FAKE_PASSWORD}@/tmp/fixture.db"


def _all_healthy(*, as_of: str = "2026-08-13T00:00:00Z") -> list:
    return [
        make_component(name, HealthStatus.HEALTHY, detail=f"{name} ok", as_of=as_of)
        for name in HEALTH_COMPONENT_NAMES
    ]


def test_secret_redaction_in_logs_and_health() -> None:
    dirty_message = (
        f"fetch failed Authorization: Bearer {FAKE_BEARER} "
        f"api_key={FAKE_API_KEY} {FAKE_ODDS_ENV} url={FAKE_SQLITE_URL}"
    )
    record = build_log_record(
        level="ERROR",
        message=dirty_message,
        error_class=JobErrorClass.AUTHENTICATION,
        extra={"exception": f"OddsApiError: password={FAKE_PASSWORD}"},
        timestamp="2026-08-13T12:00:00Z",
    )
    line = format_log_line(record)
    for secret in (FAKE_API_KEY, FAKE_BEARER, FAKE_PASSWORD):
        assert secret not in line
        assert secret not in json.dumps(record)
    assert "[REDACTED]" in line
    assert "THE_ODDS_API_KEY=[REDACTED]" in line or "THE_ODDS_API_KEY=[REDACTED]" in redact_secrets(
        FAKE_ODDS_ENV
    )

    exc = RuntimeError(
        f"Authorization: Bearer {FAKE_BEARER}; {FAKE_ODDS_ENV}; {FAKE_SQLITE_URL}"
    )
    logged = log_event(
        level="ERROR",
        message=str(exc),
        exc=exc,
        emit=False,
    )
    rendered = format_log_line(logged)
    for secret in (FAKE_API_KEY, FAKE_BEARER, FAKE_PASSWORD):
        assert secret not in rendered

    report = build_health_report(
        [
            make_component(
                "odds",
                HealthStatus.FAILED,
                detail=(
                    f"quota auth failed api_key={FAKE_API_KEY} "
                    f"Bearer {FAKE_BEARER} {FAKE_SQLITE_URL}"
                ),
                as_of="2026-08-13T12:00:00Z",
            )
        ]
    )
    payload = dumps_health(report)
    for secret in (FAKE_API_KEY, FAKE_BEARER, FAKE_PASSWORD):
        assert secret not in payload


def test_health_statuses_distinct_and_rollup() -> None:
    as_of = "2026-08-13T12:00:00Z"
    fixtures = {
        HealthStatus.HEALTHY: make_component(
            "backup", HealthStatus.HEALTHY, as_of=as_of, detail="ok"
        ),
        HealthStatus.MISSING: make_component(
            "quota", HealthStatus.MISSING, as_of=as_of, detail="no probe"
        ),
        HealthStatus.STALE: make_component(
            "staleness", HealthStatus.STALE, as_of=as_of, detail="as_of lag"
        ),
        HealthStatus.BLOCKED: make_component(
            "identity", HealthStatus.BLOCKED, as_of=as_of, detail="unresolved"
        ),
        HealthStatus.FAILED: make_component(
            "sources", HealthStatus.FAILED, as_of=as_of, detail="schema drift"
        ),
    }
    statuses = {c.status for c in fixtures.values()}
    assert statuses == {
        HealthStatus.HEALTHY,
        HealthStatus.MISSING,
        HealthStatus.STALE,
        HealthStatus.BLOCKED,
        HealthStatus.FAILED,
    }
    assert severity_for(HealthStatus.HEALTHY) == HealthSeverity.GREEN
    assert severity_for(HealthStatus.STALE) == HealthSeverity.YELLOW
    assert severity_for(HealthStatus.MISSING, component="quota") == HealthSeverity.YELLOW
    assert severity_for(HealthStatus.MISSING, component="sources") == HealthSeverity.RED
    assert severity_for(HealthStatus.BLOCKED) == HealthSeverity.RED
    assert severity_for(HealthStatus.FAILED) == HealthSeverity.RED

    green = build_health_report(_all_healthy(as_of=as_of), as_of=as_of)
    assert green.rollup == HealthSeverity.GREEN
    assert green.ok is True
    assert green.exit_code == EXIT_OK

    yellow = build_health_report(
        [
            make_component("staleness", HealthStatus.STALE, as_of=as_of),
            make_component("quota", HealthStatus.MISSING, as_of=as_of),
            *[
                make_component(n, HealthStatus.HEALTHY, as_of=as_of)
                for n in HEALTH_COMPONENT_NAMES
                if n not in {"staleness", "quota"}
            ],
        ],
        as_of=as_of,
    )
    assert yellow.rollup == HealthSeverity.YELLOW
    assert yellow.ok is True

    red = build_health_report(list(fixtures.values()), as_of=as_of)
    assert red.rollup == HealthSeverity.RED
    assert red.ok is False
    assert red.exit_code == EXIT_STRICT_BLOCKERS
    assert any(code.startswith("health.") for code in red.blocker_codes)


def test_health_cli_strict_exit_codes(tmp_path: Path, capsys) -> None:
    as_of = "2026-08-13T12:00:00Z"
    good_path = tmp_path / "healthy.json"
    good_path.write_text(
        json.dumps(
            {
                "as_of": as_of,
                "components": [
                    {
                        "name": name,
                        "status": "healthy",
                        "detail": "ok",
                        "as_of": as_of,
                    }
                    for name in HEALTH_COMPONENT_NAMES
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    code = main(["health", "--strict", "--json", "--state", str(good_path)])
    out = capsys.readouterr().out
    assert code == 0
    payload = json.loads(out)
    assert payload["rollup"] == "green"
    assert payload["ok"] is True
    # Deterministic sorted keys: first key alphabetically is as_of
    assert list(payload.keys())[0] == "as_of"

    bad_path = tmp_path / "blocked.json"
    bad_path.write_text(
        json.dumps(
            {
                "as_of": as_of,
                "components": [
                    {
                        "name": "publish",
                        "status": "blocked",
                        "detail": "dependency blocked",
                        "as_of": as_of,
                    },
                    *[
                        {
                            "name": name,
                            "status": "healthy",
                            "detail": "ok",
                            "as_of": as_of,
                        }
                        for name in HEALTH_COMPONENT_NAMES
                        if name != "publish"
                    ],
                ],
            }
        ),
        encoding="utf-8",
    )
    code = main(["health", "--strict", "--json", "--state", str(bad_path)])
    capsys.readouterr()
    assert code == EXIT_STRICT_BLOCKERS


def test_atomic_publish_keeps_lkg_on_validation_failure(tmp_path: Path) -> None:
    root = tmp_path / "releases-root"
    pointer = FilesystemPublishPointer(root)
    good = pointer.publish_release(
        "release-good-1",
        {
            "release.json": json.dumps({"ok": True}, sort_keys=True),
            "manifest.json": json.dumps({"files": ["release.json"]}, sort_keys=True),
        },
        required_files=("release.json", "manifest.json"),
    )
    assert pointer.current_release_id == "release-good-1"
    assert good.replaced is True

    with pytest.raises(PublishValidationError):
        pointer.publish_release(
            "release-bad-2",
            {
                "release.json": "{not-valid-json",
                "manifest.json": json.dumps({"files": ["release.json"]}, sort_keys=True),
            },
            required_files=("release.json", "manifest.json"),
        )
    assert pointer.current_release_id == "release-good-1"
    assert not (root / "releases" / "release-bad-2").exists()
    assert not (root / "releases" / "release-bad-2.candidate").exists()

    with pytest.raises(PublishValidationError):
        pointer.publish_release(
            "release-partial-3",
            {"release.json": json.dumps({"ok": True}, sort_keys=True)},
            required_files=("release.json", "manifest.json"),
        )
    assert pointer.current_release_id == "release-good-1"

    second = pointer.publish_release(
        "release-good-4",
        {
            "release.json": json.dumps({"ok": True, "n": 4}, sort_keys=True),
            "manifest.json": json.dumps({"files": ["release.json"]}, sort_keys=True),
        },
        required_files=("release.json", "manifest.json"),
    )
    assert second.current_release_id == "release-good-4"
    assert pointer.current_release_id == "release-good-4"


def test_same_id_invalid_republish_keeps_lkg_files(tmp_path: Path) -> None:
    """Same release_id invalid retry must not delete the live LKG files."""
    root = tmp_path / "releases-root"
    pointer = FilesystemPublishPointer(root)
    original = json.dumps({"ok": True, "version": 1}, sort_keys=True)
    pointer.publish_release(
        "release-good-1",
        {
            "release.json": original,
            "manifest.json": json.dumps({"files": ["release.json"]}, sort_keys=True),
        },
        required_files=("release.json", "manifest.json"),
    )
    live = root / "releases" / "release-good-1" / "release.json"
    assert live.is_file()
    assert live.read_text(encoding="utf-8") == original

    with pytest.raises(PublishValidationError):
        pointer.publish_release(
            "release-good-1",
            {
                "release.json": "{not-valid-json",
                "manifest.json": json.dumps({"files": ["release.json"]}, sort_keys=True),
            },
            required_files=("release.json", "manifest.json"),
        )

    assert pointer.current_release_id == "release-good-1"
    assert live.is_file()
    assert live.read_text(encoding="utf-8") == original
    assert json.loads(live.read_text(encoding="utf-8")) == {"ok": True, "version": 1}
    assert not (root / "releases" / "release-good-1.candidate").exists()


def test_auth_and_schema_non_retryable_transient_retryable() -> None:
    policy = BoundedRetryPolicy(max_attempts=DEFAULT_MAX_TRANSIENT_ATTEMPTS)
    assert is_non_retryable(JobErrorClass.AUTHENTICATION)
    assert is_non_retryable(JobErrorClass.SCHEMA)
    assert is_non_retryable(JobErrorClass.ENTITLEMENT)
    assert JobErrorClass.AUTHENTICATION in NON_RETRYABLE_ERRORS
    assert JobErrorClass.SCHEMA in NON_RETRYABLE_ERRORS
    assert is_retryable(JobErrorClass.TRANSIENT)
    assert not is_retryable(JobErrorClass.AUTHENTICATION)
    assert not is_retryable(JobErrorClass.SCHEMA)
    assert policy.should_retry(JobErrorClass.TRANSIENT, attempt=1) is True
    assert policy.should_retry(JobErrorClass.TRANSIENT, attempt=3) is False
    assert policy.should_retry(JobErrorClass.AUTHENTICATION, attempt=1) is False
    assert policy.should_retry(JobErrorClass.SCHEMA, attempt=1) is False
    assert policy.classify_exception_message("401 unauthorized") == (
        JobErrorClass.AUTHENTICATION
    )
    assert policy.classify_exception_message("schema validation failed") == (
        JobErrorClass.SCHEMA
    )
    assert policy.classify_exception_message("connection reset") == (
        JobErrorClass.TRANSIENT
    )


def test_health_refuses_live_mma_db(capsys) -> None:
    code = main(["health", "--json", "--database-url", "sqlite:///data/mma.db"])
    out = capsys.readouterr().out
    assert code == 1
    assert "refusing" in out
    assert "mma.db" in out


def test_health_json_validates_against_schema() -> None:
    as_of = datetime(2026, 8, 13, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    report = build_health_report(_all_healthy(as_of=as_of), as_of=as_of)
    payload = report.to_dict()
    validate_health_json(payload)
    # Round-trip through dumps also validates shape.
    reloaded = json.loads(dumps_health(report))
    validate_health_json(reloaded)
    assert reloaded["ticket"] == "DWCS-403"
    assert reloaded["contract_id"] == "dwcs_health"
    assert set(c["name"] for c in reloaded["components"]) == set(HEALTH_COMPONENT_NAMES)
