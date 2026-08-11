"""Prove the evaluation contract loads from a real non-editable wheel install."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from mma_model.evaluation.contract import PINNED_CONTRACT_HASH

REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_DIGEST_LITERAL = "af0ad518a6417ac7d67e5f56fe836ab58afe55d8ac70813bf6045307ea6fb2cf"


@pytest.mark.slow
def test_contract_loads_from_non_editable_wheel_install(tmp_path: Path) -> None:
    """Build a wheel, install it into an isolated venv, and load the contract.

    This must succeed without the git checkout on PYTHONPATH and without relying on
    an editable `src/` layout. The packaged resource is the authority.
    """
    assert PINNED_CONTRACT_HASH == PINNED_DIGEST_LITERAL

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

    # Ensure the subprocess cannot import from the checkout src/ tree.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"

    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import mma_model, mma_model.evaluation as ev; "
                "from pathlib import Path; "
                "root = Path(mma_model.__file__).resolve().parent; "
                "assert (root / 'evaluation' / 'dwcs_v1.json').is_file(), root; "
                "c = ev.load_evaluation_contract(); "
                f"assert c.content_hash == {PINNED_DIGEST_LITERAL!r}; "
                "assert c.splits.holdout.locked is True; "
                "assert c.splits.holdout.seasons == (2025,); "
                "assert c.metrics.outcome; "
                "print('WHEEL_CONTRACT_OK', c.contract_version, c.content_hash)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "WHEEL_CONTRACT_OK" in probe.stdout
    assert PINNED_DIGEST_LITERAL in probe.stdout
