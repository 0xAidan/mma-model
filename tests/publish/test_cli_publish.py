"""CLI smoke for `mma-model publish --output` (DWCS-500)."""

from __future__ import annotations

import json
from pathlib import Path

from mma_model.cli import main
from mma_model.domain.markets import RecommendationState
from mma_model.observability.publish_guard import FilesystemPublishPointer
from mma_model.publish.constants import DASHBOARD_RELEASE_FILES, RELEASE_JSON
from tests.publish.helpers import open_publish_session, seed_publication


def test_cli_publish_smoke(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path, name="cli.db")
    db_path = tmp_path / "cli.db"
    try:
        seed_publication(
            session,
            bout_id="bout-cli",
            state=RecommendationState.PRICE_TARGET,
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    out = tmp_path / "public"
    code = main(
        [
            "publish",
            "--output",
            str(out),
            "--database-url",
            f"sqlite:///{db_path}",
            "--event-id",
            "evt-1",
            "--release-id",
            "cli-release-1",
        ]
    )
    assert code == 0
    pointer = FilesystemPublishPointer(out)
    assert pointer.current_release_id == "cli-release-1"
    release_dir = out / "releases" / "cli-release-1"
    for name in DASHBOARD_RELEASE_FILES:
        assert (release_dir / name).is_file()
    payload = json.loads((release_dir / RELEASE_JSON).read_text(encoding="utf-8"))
    assert payload["ticket"] == "DWCS-500"
    assert payload["schema_version"] == 1


def test_cli_publish_refuses_live_db(tmp_path: Path) -> None:
    out = tmp_path / "public"
    code = main(
        [
            "publish",
            "--output",
            str(out),
            "--database-url",
            "sqlite:///data/mma.db",
        ]
    )
    assert code != 0
