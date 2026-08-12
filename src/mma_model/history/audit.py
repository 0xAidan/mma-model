"""Regional history audit and coverage evidence (DWCS-105)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.history import (
    HistoryConflict,
    HistorySourceBout,
    HistorySourceFailure,
)
from mma_model.history.constants import (
    SOURCE_CLASS,
    SOURCE_COMBAT_REGISTRY,
    SOURCE_SHERDOG,
    SOURCE_TAPOLOGY,
    UNRESOLVED_IDENTITY_STATUSES,
)
from mma_model.history.coverage import (
    db_bout_dates,
    evidence_class_for,
    filter_sample_rows,
    left_truncated_history_count,
    live_source_coverage_map,
    measured_identity_conflations,
)
from mma_model.history.models import RegionalCoverageReport
from mma_model.history.reconstruct import persist_reconstruction, reconstruct_pre_fight_record

PRO_GATE = 0.95
AMATEUR_GATE = 0.80
AGREEMENT_GATE = 0.98
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COVERAGE_DOC = REPO_ROOT / "docs" / "data" / "regional-coverage.md"
DEFAULT_SAMPLE_PATH = REPO_ROOT / "config" / "history" / "adjudicated_regional_sample_v1.json"
DEFAULT_LIVE_PROBE_PATH = REPO_ROOT / "config" / "history" / "live_probe_evidence_v1.json"


def _hash_payload(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_adjudicated_sample(path: Path | None = None) -> dict[str, Any]:
    sample_path = path or DEFAULT_SAMPLE_PATH
    return json.loads(sample_path.read_text(encoding="utf-8"))


def _found_ids(session: Session) -> set[str]:
    rows = session.scalars(select(HistorySourceBout)).all()
    return {row.external_bout_id for row in rows}


def _source_failures(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(HistorySourceFailure).order_by(
            HistorySourceFailure.source.asc(), HistorySourceFailure.reason.asc()
        )
    ).all()
    return [
        {
            "source": row.source,
            "reason": row.reason,
            "scope": row.scope,
            "host": row.host,
            "path_category": row.path_category,
            "http_status": row.http_status,
        }
        for row in rows
    ]


def evaluate_sample_coverage(
    session: Session,
    *,
    sample: Mapping[str, Any] | None = None,
    years: range | None = None,
    live_probes: Mapping[str, Any] | None = None,
) -> RegionalCoverageReport:
    payload = dict(sample or load_adjudicated_sample())
    found = _found_ids(session)
    failures = _source_failures(session)
    failed_sources = {row["source"] for row in failures}
    db_dates = db_bout_dates(session)
    live_coverage = live_source_coverage_map(session, _select_live_probes(live_probes))
    evidence_class = evidence_class_for(session)

    pro_all = list(payload.get("professional_bouts") or [])
    am_all = list(payload.get("amateur_regulated_us_bouts") or [])
    fixture_pro = filter_sample_rows(pro_all, years=None, db_dates=db_dates)
    fixture_am = filter_sample_rows(
        am_all, years=None, db_dates=db_dates, require_regulated_us=True
    )
    pro_rows = filter_sample_rows(pro_all, years=years, db_dates=db_dates)
    am_rows = filter_sample_rows(
        am_all, years=years, db_dates=db_dates, require_regulated_us=True
    )
    unknown_rows = filter_sample_rows(
        list(payload.get("unknown_classification_bouts") or []),
        years=years,
        db_dates=db_dates,
    )
    eligible = tuple(pro_rows + am_rows + unknown_rows)

    def _score(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, int, int]:
        n = 0
        found_n = 0
        failed_n = 0
        unexplained = 0
        for row in rows:
            n += 1
            bout_id = str(row["bout_id"])
            source = str(row.get("source") or "")
            if bout_id in found:
                found_n += 1
            elif source in failed_sources or bool(row.get("source_failed")):
                failed_n += 1
            else:
                unexplained += 1
        return n, found_n, failed_n, unexplained

    pro_n, pro_found, pro_failed, pro_unexplained = _score(pro_rows)
    am_n, am_found, am_failed, am_unexplained = _score(am_rows)
    fixture_pro_n, fixture_pro_found, _, _ = _score(fixture_pro)
    fixture_am_n, fixture_am_found, _, _ = _score(fixture_am)
    unknown_n = len(unknown_rows)

    agreement_n = 0
    agreement_d = 0
    exclusions: list[str] = []
    invariance_failures = 0
    reconstruction_hashes: list[str] = []
    for row in list(payload.get("explicit_pre_fight_records") or []):
        fighter_id = str(row["fighter_id"])
        if fighter_id in {"", "WILL_BE_SET_IN_TEST"} or row.get("exclude"):
            exclusions.append(str(row.get("exclude_reason") or "excluded"))
            continue
        cutoff = datetime.fromisoformat(str(row["cutoff"]).replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        reconstructed = reconstruct_pre_fight_record(
            fighter_id=fighter_id, cutoff=cutoff, session=session
        )
        try:
            stored = persist_reconstruction(session, reconstructed)
            reconstruction_hashes.append(stored.payload_hash)
        except RuntimeError:
            invariance_failures += 1
        expected = (
            int(row["wins"]),
            int(row["losses"]),
            int(row.get("draws") or 0),
            int(row.get("no_contests") or 0),
        )
        agreement_d += 1
        if reconstructed.comparable_tuple() == expected:
            agreement_n += 1

    conflicts = list(session.scalars(select(HistoryConflict)).all())
    bout_rows = list(session.scalars(select(HistorySourceBout)).all())
    identity_payload = payload.get("identity") or {}
    identity_linked = sum(1 for row in bout_rows if row.identity_status == "linked")
    identity_queued = sum(1 for row in bout_rows if row.identity_status == "queued")
    identity_blocks = sum(1 for row in bout_rows if row.identity_status == "blocked")
    unresolved = sum(
        1 for row in bout_rows if row.identity_status in UNRESOLVED_IDENTITY_STATUSES
    )
    left_truncated = left_truncated_history_count(session)
    identity_conflations = measured_identity_conflations(session)
    tier_counts: dict[str, int] = {}
    for row in bout_rows:
        tier_counts[row.quality_tier] = tier_counts.get(row.quality_tier, 0) + 1
    pit_tiers = tuple(sorted(tier_counts.items()))
    source_rows: list[dict[str, Any]] = []
    for source in (SOURCE_TAPOLOGY, SOURCE_SHERDOG, SOURCE_COMBAT_REGISTRY):
        src_bouts = [row for row in bout_rows if row.source == source]
        killed = [row for row in failures if row["source"] == source]
        live = live_coverage.get(source) or {}
        source_rows.append(
            {
                "source": source,
                "source_class": SOURCE_CLASS.get(source),
                "bouts": len(src_bouts),
                "killed": bool(killed) or live.get("status") in {"source_killed", "source_failed"},
                "kill_reasons": tuple(row["reason"] for row in killed),
                "live_status": live.get("status"),
            }
        )
    pro_rate = (pro_found / pro_n) if pro_n else None
    am_rate = (am_found / am_n) if am_n else None
    agree_rate = (agreement_n / agreement_d) if agreement_d else None
    invariance_hash = _hash_payload({"hashes": reconstruction_hashes})
    body = {
        "professional_n": pro_n,
        "professional_found": pro_found,
        "professional_rate": pro_rate,
        "professional_source_failed": pro_failed,
        "professional_missing_unexplained": pro_unexplained,
        "amateur_n": am_n,
        "amateur_found": am_found,
        "amateur_rate": am_rate,
        "amateur_source_failed": am_failed,
        "amateur_missing_unexplained": am_unexplained,
        "unknown_class_n": unknown_n,
        "source_failed": failures,
        "pre_fight_agreement_n": agreement_n,
        "pre_fight_agreement_d": agreement_d,
        "pre_fight_agreement_rate": agree_rate,
        "pre_fight_exclusions": tuple(exclusions),
        "future_invariance_failures": invariance_failures,
        "conflicts": len(conflicts),
        "identity_exact_links": (
            identity_linked if bout_rows else int(identity_payload.get("exact_links") or 0)
        ),
        "identity_queued": (
            identity_queued if bout_rows else int(identity_payload.get("queued") or 0)
        ),
        "identity_blocks": (
            identity_blocks if bout_rows else int(identity_payload.get("blocks") or 0)
        ),
        "identity_conflations": identity_conflations,
        "left_truncated": left_truncated,
        "unresolved_identities": unresolved,
        "pit_tiers": pit_tiers,
        "sources": source_rows,
        "invariance_hash": invariance_hash,
        "evidence_class": evidence_class,
        "live_source_coverage": live_coverage,
        "years": {
            "start": years.start if years else None,
            "stop": (years.stop - 1) if years else None,
        },
    }
    report = RegionalCoverageReport(
        professional_n=pro_n,
        professional_found=pro_found,
        professional_rate=pro_rate,
        amateur_n=am_n,
        amateur_found=am_found,
        amateur_rate=am_rate,
        unknown_class_n=unknown_n,
        source_failed=tuple(failures),
        pre_fight_agreement_n=agreement_n,
        pre_fight_agreement_d=agreement_d,
        pre_fight_agreement_rate=agree_rate,
        pre_fight_exclusions=tuple(exclusions),
        future_invariance_failures=invariance_failures,
        conflicts=len(conflicts),
        identity_exact_links=int(body["identity_exact_links"]),
        identity_queued=int(body["identity_queued"]),
        identity_blocks=int(body["identity_blocks"]),
        identity_conflations=identity_conflations,
        professional_source_failed=pro_failed,
        professional_missing_unexplained=pro_unexplained,
        amateur_source_failed=am_failed,
        amateur_missing_unexplained=am_unexplained,
        left_truncated=left_truncated,
        unresolved_identities=unresolved,
        pit_tiers=pit_tiers,
        sources=tuple(source_rows),
        invariance_hash=invariance_hash,
        report_hash=_hash_payload(body),
        notes=(),
        evidence_class=evidence_class,  # type: ignore[arg-type]
        live_source_coverage=live_coverage,
        eligible_sample_bouts=eligible,
        fixture_professional_n=fixture_pro_n,
        fixture_professional_found=fixture_pro_found,
        fixture_amateur_n=fixture_am_n,
        fixture_amateur_found=fixture_am_found,
    )
    return report


def _segment_gate_ok(
    *,
    n: int,
    found: int,
    failed: int,
    unexplained: int,
    gate: float,
) -> bool:
    if n == 0:
        return True
    if unexplained > 0:
        return False
    rate = found / n
    if rate >= gate:
        return True
    return (found + failed) == n


def coverage_gates_ok(report: RegionalCoverageReport) -> tuple[bool, tuple[str, ...]]:
    blockers: list[str] = []
    if report.pre_fight_agreement_d == 0:
        blockers.append("insufficient_comparable_records")
    elif (report.pre_fight_agreement_rate or 0) < AGREEMENT_GATE:
        blockers.append("pre_fight_agreement")
    if report.evidence_class != "live_source_coverage":
        blockers.append("live_source_unmeasured")
    else:
        if not _segment_gate_ok(
            n=report.professional_n,
            found=report.professional_found,
            failed=report.professional_source_failed,
            unexplained=report.professional_missing_unexplained,
            gate=PRO_GATE,
        ):
            blockers.append("professional_coverage")
        if not _segment_gate_ok(
            n=report.amateur_n,
            found=report.amateur_found,
            failed=report.amateur_source_failed,
            unexplained=report.amateur_missing_unexplained,
            gate=AMATEUR_GATE,
        ):
            blockers.append("amateur_coverage")
    for source, row in (report.live_source_coverage or {}).items():
        status = str(row.get("status") or "unmeasured")
        if status in {"source_killed", "source_failed", "unmeasured", "accessibility_only"}:
            blockers.append(f"live_{status}:{source}")
    if report.future_invariance_failures != 0:
        blockers.append("future_row_invariance")
    if report.identity_conflations != 0:
        blockers.append("identity_conflation")
    return (not blockers, tuple(blockers))


def _select_live_probes(live_probes: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(live_probes or {})
    probes = payload.get("probes") if isinstance(payload.get("probes"), dict) else payload
    probes = dict(probes or {})
    live_ran = any(
        isinstance(row, dict) and row.get("result") not in {None, "NOT_RUN"}
        for row in probes.values()
    )
    if live_ran:
        return probes
    if DEFAULT_LIVE_PROBE_PATH.is_file():
        frozen = json.loads(DEFAULT_LIVE_PROBE_PATH.read_text(encoding="utf-8"))
        frozen_probes = frozen.get("probes") if isinstance(frozen.get("probes"), dict) else frozen
        return dict(frozen_probes or {})
    return probes


def _live_probe_lines(live_probes: Mapping[str, Any] | None) -> str:
    probes = _select_live_probes(live_probes)
    if not probes:
        return "- none (live probe not executed)"
    lines = []
    for source in (SOURCE_TAPOLOGY, SOURCE_SHERDOG, SOURCE_COMBAT_REGISTRY):
        row = probes.get(source) or {}
        robots = row.get("robots") if isinstance(row.get("robots"), dict) else {}
        lines.append(
            f"- `{source}`: result=`{row.get('result')}` "
            f"reason=`{row.get('block_reason')}` "
            f"http={row.get('http_status')} "
            f"path=`{row.get('path_category')}` "
            f"robots=`{robots.get('policy_decision')}`"
        )
    return "\n".join(lines)


def render_regional_coverage_markdown(
    report: RegionalCoverageReport,
    *,
    live_probes: Mapping[str, Any] | None = None,
) -> str:
    failed = report.source_failed or ()
    failed_lines = "\n".join(
        f"- `{row['source']}`: `{row['reason']}`"
        for row in failed
    ) or "- none"
    pro_rate = "n/a" if report.professional_rate is None else f"{report.professional_rate:.4f}"
    am_rate = "n/a" if report.amateur_rate is None else f"{report.amateur_rate:.4f}"
    agree = (
        "n/a"
        if report.pre_fight_agreement_rate is None
        else f"{report.pre_fight_agreement_rate:.4f}"
    )
    live_lines = []
    for source, row in (report.live_source_coverage or {}).items():
        live_lines.append(
            f"- `{source}`: status=`{row.get('status')}` result=`{row.get('result')}` "
            f"reason=`{row.get('reason')}` http={row.get('http_status')} "
            f"path=`{row.get('path_category')}`"
        )
    live_block = "\n".join(live_lines) or "- none"
    agree_note = (
        "blocker (insufficient_comparable_records); never a pass"
        if report.pre_fight_agreement_d == 0
        else agree
    )
    return (
        "# Regional / pre-UFC history coverage\n\n"
        "Sanitized DWCS-105 evidence. No raw HTML or live payloads.\n\n"
        f"- Report hash: `{report.report_hash}`\n"
        f"- Evidence class: `{report.evidence_class}`\n"
        f"- Year-filtered professional sample: {report.professional_found}/{report.professional_n} ({pro_rate}); "
        f"source_failed={report.professional_source_failed}; "
        f"missing_unexplained={report.professional_missing_unexplained}\n"
        f"- Year-filtered regulated-US amateur sample: {report.amateur_found}/{report.amateur_n} ({am_rate}); "
        f"source_failed={report.amateur_source_failed}; "
        f"missing_unexplained={report.amateur_missing_unexplained}\n"
        f"- Unknown classification rows: {report.unknown_class_n}\n"
        f"- Pre-fight agreement: {report.pre_fight_agreement_n}/{report.pre_fight_agreement_d} ({agree_note})\n"
        f"- Pre-fight exclusions: {', '.join(report.pre_fight_exclusions) or 'none'}\n"
        f"- Future-row invariance failures: {report.future_invariance_failures}\n"
        f"- Invariance hash: `{report.invariance_hash}`\n"
        f"- Conflicts: {report.conflicts}\n"
        f"- Left-truncated histories: {report.left_truncated}\n"
        f"- Unresolved identities: {report.unresolved_identities}\n"
        f"- PIT tiers: {dict(report.pit_tiers)}\n"
        f"- Identity exact links / queued / blocks / conflations: "
        f"{report.identity_exact_links}/{report.identity_queued}/"
        f"{report.identity_blocks}/{report.identity_conflations}\n\n"
        "## fixture_validation\n\n"
        "Synthetic `data-schema` decoder counts. These are **not live coverage** "
        "and must not satisfy live 95%/80% gates.\n\n"
        f"- Professional decoder: {report.fixture_professional_found}/{report.fixture_professional_n} "
        f"(synthetic fixtures; not live coverage)\n"
        f"- Amateur decoder: {report.fixture_amateur_found}/{report.fixture_amateur_n} "
        f"(synthetic fixtures; not live coverage)\n"
        f"- `{report.fixture_professional_found}/{report.fixture_professional_n}` and "
        f"`{report.fixture_amateur_found}/{report.fixture_amateur_n}` must not be treated as measured live coverage.\n\n"
        "## live_source_coverage\n\n"
        f"{live_block}\n\n"
        "Sherdog hash-only 200 on `/events/` is accessibility only and is not measured "
        "fighter-history coverage. Tapology HTTP 403 and Combat Registry login wall "
        "kill those source roles. Unknown remains unknown, not zero.\n\n"
        "## Source failures\n\n"
        f"{failed_lines}\n\n"
        "## Sources\n\n"
        + "\n".join(
            f"- `{row['source']}` ({row.get('source_class')}): "
            f"bouts={row.get('bouts')} killed={row.get('killed')} "
            f"live_status={row.get('live_status')} "
            f"reasons={','.join(row.get('kill_reasons') or ()) or 'none'}"
            for row in report.sources
        )
        + "\n\n## Live probes\n\n"
        + _live_probe_lines(live_probes)
        + "\n"
        + "\nLicensed SportsDataIO / BALLDONTLIE validation remains `source_failed` "
        + "under recorded limitations and is not a DWCS-105 stop.\n"
        + "Pre-fight agreement 0/0 is a blocker (`insufficient_comparable_records`), "
        + "never a passing coverage gate.\n"
    )


def write_regional_coverage_doc(
    report: RegionalCoverageReport,
    path: Path | None = None,
    *,
    live_probes: Mapping[str, Any] | None = None,
) -> Path:
    target = path or DEFAULT_COVERAGE_DOC
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_regional_coverage_markdown(report, live_probes=live_probes)
    target.write_text(text, encoding="utf-8")
    return target
