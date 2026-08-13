"""Prove DWCS-202 odds decision loads from a real non-editable wheel install."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from mma_model.odds.provider_decision import PINNED_ODDS_DECISION_HASH

REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED_DIGEST_LITERAL = (
    "85e036e1717ba9df41bd31ed7aed1e2fcc1a54747fc0175ce5d53679ac6a1637"
)


@pytest.mark.slow
def test_odds_decision_loads_from_non_editable_wheel_install(tmp_path: Path) -> None:
    """Build a wheel, install it, and load the packaged odds decision contract.

    Must succeed without the git checkout on PYTHONPATH. Checkout evidence paths
    are optional cross-checks only and must not be required at runtime.
    """
    assert PINNED_ODDS_DECISION_HASH == PINNED_DIGEST_LITERAL

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
    python = venv_dir / ("Scripts" if os.name == "nt" else "bin") / "python"

    install = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--force-reinstall",
            str(wheels[0]),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("__PYVENV_LAUNCHER__", None)
    env["PYTHONNOUSERSITE"] = "1"

    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from pathlib import Path; "
                "import mma_model; "
                "from mma_model.odds.provider_decision import ("
                "  PINNED_ODDS_DECISION_HASH, load_phase0_odds_decision, "
                "  load_odds_decision_contract, package_decision_resource_path, "
                "  DECISION_PATH_REFERENCE_FALLBACK"
                "); "
                "from mma_model.odds.bookmaker_audit import run_bookmaker_audit; "
                "root = Path(mma_model.__file__).resolve().parent; "
                "assert (root / 'odds' / 'odds_decision_v1.yaml').is_file(), root; "
                "c = load_odds_decision_contract(); "
                f"assert c.content_hash == {PINNED_DIGEST_LITERAL!r}; "
                "assert c.content_hash == PINNED_ODDS_DECISION_HASH; "
                "d = load_phase0_odds_decision(); "
                "assert d.path == DECISION_PATH_REFERENCE_FALLBACK; "
                "assert d.licensed_bookmaker_adapter_authorized is False; "
                "\n_mutable=True\n"
                "try:\n"
                "  c.trial_providers['opticodds'] = 'pass'\n"
                "except TypeError:\n"
                "  _mutable=False\n"
                "assert not _mutable, 'mutable trial_providers'\n"
                "report = run_bookmaker_audit(next_dwcs=True); "
                "assert report['licensed_bookmaker_adapter_authorized'] is False; "
                "assert report['sample_price_targets']; "
                "print('WHEEL_ODDS_DECISION_OK', d.contract_version, d.content_hash)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "WHEEL_ODDS_DECISION_OK" in probe.stdout
    assert PINNED_DIGEST_LITERAL in probe.stdout
