"""Atomic publish LKG behavior for dashboard contracts (DWCS-500)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mma_model.domain.markets import RecommendationState
from mma_model.observability.publish_guard import (
    FilesystemPublishPointer,
    PublishValidationError,
)
from mma_model.publish.builder import build_release_files
from mma_model.publish.constants import DASHBOARD_RELEASE_FILES, MATCHUPS_JSON, RELEASE_JSON
from mma_model.publish.publisher import publish_dashboard, publish_dashboard_from_bodies
from mma_model.publish.secrets import SecretScanError, scan_payload_for_secrets
from tests.publish.helpers import open_publish_session, seed_publication


def test_invalid_and_partial_cannot_become_current(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-1",
            state=RecommendationState.PRICE_TARGET,
        )
        root = tmp_path / "out"
        good = publish_dashboard(
            session,
            output_root=root,
            release_id="release-good",
            event_id="evt-1",
        )
        assert good.current_release_id == "release-good"
        pointer = FilesystemPublishPointer(root)
        assert pointer.current_release_id == "release-good"
        live = root / "releases" / "release-good" / RELEASE_JSON
        original = live.read_text(encoding="utf-8")

        # Well-formed JSON that fails schema validation.
        bad_files = build_release_files(
            session, release_id="release-bad", event_id="evt-1"
        )
        bad_payload = json.loads(bad_files[MATCHUPS_JSON])
        bad_payload["unexpected_field"] = True
        bad_files = dict(bad_files)
        bad_files[MATCHUPS_JSON] = json.dumps(bad_payload, sort_keys=True)

        with pytest.raises(PublishValidationError):
            publish_dashboard_from_bodies(
                output_root=root,
                release_id="release-bad",
                files=bad_files,
            )
        assert pointer.current_release_id == "release-good"
        assert live.read_text(encoding="utf-8") == original
        assert not (root / "releases" / "release-bad").exists()
        assert not (root / "releases" / "release-bad.candidate").exists()

        # Partial files (missing required dashboard docs).
        with pytest.raises(PublishValidationError):
            pointer.publish_release(
                "release-partial",
                {RELEASE_JSON: original},
                required_files=DASHBOARD_RELEASE_FILES,
            )
        assert pointer.current_release_id == "release-good"
    finally:
        session.close()
        engine.dispose()


def test_failed_publish_leaves_previous_untouched(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-1",
            state=RecommendationState.NO_BET,
        )
        root = tmp_path / "out"
        publish_dashboard(
            session,
            output_root=root,
            release_id="release-1",
            event_id="evt-1",
        )
        pointer = FilesystemPublishPointer(root)
        live = root / "releases" / "release-1" / RELEASE_JSON
        before = live.read_text(encoding="utf-8")

        with pytest.raises(PublishValidationError):
            pointer.publish_release(
                "release-1",
                {RELEASE_JSON: "{not-json", "manifest.json": "{}"},
                required_files=DASHBOARD_RELEASE_FILES,
            )
        assert pointer.current_release_id == "release-1"
        assert live.read_text(encoding="utf-8") == before
    finally:
        session.close()
        engine.dispose()


def test_secret_scan_rejects_api_keys_and_raw_payloads() -> None:
    with pytest.raises(SecretScanError):
        scan_payload_for_secrets({"the_odds_api_key": "secret"})
    with pytest.raises(SecretScanError):
        scan_payload_for_secrets({"detail": "Authorization: Bearer abc.def"})
    with pytest.raises(SecretScanError):
        scan_payload_for_secrets({"licensed_raw": {"dump": 1}})
    with pytest.raises(SecretScanError):
        scan_payload_for_secrets({"note": "THE_ODDS_API_KEY=xyz"})


def test_secret_scan_blocks_publish(tmp_path: Path) -> None:
    session, engine = open_publish_session(tmp_path)
    try:
        seed_publication(
            session,
            bout_id="bout-1",
            state=RecommendationState.NO_BET,
        )
        root = tmp_path / "out"
        publish_dashboard(
            session,
            output_root=root,
            release_id="release-clean",
            event_id="evt-1",
        )
        files = build_release_files(
            session, release_id="release-secret", event_id="evt-1"
        )
        poisoned = dict(files)
        payload = json.loads(poisoned[RELEASE_JSON])
        payload["hashes"] = {"api_key": "should-not-publish"}
        # hashes is ArtifactHashes object fields — inject via detail path instead
        matchups = json.loads(poisoned[MATCHUPS_JSON])
        if matchups["matchups"]:
            matchups["matchups"][0]["detail"] = "Bearer tokensecret"
        else:
            matchups["event_id"] = {
                "presence": "known",
                "value": "THE_ODDS_API_KEY=leak",
            }
        poisoned[MATCHUPS_JSON] = json.dumps(matchups, sort_keys=True)
        with pytest.raises(PublishValidationError):
            publish_dashboard_from_bodies(
                output_root=root,
                release_id="release-secret",
                files=poisoned,
            )
        assert FilesystemPublishPointer(root).current_release_id == "release-clean"
    finally:
        session.close()
        engine.dispose()
