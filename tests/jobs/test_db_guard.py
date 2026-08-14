"""Tests for jobs tick database URL guard (DWCS-504)."""

from __future__ import annotations

from pathlib import Path

from mma_model.jobs.db_guard import (
    ALLOWED_JOBS_TICK_ABSOLUTE_MMA_DB_URL,
    is_refused_jobs_tick_database_url,
)
from mma_model.quality.constants import LIVE_DB_URLS


def test_guard_refuses_relative_live_urls() -> None:
    assert is_refused_jobs_tick_database_url("sqlite:///data/mma.db") is True
    assert is_refused_jobs_tick_database_url("sqlite:///./data/mma.db") is True
    for url in LIVE_DB_URLS:
        assert is_refused_jobs_tick_database_url(url) is True


def test_guard_allows_explicit_absolute_production_url() -> None:
    assert ALLOWED_JOBS_TICK_ABSOLUTE_MMA_DB_URL == "sqlite:////data/mma.db"
    assert is_refused_jobs_tick_database_url("sqlite:////data/mma.db") is False


def test_guard_allows_disposable_absolute_tmp_urls(tmp_path: Path) -> None:
    # Four-slash absolute form under a temp dir — CI must not touch a live DB.
    abs_url = f"sqlite:////{tmp_path.resolve() / 'jobs-tick.db'}"
    assert abs_url.startswith("sqlite:////")
    assert "data/mma.db" not in abs_url
    assert is_refused_jobs_tick_database_url(abs_url) is False


def test_guard_refuses_empty() -> None:
    assert is_refused_jobs_tick_database_url("") is True
    assert is_refused_jobs_tick_database_url("   ") is True


def test_cli_jobs_tick_refuses_relative_accepts_absolute_dry_run(
    tmp_path: Path, capsys
) -> None:
    from mma_model.cli import main

    refuse = main(
        [
            "jobs",
            "tick",
            "--now",
            "2026-08-11T18:00:00Z",
            "--dry-run",
            "--database-url",
            "sqlite:///data/mma.db",
            "--event-id",
            "evt-1",
            "--event-start",
            "2026-08-11T18:00:00Z",
        ]
    )
    out = capsys.readouterr().out
    assert refuse == 2
    assert "refusing live data/mma.db" in out

    # Explicit absolute production URL is allowed for dry-run (no live file touch).
    ok = main(
        [
            "jobs",
            "tick",
            "--now",
            "2026-08-11T18:00:00Z",
            "--dry-run",
            "--database-url",
            "sqlite:////data/mma.db",
            "--event-id",
            "evt-1",
            "--event-start",
            "2026-08-11T18:00:00Z",
        ]
    )
    assert ok == 0

    # Disposable absolute tmp URL also allowed (four-slash).
    abs_tmp = f"sqlite:////{tmp_path.resolve() / 'jobs-tick.db'}"
    ok_tmp = main(
        [
            "jobs",
            "tick",
            "--now",
            "2026-08-11T18:00:00Z",
            "--dry-run",
            "--database-url",
            abs_tmp,
            "--event-id",
            "evt-1",
            "--event-start",
            "2026-08-11T18:00:00Z",
        ]
    )
    assert ok_tmp == 0


def test_cli_jobs_tick_requires_database_url_when_not_dry_run(capsys) -> None:
    from mma_model.cli import main

    code = main(
        [
            "jobs",
            "tick",
            "--now",
            "2026-08-11T18:00:00Z",
            "--event-id",
            "evt-1",
            "--event-start",
            "2026-08-11T18:00:00Z",
        ]
    )
    assert code == 2
    assert "requires --database-url" in capsys.readouterr().out
