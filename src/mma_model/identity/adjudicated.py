"""Frozen adjudicated identity case loading and honest scoring (DWCS-104)."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mma_model.config import get_settings
from mma_model.db.session import _attach_sqlite_listeners, create_all_for_tests
from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterProfileObservation,
    FighterSourceId,
)
from mma_model.dwcs.ids import canonical_fighter_id
from mma_model.identity.constants import (
    RULE_EXACT_SOURCE_EXTERNAL_ID,
    RULE_EXACT_WIKIDATA,
    RULE_NAME_CONTEXT_UNIQUE,
    RULE_NAME_DOB_UNIQUE,
)
from mma_model.identity.models import ResolveResult
from mma_model.identity.resolver import resolve_fighter

CASES_FILENAME = "adjudicated_cases_v1.json"
_EVAL_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)

AUTO_LINK_RULES = frozenset(
    {
        RULE_EXACT_SOURCE_EXTERNAL_ID,
        RULE_EXACT_WIKIDATA,
        RULE_NAME_DOB_UNIQUE,
        RULE_NAME_CONTEXT_UNIQUE,
    }
)


class AdjudicatedCasesError(ValueError):
    """Fail-closed loader error for the adjudicated identity case file."""


def package_adjudicated_cases_path() -> Path:
    root = resources.files("mma_model.identity.data")
    resource = root.joinpath(CASES_FILENAME)
    if not resource.is_file():
        raise AdjudicatedCasesError("packaged adjudicated identity cases not found")
    with resources.as_file(resource) as path:
        resolved = Path(path)
        if not resolved.is_file():
            raise AdjudicatedCasesError("packaged adjudicated identity cases not found")
        return resolved


def visible_adjudicated_cases_path(*, root: Path | None = None) -> Path:
    base = root if root is not None else get_settings().project_root
    return base / "config" / "identity" / CASES_FILENAME


def _package_cases_bytes() -> bytes | None:
    try:
        root = resources.files("mma_model.identity.data")
        resource = root.joinpath(CASES_FILENAME)
    except (ModuleNotFoundError, FileNotFoundError, AttributeError, OSError):
        return None
    if not resource.is_file():
        return None
    return resource.read_bytes()


def adjudicated_cases_path(*, root: Path | None = None) -> Path:
    """Canonical checkout config, else the byte-identical packaged copy."""
    visible = visible_adjudicated_cases_path(root=root)
    if visible.is_file():
        return visible
    return package_adjudicated_cases_path()


def case_file_hash(path: Path | None = None) -> str:
    if path is not None:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    visible = visible_adjudicated_cases_path()
    packaged = _package_cases_bytes()
    if visible.is_file():
        return hashlib.sha256(visible.read_bytes()).hexdigest()
    if packaged is not None:
        return hashlib.sha256(packaged).hexdigest()
    raise AdjudicatedCasesError("adjudicated identity cases not found")


def _parse_adjudicated_cases(raw_bytes: bytes) -> dict[str, Any]:
    try:
        raw_text = raw_bytes.decode("utf-8")
        raw = json.loads(raw_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdjudicatedCasesError(f"invalid adjudicated identity cases: {exc}") from exc
    if not isinstance(raw, dict):
        raise AdjudicatedCasesError("adjudicated identity cases root must be an object")
    if raw.get("exact_id_expansion"):
        raise AdjudicatedCasesError("generated exact_id_expansion is not allowed")
    if not raw.get("version"):
        raise AdjudicatedCasesError("adjudicated identity cases version is required")
    if not isinstance(raw.get("cases"), list) or not raw["cases"]:
        raise AdjudicatedCasesError("adjudicated identity cases list is required")
    if raw.get("statistical_confidence_claim") is True:
        raise AdjudicatedCasesError("statistical confidence claims are not allowed")
    raw["case_file_hash"] = hashlib.sha256(raw_bytes).hexdigest()
    return raw


def load_adjudicated_cases(path: Path | None = None) -> dict[str, Any]:
    """Load the versioned explicit case file. No generated expansion."""
    if path is not None:
        try:
            return _parse_adjudicated_cases(path.read_bytes())
        except FileNotFoundError as exc:
            raise AdjudicatedCasesError("adjudicated identity cases not found") from exc
        except OSError as exc:
            raise AdjudicatedCasesError(f"invalid adjudicated identity cases: {exc}") from exc
    visible = visible_adjudicated_cases_path()
    packaged_bytes = _package_cases_bytes()
    if visible.is_file() and packaged_bytes is not None:
        visible_bytes = visible.read_bytes()
        if visible_bytes != packaged_bytes:
            raise AdjudicatedCasesError(
                "config and package adjudicated cases are not byte-identical"
            )
        return _parse_adjudicated_cases(visible_bytes)
    if visible.is_file():
        return _parse_adjudicated_cases(visible.read_bytes())
    if packaged_bytes is not None:
        return _parse_adjudicated_cases(packaged_bytes)
    raise AdjudicatedCasesError("adjudicated identity cases not found")


def is_auto_link_eligible(case: Mapping[str, Any]) -> bool:
    """Exact-ID / Wikidata / unique name+DOB / unique context only."""
    if "auto_eligible" in case:
        return bool(case["auto_eligible"])
    expected = case.get("expected") or {}
    return expected.get("kind") == "linked" and bool(expected.get("canonical_espn_id"))


def seed_adjudicated_fixture(session: Session, fixture: Mapping[str, Any]) -> None:
    for seed in fixture.get("seeds") or []:
        fid = canonical_fighter_id(str(seed["espn_id"]))
        session.add(
            CanonicalFighter(
                id=fid,
                display_name=str(seed["display_name"]),
                created_at=_EVAL_NOW,
                updated_at=_EVAL_NOW,
            )
        )
        session.add(
            FighterSourceId(
                fighter_id=fid,
                source="espn",
                external_id=str(seed["espn_id"]),
            )
        )
        if seed.get("dob"):
            session.add(
                FighterProfileObservation(
                    fighter_id=fid,
                    attribute="dob",
                    value_date=date.fromisoformat(str(seed["dob"])),
                    source="espn",
                    effective_at=_EVAL_NOW,
                    observed_at=_EVAL_NOW,
                )
            )
        if seed.get("wikidata_id"):
            session.add(
                FighterSourceId(
                    fighter_id=fid,
                    source="wikidata",
                    external_id=str(seed["wikidata_id"]),
                )
            )
        for src in seed.get("extra_source_ids") or []:
            session.add(
                FighterSourceId(
                    fighter_id=fid,
                    source=str(src["source"]),
                    external_id=str(src["external_id"]),
                )
            )
    for event in fixture.get("events") or []:
        session.add(
            CanonicalEvent(
                id=event["id"],
                name=event["name"],
                series=event.get("series", "dwcs"),
                status=event.get("status", "completed"),
                event_date=date.fromisoformat(event["event_date"]),
            )
        )
    session.flush()
    for bout in fixture.get("bouts") or []:
        session.add(
            CanonicalBout(
                id=bout["id"],
                event_id=bout["event_id"],
                fighter_a_id=canonical_fighter_id(bout["fighter_a_espn"]),
                fighter_b_id=canonical_fighter_id(bout["fighter_b_espn"]),
                status=bout.get("status", "completed"),
            )
        )
    session.flush()
    for bout in fixture.get("bouts") or []:
        session.add(
            BoutParticipant(
                bout_id=bout["id"],
                fighter_id=canonical_fighter_id(bout["fighter_a_espn"]),
                corner="a",
            )
        )
        session.add(
            BoutParticipant(
                bout_id=bout["id"],
                fighter_id=canonical_fighter_id(bout["fighter_b_espn"]),
                corner="b",
            )
        )
    session.flush()


def resolve_adjudicated_cases(
    session: Session, fixture: Mapping[str, Any]
) -> dict[str, ResolveResult]:
    results: dict[str, ResolveResult] = {}
    for case in fixture.get("cases") or []:
        results[str(case["id"])] = resolve_fighter(
            session,
            source=case["source"],
            external_id=case["external_id"],
            display_name=case["display_name"],
            wikidata_id=case.get("wikidata_id"),
            dob=date.fromisoformat(case["dob"]) if case.get("dob") else None,
            opponent_normalized_name=case.get("opponent_normalized_name"),
            event_id=case.get("event_id"),
            event_date=(
                date.fromisoformat(case["event_date"]) if case.get("event_date") else None
            ),
            bout_id=case.get("bout_id"),
            bout_status=case.get("bout_status"),
            candidate_hints=tuple(case.get("candidate_hints") or ()),
            actor="system",
            now=_EVAL_NOW,
        )
    return results


def score_adjudicated_results(
    fixture: Mapping[str, Any],
    results_by_id: Mapping[str, ResolveResult | Any],
    *,
    series: str | None = None,
) -> dict[str, Any]:
    """Score auto-link eligibility honestly; queued/blocked never count as TPs."""
    cases = list(fixture.get("cases") or [])
    if series is not None:
        cases = [case for case in cases if case.get("series", "dwcs") == series]
    denominator_all = len(cases)
    auto_true_pos = 0
    auto_false_pos = 0
    auto_false_neg = 0
    queued = 0
    blocked = 0
    same_name_conflations = 0
    matched_expected = 0
    auto_eligible_count = 0

    for case in cases:
        result = results_by_id[case["id"]]
        kind = getattr(result, "kind", None)
        expected = case.get("expected") or {}
        expected_kind = expected.get("kind")
        if kind == "queued":
            queued += 1
        elif kind == "blocked":
            blocked += 1

        if expected_kind == "blocked_or_queued":
            if kind in {"queued", "blocked"}:
                matched_expected += 1
        elif expected_kind == kind:
            if expected_kind == "linked":
                want = canonical_fighter_id(str(expected["canonical_espn_id"]))
                if getattr(result, "canonical_id", None) == want:
                    matched_expected += 1
            else:
                matched_expected += 1
        elif expected_kind == "queued" and kind == "blocked":
            matched_expected += 1

        if not is_auto_link_eligible(case):
            continue
        auto_eligible_count += 1
        want_id = expected.get("canonical_espn_id")
        want = canonical_fighter_id(str(want_id)) if want_id else None
        actual_id = getattr(result, "canonical_id", None)
        rule_id = getattr(result, "rule_id", None)
        if (
            kind == "linked"
            and want is not None
            and actual_id == want
            and rule_id in AUTO_LINK_RULES
        ):
            auto_true_pos += 1
            continue
        if kind in {"linked", "created"}:
            auto_false_pos += 1
            if case.get("same_name_group") and actual_id != want:
                same_name_conflations += 1
            continue
        auto_false_neg += 1

    precision_den = auto_true_pos + auto_false_pos
    recall_den = auto_true_pos + auto_false_neg
    precision = auto_true_pos / precision_den if precision_den else 0.0
    recall = auto_true_pos / recall_den if recall_den else 0.0
    queue_or_blocked = queued + blocked
    queue_rate = queued / denominator_all if denominator_all else 0.0
    blocked_rate = blocked / denominator_all if denominator_all else 0.0
    coverage = matched_expected / denominator_all if denominator_all else 0.0
    return {
        "n": denominator_all,
        "denominator_all": denominator_all,
        "denominator_auto_eligible": auto_eligible_count,
        "auto_true_pos": auto_true_pos,
        "auto_false_pos": auto_false_pos,
        "auto_false_neg": auto_false_neg,
        "precision": precision,
        "recall": recall,
        "queued": queued,
        "queue_rate": queue_rate,
        "blocked": blocked,
        "blocked_rate": blocked_rate,
        "queue_or_blocked": queue_or_blocked,
        "coverage": coverage,
        "same_name_conflations": same_name_conflations,
        "fixture_status": fixture.get("fixture_status", "synthetic_explicit"),
        "statistical_confidence_claim": False,
        "version": str(fixture.get("version") or ""),
        "case_file_hash": str(fixture.get("case_file_hash") or ""),
        "label": "global_synthetic_validation",
    }


def evaluate_frozen_adjudicated_fixture(
    path: Path | None = None, *, series: str | None = None
) -> dict[str, Any]:
    """Run the frozen fixture against the current resolver in an isolated temp DB."""
    fixture = load_adjudicated_cases(path)
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_sqlite_listeners(engine)
    create_all_for_tests(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    try:
        with SessionLocal() as session:
            seed_adjudicated_fixture(session, fixture)
            session.commit()
            results = resolve_adjudicated_cases(session, fixture)
            session.commit()
            return score_adjudicated_results(fixture, results, series=series)
    finally:
        engine.dispose()
