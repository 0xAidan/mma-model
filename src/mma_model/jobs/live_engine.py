"""Live weekly-engine helpers for discover / ingest / identity / score / preview."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mma_model.db.tables.core import CanonicalBout, CanonicalFighter, FighterSourceId
from mma_model.history.sync import load_upcoming_dwcs_fighters, sync_regional_history
from mma_model.identity.resolver import IdentityResolver
from mma_model.ingest.raw_store import ContentAddressedRawStore
from mma_model.ingest.repository import IngestRepository
from mma_model.jobs.discover_live import (
    DiscoverEventPage,
    DiscoverResult,
    active_bout_ids,
    fetch_live_listing_and_pages,
    persist_from_listing,
)
from mma_model.jobs.horizons import PREVIEW_EVENT_HORIZON, TICK_EVENT_HORIZON
from mma_model.jobs.types import DueJob, EventContext, HandlerResult, JobErrorClass, JobStatus
from mma_model.modeling.registry import load_model_registry
from mma_model.odds.events_for_schedule import load_upcoming_dwcs_events_from_db
from mma_model.observability.health import HealthReport
from mma_model.publish.publisher import publish_dashboard
from mma_model.sources.http.block_signals import SourceBlockedError
from mma_model.sources.policy import SourceId

UFCSTATS_SOURCE = SourceId.UFCSTATS_PUBLIC.value
SEAM_ARTIFACT = "incumbent-artifact-v1"


def context_is_live(context: Mapping[str, Any]) -> bool:
    return bool(context.get("live") or context.get("require_champion"))


def has_discover_input(context: Mapping[str, Any]) -> bool:
    return bool(
        context.get("discover_listing")
        or context.get("discover_event_pages")
        or context.get("discover_runner")
        or context_is_live(context)
    )


def has_ingest_input(context: Mapping[str, Any]) -> bool:
    return bool(
        context.get("fixture_roots")
        or context.get("history_clients")
        or context_is_live(context)
    )


def next_preview_event_id(session: Session, *, as_of) -> str | None:
    rows = load_upcoming_dwcs_events_from_db(
        session, as_of=as_of, horizon=PREVIEW_EVENT_HORIZON
    )
    if not rows:
        return None
    return str(rows[0]["event_id"])


def bout_ids_for_event(
    session: Session,
    *,
    job: DueJob,
    events: Sequence[EventContext],
) -> tuple[EventContext | None, tuple[str, ...]]:
    event = next((item for item in events if item.event_id == job.event_id), None)
    if event is not None and event.bout_ids:
        return event, event.bout_ids
    if job.event_id:
        return event, active_bout_ids(session, job.event_id)
    return event, ()


def _pages_from_context(context: Mapping[str, Any]) -> dict[str, DiscoverEventPage]:
    raw = context.get("discover_event_pages") or {}
    pages: dict[str, DiscoverEventPage] = {}
    for key, value in dict(raw).items():
        if isinstance(value, DiscoverEventPage):
            pages[str(key)] = value
            continue
        if not isinstance(value, Mapping):
            continue
        event_date = value.get("event_date")
        pages[str(key)] = DiscoverEventPage(
            event_name=str(value.get("event_name") or ""),
            date_text=str(value.get("date_text") or ""),
            event_date=event_date if event_date is not None else None,
            location=str(value.get("location") or ""),
            fights=tuple(value.get("fights") or ()),
            cancelled=bool(value.get("cancelled")),
        )
    return pages


def run_discover(
    session: Session,
    *,
    as_of,
    context: Mapping[str, Any],
) -> HandlerResult:
    runner = context.get("discover_runner")
    if callable(runner):
        result = runner(session, as_of=as_of, context=context)
        if isinstance(result, HandlerResult):
            return result
        if isinstance(result, DiscoverResult):
            written = result
        else:
            return HandlerResult(
                status=JobStatus.FAILED,
                error_class=JobErrorClass.INTERNAL,
                detail="discover_runner returned an unsupported type",
                blocks_downstream=True,
            )
    else:
        listing = context.get("discover_listing")
        pages = _pages_from_context(context)
        if listing is None and context_is_live(context):
            cache_dir = Path(
                str(context.get("cache_dir") or tempfile.mkdtemp(prefix="dwcs-discover-"))
            )
            try:
                listing, pages = fetch_live_listing_and_pages(cache_dir=cache_dir)
            except SourceBlockedError as exc:
                return HandlerResult(
                    status=JobStatus.FAILED,
                    error_class=JobErrorClass.ENTITLEMENT,
                    detail=f"discover blocked by UFCStats robots/source policy: {exc}",
                    blocks_downstream=True,
                )
        if listing is None:
            return HandlerResult(
                status=JobStatus.FAILED,
                error_class=JobErrorClass.SCHEMA,
                detail="discover requires listing fixtures or --live",
                blocks_downstream=True,
            )
        written = persist_from_listing(
            session,
            listing=list(listing),
            pages=pages,
        )

    preview_counts: dict[str, Any] = {}
    preview_release: str | None = None
    publish_root = context.get("publish_root")
    if publish_root:
        preview = publish_preview_card(
            session,
            as_of=as_of,
            publish_root=Path(str(publish_root)),
            event_id=(
                written.event_ids[0]
                if written.event_ids
                else next_preview_event_id(session, as_of=as_of)
            ),
            health=(
                context.get("health")
                if isinstance(context.get("health"), HealthReport)
                else None
            ),
        )
        preview_counts = dict(preview.counts)
        preview_release = preview.current_release_id
        if preview.status is JobStatus.FAILED:
            return HandlerResult(
                status=JobStatus.FAILED,
                error_class=preview.error_class,
                detail=f"{written.detail}; preview publish failed: {preview.detail}",
                counts={
                    "events_written": written.events_written,
                    "bouts_written": written.bouts_written,
                    "fighters_written": written.fighters_written,
                    **preview_counts,
                },
                current_release_id=preview.current_release_id,
                blocks_downstream=True,
            )

    return HandlerResult(
        status=JobStatus.SUCCESS,
        counts={
            "events_written": written.events_written,
            "bouts_written": written.bouts_written,
            "fighters_written": written.fighters_written,
            "events_seen": written.events_written,
            **preview_counts,
        },
        current_release_id=preview_release,
        detail=written.detail,
    )


def publish_preview_card(
    session: Session,
    *,
    as_of,
    publish_root: Path,
    event_id: str | None,
    health: HealthReport | None = None,
) -> HandlerResult:
    """Replace demo live/ JSON with a paper card or an honest empty state."""
    release_id = f"release-preview-{event_id or 'empty'}-{as_of.date().isoformat()}"
    try:
        outcome = publish_dashboard(
            session,
            output_root=publish_root,
            release_id=release_id,
            event_id=event_id,
            window_slot="preview",
            publications=0,
            as_of=as_of,
            health=health,
        )
    except Exception as exc:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.SCHEMA,
            detail=f"preview publish failed; prior live/ kept: {exc}",
            blocks_downstream=True,
        )
    return HandlerResult(
        status=JobStatus.SUCCESS,
        current_release_id=outcome.current_release_id,
        counts={
            "preview_published": 1,
            "release_id": outcome.current_release_id,
            "preview_event_id": event_id or "",
        },
        detail="preview publish: paper card or honest empty state",
    )


def run_ingest_history(
    session: Session,
    *,
    as_of,
    context: Mapping[str, Any],
) -> HandlerResult:
    fighters = load_upcoming_dwcs_fighters(session=session)
    if not fighters:
        return HandlerResult(
            status=JobStatus.SUCCESS,
            counts={"profiles": 0, "histories": 0},
            detail="ingest-history: no upcoming DWCS fighters in DB",
        )

    fixture_roots = context.get("fixture_roots") or {}
    clients = dict(context.get("history_clients") or {})
    tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
    created_clients = False
    try:
        if (
            context_is_live(context)
            and bool(context.get("allow_live_http"))
            and not fixture_roots
            and not clients
        ):
            from mma_model.sources.combat_registry.client import CombatRegistryPublicClient
            from mma_model.sources.sherdog_public.client import SherdogPublicClient
            from mma_model.sources.tapology_public.client import TapologyPublicClient

            cache_dir = context.get("cache_dir")
            if cache_dir is None:
                tmp_ctx = tempfile.TemporaryDirectory(prefix="dwcs-history-")
                cache_dir = Path(tmp_ctx.name)
            else:
                cache_dir = Path(str(cache_dir))
            clients = {
                "tapology_public": TapologyPublicClient(cache_dir=cache_dir / "tapology"),
                "sherdog_public": SherdogPublicClient(cache_dir=cache_dir / "sherdog"),
                "combat_registry": CombatRegistryPublicClient(
                    cache_dir=cache_dir / "combat_registry"
                ),
            }
            created_clients = True

        if not fixture_roots and not clients:
            return HandlerResult(
                status=JobStatus.SUCCESS,
                counts={"profiles": len(fighters), "histories": 0},
                detail="ingest-history: fighters loaded; skipped network (no fixtures/clients)",
            )

        raw_dir = Path(
            str(context.get("raw_store_dir") or Path(tempfile.mkdtemp(prefix="dwcs-raw-")))
        )
        raw_dir.mkdir(parents=True, exist_ok=True)
        store = ContentAddressedRawStore(raw_dir)
        factory = sessionmaker(bind=session.get_bind(), future=True)
        repo = IngestRepository(session_factory=factory, raw_store=store)
        report = sync_regional_history(
            repo=repo,
            session_factory=factory,
            fighters=fighters,
            fixture_roots=dict(fixture_roots) or None,
            clients=clients or None,
            dry_run=bool(context.get("history_dry_run", False)),
            observed_at=as_of,
            actor="jobs.ingest-history",
        )
        return HandlerResult(
            status=JobStatus.SUCCESS,
            counts={
                "profiles": report.fighters,
                "histories": report.inserted_observations,
                "conflicts": report.conflicts,
                "unresolved_source_ids": report.unresolved_source_ids,
            },
            detail=report.human_summary(),
        )
    except SourceBlockedError as exc:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.ENTITLEMENT,
            detail=f"ingest-history blocked: {exc}",
            counts={"profiles": len(fighters), "histories": 0},
            blocks_downstream=True,
        )
    finally:
        if created_clients:
            for client in clients.values():
                close = getattr(client, "close", None)
                if callable(close):
                    close()
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def run_identity(
    session: Session,
    *,
    job: DueJob,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
) -> HandlerResult:
    _ = context
    _event, bout_ids = bout_ids_for_event(session, job=job, events=events)
    resolver = IdentityResolver(session, actor="jobs.identity")
    unresolved: list[str] = []
    resolved = 0
    for bout_id in bout_ids:
        if resolver.is_bout_scoring_blocked(bout_id):
            unresolved.append(bout_id)
            continue
        if not _resolve_bout_fighters(session, resolver=resolver, bout_id=bout_id):
            unresolved.append(bout_id)
            continue
        resolved += 1

    if bout_ids and unresolved and len(unresolved) == len(bout_ids):
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.IDENTITY_UNRESOLVED,
            detail=f"all bouts unresolved: {unresolved}",
            blocked_bout_ids=tuple(unresolved),
            counts={"resolved": 0, "unresolved": len(unresolved)},
            blocks_downstream=True,
        )
    return HandlerResult(
        status=JobStatus.SUCCESS,
        blocked_bout_ids=tuple(unresolved),
        counts={"resolved": resolved, "unresolved": len(unresolved)},
        detail="identity: resolved UFCStats fighters; blocked unresolved bouts",
    )


def _resolve_bout_fighters(
    session: Session,
    *,
    resolver: IdentityResolver,
    bout_id: str,
) -> bool:
    bout = session.get(CanonicalBout, bout_id)
    if bout is None:
        return False
    for fighter_id in (bout.fighter_a_id, bout.fighter_b_id):
        fighter = session.get(CanonicalFighter, fighter_id)
        if fighter is None or not (fighter.display_name or "").strip():
            return False
        source_row = session.scalar(
            select(FighterSourceId).where(
                FighterSourceId.fighter_id == fighter_id,
                FighterSourceId.source == UFCSTATS_SOURCE,
            )
        )
        if source_row is None or not source_row.external_id:
            return False
        result = resolver.resolve_fighter(
            source=UFCSTATS_SOURCE,
            external_id=source_row.external_id,
            display_name=fighter.display_name,
            bout_id=bout_id,
            bout_status=bout.status,
            create_if_absent=False,
        )
        if result.kind in {"queued", "blocked"} or result.canonical_id is None:
            return False
    return True


def run_score(
    session: Session,
    *,
    job: DueJob,
    events: Sequence[EventContext],
    context: Mapping[str, Any],
    prior_digest: str,
    blocked: Sequence[str],
) -> HandlerResult:
    registry_path = context.get("model_registry_path")
    try:
        state = load_model_registry(
            path=Path(str(registry_path)) if registry_path is not None else None,
            enforce_pinned_digest=False,
        )
    except Exception as exc:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.INTERNAL,
            detail=f"score failed closed: registry unreadable: {exc}",
            artifact_digest=prior_digest if prior_digest != SEAM_ARTIFACT else None,
            blocked_bout_ids=tuple(blocked),
            counts={"scored": 0, "blocked": len(blocked)},
            blocks_downstream=True,
        )

    digest = state.champion.artifact_digest
    relpath = state.champion.artifact_relpath
    if not digest or digest == SEAM_ARTIFACT:
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.DEPENDENCY_BLOCKED,
            detail="score failed closed: no champion artifact; retaining prior digest",
            artifact_digest=prior_digest if prior_digest != SEAM_ARTIFACT else None,
            blocked_bout_ids=tuple(blocked),
            counts={"scored": 0, "blocked": len(blocked)},
            blocks_downstream=True,
        )

    artifact_path = _resolve_artifact_path(relpath)
    if artifact_path is None or not artifact_path.is_file():
        return HandlerResult(
            status=JobStatus.FAILED,
            error_class=JobErrorClass.DEPENDENCY_BLOCKED,
            detail="score failed closed: champion artifact file missing; retaining prior digest",
            artifact_digest=digest,
            blocked_bout_ids=tuple(blocked),
            counts={"scored": 0, "blocked": len(blocked)},
            blocks_downstream=True,
        )

    _event, bout_ids = bout_ids_for_event(session, job=job, events=events)
    scored = len([bout_id for bout_id in bout_ids if bout_id not in set(blocked)])
    return HandlerResult(
        status=JobStatus.SUCCESS,
        artifact_digest=digest,
        blocked_bout_ids=tuple(blocked),
        counts={"scored": scored, "blocked": len(blocked)},
        detail="score: incumbent champion artifact present (paper / fail-closed if unused)",
    )


def _resolve_artifact_path(relpath: str | None) -> Path | None:
    if not relpath:
        return None
    path = Path(relpath)
    if path.is_file():
        return path
    from mma_model.config import get_settings

    candidate = get_settings().project_root / relpath
    return candidate if candidate.is_file() else path


def is_preview_publish(job: DueJob, context: Mapping[str, Any]) -> bool:
    if bool(context.get("preview")):
        return True
    return str(job.window_slot or "") == "preview"


__all__ = [
    "PREVIEW_EVENT_HORIZON",
    "SEAM_ARTIFACT",
    "TICK_EVENT_HORIZON",
    "bout_ids_for_event",
    "context_is_live",
    "has_discover_input",
    "has_ingest_input",
    "is_preview_publish",
    "next_preview_event_id",
    "publish_preview_card",
    "run_discover",
    "run_identity",
    "run_ingest_history",
    "run_score",
]
