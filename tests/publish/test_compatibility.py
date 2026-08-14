"""Backward-compat: committed v1 golden fixtures still validate. """

from __future__ import annotations

import json
from pathlib import Path

from mma_model.publish.constants import DASHBOARD_RELEASE_FILES
from mma_model.publish.schema import validate_document

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_v1_fixtures_validate_when_present() -> None:
    if not FIXTURES.is_dir():
        return
    present = [name for name in DASHBOARD_RELEASE_FILES if (FIXTURES / name).is_file()]
    if not present:
        return
    for name in present:
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        validate_document(name, payload)
        # Unknown fields rejected.
        if isinstance(payload, dict):
            bad = dict(payload)
            bad["not_in_contract"] = True
            try:
                validate_document(name, bad)
            except Exception:
                continue
            raise AssertionError(f"{name} accepted unknown field")
