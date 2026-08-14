"""Regional history sync orchestrator (DWCS-105)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.tables.core import (
    BoutParticipant,
    CanonicalBout,
    CanonicalEvent,
    CanonicalFighter,
    FighterSourceId,
)
from mma_model.history.apply import conflict_observation, source_failure_observation
from mma_model.history.constants import (
    REGIONAL_FALLBACK_ORDER,
    SOURCE_COMBAT_REGISTRY,
    SOURCE_SHERDOG,
    SOURCE_TAPOLOGY,
    SOURCE_WIKIDATA,
)
from mma_model.history.frontier import RegionalFrontier
from mma_model.history.identity import (
    identity_status_from_result,
    identity_summary,
    resolve_regional_fighter,
)
from mma_model.history.licensed_optional import licensed_optional_validation_status
from mma_model.history.reconstruct import reconstruct_pre_fight_record
from mma_model.identity.models import ResolveResult
from mma_model.ingest.repository import IngestRepository
from mma_model.sources.combat_registry.adapter import CombatRegistryPublicAdapter
from mma_model.sources.combat_registry.errors import (
    ParserSchemaDriftError as CombatSchemaDrift,
)
from mma_model.sources.contracts import SourceObservationRecord
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.sherdog_public.adapter import SherdogPublicAdapter
from mma_model.sources.sherdog_public.errors import (
    ParserSchemaDriftError as SherdogSchemaDrift,
)
from mma_model.sources.tapology_public.adapter import TapologyPublicAdapter
from mma_model.sources.tapology_public.errors import (
    ParserSchemaDriftError as TapologySchemaDrift,
)

SessionFactory = Callable[[], Session]
CHECKPOINT_VERSION = "regional_history_v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_UPCOMING_PATH = REPO_ROOT / "config" / "history" / "upcoming_dwcs_fighters_v1.json"
SCHEMA_DRIFT_TYPES = (TapologySchemaDrift, SherdogSchemaDrift, CombatSchemaDrift)
UPCOMING_EVENT_STATUSES = frozenset({"scheduled", "upcoming"})
EXCLUDED_UPCOMING_BOUT_STATUSES = frozenset({"scratched", "cancelled", "completed"})


@dataclass(frozen=True)
class FighterSeed:
    display_name: str
    source_ids: Mapping[str, str]
    canonical_id: str | None = None
    wikidata_id: str | None = None


@dataclass
class HistorySyncReport:
    dry_run: bool
    fighters: int
    inserted_observations: int
    skipped_identical: int
    batches_committed: int
    source_failed: list[dict[str, Any]] = field(default_factory=list)
    killed_sources: list[str] = field(default_factory=list)
    identity: dict[str, int] = field(default_factory=dict)
    conflicts: int = 0
    notes: list[str] = field(default_factory=list)
    licensed_optional: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    unresolved_source_ids: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "fighters": self.fighters,
            "inserted_observations": self.inserted_observations,
            "skipped_identical": self.skipped_identical,
            "batches_committed": self.batches_committed,
            "source_failed": list(self.source_failed),
            "killed_sources": list(self.killed_sources),
            "identity": dict(self.identity),
            "conflicts": self.conflicts,
            "licensed_optional": list(self.licensed_optional),
            "notes": list(self.notes),
            "blockers": list(self.blockers),
            "unresolved_source_ids": self.unresolved_source_ids,
        }

    def human_summary(self) -> str:
        failed = ",".join(self.killed_sources) or "none"
        return (
            f"fighters={self.fighters} inserted={self.inserted_observations} "
            f"conflicts={self.conflicts} killed={failed} dry_run={self.dry_run}"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_payload(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _json_fighter_seeds(path: Path | None = None) -> list[FighterSeed]:
    seed_path = path or DEFAULT_UPCOMING_PATH
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    rows = []
    for item in raw.get("fighters") or []:
        source_ids = dict(item.get("source_ids") or {})
        rows.append(
            FighterSeed(
                display_name=str(item["display_name"]),
                source_ids=source_ids,
                canonical_id=item.get("canonical_hint_id"),
                wikidata_id=source_ids.get(SOURCE_WIKIDATA) or item.get("wikidata_id"),
            )
        )
    return rows


def _merge_source_ids(
    *,
    json_ids: Mapping[str, str],
    db_ids: Mapping[str, str],
) -> dict[str, str]:
    merged = dict(json_ids)
    for key, value in db_ids.items():
        if value:
            merged[key] = value
    return merged


def _match_json_seed(
    seeds: Sequence[FighterSeed],
    fighter: CanonicalFighter,
    db_ids: Mapping[str, str],
) -> FighterSeed | None:
    for seed in seeds:
        if seed.canonical_id and seed.canonical_id == fighter.id:
            return seed
        for source, external_id in seed.source_ids.items():
            if external_id and db_ids.get(source) == external_id:
                return seed
    return None


def load_upcoming_dwcs_fighters(
    path: Path | None = None,
    *,
    session: Session | None = None,
) -> list[FighterSeed]:
    json_seeds = _json_fighter_seeds(path)
    if session is None:
        return json_seeds
    upcoming = session.scalars(
        select(CanonicalEvent).where(
            CanonicalEvent.series.in_(("dwcs", "dwcs_brazil")),
            CanonicalEvent.status.in_(tuple(UPCOMING_EVENT_STATUSES)),
        )
    ).all()
    if not upcoming:
        # Never fall through to the sample JSON roster when a session is bound.
        return []
    event_ids = [event.id for event in upcoming]
    bouts = session.scalars(
        select(CanonicalBout).where(CanonicalBout.event_id.in_(event_ids))
    ).all()
    fighter_ids: set[str] = set()
    active_bout_ids: list[str] = []
    for bout in bouts:
        status = (bout.status or "scheduled").strip().casefold()
        if status in EXCLUDED_UPCOMING_BOUT_STATUSES:
            continue
        fighter_ids.add(bout.fighter_a_id)
        fighter_ids.add(bout.fighter_b_id)
        active_bout_ids.append(bout.id)
    if active_bout_ids:
        participants = session.scalars(
            select(BoutParticipant).where(BoutParticipant.bout_id.in_(active_bout_ids))
        ).all()
        for participant in participants:
            fighter_ids.add(participant.fighter_id)
    if not fighter_ids:
        return []
    fighters = session.scalars(
        select(CanonicalFighter)
        .where(CanonicalFighter.id.in_(tuple(fighter_ids)))
        .order_by(CanonicalFighter.id.asc())
    ).all()
    id_rows = session.scalars(
        select(FighterSourceId).where(FighterSourceId.fighter_id.in_(tuple(fighter_ids)))
    ).all()
    ids_by_fighter: dict[str, dict[str, str]] = {}
    for row in id_rows:
        if row.external_id:
            ids_by_fighter.setdefault(row.fighter_id, {})[row.source] = row.external_id
    out: list[FighterSeed] = []
    for fighter in fighters:
        db_ids = dict(ids_by_fighter.get(fighter.id) or {})
        json_match = _match_json_seed(json_seeds, fighter, db_ids)
        json_ids = dict(json_match.source_ids) if json_match else {}
        merged = _merge_source_ids(json_ids=json_ids, db_ids=db_ids)
        wikidata = merged.get(SOURCE_WIKIDATA)
        if json_match and json_match.wikidata_id:
            wikidata = wikidata or json_match.wikidata_id
        out.append(
            FighterSeed(
                display_name=fighter.display_name,
                source_ids=merged,
                canonical_id=fighter.id,
                wikidata_id=wikidata,
            )
        )
    return out


def _adapter_for(
    source: str,
    *,
    fixture_roots: Mapping[str, Path],
    clients: Mapping[str, Any],
    raw_store: Any,
) -> Any:
    root = fixture_roots.get(source)
    client = clients.get(source)
    if source == SOURCE_TAPOLOGY:
        if root is not None:
            return TapologyPublicAdapter.for_fixtures(fixture_root=root, raw_store=raw_store)
        return TapologyPublicAdapter(client=client, raw_store=raw_store)
    if source == SOURCE_SHERDOG:
        if root is not None:
            return SherdogPublicAdapter.for_fixtures(fixture_root=root, raw_store=raw_store)
        return SherdogPublicAdapter(client=client, raw_store=raw_store)
    if source == SOURCE_COMBAT_REGISTRY:
        if root is not None:
            return CombatRegistryPublicAdapter.for_fixtures(
                fixture_root=root, raw_store=raw_store
            )
        return CombatRegistryPublicAdapter(client=client, raw_store=raw_store)
    raise ValueError(f"unsupported regional source: {source}")


def _bout_key(obs: SourceObservationRecord) -> str:
    attrs = dict(obs.attributes)
    date_part = str(attrs.get("event_date") or "undated")
    opponent = str(
        attrs.get("opponent_canonical_id")
        or attrs.get("opponent_external_id")
        or attrs.get("opponent_name")
        or ""
    ).casefold()
    event = str(attrs.get("event_external_id") or attrs.get("event_name") or "").casefold()
    return f"{date_part}|{opponent}|{event}"


def sync_regional_history(
    *,
    repo: IngestRepository,
    session_factory: sessionmaker[Session] | SessionFactory,
    fighters: Sequence[FighterSeed],
    fixture_roots: Mapping[str, Path] | None = None,
    clients: Mapping[str, Any] | None = None,
    sources: Sequence[str] | None = None,
    dry_run: bool = False,
    observed_at: datetime | None = None,
    actor: str = "system",
) -> HistorySyncReport:
    observed = observed_at or _utc_now()
    enabled = tuple(sources or REGIONAL_FALLBACK_ORDER)
    roots = dict(fixture_roots or {})
    live_clients = dict(clients or {})
    report = HistorySyncReport(dry_run=dry_run, fighters=len(fighters), inserted_observations=0, skipped_identical=0, batches_committed=0)
    report.licensed_optional = licensed_optional_validation_status(observed_at=observed)
    killed: set[str] = set()
    resolve_results: list[ResolveResult] = []
    collected: list[SourceObservationRecord] = []

    for source in enabled:
        if source in killed:
            continue
        adapter = _adapter_for(
            source,
            fixture_roots=roots,
            clients=live_clients,
            raw_store=repo._raw_store,
        )
        run = None if dry_run else repo.start_run(
            source=source, stream="fighter_history", scope="upcoming-dwcs"
        )
        frontier = RegionalFrontier(source=source)
        with session_factory() as session:
            frontier.seed(
                session,
                entity_kind="fighter",
                entity_ids=[
                    seed.source_ids.get(source, "")
                    for seed in fighters
                    if seed.source_ids.get(source)
                ],
            )
            if not dry_run:
                session.commit()

        for seed in fighters:
            external_id = seed.source_ids.get(source)
            if not external_id:
                has_any_id = any(seed.source_ids.get(item) for item in enabled)
                if not has_any_id:
                    evidence = {
                        "reason": "missing_source_id",
                        "display_name": seed.display_name,
                        "canonical_id": seed.canonical_id,
                        "source": source,
                    }
                    report.source_failed.append(
                        {
                            "source": source,
                            "reason": "missing_source_id",
                            "evidence": evidence,
                        }
                    )
                    report.notes.append(f"{source}:{seed.display_name} missing_source_id")
                    report.unresolved_source_ids += 1
                    report.blockers.append(
                        f"missing_source_id:{source}:{seed.display_name}"
                    )
                    if not dry_run:
                        fail_obs = source_failure_observation(
                            source=source,
                            reason="missing_source_id",
                            observed_at=observed,
                            payload_hash=_hash_payload(evidence),
                            scope="upcoming-dwcs",
                            subject=seed.canonical_id or seed.display_name,
                            checkpoint_token=f"missing_source_id:{source}",
                            evidence=evidence,
                        )
                        if run is None:
                            run = repo.start_run(
                                source=source,
                                stream="fighter_history",
                                scope="upcoming-dwcs",
                            )
                        repo.commit_batch(
                            run_id=run.id,
                            observations=[fail_obs],
                            checkpoint_token=(
                                f"missing_source_id:{source}:{seed.display_name}"
                            ),
                            checkpoint_version=CHECKPOINT_VERSION,
                        )
                continue
            try:
                with session_factory() as session:
                    resolved = resolve_regional_fighter(
                        session,
                        source=source,
                        external_id=external_id,
                        display_name=seed.display_name,
                        wikidata_id=seed.wikidata_id,
                        actor=actor,
                        now=observed,
                    )
                    if not dry_run:
                        session.commit()
                    resolve_results.append(resolved)
                    ident = identity_status_from_result(resolved)
                    canonical_id = resolved.canonical_id
                rows = list(
                    adapter.iter_fighter_observations(
                        fighter_external_id=external_id,
                        observed_at=observed,
                        identity_status=ident,
                        fighter_canonical_id=canonical_id,
                    )
                )
                patched: list[SourceObservationRecord] = []
                for row in rows:
                    attrs = dict(row.attributes)
                    attrs["identity_status"] = ident
                    attrs["fighter_canonical_id"] = canonical_id
                    patched.append(row.model_copy(update={"attributes": attrs}))
                collected.extend(patched)
                if dry_run:
                    continue
                assert run is not None
                result = repo.commit_batch(
                    run_id=run.id,
                    observations=patched,
                    checkpoint_token=f"fighter:{external_id}",
                    checkpoint_version=CHECKPOINT_VERSION,
                )
                report.inserted_observations += result.inserted
                report.skipped_identical += result.skipped_identical
                report.batches_committed += 1
                with session_factory() as session:
                    frontier.mark(
                        session,
                        entity_kind="fighter",
                        entity_id=external_id,
                        status="done" if ident != "blocked" else "blocked",
                    )
                    session.commit()
            except SourceBlockedError as exc:
                killed.add(source)
                evidence = {
                    "reason": exc.reason,
                    "host": exc.host,
                    "status_code": exc.status_code,
                    "fighter_external_id": external_id,
                }
                report.source_failed.append(
                    {"source": source, "reason": exc.reason, "evidence": evidence}
                )
                report.killed_sources.append(source)
                report.notes.append(
                    f"{source} killed ({exc.reason}); not inferred as zero coverage"
                )
                if not dry_run:
                    fail_obs = source_failure_observation(
                        source=source,
                        reason=exc.reason,
                        observed_at=observed,
                        payload_hash=_hash_payload(evidence),
                        scope="upcoming-dwcs",
                        subject=str(external_id),
                        host=exc.host,
                        http_status=exc.status_code,
                        checkpoint_token=f"killed:{exc.reason}",
                        evidence=evidence,
                    )
                    assert run is not None
                    repo.commit_batch(
                        run_id=run.id,
                        observations=[fail_obs],
                        checkpoint_token=f"killed:{exc.reason}",
                        checkpoint_version=CHECKPOINT_VERSION,
                    )
                break
            except SCHEMA_DRIFT_TYPES as exc:
                killed.add(source)
                evidence = {
                    "reason": "schema_drift",
                    "detail": str(exc),
                    "fighter_external_id": external_id,
                }
                report.source_failed.append(
                    {"source": source, "reason": "schema_drift", "evidence": evidence}
                )
                report.killed_sources.append(source)
                if not dry_run:
                    fail_obs = source_failure_observation(
                        source=source,
                        reason="schema_drift",
                        observed_at=observed,
                        payload_hash=_hash_payload(evidence),
                        scope="upcoming-dwcs",
                        subject=str(external_id),
                        checkpoint_token="killed:schema_drift",
                        evidence=evidence,
                    )
                    assert run is not None
                    repo.commit_batch(
                        run_id=run.id,
                        observations=[fail_obs],
                        checkpoint_token="killed:schema_drift",
                        checkpoint_version=CHECKPOINT_VERSION,
                    )
                break
            except FileNotFoundError as exc:
                evidence = {
                    "reason": "missing_page",
                    "fighter_external_id": external_id,
                    "path": str(exc),
                }
                report.source_failed.append(
                    {"source": source, "reason": "missing_page", "evidence": evidence}
                )
                report.notes.append(f"{source}:{external_id} missing_page")
                report.blockers.append(f"missing_page:{source}:{external_id}")
                if not dry_run:
                    fail_obs = source_failure_observation(
                        source=source,
                        reason="missing_page",
                        observed_at=observed,
                        payload_hash=_hash_payload(evidence),
                        scope="upcoming-dwcs",
                        subject=str(external_id),
                        checkpoint_token=f"missing_page:{external_id}",
                        evidence=evidence,
                    )
                    assert run is not None
                    repo.commit_batch(
                        run_id=run.id,
                        observations=[fail_obs],
                        checkpoint_token=f"missing_page:{external_id}",
                        checkpoint_version=CHECKPOINT_VERSION,
                    )
                continue
        if run is not None and not dry_run:
            status = "failed" if source in killed else "succeeded"
            repo.finish_run(
                run.id,
                status=status,
                error_class="source_killed" if source in killed else None,
            )

    conflicts = _emit_result_conflicts(collected, observed_at=observed)
    report.conflicts = len(conflicts)
    if conflicts and not dry_run:
        run = repo.start_run(
            source=SOURCE_TAPOLOGY, stream="conflicts", scope="upcoming-dwcs"
        )
        result = repo.commit_batch(
            run_id=run.id,
            observations=conflicts,
            checkpoint_token="conflicts",
            checkpoint_version=CHECKPOINT_VERSION,
        )
        report.inserted_observations += result.inserted
        report.batches_committed += 1
        repo.finish_run(run.id, status="succeeded")

    with session_factory() as session:
        report.identity = identity_summary(resolve_results, session=session)
        if report.unresolved_source_ids:
            report.identity["unresolved"] = (
                int(report.identity.get("unresolved") or 0) + report.unresolved_source_ids
            )
    return report


def _emit_result_conflicts(
    rows: Sequence[SourceObservationRecord],
    *,
    observed_at: datetime,
) -> list[SourceObservationRecord]:
    bouts = [row for row in rows if row.entity_kind == "regional_bout"]
    grouped: dict[str, list[SourceObservationRecord]] = {}
    for row in bouts:
        grouped.setdefault(_bout_key(row), []).append(row)
    out: list[SourceObservationRecord] = []
    for key, group in sorted(grouped.items()):
        results = {(row.source, str(row.attributes.get("result"))) for row in group}
        unique_results = {item[1] for item in results}
        if len(unique_results) <= 1:
            continue
        left, right = group[0], group[-1]
        detail = {
            "bout_key": key,
            "results": sorted(unique_results),
        }
        out.append(
            conflict_observation(
                source=left.source,
                conflict_type="result",
                conflict_key=f"result:{key}",
                left_source=left.source,
                left_external_id=left.external_id,
                right_source=right.source,
                right_external_id=right.external_id,
                observed_at=observed_at,
                effective_at=left.effective_at,
                payload_hash=_hash_payload(detail),
                detail=detail,
                fighter_canonical_id=str(left.attributes.get("fighter_canonical_id") or "")
                or None,
            )
        )
    return out


def compare_explicit_pre_fight(
    session: Session,
    *,
    fighter_id: str,
    cutoff: datetime,
    explicit_wins: int | None,
    explicit_losses: int | None,
    explicit_draws: int | None = None,
    explicit_nc: int | None = None,
) -> dict[str, Any]:
    reconstructed = reconstruct_pre_fight_record(
        fighter_id=fighter_id, cutoff=cutoff, session=session
    )
    comparable = (
        explicit_wins is not None
        and explicit_losses is not None
        and explicit_draws is not None
    )
    if not comparable:
        return {
            "comparable": False,
            "exclusion": "explicit_record_incomplete",
            "reconstructed": reconstructed.comparable_tuple(),
        }
    expected = (
        explicit_wins,
        explicit_losses,
        explicit_draws if explicit_draws is not None else 0,
        explicit_nc if explicit_nc is not None else 0,
    )
    actual = reconstructed.comparable_tuple()
    if actual is None:
        return {
            "comparable": False,
            "exclusion": "reconstructed_unknown",
            "reconstructed": None,
        }
    return {
        "comparable": True,
        "agree": actual == expected,
        "reconstructed": actual,
        "explicit": expected,
        "exclusion": None,
    }
