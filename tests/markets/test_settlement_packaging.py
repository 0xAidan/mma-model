"""Prove settlement rules load from a real non-editable wheel install."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from mma_model.markets.rules import PINNED_SETTLEMENT_HASH

REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED_DIGEST_LITERAL = (
    "7403941cc821e340eaf4bb50e969a6882be19f72460e808038e1567a64993ff4"
)


@pytest.mark.slow
def test_settlement_rules_load_from_non_editable_wheel_install(tmp_path: Path) -> None:
    """Build a wheel, install it into an isolated venv, and load settlement rules.

    This must succeed without the git checkout on PYTHONPATH and without relying on
    an editable ``src/`` layout. The packaged YAML resource is the authority.
    """
    assert PINNED_SETTLEMENT_HASH == PINNED_DIGEST_LITERAL

    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(REPO_ROOT), "-w", str(wheel_dir), "--no-deps"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_dir.glob("mma_model-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found {wheels}"

    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    pip = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "pip"
    python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

    install = subprocess.run(
        [str(pip), "install", str(wheels[0])],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"

    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import mma_model; "
                "from pathlib import Path; "
                "from mma_model.markets.rules import ("
                "  PINNED_SETTLEMENT_HASH, load_settlement_rules, "
                "  EXPECTED_CONTRACT_VERSION, RuleSetStatus"
                "); "
                "root = Path(mma_model.__file__).resolve().parent; "
                "assert (root / 'markets' / 'settlement_v1.yaml').is_file(), root; "
                "c = load_settlement_rules(); "
                f"assert c.content_hash == {PINNED_DIGEST_LITERAL!r}; "
                "assert c.content_hash == PINNED_SETTLEMENT_HASH; "
                "assert c.contract_version == EXPECTED_CONTRACT_VERSION; "
                "assert c.rule_sets['mma_generic'].status is RuleSetStatus.EXTERNALLY_SOURCED; "
                "print('WHEEL_SETTLEMENT_OK', c.contract_version, c.content_hash)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "WHEEL_SETTLEMENT_OK" in probe.stdout
    assert PINNED_DIGEST_LITERAL in probe.stdout
