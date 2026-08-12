"""Fail-closed winner validation for event-night and current result versions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence


class WinnerValidationError(ValueError):
    """Raised when winner/participant evidence is contradictory or incomplete."""

    def __init__(self, message: str, *, evidence: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.evidence = dict(evidence)


@dataclass(frozen=True)
class WinnerResolution:
    winner_espn_athlete_id: str | None
    winner_fighter_id: str | None
    source: str  # winner_espn_athlete_id | current_winner_flag | none


def _participant_espn_ids(participants: Sequence[Mapping[str, Any]]) -> list[str]:
    ids: list[str] = []
    for part in participants:
        espn_id = part.get("espn_athlete_id")
        if not isinstance(espn_id, str) or not espn_id.strip():
            raise WinnerValidationError(
                "participant missing espn_athlete_id",
                evidence={"reason": "missing_participant_espn_id", "participant": dict(part)},
            )
        ids.append(espn_id.strip())
    return ids


def resolve_version_winner(
    *,
    version_kind: Literal["event_night", "current"],
    result_class: str,
    winner_espn_athlete_id: str | None,
    participants: Sequence[Mapping[str, Any]],
    fighter_id_by_espn: Mapping[str, str],
) -> WinnerResolution:
    """Validate winner ESPN IDs and flags against the two participants.

    Decisive results require an explicit participant winner. Draw/NC forbid winners.
    Never silently prefer flags over a disagreeing ESPN winner id.
    """
    if result_class not in {"decisive", "draw", "no_contest"}:
        raise WinnerValidationError(
            f"unknown result class {result_class!r}",
            evidence={"reason": "unknown_result_class", "result_class": result_class},
        )

    espn_ids = _participant_espn_ids(participants)
    if len(espn_ids) != 2 or espn_ids[0] == espn_ids[1]:
        raise WinnerValidationError(
            "bout requires exactly two distinct participant espn ids",
            evidence={
                "reason": "invalid_participant_set",
                "espn_athlete_ids": espn_ids,
                "version_kind": version_kind,
            },
        )
    participant_set = set(espn_ids)

    flagged = [
        str(p["espn_athlete_id"]).strip()
        for p in participants
        if bool(p.get("current_winner_flag"))
    ]
    winner_raw = winner_espn_athlete_id
    if isinstance(winner_raw, str):
        winner_raw = winner_raw.strip() or None
    elif winner_raw is not None:
        raise WinnerValidationError(
            "malformed winner_espn_athlete_id",
            evidence={
                "reason": "malformed_winner_espn_athlete_id",
                "winner_espn_athlete_id": winner_espn_athlete_id,
                "version_kind": version_kind,
            },
        )

    if result_class in {"draw", "no_contest"}:
        reasons: list[str] = []
        if winner_raw is not None:
            reasons.append("winner_id_present_for_non_decisive")
        if flagged:
            reasons.append("winner_flag_present_for_non_decisive")
        if reasons:
            raise WinnerValidationError(
                "draw/no_contest cannot carry a winner",
                evidence={
                    "reason": "non_decisive_has_winner",
                    "reasons": reasons,
                    "version_kind": version_kind,
                    "result_class": result_class,
                    "winner_espn_athlete_id": winner_raw,
                    "flagged_espn_athlete_ids": flagged,
                },
            )
        return WinnerResolution(
            winner_espn_athlete_id=None,
            winner_fighter_id=None,
            source="none",
        )

    # Decisive
    if len(flagged) > 1:
        raise WinnerValidationError(
            "duplicate current_winner_flag",
            evidence={
                "reason": "duplicate_winner_flag",
                "version_kind": version_kind,
                "flagged_espn_athlete_ids": flagged,
            },
        )

    if version_kind == "event_night":
        if winner_raw is None:
            raise WinnerValidationError(
                "decisive event_night missing winner_espn_athlete_id",
                evidence={
                    "reason": "missing_decisive_winner",
                    "version_kind": version_kind,
                },
            )
        if winner_raw not in participant_set:
            raise WinnerValidationError(
                "event_night winner is not a participant",
                evidence={
                    "reason": "nonparticipant_winner",
                    "version_kind": version_kind,
                    "winner_espn_athlete_id": winner_raw,
                    "participant_espn_athlete_ids": sorted(participant_set),
                },
            )
        # Flags are current-oriented; if present they must not contradict event-night.
        if flagged and flagged[0] != winner_raw:
            raise WinnerValidationError(
                "current_winner_flag contradicts event_night winner",
                evidence={
                    "reason": "flag_winner_contradiction",
                    "version_kind": version_kind,
                    "winner_espn_athlete_id": winner_raw,
                    "flagged_espn_athlete_ids": flagged,
                },
            )
        return WinnerResolution(
            winner_espn_athlete_id=winner_raw,
            winner_fighter_id=fighter_id_by_espn[winner_raw],
            source="winner_espn_athlete_id",
        )

    # current decisive
    if winner_raw is not None and winner_raw not in participant_set:
        raise WinnerValidationError(
            "current winner is not a participant",
            evidence={
                "reason": "nonparticipant_winner",
                "version_kind": version_kind,
                "winner_espn_athlete_id": winner_raw,
                "participant_espn_athlete_ids": sorted(participant_set),
            },
        )
    if winner_raw is not None and flagged and flagged[0] != winner_raw:
        raise WinnerValidationError(
            "current winner_espn_athlete_id contradicts current_winner_flag",
            evidence={
                "reason": "flag_winner_contradiction",
                "version_kind": version_kind,
                "winner_espn_athlete_id": winner_raw,
                "flagged_espn_athlete_ids": flagged,
            },
        )
    if winner_raw is not None:
        return WinnerResolution(
            winner_espn_athlete_id=winner_raw,
            winner_fighter_id=fighter_id_by_espn[winner_raw],
            source="winner_espn_athlete_id",
        )
    if len(flagged) != 1:
        raise WinnerValidationError(
            "decisive current missing winner (no espn id and no single winner flag)",
            evidence={
                "reason": "missing_decisive_winner",
                "version_kind": version_kind,
                "flagged_espn_athlete_ids": flagged,
            },
        )
    # Explicit flag path only when ESPN winner id is absent — not a silent override.
    return WinnerResolution(
        winner_espn_athlete_id=flagged[0],
        winner_fighter_id=fighter_id_by_espn[flagged[0]],
        source="current_winner_flag",
    )
