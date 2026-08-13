"""Wheel-install identity audit, import, and canonical case-file smoke (DWCS-104)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mma_model.identity.adjudicated import (
    CASES_FILENAME,
    load_adjudicated_cases,
    package_adjudicated_cases_path,
    visible_adjudicated_cases_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STALE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "identity" / CASES_FILENAME


def test_config_and_package_adjudicated_cases_are_byte_identical() -> None:
    config_path = visible_adjudicated_cases_path(root=REPO_ROOT)
    package_path = package_adjudicated_cases_path()
    assert config_path.is_file()
    assert package_path.is_file()
    assert config_path.read_bytes() == package_path.read_bytes()
    loaded = load_adjudicated_cases()
    assert "exact_id_expansion" not in loaded
    assert loaded["statistical_confidence_claim"] is False
    assert loaded["fixture_status"] == "synthetic_explicit"
    assert loaded["cases"]


def test_stale_tests_fixtures_adjudicated_cases_are_retired() -> None:
    assert not STALE_FIXTURE.exists()
    loaded = load_adjudicated_cases()
    assert loaded["case_file_hash"]
    assert not str(visible_adjudicated_cases_path(root=REPO_ROOT)).endswith(
        str(Path("tests") / "fixtures" / "identity" / CASES_FILENAME)
    )


@pytest.mark.slow
def test_identity_audit_and_import_from_wheel_install(tmp_path: Path) -> None:
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
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stdout + created.stderr
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

    policy_probe = subprocess.run(
        [
            str(python),
            "-c",
            "from mma_model.sources.policy import default_source_policy_path; "
            "print(default_source_policy_path())",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert policy_probe.returncode == 0, policy_probe.stdout + policy_probe.stderr
    policy_dest = Path(policy_probe.stdout.strip())
    policy_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(
        REPO_ROOT / "config" / "sources" / "source_policy_v1.json",
        policy_dest,
    )

    db_path = tmp_path / "wheel-audit.db"
    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import mma_model.identity; "
                "from mma_model.db.base import Base as BaseA; "
                "from mma_model.db.models import Base as BaseB; "
                "assert BaseA is BaseB; "
                "from mma_model.identity.adjudicated import load_adjudicated_cases; "
                "from importlib import resources; "
                "pkg = resources.files('mma_model.identity.data'); "
                "assert pkg.joinpath('adjudicated_cases_v1.json').is_file(); "
                "cases = load_adjudicated_cases(); "
                "assert cases['cases']; "
                "assert 'exact_id_expansion' not in cases; "
                "from sqlalchemy import create_engine; "
                "from mma_model.db.session import create_all_for_tests; "
                f"db = {str(db_path)!r}; "
                "engine = create_engine(f'sqlite:///{db}', future=True); "
                "create_all_for_tests(engine); "
                "from mma_model.cli import main; "
                "raise SystemExit(main(["
                "'identity','audit','--database-url', f'sqlite:///{db}', "
                "'--series','dwcs','--json']))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert "report_hash" in probe.stdout
    assert "fixture_validation" in probe.stdout
    assert "unscoped_pending" in probe.stdout
    assert "statistical_confidence_claim" in probe.stdout
