"""Tests for DWCS-002 verified event/bout manifest builder."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "spikes" / "build_dwcs_manifest.py"
SOURCE_DIR = REPO_ROOT / "tests" / "fixtures" / "manifests" / "source"
MINI_DIR = REPO_ROOT / "tests" / "fixtures" / "manifests" / "mini"
MANIFEST_DIR = REPO_ROOT / "data" / "manifests"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("build_dwcs_manifest", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build() -> Any:
    if not SCRIPT_PATH.is_file():
        pytest.fail(f"missing build script: {SCRIPT_PATH}")
    return _load_module()


@pytest.fixture(scope="module")
def built_universe(build: Any, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dwcs-manifests")
    build.run_build(
        source_dir=SOURCE_DIR,
        out_dir=out,
        through_year=2025,
        refresh_espn=False,
        verify=True,
        built_at="2026-08-11T00:00:00+00:00",
    )
    return out


def test_normalize_and_canonical_pair_are_deterministic(build: Any) -> None:
    assert build.normalize_fighter_name("José Mauro") == "jose mauro"
    assert build.canonical_participant_pair(["Bravo", "Alpha"]) == ("alpha", "bravo")
    assert build.canonical_participant_pair(["Alpha", "Bravo"]) == ("alpha", "bravo")


def test_duplicate_pair_within_event_raises(build: Any) -> None:
    events = build.read_jsonl(MINI_DIR / "events.jsonl")[:1]
    bouts = build.read_jsonl(MINI_DIR / "bouts.jsonl")[:1]
    duplicate = dict(bouts[0])
    duplicate["espn_competition_id"] = "mini-dup"
    with pytest.raises(ValueError, match="duplicate canonical participant pair"):
        build.build_manifests(
            events_facts=events,
            bouts_facts=[bouts[0], duplicate],
            reconciliations=[],
            through_year=2025,
            built_at="2026-08-11T00:00:00+00:00",
        )


def test_mini_universe_draw_nc_reversal_cancellation_replacement(build: Any) -> None:
    events = build.read_jsonl(MINI_DIR / "events.jsonl")
    bouts = build.read_jsonl(MINI_DIR / "bouts.jsonl")
    recon = build.read_jsonl(MINI_DIR / "event_night_reconciliations.jsonl")
    cancels = build.read_jsonl(MINI_DIR / "cancellations_replacements.jsonl")

    event_rows, bout_rows, counts, mismatches = build.build_manifests(
        events_facts=events,
        bouts_facts=bouts,
        reconciliations=recon,
        cancellations_replacements=cancels,
        through_year=2025,
        built_at="2026-08-11T00:00:00+00:00",
    )

    assert len(event_rows) == 2
    assert len(bout_rows) == 5
    assert counts["cards"]["standard"] == 1
    assert counts["cards"]["brazil"] == 1
    assert counts["event_night_results"]["decisive"] == 3
    assert counts["event_night_results"]["draw"] == 1
    assert counts["event_night_results"]["no_contest"] == 1
    assert counts["current_results"]["decisive"] == 2
    assert counts["current_results"]["draw"] == 1
    assert counts["current_results"]["no_contest"] == 2

    reversed_bout = next(b for b in bout_rows if b["espn_competition_id"] == "mini-b3")
    assert reversed_bout["event_night_result"]["class"] == "decisive"
    assert reversed_bout["current_result"]["class"] == "no_contest"
    assert reversed_bout["version_state"] == "reversed_to_no_contest"

    standard_event = next(e for e in event_rows if e["espn_event_id"] == "mini-e1")
    kinds = {row["kind"] for row in standard_event["cancellations_replacements"]}
    assert kinds == {"cancellation", "replacement"}

    assert mismatches["ok"] is False
    paths = {row["path"] for row in mismatches["mismatches"]}
    assert "cards.all" in paths
    assert "bouts.all" in paths


def test_full_universe_counts_from_committed_fixtures(
    build: Any, built_universe: Path
) -> None:
    counts = json.loads((built_universe / "dwcs_counts_v1.json").read_text(encoding="utf-8"))
    mismatches = json.loads(
        (built_universe / "dwcs_mismatches_v1.json").read_text(encoding="utf-8")
    )
    events = build.read_jsonl(built_universe / "dwcs_events_v1.jsonl")
    bouts = build.read_jsonl(built_universe / "dwcs_bouts_v1.jsonl")

    assert mismatches["ok"] is True
    assert counts["cards"]["all"] == build.EXPECTED_ALL_CARDS
    assert counts["bouts"]["all"] == build.EXPECTED_ALL_BOUTS
    assert counts["cards"]["standard"] == build.EXPECTED_STANDARD_CARDS
    assert counts["bouts"]["standard"] == build.EXPECTED_STANDARD_BOUTS
    assert counts["event_night_results"] == build.EXPECTED_EVENT_NIGHT
    assert counts["current_results"] == build.EXPECTED_CURRENT
    assert len(events) == build.EXPECTED_ALL_CARDS
    assert len(bouts) == build.EXPECTED_ALL_BOUTS


def test_no_duplicate_pairs_and_referential_integrity(
    build: Any, built_universe: Path
) -> None:
    events = build.read_jsonl(built_universe / "dwcs_events_v1.jsonl")
    bouts = build.read_jsonl(built_universe / "dwcs_bouts_v1.jsonl")
    event_ids = {row["event_id"] for row in events}
    assert len(event_ids) == len(events)
    assert len({row["bout_id"] for row in bouts}) == len(bouts)

    pairs_by_event: dict[str, set[tuple[str, str]]] = {}
    for bout in bouts:
        assert bout["event_id"] in event_ids
        assert bout["ufcstats_bout_id"] is None
        assert bout["publication_timestamp"] is None
        pair = tuple(bout["canonical_participant_pair"])
        bucket = pairs_by_event.setdefault(bout["event_id"], set())
        assert pair not in bucket
        bucket.add(pair)
        left = bout["participants"][0]["normalized_name"]
        right = bout["participants"][1]["normalized_name"]
        assert left <= right


def test_deterministic_rerun_is_byte_stable(build: Any, tmp_path: Path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    fixed = "2026-08-11T12:00:00+00:00"
    build.run_build(
        source_dir=SOURCE_DIR,
        out_dir=out_a,
        through_year=2025,
        refresh_espn=False,
        verify=True,
        built_at=fixed,
    )
    build.run_build(
        source_dir=SOURCE_DIR,
        out_dir=out_b,
        through_year=2025,
        refresh_espn=False,
        verify=True,
        built_at=fixed,
    )
    for name in (
        "dwcs_events_v1.jsonl",
        "dwcs_bouts_v1.jsonl",
        "dwcs_counts_v1.json",
        "dwcs_mismatches_v1.json",
    ):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()


def test_cli_verify_offline(tmp_path: Path) -> None:
    out = tmp_path / "out"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--through",
            "2025",
            "--verify",
            "--source-dir",
            str(SOURCE_DIR),
            "--out-dir",
            str(out),
            "--built-at",
            "2026-08-11T00:00:00+00:00",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    payload = json.loads(completed.stdout.strip())
    assert payload["ok"] is True
    assert payload["cards"]["all"] == 89
    assert payload["bouts"]["all"] == 440


def test_source_fixtures_are_minimal_and_secret_free(build: Any) -> None:
    events, bouts, recon, _cancels = build.load_source_bundle(SOURCE_DIR)
    assert len(events) == build.EXPECTED_ALL_CARDS
    assert len(bouts) == build.EXPECTED_ALL_BOUTS
    assert len(recon) == 9
    blob = json.dumps([events, bouts, recon])
    for fragment in ("api_key", "authorization", "password", "secret"):
        assert fragment not in blob.lower()
    for row in bouts:
        assert "competitions" not in row
        assert "geoBroadcasts" not in row
        assert "highlights" not in row


def test_committed_repo_manifests_match_builder_when_present(build: Any) -> None:
    if not (MANIFEST_DIR / "dwcs_events_v1.jsonl").is_file():
        pytest.skip("committed manifests not present yet")
    tmp = Path("/tmp/dwcs-manifest-rebuild-check")
    tmp.mkdir(parents=True, exist_ok=True)
    for child in tmp.iterdir():
        if child.is_file():
            child.unlink()
    build.run_build(
        source_dir=SOURCE_DIR,
        out_dir=tmp,
        through_year=2025,
        refresh_espn=False,
        verify=True,
        built_at=json.loads(
            (MANIFEST_DIR / "dwcs_counts_v1.json").read_text(encoding="utf-8")
        )["built_at"],
    )
    for name in (
        "dwcs_events_v1.jsonl",
        "dwcs_bouts_v1.jsonl",
        "dwcs_counts_v1.json",
        "dwcs_mismatches_v1.json",
    ):
        assert (MANIFEST_DIR / name).read_bytes() == (tmp / name).read_bytes()
