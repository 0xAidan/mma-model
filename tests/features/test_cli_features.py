"""CLI: features audit future-invariance and live-DB refusal."""

from __future__ import annotations

from mma_model.cli import main
from mma_model.quality.constants import EXIT_INTERNAL, EXIT_OK


def test_features_audit_fixture_passes(capsys) -> None:
    code = main(["features", "audit", "--series", "dwcs", "--future-invariance"])
    assert code == EXIT_OK
    assert "future-invariance ok" in capsys.readouterr().out


def test_features_audit_requires_flag(capsys) -> None:
    code = main(["features", "audit", "--series", "dwcs"])
    assert code == EXIT_INTERNAL
    assert "future-invariance" in capsys.readouterr().out


def test_features_audit_refuses_live_db(capsys) -> None:
    code = main(
        [
            "features",
            "audit",
            "--series",
            "dwcs",
            "--future-invariance",
            "--database-url",
            "sqlite:///data/mma.db",
        ]
    )
    assert code == EXIT_INTERNAL
    assert "refusing" in capsys.readouterr().out


def test_features_audit_refuses_empty_url(capsys) -> None:
    code = main(
        [
            "features",
            "audit",
            "--series",
            "dwcs",
            "--future-invariance",
            "--database-url",
            "   ",
        ]
    )
    assert code == EXIT_INTERNAL
    assert "empty" in capsys.readouterr().out
