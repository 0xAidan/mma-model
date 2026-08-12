from __future__ import annotations

from pathlib import Path

import pytest

from tests.history.helpers import make_history_db


@pytest.fixture
def history_env(tmp_path: Path):
    env = make_history_db(tmp_path)
    try:
        yield env
    finally:
        env["engine"].dispose()
