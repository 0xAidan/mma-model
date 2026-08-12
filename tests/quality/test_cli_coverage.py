"""CLI coverage command tests (DWCS-106)."""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

import mma_model.cli as cli
from mma_model.cli import main


def test_coverage_refuses_live_db(capsys) -> None:
    code = main(
        [
            "coverage",
            "--series",
            "dwcs",
            "--database-url",
            "sqlite:///data/mma.db",
        ]
    )
    assert code == 2
    assert "refusing" in capsys.readouterr().out


def test_coverage_refuses_empty_url(capsys) -> None:
    code = main(["coverage", "--series", "dwcs", "--database-url", "   "])
    assert code == 2
    assert "refusing" in capsys.readouterr().out


def test_coverage_cli_non_strict_and_strict(populated, capsys) -> None:
    args = [
        "coverage",
        "--series",
        "dwcs",
        "--database-url",
        populated["db_url"],
        "--json",
    ]
    non_strict = main(args)
    out = capsys.readouterr().out
    assert non_strict == 0
    payload = json.loads(out)
    assert payload["universe_bouts"] == 440
    assert payload["core_tiers"]["silver"] == 440
    assert payload["licensed_status"]["phase1_global_blocker"] is False
    strict = main(args + ["--strict"])
    capsys.readouterr()
    assert strict == 2


def test_coverage_cli_no_mutation_no_network(populated, monkeypatch, capsys) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("network attempted")

    monkeypatch.setattr(cli, "UfcstatsPublicClient", _boom)
    monkeypatch.setattr(cli, "TapologyPublicClient", _boom)
    before = populated["db_path"].stat().st_mtime_ns
    code = main(
        [
            "coverage",
            "--series",
            "dwcs",
            "--database-url",
            populated["db_url"],
        ]
    )
    after = populated["db_path"].stat().st_mtime_ns
    assert code == 0
    assert after == before
    assert "licensed_primary_unselected" in capsys.readouterr().out


def test_coverage_cli_subprocess_no_network(populated) -> None:
    repo = Path(__file__).resolve().parents[2]
    script = (
        "from mma_model.cli import main; "
        f"raise SystemExit(main(['coverage','--series','dwcs',"
        f"'--database-url', {populated['db_url']!r}]))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo / "src")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "universe=89/440" in proc.stdout


def test_help_lists_legacy_commands(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for name in ("init-db", "sync", "odds", "train", "predict-fight", "backtest", "coverage"):
        assert name in text
