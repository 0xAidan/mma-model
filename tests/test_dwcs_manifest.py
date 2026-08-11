"""Tests for DWCS-002 verified event/bout manifest builder."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "spikes" / "build_dwcs_manifest.py"
SOURCE_DIR = REPO_ROOT / "tests" / "fixtures" / "manifests" / "source"
MINI_DIR = REPO_ROOT / "tests" / "fixtures" / "manifests" / "mini"
MANIFEST_DIR = REPO_ROOT / "data" / "manifests"
EXPECTED_PATH = REPO_ROOT / "config" / "manifests" / "dwcs_expected_universe_v1.json"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("build_dwcs_manifest", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


@pytest.fixture(scope="module")
def build() -> Any:
    if not SCRIPT_PATH.is_file():
        pytest.fail(f"missing build script: {SCRIPT_PATH}")
    return _load_module()


@pytest.fixture(scope="module")
def expected(build: Any) -> dict[str, Any]:
    return build.load_expected_universe(EXPECTED_PATH)


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


def test_pinned_expected_universe_is_single_source(build: Any, expected: dict[str, Any]) -> None:
    digest = build.compute_expected_universe_hash(expected)
    assert digest == build.PINNED_EXPECTED_UNIVERSE_HASH
    assert expected["contract_id"] == "dwcs_expected_universe"
    assert expected["cards"]["all"] == 89
    assert expected["bouts"]["all"] == 440
    with pytest.raises(build.ExpectedUniverseError, match="hash mismatch"):
        build.load_expected_universe(EXPECTED_PATH, expected_hash="0" * 64)


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
    assert reversed_bout["reconciliation_provenance"]["citation_only"] is True

    standard_event = next(e for e in event_rows if e["espn_event_id"] == "mini-e1")
    kinds = {row["kind"] for row in standard_event["cancellations_replacements"]}
    assert kinds == {"cancellation", "replacement"}

    assert mismatches["ok"] is False
    paths = {row["path"] for row in mismatches["mismatches"]}
    assert "cards.all" in paths
    assert "bouts.all" in paths


def test_independent_full_universe_counters(
    build: Any, built_universe: Path, expected: dict[str, Any]
) -> None:
    events = build.read_jsonl(built_universe / "dwcs_events_v1.jsonl")
    bouts = build.read_jsonl(built_universe / "dwcs_bouts_v1.jsonl")

    card_variants = Counter(str(e["series_variant"]) for e in events)
    bout_variants = Counter(str(b["series_variant"]) for b in bouts)
    event_night = Counter(str(b["event_night_result"]["class"]) for b in bouts)
    current = Counter(str(b["current_result"]["class"]) for b in bouts)
    version_states = Counter(str(b["version_state"]) for b in bouts)

    assert len(events) == expected["cards"]["all"]
    assert len(bouts) == expected["bouts"]["all"]
    assert card_variants["standard"] == expected["cards"]["standard"]
    assert card_variants["brazil"] == expected["cards"]["brazil"]
    assert bout_variants["standard"] == expected["bouts"]["standard"]
    assert bout_variants["brazil"] == expected["bouts"]["brazil"]
    assert dict(event_night) == expected["event_night_results"]
    assert dict(current) == expected["current_results"]
    assert dict(version_states) == expected["version_states"]

    season_cards: dict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        season_cards[str(event["calendar_year"])][str(event["series_variant"])] += 1
    for year, season_expected in expected["season_cards"].items():
        assert season_cards[year]["standard"] == season_expected["standard"]
        assert season_cards[year]["brazil"] == season_expected["brazil"]

    event_ids = {e["event_id"] for e in events}
    bout_ids = [b["bout_id"] for b in bouts]
    assert len(event_ids) == len(events)
    assert len(set(bout_ids)) == len(bout_ids)

    pairs_by_event: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for bout in bouts:
        assert bout["event_id"] in event_ids
        assert bout["ufcstats_bout_id"] is None
        assert bout["publication_timestamp"] is None
        pair = (bout["canonical_participant_pair"][0], bout["canonical_participant_pair"][1])
        assert pair not in pairs_by_event[bout["event_id"]]
        pairs_by_event[bout["event_id"]].add(pair)
        assert (
            bout["participants"][0]["normalized_name"]
            <= bout["participants"][1]["normalized_name"]
        )


def test_corruption_drops_event_fails_verify(build: Any, tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source)
    events = build.read_jsonl(source / build.EVENTS_FACTS_NAME)
    _write_jsonl(source / build.EVENTS_FACTS_NAME, events[1:])
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="verification failed"):
        build.run_build(
            source_dir=source,
            out_dir=out,
            through_year=2025,
            refresh_espn=False,
            verify=True,
            built_at="2026-08-11T00:00:00+00:00",
        )
    report = json.loads((out / "dwcs_mismatches_v1.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    paths = {row["path"] for row in report["mismatches"]}
    assert "cards.all" in paths


def test_corruption_mutates_bout_result_fails_verify(build: Any, tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source)
    bouts = build.read_jsonl(source / build.BOUTS_FACTS_NAME)
    target = next(b for b in bouts if b["current_result_class"] == "decisive")
    target["current_result_class"] = "no_contest"
    target["current_result_name"] = "no-contest"
    target["current_result_display"] = "No Contest"
    for part in target["participants"]:
        part["winner"] = False
    _write_jsonl(source / build.BOUTS_FACTS_NAME, bouts)
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="verification failed"):
        build.run_build(
            source_dir=source,
            out_dir=out,
            through_year=2025,
            refresh_espn=False,
            verify=True,
            built_at="2026-08-11T00:00:00+00:00",
        )
    report = json.loads((out / "dwcs_mismatches_v1.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    paths = {row["path"] for row in report["mismatches"]}
    assert "current_results.decisive" in paths or "current_results.no_contest" in paths


def test_corruption_removes_recon_fails_verify(build: Any, tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source)
    recon = build.read_jsonl(source / build.RECON_FACTS_NAME)
    # Drop a reversal overlay so event-night collapses toward current NC counts.
    remaining = [row for row in recon if row["version_state"] != "reversed_to_no_contest"]
    _write_jsonl(source / build.RECON_FACTS_NAME, remaining)
    out = tmp_path / "out"
    with pytest.raises(SystemExit, match="verification failed"):
        build.run_build(
            source_dir=source,
            out_dir=out,
            through_year=2025,
            refresh_espn=False,
            verify=True,
            built_at="2026-08-11T00:00:00+00:00",
        )
    report = json.loads((out / "dwcs_mismatches_v1.json").read_text(encoding="utf-8"))
    assert report["ok"] is False
    paths = {row["path"] for row in report["mismatches"]}
    assert (
        "event_night_results.decisive" in paths
        or "version_states.reversed_to_no_contest" in paths
    )


def test_reconciliation_provenance_validated_offline(build: Any) -> None:
    recon = build.read_jsonl(SOURCE_DIR / build.RECON_FACTS_NAME)
    build.validate_reconciliation_provenance(recon)
    for row in recon:
        assert row["citation_only"] is True
        assert row["evidence_checked_at"]
        assert row["evidence_grade"]
        assert "does not fetch" in row["evidence_limitations"].lower()
        assert all(build.is_valid_https_url(url) for url in row["evidence_urls"])
        assert any(item.get("kind") == "contemporaneous_news" for item in row["evidence"])

    bad = dict(recon[0])
    bad["evidence_urls"] = ["http://insecure.example/x"]
    with pytest.raises(ValueError, match="invalid evidence_urls"):
        build.validate_reconciliation_provenance([bad])


def test_open_gaps_deferred_to_later_tickets(built_universe: Path) -> None:
    report = json.loads(
        (built_universe / "dwcs_mismatches_v1.json").read_text(encoding="utf-8")
    )
    by_path = {row["path"]: row for row in report["open_gaps"]}
    assert "DWCS-103" in by_path["ufc_ufcstats_ids"]["deferred_to"]
    assert "DWCS-104" in by_path["ufc_ufcstats_ids"]["deferred_to"]
    assert "DWCS-103" in by_path["full_cancellation_replacement_ledger"]["deferred_to"]
    assert by_path["ufc_ufcstats_ids"]["severity"] == "incomplete_not_done"


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


def test_cli_verify_offline(build: Any, expected: dict[str, Any], tmp_path: Path) -> None:
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
    # Smoke-check CLI JSON against the pinned contract (not duplicated literals).
    assert payload["cards"]["all"] == expected["cards"]["all"]
    assert payload["bouts"]["all"] == expected["bouts"]["all"]
    assert build.compute_expected_universe_hash(expected) == build.PINNED_EXPECTED_UNIVERSE_HASH


def test_source_fixtures_are_minimal_and_secret_free(
    build: Any, expected: dict[str, Any]
) -> None:
    events, bouts, recon, _cancels = build.load_source_bundle(SOURCE_DIR)
    assert len(events) == expected["cards"]["all"]
    assert len(bouts) == expected["bouts"]["all"]
    # Recon overlays cover every non-assumed version state (reversals + unchanged).
    assert len(recon) == (
        expected["version_states"]["reversed_to_no_contest"]
        + expected["version_states"]["unchanged"]
    )
    blob = json.dumps([events, bouts, recon])
    for fragment in ("api_key", "authorization", "password", "secret"):
        assert fragment not in blob.lower()
    for row in bouts:
        assert "competitions" not in row
        assert "geoBroadcasts" not in row
        assert "highlights" not in row
        assert "current_result_class" in row


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
