"""Prove DWCS-205 schedule contract loads from a real non-editable wheel install."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from mma_model.odds.schedule import PINNED_SCHEDULE_CONTRACT_HASH

REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED_DIGEST_LITERAL = (
    "d966bdb1f1cbc14806001e2f11d6f273e7f93ceda25f49969e38a42eb3798b75"
)


@pytest.mark.slow
def test_schedule_loads_from_non_editable_wheel_install(tmp_path: Path) -> None:
    """Build a wheel, install it, and load the packaged schedule contract."""
    assert PINNED_SCHEDULE_CONTRACT_HASH == PINNED_DIGEST_LITERAL

    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(REPO_ROOT),
            "-w",
            str(wheel_dir),
            "--no-deps",
        ],
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
                "from mma_model.odds.schedule import ("
                "  PINNED_SCHEDULE_CONTRACT_HASH, load_schedule_contract, "
                "  package_schedule_resource_path"
                "); "
                "root = Path(mma_model.__file__).resolve().parent; "
                "assert (root / 'odds' / 'schedule_v1.yaml').is_file(), root; "
                "c = load_schedule_contract(); "
                f"assert c.content_hash == {PINNED_DIGEST_LITERAL!r}; "
                "assert c.content_hash == PINNED_SCHEDULE_CONTRACT_HASH; "
                "\n_mutable=True\n"
                "try:\n"
                "  c.quota.cost_fixed['events'] = 9\n"
                "except TypeError:\n"
                "  _mutable=False\n"
                "assert not _mutable, 'mutable cost_fixed'\n"
                "print('WHEEL_SCHEDULE_OK', c.contract_version, c.content_hash)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "WHEEL_SCHEDULE_OK" in probe.stdout
    assert PINNED_DIGEST_LITERAL in probe.stdout
