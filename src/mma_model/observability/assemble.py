"""Production health assembly from DB, artifacts, backup stamp, and publish root."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mma_model.config import get_settings
from mma_model.db.tables.core import CanonicalBout, FighterSourceId
from mma_model.db.tables.identity import IdentityScoringBlock
from mma_model.db.tables.odds import OddsQuotaObservation
from mma_model.db.tables.recommendations import OfficialPublication, PredictionGrade
from mma_model.observability.health import (
    HEALTH_COMPONENT_NAMES,
    HealthComponent,
    HealthReport,
    HealthStatus,
    build_health_report,
    make_component,
)
from mma_model.observability.publish_guard import FilesystemPublishPointer
from mma_model.odds.events_for_schedule import load_upcoming_dwcs_events_from_db
from mma_model.sources.espn_public.parser import ESPN_IDENTITY_SOURCE
from mma_model.sources.policy import SourceId

UFCSTATS_SOURCE = SourceId.UFCSTATS_PUBLIC.value
LIVE_IDENTITY_SOURCES = (ESPN_IDENTITY_SOURCE, UFCSTATS_SOURCE)
BACKUP_STALE_AFTER = timedelta(hours=36)
PUBLISH_STALE_AFTER = timedelta(hours=26)
QUOTA_STALE_AFTER = timedelta(hours=72)
SEAM_ARTIFACT = "incumbent-artifact-v1"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_stamp(text: str) -> datetime | None:
    raw = text.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _aware(parsed)


def _resolve_backup_stamp(*, backup_stamp_path: Path | None, data_dir: Path | None) -> Path | None:
    if backup_stamp_path is not None:
        return Path(backup_stamp_path)
    if data_dir is not None:
        return Path(data_dir) / "backup.last_ok"
    env_dir = os.environ.get("MMA_DATA_DIR")
    if env_dir:
        return Path(env_dir) / "backup.last_ok"
    return None


def _sources_component(session: Session, *, as_of: datetime) -> HealthComponent:
    rows = load_upcoming_dwcs_events_from_db(
        session, as_of=as_of, horizon=timedelta(days=120)
    )
    if not rows:
        return make_component(
            "sources",
            HealthStatus.MISSING,
            detail="no upcoming DWCS card in the production database",
            as_of=_iso(as_of),
            counts={"upcoming_events": 0},
        )
    bout_count = sum(len(item.get("bout_ids") or ()) for item in rows)
    if bout_count == 0:
        return make_component(
            "sources",
            HealthStatus.BLOCKED,
            detail="upcoming DWCS event has no active bouts",
            as_of=_iso(as_of),
            counts={"upcoming_events": len(rows), "bouts": 0},
        )
    return make_component(
        "sources",
        HealthStatus.HEALTHY,
        detail=f"{len(rows)} upcoming DWCS event(s), {bout_count} bout(s)",
        as_of=_iso(as_of),
        counts={"upcoming_events": len(rows), "bouts": bout_count},
    )


def _identity_component(session: Session, *, as_of: datetime) -> HealthComponent:
    blocked = int(
        session.scalar(
            select(func.count())
            .select_from(IdentityScoringBlock)
            .where(IdentityScoringBlock.active.is_(True))
        )
        or 0
    )
    upcoming = load_upcoming_dwcs_events_from_db(
        session, as_of=as_of, horizon=timedelta(days=120)
    )
    unresolved = 0
    checked = 0
    for item in upcoming:
        for bout_id in item.get("bout_ids") or ():
            checked += 1
            bout = session.get(CanonicalBout, str(bout_id))
            if bout is None:
                unresolved += 1
                continue
            for fighter_id in (bout.fighter_a_id, bout.fighter_b_id):
                source = session.scalar(
                    select(FighterSourceId).where(
                        FighterSourceId.fighter_id == fighter_id,
                        FighterSourceId.source.in_(LIVE_IDENTITY_SOURCES),
                    )
                )
                if source is None or not source.external_id:
                    unresolved += 1
                    break
    if blocked or unresolved:
        return make_component(
            "identity",
            HealthStatus.BLOCKED,
            detail=(
                f"{blocked} active scoring block(s); "
                f"{unresolved} upcoming bout(s) missing ESPN or UFCStats identity"
            ),
            as_of=_iso(as_of),
            counts={
                "scoring_blocks": blocked,
                "unresolved_bouts": unresolved,
                "checked_bouts": checked,
            },
        )
    if checked == 0:
        return make_component(
            "identity",
            HealthStatus.MISSING,
            detail="no upcoming bouts to resolve",
            as_of=_iso(as_of),
            counts={"checked_bouts": 0},
        )
    return make_component(
        "identity",
        HealthStatus.HEALTHY,
        detail=f"{checked} upcoming bout(s) have ESPN or UFCStats source ids",
        as_of=_iso(as_of),
        counts={"checked_bouts": checked, "scoring_blocks": 0},
    )


def _odds_and_quota(
    session: Session, *, as_of: datetime
) -> tuple[HealthComponent, HealthComponent]:
    latest = session.scalar(
        select(OddsQuotaObservation)
        .order_by(OddsQuotaObservation.observed_at.desc(), OddsQuotaObservation.id.desc())
        .limit(1)
    )
    api_key = (os.environ.get("THE_ODDS_API_KEY") or "").strip()
    key_missing = (not api_key) or ("PLACEHOLDER" in api_key.upper())
    if latest is None:
        odds = make_component(
            "odds",
            HealthStatus.MISSING,
            detail=(
                "no odds quota observation; sportsbook lines optional for paper preview"
                + ("; API key not configured" if key_missing else "")
            ),
            as_of=_iso(as_of),
        )
        quota = make_component(
            "quota",
            HealthStatus.MISSING,
            detail="odds quota has not been probed",
            as_of=_iso(as_of),
        )
        return odds, quota
    observed = _aware(latest.observed_at)
    age = as_of - observed
    stale = age > QUOTA_STALE_AFTER
    status = HealthStatus.STALE if stale else HealthStatus.HEALTHY
    detail = f"last quota observation { _iso(observed) }"
    if key_missing:
        detail += "; API key not configured (paper targets only)"
    odds = make_component(
        "odds",
        status,
        detail=detail,
        as_of=_iso(as_of),
        counts={"age_hours": round(age.total_seconds() / 3600, 2)},
    )
    quota = make_component(
        "quota",
        status,
        detail=detail,
        as_of=_iso(as_of),
    )
    return odds, quota


def _model_component(*, as_of: datetime) -> HealthComponent:
    # Deferred: modeling.registry -> artifacts -> backtest is circular if this
    # module is imported while artifacts is still loading.
    from mma_model.modeling.registry import load_model_registry

    try:
        state = load_model_registry(enforce_pinned_digest=False)
    except Exception as exc:
        return make_component(
            "model",
            HealthStatus.FAILED,
            detail=f"model registry unreadable: {exc}",
            as_of=_iso(as_of),
        )
    digest = state.champion.artifact_digest
    relpath = state.champion.artifact_relpath
    if not digest or digest == SEAM_ARTIFACT:
        return make_component(
            "model",
            HealthStatus.BLOCKED,
            detail="no champion artifact; scoring fails closed (paper / No-bet only)",
            as_of=_iso(as_of),
        )
    path = Path(relpath) if relpath else None
    if path is not None and not path.is_file():
        candidate = get_settings().project_root / relpath
        path = candidate if candidate.is_file() else path
    if path is None or not path.is_file():
        return make_component(
            "model",
            HealthStatus.FAILED,
            detail="champion digest is set but the artifact file is missing",
            as_of=_iso(as_of),
            hashes={"artifact_digest": digest},
        )
    return make_component(
        "model",
        HealthStatus.HEALTHY,
        detail="champion artifact present",
        as_of=_iso(as_of),
        hashes={"artifact_digest": digest},
    )


def _publish_and_staleness(
    *,
    as_of: datetime,
    publish_root: Path | None,
) -> tuple[HealthComponent, HealthComponent]:
    if publish_root is None:
        missing = make_component(
            "publish",
            HealthStatus.MISSING,
            detail="publish root not provided; live/ JSON not inspected",
            as_of=_iso(as_of),
        )
        stale = make_component(
            "staleness",
            HealthStatus.MISSING,
            detail="cannot judge freshness without a publish root",
            as_of=_iso(as_of),
        )
        return missing, stale
    pointer = FilesystemPublishPointer(publish_root)
    release_id = pointer.current_release_id
    live_event = Path(publish_root) / "live" / "current-event.json"
    if release_id is None and not live_event.is_file():
        return (
            make_component(
                "publish",
                HealthStatus.MISSING,
                detail="no current release pointer and no live/current-event.json",
                as_of=_iso(as_of),
            ),
            make_component(
                "staleness",
                HealthStatus.MISSING,
                detail="no published dashboard to age",
                as_of=_iso(as_of),
            ),
        )
    mtime = None
    if live_event.is_file():
        mtime = datetime.fromtimestamp(live_event.stat().st_mtime, tz=UTC)
    age = (as_of - mtime) if mtime is not None else None
    stale = age is not None and age > PUBLISH_STALE_AFTER
    demo = False
    if live_event.is_file():
        text = live_event.read_text(encoding="utf-8")
        demo = '"evt-1"' in text and "fixture" in text
    if demo:
        pub_status = HealthStatus.STALE
        pub_detail = "live/ still has the bootstrap evt-1 demo card"
    elif release_id:
        pub_status = HealthStatus.HEALTHY
        pub_detail = f"current release {release_id}"
    else:
        pub_status = HealthStatus.MISSING
        pub_detail = "live JSON exists but current pointer is missing"
    publish = make_component(
        "publish",
        pub_status,
        detail=pub_detail,
        as_of=_iso(as_of),
        counts={"has_live_json": int(live_event.is_file())},
    )
    if age is None:
        staleness = make_component(
            "staleness",
            HealthStatus.MISSING,
            detail="live/current-event.json mtime unavailable",
            as_of=_iso(as_of),
        )
    elif stale:
        staleness = make_component(
            "staleness",
            HealthStatus.STALE,
            detail=f"last live JSON is {round(age.total_seconds() / 3600, 1)}h old",
            as_of=_iso(as_of),
        )
    else:
        staleness = make_component(
            "staleness",
            HealthStatus.HEALTHY,
            detail="live JSON is within the freshness window",
            as_of=_iso(as_of),
        )
    return publish, staleness


def _grade_component(session: Session, *, as_of: datetime) -> HealthComponent:
    pubs = int(session.scalar(select(func.count()).select_from(OfficialPublication)) or 0)
    grades = int(session.scalar(select(func.count()).select_from(PredictionGrade)) or 0)
    if pubs == 0 and grades == 0:
        return make_component(
            "grade",
            HealthStatus.MISSING,
            detail="no official publications or grades yet (paper preview only)",
            as_of=_iso(as_of),
            counts={"publications": 0, "grades": 0},
        )
    return make_component(
        "grade",
        HealthStatus.HEALTHY,
        detail=f"{pubs} official publication(s), {grades} grade(s)",
        as_of=_iso(as_of),
        counts={"publications": pubs, "grades": grades},
    )


def _backup_component(*, as_of: datetime, stamp_path: Path | None) -> HealthComponent:
    if stamp_path is None or not stamp_path.is_file():
        return make_component(
            "backup",
            HealthStatus.MISSING,
            detail="backup.last_ok stamp not found (DWCS-505 restic not installed)",
            as_of=_iso(as_of),
        )
    parsed = _parse_stamp(stamp_path.read_text(encoding="utf-8"))
    if parsed is None:
        parsed = datetime.fromtimestamp(stamp_path.stat().st_mtime, tz=UTC)
    age = as_of - parsed
    if age > BACKUP_STALE_AFTER:
        return make_component(
            "backup",
            HealthStatus.STALE,
            detail=f"backup stamp is {round(age.total_seconds() / 3600, 1)}h old",
            as_of=_iso(as_of),
        )
    return make_component(
        "backup",
        HealthStatus.HEALTHY,
        detail=f"backup stamp { _iso(parsed) } (stub until DWCS-505)",
        as_of=_iso(as_of),
    )


def assemble_health(
    session: Session,
    *,
    as_of: datetime | None = None,
    publish_root: Path | str | None = None,
    backup_stamp_path: Path | str | None = None,
    data_dir: Path | str | None = None,
    series: str = "dwcs",
) -> HealthReport:
    """Inspect live disk/DB state. Never invent a healthy report."""
    stamp = as_of or datetime.now(tz=UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    root = Path(publish_root) if publish_root is not None else None
    data = Path(data_dir) if data_dir is not None else None
    backup_path = _resolve_backup_stamp(
        backup_stamp_path=Path(backup_stamp_path) if backup_stamp_path else None,
        data_dir=data,
    )
    odds, quota = _odds_and_quota(session, as_of=stamp)
    publish, staleness = _publish_and_staleness(as_of=stamp, publish_root=root)
    components = [
        _sources_component(session, as_of=stamp),
        _identity_component(session, as_of=stamp),
        odds,
        _model_component(as_of=stamp),
        publish,
        _grade_component(session, as_of=stamp),
        _backup_component(as_of=stamp, stamp_path=backup_path),
        quota,
        staleness,
    ]
    by_name = {c.name: c for c in components}
    ordered = [by_name[name] for name in HEALTH_COMPONENT_NAMES if name in by_name]
    return build_health_report(ordered, as_of=_iso(stamp), series=series)


__all__ = ["assemble_health"]
