"""Import-order tests for identity and db facades (DWCS-104)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env={
            "PYTHONPATH": str(SRC),
            "PYTHONNOUSERSITE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_import_mma_model_identity_from_clean_interpreter() -> None:
    proc = _run("import mma_model.identity; print('IDENTITY_OK')")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "IDENTITY_OK" in proc.stdout
    assert "circular" not in proc.stderr.lower()


def test_import_orders_core_models_identity() -> None:
    snippets = (
        "import mma_model.identity; import mma_model.db.models; print('A')",
        "import mma_model.db.models; import mma_model.identity; print('B')",
        "import mma_model.db.tables.core; import mma_model.db.models; print('C')",
        "import mma_model.db.models; import mma_model.db.tables.core; print('D')",
        "from mma_model.db.models import Base, CanonicalFighter, Fighter; print('E')",
        (
            "from mma_model.db.base import Base as A; "
            "from mma_model.db.models import Base as B; "
            "assert A is B; print('F')"
        ),
        (
            "from mma_model.db.models import Base as B; "
            "from mma_model.db.base import Base as A; "
            "assert A is B; print('G')"
        ),
        (
            "import mma_model.identity; "
            "from mma_model.db.base import Base as A; "
            "from mma_model.db.models import Base as B; "
            "assert A is B; print('H')"
        ),
    )
    for code in snippets:
        proc = _run(code)
        assert proc.returncode == 0, f"{code!r}\n{proc.stdout}\n{proc.stderr}"
