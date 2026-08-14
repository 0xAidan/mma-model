"""Project ledger + health into versioned dashboard JSON documents (DWCS-500)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, assert_never

from sqlalchemy import select
from sqlalchemy.orm import Session

from mma_model.db.tables.core import CanonicalBout, CanonicalEvent, CanonicalFighter
from mma_model.db.tables.recommendations import (
    ObservedPrice,
    OfficialPublication,
    Prediction,
    PredictionGrade,
    PriceTarget,
    RecommendationSettlement,
    RecommendationStateEvent,
)
from mma_model.domain.markets import RecommendationState
from mma_model.observability.health import (
    HEALTH_COMPONENT_NAMES,
    HealthReport,
    default_missing_report,
)
from mma_model.publish.constants import (
    CURRENT_EVENT_JSON,
    DASHBOARD_HEALTH_NAMES,
    DASHBOARD_RELEASE_FILES,
    HEALTH_COMPONENT_MAP,
    HEALTH_JSON,
    HISTORY_JSON,
    MANIFEST_JSON,
    MATCHUPS_JSON,
    PERFORMANCE_JSON,
    RELEASE_JSON,
)
from mma_model.publish.schema import (
    ArtifactHashes,
    ConfirmedPriceMetrics,
    CountdownFields,
    CurrentEventDocument,
    DashboardHealthComponent,
    DashboardHealthDocument,
    FieldPresence,
    FighterSummary,
    HealthStatusView,
    HistoryDocument,
    HistoryPoint,
    LaneMetricsBucket,
    LineFreshness,
    ManifestDocument,
    MatchupCardChangeWarning,
    MatchupPrices,
    MatchupRow,
    MatchupsDocument,
    ObservedPriceView,
    OptionalStringField,
    PerformanceDocument,
    PerformanceFilters,
    PerformanceLaneView,
    PredictiveMetrics,
    PriceAvailability,
    PriceTargetOnlyMetrics,
    QuoteSourceTypeView,
    ReasonBlocker,
    RecommendationStateView,
    ReleaseDocument,
    ReleaseFileEntry,
)
from mma_model.value.ev import expected_value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_str(value: str | None, *, unknown: bool = False) -> OptionalStringField:
    if value is not None and str(value).strip():
        return OptionalStringField(presence=FieldPresence.KNOWN, value=str(value).strip())
    if unknown:
        return OptionalStringField(presence=FieldPresence.UNKNOWN, value=None)
    return OptionalStringField(presence=FieldPresence.MISSING, value=None)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_reasons(raw: str | None) -> tuple[ReasonBlocker, ...]:
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return (ReasonBlocker(code="unparsed_reason", message=str(raw)[:200]),)
    if not isinstance(loaded, list):
        return ()
    out: list[ReasonBlocker] = []
    for item in loaded:
        if isinstance(item, str) and item.strip():
            out.append(ReasonBlocker(code=item.strip(), message=item.strip()))
        elif isinstance(item, dict):
            code = str(item.get("code") or item.get("reason") or "").strip()
            message = str(item.get("message") or item.get("detail") or code).strip()
            if code and message:
                out.append(ReasonBlocker(code=code, message=message))
    return tuple(out)


def _lane_view(raw: str | None) -> PerformanceLaneView:
    try:
        return PerformanceLaneView(str(raw or PerformanceLaneView.PAPER.value))
    except ValueError:
        return PerformanceLaneView.PAPER


def _map_primary_state(
    ledger_state: str,
    *,
    has_observed: bool,
    has_exact_ev: bool,
) -> RecommendationStateView:
    """Project ledger state; demote confirmed_value without observed price + EV."""
    try:
        state = RecommendationState(ledger_state)
    except ValueError:
        return RecommendationStateView.NO_BET
    if state is RecommendationState.CONFIRMED_VALUE:
        if has_observed and has_exact_ev:
            return RecommendationStateView.CONFIRMED_VALUE
        return RecommendationStateView.PRICE_TARGET
    if state is RecommendationState.PRICE_TARGET:
        return RecommendationStateView.PRICE_TARGET
    if state is RecommendationState.NO_BET:
        return RecommendationStateView.NO_BET
    assert_never(state)


def _fighter_summary(
    session: Session,
    *,
    fighter_id: str | None,
    corner: Literal["a", "b", "unknown"],
) -> FighterSummary:
    if not fighter_id:
        return FighterSummary(
            fighter_id=_optional_str(None),
            display_name=_optional_str(None, unknown=True),
            corner=corner,
        )
    fighter = session.get(CanonicalFighter, fighter_id)
    if fighter is None:
        return FighterSummary(
            fighter_id=_optional_str(fighter_id),
            display_name=_optional_str(None, unknown=True),
            corner=corner,
        )
    return FighterSummary(
        fighter_id=_optional_str(fighter_id),
        display_name=_optional_str(fighter.display_name),
        corner=corner,
    )


def _latest_observed(
    session: Session, publication_id: str
) -> ObservedPrice | None:
    return session.scalar(
        select(ObservedPrice)
        .where(ObservedPrice.official_publication_id == publication_id)
        .order_by(ObservedPrice.source_timestamp.desc(), ObservedPrice.created_at.desc())
        .limit(1)
    )


def _price_target(session: Session, price_target_id: str | None) -> PriceTarget | None:
    if not price_target_id:
        return None
    return session.get(PriceTarget, price_target_id)


def _prediction(session: Session, prediction_id: str | None) -> Prediction | None:
    if not prediction_id:
        return None
    return session.get(Prediction, prediction_id)


def _line_movement_and_warnings(
    session: Session, publication_id: str
) -> tuple[float | None, LineFreshness, tuple[MatchupCardChangeWarning, ...]]:
    events = list(
        session.scalars(
            select(RecommendationStateEvent)
            .where(RecommendationStateEvent.official_publication_id == publication_id)
            .order_by(
                RecommendationStateEvent.observed_at.asc(),
                RecommendationStateEvent.created_at.asc(),
            )
        ).all()
    )
    movement: float | None = None
    freshness = LineFreshness.UNKNOWN
    warnings: list[MatchupCardChangeWarning] = []
    for event in events:
        etype = str(event.event_type or "")
        if etype in {"replacement", "card_change", "fighter_replacement"}:
            warnings.append(
                MatchupCardChangeWarning(
                    code=str(event.reason_code or etype),
                    message=str(event.detail or "card change detected"),
                    event_type=etype,
                    observed_at=_iso(event.observed_at) or "",
                )
            )
        if etype in {"line_change", "LINE_CHANGE", "stale_line"}:
            freshness = (
                LineFreshness.STALE if "stale" in etype.lower() else LineFreshness.FRESH
            )
            try:
                payload = json.loads(event.payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                for key in ("new_decimal", "decimal_odds", "delta"):
                    if key in payload:
                        try:
                            movement = float(payload[key])
                            break
                        except (TypeError, ValueError):
                            continue
        if etype == "stale_line":
            freshness = LineFreshness.STALE
    return movement, freshness, tuple(warnings)


def _build_prices(
    *,
    target: PriceTarget | None,
    prediction: Prediction | None,
    observed: ObservedPrice | None,
    line_movement: float | None,
    line_freshness: LineFreshness,
) -> MatchupPrices:
    fair_p = float(prediction.p50) if prediction is not None else None
    if observed is None:
        availability = (
            PriceAvailability.UNAVAILABLE
            if target is None
            else PriceAvailability.AVAILABLE
        )
        # Stale without a usable observed quote still surfaces as stale/unavailable.
        if line_freshness is LineFreshness.STALE:
            availability = PriceAvailability.STALE
        return MatchupPrices(
            model_fair_probability=fair_p,
            fair_decimal=float(target.fair_decimal) if target else None,
            fair_american=float(target.fair_american) if target else None,
            actionable_decimal=float(target.actionable_decimal) if target else None,
            actionable_american=float(target.actionable_american) if target else None,
            strong_value_decimal=float(target.strong_value_decimal) if target else None,
            strong_value_american=float(target.strong_value_american) if target else None,
            observed=None,
            exact_ev=None,
            line_movement=line_movement,
            price_availability=availability,
            line_freshness=line_freshness,
        )

    observed_view = ObservedPriceView(
        decimal_odds=float(observed.decimal_odds),
        american_odds=float(observed.american_odds),
        sportsbook=str(observed.sportsbook),
        source_type=QuoteSourceTypeView(observed.source_type),
        timestamp=_iso(observed.source_timestamp) or "",
    )
    exact: float | None = None
    if fair_p is not None:
        exact = float(expected_value(fair_p, float(observed.decimal_odds)))
    availability = PriceAvailability.AVAILABLE
    if line_freshness is LineFreshness.STALE:
        availability = PriceAvailability.STALE
    return MatchupPrices(
        model_fair_probability=fair_p,
        fair_decimal=float(target.fair_decimal) if target else None,
        fair_american=float(target.fair_american) if target else None,
        actionable_decimal=float(target.actionable_decimal) if target else None,
        actionable_american=float(target.actionable_american) if target else None,
        strong_value_decimal=float(target.strong_value_decimal) if target else None,
        strong_value_american=float(target.strong_value_american) if target else None,
        observed=observed_view,
        exact_ev=exact,
        line_movement=line_movement,
        price_availability=availability,
        line_freshness=(
            line_freshness
            if line_freshness is not LineFreshness.UNKNOWN
            else LineFreshness.FRESH
        ),
    )


def _select_publications(
    session: Session,
    *,
    event_id: str | None,
) -> list[OfficialPublication]:
    stmt = select(OfficialPublication).order_by(
        OfficialPublication.published_at.asc(),
        OfficialPublication.created_at.asc(),
    )
    if event_id:
        stmt = stmt.where(OfficialPublication.event_id == event_id)
    rows = list(session.scalars(stmt).all())
    # One primary publication per bout: latest by published_at.
    by_bout: dict[str, OfficialPublication] = {}
    for row in rows:
        prev = by_bout.get(row.bout_id)
        if prev is None or row.published_at >= prev.published_at:
            by_bout[row.bout_id] = row
    return list(by_bout.values())


def build_matchups_document(
    session: Session,
    *,
    event_id: str | None,
    as_of: datetime | None = None,
) -> MatchupsDocument:
    now = as_of or _utc_now()
    pubs = _select_publications(session, event_id=event_id)
    matchups: list[MatchupRow] = []
    confirmed: list[tuple[float, str]] = []
    watchlist: list[str] = []
    no_bets: list[str] = []

    for pub in pubs:
        observed = _latest_observed(session, pub.id)
        target = _price_target(session, pub.price_target_id)
        prediction = _prediction(session, pub.prediction_id)
        movement, freshness, warnings = _line_movement_and_warnings(session, pub.id)
        prices = _build_prices(
            target=target,
            prediction=prediction,
            observed=observed,
            line_movement=movement,
            line_freshness=freshness,
        )
        primary = _map_primary_state(
            pub.state,
            has_observed=observed is not None,
            has_exact_ev=prices.exact_ev is not None,
        )
        bout = session.get(CanonicalBout, pub.bout_id)
        fighters: list[FighterSummary] = []
        if bout is not None:
            fighters = [
                _fighter_summary(session, fighter_id=bout.fighter_a_id, corner="a"),
                _fighter_summary(session, fighter_id=bout.fighter_b_id, corner="b"),
            ]
        else:
            fighters = [
                _fighter_summary(session, fighter_id=None, corner="a"),
                _fighter_summary(session, fighter_id=None, corner="b"),
            ]

        reasons = _parse_reasons(pub.reasons_json)
        blockers: tuple[ReasonBlocker, ...] = ()
        if primary is RecommendationStateView.NO_BET:
            blockers = reasons

        hashes = ArtifactHashes(
            model_hash=prediction.model_hash if prediction else None,
            feature_hash=prediction.feature_hash if prediction else None,
            data_hash=prediction.data_hash if prediction else None,
            config_hash=pub.config_hash or (prediction.config_hash if prediction else None),
            artifact_hash=prediction.artifact_digest if prediction else None,
            policy_hash=pub.policy_hash,
            thresholds_hash=target.thresholds_hash if target else None,
        )
        row = MatchupRow(
            bout_id=pub.bout_id,
            event_id=pub.event_id,
            publication_id=pub.id,
            primary_state=primary,
            performance_lane=_lane_view(pub.performance_lane),
            market_family=pub.market_family,
            outcome_key=pub.outcome_key,
            line_point=pub.line_point,
            selection_id=pub.selection_id,
            fighters=tuple(fighters),
            prices=prices,
            primary_reason=pub.primary_reason,
            reasons=reasons,
            blockers=blockers,
            card_change_warnings=warnings,
            hashes=hashes,
            detail=str(pub.detail or ""),
        )
        matchups.append(row)
        if primary is RecommendationStateView.CONFIRMED_VALUE:
            ev = float(prices.exact_ev or 0.0)
            confirmed.append((ev, pub.bout_id))
        elif primary is RecommendationStateView.PRICE_TARGET:
            watchlist.append(pub.bout_id)
        elif primary is RecommendationStateView.NO_BET:
            no_bets.append(pub.bout_id)
        else:
            assert_never(primary)

    confirmed.sort(key=lambda item: item[0], reverse=True)
    event_field = (
        _optional_str(event_id)
        if event_id
        else (
            _optional_str(matchups[0].event_id)
            if matchups
            else _optional_str(None, unknown=True)
        )
    )
    return MatchupsDocument(
        as_of=_iso(now) or "",
        event_id=event_field,
        matchups=tuple(matchups),
        confirmed_value_ranked=tuple(bout_id for _, bout_id in confirmed),
        price_target_watchlist=tuple(watchlist),
        no_bet_ids=tuple(no_bets),
    )


def build_current_event_document(
    session: Session,
    *,
    event_id: str | None,
    as_of: datetime | None = None,
    last_successful_update_at: datetime | None = None,
) -> CurrentEventDocument:
    now = as_of or _utc_now()
    event: CanonicalEvent | None = None
    if event_id:
        event = session.get(CanonicalEvent, event_id)

    event_id_field = (
        _optional_str(event_id) if event_id else _optional_str(None, unknown=True)
    )

    if event is None:
        title = _optional_str(None, unknown=True)
        event_date = _optional_str(None, unknown=True)
        start_at = None
    else:
        title = _optional_str(event.name)
        date_value = None
        if event.event_date is not None:
            date_value = event.event_date.isoformat()
        elif event.scheduled_start_at is not None:
            date_value = event.scheduled_start_at.astimezone(UTC).date().isoformat()
        event_date = (
            _optional_str(date_value)
            if date_value
            else _optional_str(None, unknown=True)
        )
        start_at = event.scheduled_start_at

    start_iso = _iso(start_at)
    seconds: int | None = None
    is_past: bool | None = None
    if start_at is not None:
        delta = start_at.astimezone(UTC) - now.astimezone(UTC)
        seconds = int(delta.total_seconds())
        is_past = seconds < 0
    return CurrentEventDocument(
        as_of=_iso(now) or "",
        event_id=event_id_field,
        title=title,
        event_date=event_date,
        countdown=CountdownFields(
            event_start_at=_optional_str(start_iso, unknown=start_iso is None),
            seconds_until_start=seconds,
            is_past=is_past,
        ),
        last_successful_update_at=_optional_str(
            _iso(last_successful_update_at or now)
        ),
    )


def _project_health_components(
    report: HealthReport,
    *,
    as_of: str,
) -> tuple[DashboardHealthComponent, ...]:
    by_name = {c.name: c for c in report.components}
    out: list[DashboardHealthComponent] = []
    seen: set[str] = set()
    for source_name in HEALTH_COMPONENT_NAMES:
        dest = HEALTH_COMPONENT_MAP[source_name]
        if dest in seen:
            # Prefer first mapping when multiple sources share a dest.
            continue
        seen.add(dest)
        component = by_name.get(source_name)
        if component is None:
            status = HealthStatusView.MISSING
            detail = f"missing operational component {source_name}"
            component_as_of = as_of
        else:
            status = HealthStatusView(component.status.value)
            detail = component.detail
            component_as_of = component.as_of or as_of
        out.append(
            DashboardHealthComponent(
                name=dest,  # type: ignore[arg-type]
                status=status,
                detail=detail,
                as_of=component_as_of,
            )
        )
    # Ensure every dashboard health name exists.
    present = {c.name for c in out}
    for name in DASHBOARD_HEALTH_NAMES:
        if name not in present:
            out.append(
                DashboardHealthComponent(
                    name=name,  # type: ignore[arg-type]
                    status=HealthStatusView.MISSING,
                    detail="not projected",
                    as_of=as_of,
                )
            )
    return tuple(out)


def build_health_document(
    *,
    health: HealthReport | None = None,
    as_of: datetime | None = None,
) -> DashboardHealthDocument:
    now = as_of or _utc_now()
    as_of_s = _iso(now) or ""
    report = health or default_missing_report(series="dwcs", as_of=as_of_s)
    return DashboardHealthDocument(
        as_of=as_of_s,
        components=_project_health_components(report, as_of=as_of_s),
    )


def build_performance_document(
    session: Session,
    *,
    event_id: str | None = None,
    as_of: datetime | None = None,
) -> PerformanceDocument:
    now = as_of or _utc_now()
    pubs = _select_publications(session, event_id=event_id)

    predictive = PredictiveMetrics(sample_count=0)
    confirmed = ConfirmedPriceMetrics()
    price_targets = PriceTargetOnlyMetrics()
    lane_acc: dict[PerformanceLaneView, dict[str, Any]] = {
        lane: {
            "predictive": 0,
            "confirmed_count": 0,
            "confirmed_hits": 0,
            "confirmed_roi": [],
            "confirmed_clv": [],
            "pt_count": 0,
            "pt_grades": 0,
        }
        for lane in PerformanceLaneView
    }

    grade_count = 0
    for pub in pubs:
        lane = _lane_view(pub.performance_lane)
        lane_acc[lane]["predictive"] += 1
        predictive = PredictiveMetrics(sample_count=predictive.sample_count + 1)
        if pub.prediction_id:
            grade = session.scalar(
                select(PredictionGrade)
                .where(PredictionGrade.prediction_id == pub.prediction_id)
                .order_by(PredictionGrade.revision.desc())
                .limit(1)
            )
            if grade is not None:
                grade_count += 1

        observed = _latest_observed(session, pub.id)
        prediction = _prediction(session, pub.prediction_id)
        has_ev = (
            observed is not None
            and prediction is not None
            and prediction.p50 is not None
        )
        primary = _map_primary_state(
            pub.state,
            has_observed=observed is not None,
            has_exact_ev=has_ev,
        )
        if primary is RecommendationStateView.CONFIRMED_VALUE and observed is not None:
            settlement = session.scalar(
                select(RecommendationSettlement)
                .where(RecommendationSettlement.official_publication_id == pub.id)
                .order_by(RecommendationSettlement.revision.desc())
                .limit(1)
            )
            lane_acc[lane]["confirmed_count"] += 1
            if settlement is not None and settlement.roi is not None:
                lane_acc[lane]["confirmed_roi"].append(float(settlement.roi))
            if settlement is not None and settlement.clv is not None:
                lane_acc[lane]["confirmed_clv"].append(float(settlement.clv))
            if settlement is not None and settlement.settlement_result == "win":
                lane_acc[lane]["confirmed_hits"] += 1
        elif primary is RecommendationStateView.PRICE_TARGET:
            lane_acc[lane]["pt_count"] += 1
            if pub.prediction_id:
                grade = session.scalar(
                    select(PredictionGrade)
                    .where(PredictionGrade.prediction_id == pub.prediction_id)
                    .limit(1)
                )
                if grade is not None:
                    lane_acc[lane]["pt_grades"] += 1

    total_confirmed = sum(int(v["confirmed_count"]) for v in lane_acc.values())
    total_hits = sum(int(v["confirmed_hits"]) for v in lane_acc.values())
    all_roi = [x for v in lane_acc.values() for x in v["confirmed_roi"]]
    all_clv = [x for v in lane_acc.values() for x in v["confirmed_clv"]]
    confirmed = ConfirmedPriceMetrics(
        pick_count=total_confirmed,
        hit_rate=(total_hits / total_confirmed) if total_confirmed else None,
        flat_unit_roi=(sum(all_roi) / len(all_roi)) if all_roi else None,
        clv=(sum(all_clv) / len(all_clv)) if all_clv else None,
        drawdown=None,
    )
    price_targets = PriceTargetOnlyMetrics(
        pick_count=sum(int(v["pt_count"]) for v in lane_acc.values()),
        sporting_grade_count=sum(int(v["pt_grades"]) for v in lane_acc.values()),
    )
    # Attach grade count into predictive sample metadata via calibration placeholders.
    _ = grade_count
    by_lane: list[LaneMetricsBucket] = []
    for lane, acc in lane_acc.items():
        c_count = int(acc["confirmed_count"])
        c_hits = int(acc["confirmed_hits"])
        rois = list(acc["confirmed_roi"])
        clvs = list(acc["confirmed_clv"])
        by_lane.append(
            LaneMetricsBucket(
                lane=lane,
                predictive=PredictiveMetrics(sample_count=int(acc["predictive"])),
                confirmed_price=ConfirmedPriceMetrics(
                    pick_count=c_count,
                    hit_rate=(c_hits / c_count) if c_count else None,
                    flat_unit_roi=(sum(rois) / len(rois)) if rois else None,
                    clv=(sum(clvs) / len(clvs)) if clvs else None,
                ),
                price_target_only=PriceTargetOnlyMetrics(
                    pick_count=int(acc["pt_count"]),
                    sporting_grade_count=int(acc["pt_grades"]),
                ),
            )
        )

    return PerformanceDocument(
        as_of=_iso(now) or "",
        filters=PerformanceFilters(),
        predictive=predictive,
        confirmed_price=confirmed,
        price_target_only=price_targets,
        by_lane=tuple(by_lane),
    )


def build_history_document(
    session: Session,
    *,
    event_id: str | None = None,
    as_of: datetime | None = None,
) -> HistoryDocument:
    now = as_of or _utc_now()
    pubs = _select_publications(session, event_id=event_id)
    points: list[HistoryPoint] = []
    for pub in pubs:
        observed = _latest_observed(session, pub.id)
        prediction = _prediction(session, pub.prediction_id)
        has_ev = (
            observed is not None
            and prediction is not None
            and prediction.p50 is not None
        )
        primary = _map_primary_state(
            pub.state,
            has_observed=observed is not None,
            has_exact_ev=has_ev,
        )
        lane = _lane_view(pub.performance_lane)
        at = _iso(pub.published_at) or (_iso(now) or "")
        if primary is RecommendationStateView.CONFIRMED_VALUE and observed is not None:
            settlement = session.scalar(
                select(RecommendationSettlement)
                .where(RecommendationSettlement.official_publication_id == pub.id)
                .order_by(RecommendationSettlement.revision.desc())
                .limit(1)
            )
            points.append(
                HistoryPoint(
                    at=at,
                    label=pub.selection_id or pub.bout_id,
                    bucket="confirmed_price",
                    lane=lane,
                    value=(
                        float(settlement.profit)
                        if settlement and settlement.profit is not None
                        else None
                    ),
                    flat_unit_roi=float(settlement.roi)
                    if settlement and settlement.roi is not None
                    else None,
                    clv=float(settlement.clv)
                    if settlement and settlement.clv is not None
                    else None,
                )
            )
        elif primary is RecommendationStateView.PRICE_TARGET:
            points.append(
                HistoryPoint(
                    at=at,
                    label=pub.selection_id or pub.bout_id,
                    bucket="price_target_only",
                    lane=lane,
                    value=None,
                    flat_unit_roi=None,
                    clv=None,
                )
            )
        else:
            points.append(
                HistoryPoint(
                    at=at,
                    label=pub.selection_id or pub.bout_id,
                    bucket="predictive",
                    lane=lane,
                    value=None,
                )
            )
    return HistoryDocument(
        as_of=_iso(now) or "",
        filters=PerformanceFilters(),
        points=tuple(points),
    )


def build_release_files(
    session: Session,
    *,
    release_id: str,
    event_id: str | None,
    window_slot: str | None = None,
    publications: int | None = None,
    as_of: datetime | None = None,
    health: HealthReport | None = None,
    last_successful_update_at: datetime | None = None,
) -> dict[str, str]:
    """Build all dashboard JSON file bodies (pre-hash release/manifest)."""
    now = as_of or _utc_now()
    as_of_s = _iso(now) or ""
    matchups = build_matchups_document(session, event_id=event_id, as_of=now)
    current_event = build_current_event_document(
        session,
        event_id=event_id,
        as_of=now,
        last_successful_update_at=last_successful_update_at or now,
    )
    performance = build_performance_document(session, event_id=event_id, as_of=now)
    history = build_history_document(session, event_id=event_id, as_of=now)
    health_doc = build_health_document(health=health, as_of=now)

    bodies: dict[str, str] = {
        CURRENT_EVENT_JSON: _dumps(current_event.model_dump(mode="json")),
        MATCHUPS_JSON: _dumps(matchups.model_dump(mode="json")),
        PERFORMANCE_JSON: _dumps(performance.model_dump(mode="json")),
        HISTORY_JSON: _dumps(history.model_dump(mode="json")),
        HEALTH_JSON: _dumps(health_doc.model_dump(mode="json")),
    }

    file_entries = [
        ReleaseFileEntry(name=name, sha256=_sha256_text(bodies[name]))
        for name in (
            CURRENT_EVENT_JSON,
            MATCHUPS_JSON,
            PERFORMANCE_JSON,
            HISTORY_JSON,
            HEALTH_JSON,
        )
    ]
    pub_count = publications
    if pub_count is None:
        pub_count = len(matchups.matchups)

    # Aggregate hashes from first priced matchup when available.
    hashes = ArtifactHashes()
    for row in matchups.matchups:
        if row.hashes.model_hash or row.hashes.artifact_hash:
            hashes = row.hashes
            break

    release = ReleaseDocument(
        release_id=release_id,
        event_id=event_id,
        window_slot=window_slot,
        publications=int(pub_count),
        as_of=as_of_s,
        files=tuple(file_entries),
        hashes=hashes,
    )
    bodies[RELEASE_JSON] = _dumps(release.model_dump(mode="json"))

    descriptions = {
        RELEASE_JSON: "Release identity, file list, and content hashes",
        MANIFEST_JSON: "Ordered list of published dashboard files",
        CURRENT_EVENT_JSON: "Current event identity, countdown, last update",
        MATCHUPS_JSON: "Matchup card with one primary state per bout",
        PERFORMANCE_JSON: "Separated predictive / confirmed-price / price-target metrics",
        HISTORY_JSON: "Time series points with bucket-separated ROI/CLV rules",
        HEALTH_JSON: "Dashboard health projection (separate from DWCS-403 contract)",
    }
    manifest = ManifestDocument(
        release_id=release_id,
        files=DASHBOARD_RELEASE_FILES,
        descriptions=descriptions,
    )
    bodies[MANIFEST_JSON] = _dumps(manifest.model_dump(mode="json"))
    return bodies


__all__ = [
    "build_current_event_document",
    "build_health_document",
    "build_history_document",
    "build_matchups_document",
    "build_performance_document",
    "build_release_files",
]
