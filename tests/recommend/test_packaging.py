"""Prove recommendation policy loads from a real non-editable wheel install."""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from mma_model.recommend.policy import PINNED_POLICY_HASH

REPO_ROOT = Path(__file__).resolve().parents[2]
PINNED_DIGEST_LITERAL = (
    "6f18bffd536f4b9a7f41ac6e05903758595981e1dabc28a7d310a422532eb646"
)


@pytest.mark.slow
def test_recommendation_policy_loads_from_non_editable_wheel_install(tmp_path: Path) -> None:
    assert PINNED_POLICY_HASH == PINNED_DIGEST_LITERAL

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
                "from mma_model.recommend.policy import ("
                "  PINNED_POLICY_HASH, load_recommendation_policy, "
                "  EXPECTED_POLICY_VERSION"
                "); "
                "root = Path(mma_model.__file__).resolve().parent; "
                "assert (root / 'recommend' / 'recommendation_policy.yaml').is_file(), root; "
                "c = load_recommendation_policy(); "
                f"assert c.content_hash == {PINNED_DIGEST_LITERAL!r}; "
                "assert c.content_hash == PINNED_POLICY_HASH; "
                "assert c.policy_version == EXPECTED_POLICY_VERSION; "
                "print('WHEEL_POLICY_OK', c.policy_version, c.content_hash)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "WHEEL_POLICY_OK" in probe.stdout
    assert PINNED_DIGEST_LITERAL in probe.stdout
