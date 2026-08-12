"""CLI history sync/audit tests (DWCS-105)."""

from __future__ import annotations

import json
from pathlib import Path

from mma_model.cli import main
from tests.history.helpers import make_history_db, stage_sync_fixtures


def test_history_sync_and_audit_fixture_cli(tmp_path: Path, capsys) -> None:
    env = make_history_db(tmp_path)
    try:
        root = stage_sync_fixtures(tmp_path)
        code = main(
            [
                "history",
                "sync",
                "--fighters",
                "upcoming-dwcs",
                "--database-url",
                env["db_url"],
                "--raw-store",
                str(tmp_path / "raw"),
                "--fixture-root",
                str(root),
                "--json",
            ]
        )
        out = capsys.readouterr().out
        assert code == 0
        payload = json.loads(out)
        assert payload["fighters"] >= 1
        assert "licensed_optional" in payload
        summary = tmp_path / "audit.json"
        doc = tmp_path / "regional-coverage.md"
        code = main(
            [
                "history",
                "audit",
                "--years",
                "2023:2025",
                "--database-url",
                env["db_url"],
                "--json",
                "--summary-out",
                str(summary),
                "--coverage-doc",
                str(doc),
            ]
        )
        audit_out = capsys.readouterr().out
        assert code in {0, 2}
        audit = json.loads(audit_out)
        assert "professional_found" in audit
        assert "live_probes" in audit
        assert summary.is_file()
        assert doc.is_file()
        text = summary.read_text(encoding="utf-8")
        assert "Set-Cookie" not in text
        assert "<html" not in text.lower()
        assert "password" not in text.lower()
    finally:
        env["engine"].dispose()


def test_history_sync_refuses_live_db(capsys) -> None:
    code = main(
        [
            "history",
            "sync",
            "--fighters",
            "upcoming-dwcs",
            "--database-url",
            "sqlite:///data/mma.db",
            "--raw-store",
            "/tmp/raw",
            "--fixture-root",
            "/tmp/fixtures",
        ]
    )
    assert code == 2
    assert "refusing" in capsys.readouterr().out
