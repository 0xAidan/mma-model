"""Leakage-safe fight outcome labels (DWCS-300).

Pure functions: no DB / HTTP I/O. Method cells are matched by exact canonical
tokens (never substrings), so fighter names such as Decker / Nicole / Kona
cannot be read as methods.

Mapping notes
-------------
- KO, TKO, KO/TKO → ``ko_tko``.
- SUB / Submission → ``submission``.
- U-DEC / S-DEC / M-DEC / DEC / Decision → ``decision``.
- DQ and other non-KO/sub/decision stoppages → ``other_stoppage``.
- Technical Decision stays ``technical_decision`` on the training method label;
  joint terminal atoms pool it with decision (``a_decision`` / ``b_decision``).
- Technical Draw stays ``technical_draw``; the joint atom is ``draw``.
- Draw / NC / Overturned / pending / malformed never become a binary winner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Never

from mma_model.evaluation.contract import (
    SettlementOnlyLabel,
    TerminalAtom,
    mutable_fact_allowed_at_cutoff,
)


class OutcomeLabelError(ValueError):
    """Structurally invalid labeling input (not merely incomplete)."""


class VersionKind(StrEnum):
    EVENT_NIGHT = "event_night"
    CURRENT = "current"


class WinnerSide(StrEnum):
    A = "a"
    B = "b"


class ResultClass(StrEnum):
    DECISIVE = "decisive"
    DRAW = "draw"
    NO_CONTEST = "no_contest"
    OVERTURNED = "overturned"
    PENDING = "pending"
    UNKNOWN = "unknown"


class MethodLabel(StrEnum):
    KO_TKO = "ko_tko"
    SUBMISSION = "submission"
    DECISION = "decision"
    OTHER_STOPPAGE = "other_stoppage"
    TECHNICAL_DECISION = "technical_decision"
    TECHNICAL_DRAW = "technical_draw"


class NormalizationStatus(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    UNKNOWN = "unknown"
    PENDING = "pending"


@dataclass(frozen=True)
class TokenMapping:
    """Closed mapping from one exact UFCStats-style token to a result/method pair."""

    result_class: ResultClass
    method: MethodLabel | None


# Exact canonical tokens (uppercase, collapsed whitespace, slash spaces stripped).
# DQ is a stoppage that is not KO/TKO, submission, or decision → other_stoppage.
_METHOD_TOKEN_MAP: Final[dict[str, TokenMapping]] = {
    "KO/TKO": TokenMapping(ResultClass.DECISIVE, MethodLabel.KO_TKO),
    "KO": TokenMapping(ResultClass.DECISIVE, MethodLabel.KO_TKO),
    "TKO": TokenMapping(ResultClass.DECISIVE, MethodLabel.KO_TKO),
    "SUB": TokenMapping(ResultClass.DECISIVE, MethodLabel.SUBMISSION),
    "SUBMISSION": TokenMapping(ResultClass.DECISIVE, MethodLabel.SUBMISSION),
    "U-DEC": TokenMapping(ResultClass.DECISIVE, MethodLabel.DECISION),
    "S-DEC": TokenMapping(ResultClass.DECISIVE, MethodLabel.DECISION),
    "M-DEC": TokenMapping(ResultClass.DECISIVE, MethodLabel.DECISION),
    "DEC": TokenMapping(ResultClass.DECISIVE, MethodLabel.DECISION),
    "DECISION": TokenMapping(ResultClass.DECISIVE, MethodLabel.DECISION),
    "DQ": TokenMapping(ResultClass.DECISIVE, MethodLabel.OTHER_STOPPAGE),
    "OTHER": TokenMapping(ResultClass.DECISIVE, MethodLabel.OTHER_STOPPAGE),
    "OTHER STOPPAGE": TokenMapping(ResultClass.DECISIVE, MethodLabel.OTHER_STOPPAGE),
    "TECHNICAL DECISION": TokenMapping(ResultClass.DECISIVE, MethodLabel.TECHNICAL_DECISION),
    "T-DEC": TokenMapping(ResultClass.DECISIVE, MethodLabel.TECHNICAL_DECISION),
    "TECHNICAL DRAW": TokenMapping(ResultClass.DRAW, MethodLabel.TECHNICAL_DRAW),
    "DRAW": TokenMapping(ResultClass.DRAW, None),
    "NC": TokenMapping(ResultClass.NO_CONTEST, None),
    "CNC": TokenMapping(ResultClass.NO_CONTEST, None),
    "NO CONTEST": TokenMapping(ResultClass.NO_CONTEST, None),
    "COULD NOT CONTINUE": TokenMapping(ResultClass.NO_CONTEST, None),
    "OVERTURNED": TokenMapping(ResultClass.OVERTURNED, None),
}

EXACT_METHOD_TOKENS: Final[frozenset[str]] = frozenset(_METHOD_TOKEN_MAP)
CONTRACT_SETTLEMENT_ONLY_LABELS: Final[frozenset[SettlementOnlyLabel]] = frozenset(
    SettlementOnlyLabel
)
# Result classes excluded from win-fitting atoms. Contract settlement-only
# labels are ``no_contest`` and ``void`` (SettlementOnlyLabel); there is no
# UFCStats void method token. Overturned/pending/unknown are also excluded.
SETTLEMENT_ONLY_RESULT_CLASSES: Final[frozenset[ResultClass]] = frozenset(
    {ResultClass.NO_CONTEST, ResultClass.OVERTURNED, ResultClass.PENDING, ResultClass.UNKNOWN}
)

@dataclass(frozen=True)
class NormalizedOutcome:
    status: NormalizationStatus
    result_class: ResultClass
    method: MethodLabel | None
    token: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class OutcomeLabel:
    """Normalized label for settlement or training-history use."""

    result_class: ResultClass
    method: MethodLabel | None
    winner_side: WinnerSide | None
    binary_winner: WinnerSide | None
    terminal_atom: TerminalAtom | None
    status: NormalizationStatus
    source_version_kind: VersionKind | None = None


@dataclass(frozen=True)
class ResultVersion:
    """One bout result version as labeling input (mirrors bout_result_versions clocks)."""

    version_kind: VersionKind
    effective_at: datetime
    observed_at: datetime
    winner_side: WinnerSide | None = None
    method_raw: str | None = None
    result_class: ResultClass | None = None
    revision: int = 1


def canonicalize_method_text(raw: str) -> str:
    """Uppercase, collapse whitespace, and strip spaces around '/'."""
    collapsed = " ".join(raw.split()).upper()
    return collapsed.replace(" / ", "/").replace("/ ", "/").replace(" /", "/")


def match_exact_method_token(raw: str | None) -> str | None:
    """Return the canonical token when ``raw`` is an exact known method cell."""
    if raw is None:
        return None
    key = canonicalize_method_text(raw)
    if not key or key not in _METHOD_TOKEN_MAP:
        return None
    return key


def normalize_outcome_from_method(raw: str | None) -> NormalizedOutcome:
    """Map an exact method token onto result class + method. Never substring-guess."""
    if raw is None or not str(raw).strip():
        return NormalizedOutcome(
            status=NormalizationStatus.MISSING,
            result_class=ResultClass.PENDING,
            method=None,
            token=None,
            reason="method_missing",
        )
    token = match_exact_method_token(raw)
    if token is None:
        return NormalizedOutcome(
            status=NormalizationStatus.UNKNOWN,
            result_class=ResultClass.UNKNOWN,
            method=None,
            token=None,
            reason="malformed_method",
        )
    mapped = _METHOD_TOKEN_MAP[token]
    return NormalizedOutcome(
        status=NormalizationStatus.VALID,
        result_class=mapped.result_class,
        method=mapped.method,
        token=token,
        reason=None,
    )


def binary_winner_label(
    result_class: ResultClass,
    winner_side: WinnerSide | None,
) -> WinnerSide | None:
    """Return A/B only for a decisive result with a validated winner.

    Draw, NC, overturned, pending, and unknown never become fighter A or B.
    Draw/NC with a winner is structurally invalid (same rule as ingest winners).
    """
    if result_class is ResultClass.DECISIVE:
        return winner_side
    if result_class is ResultClass.DRAW or result_class is ResultClass.NO_CONTEST:
        if winner_side is not None:
            raise OutcomeLabelError(
                f"{result_class.value} cannot carry a winner"
            )
        return None
    if result_class is ResultClass.OVERTURNED:
        return None
    if result_class is ResultClass.PENDING:
        return None
    if result_class is ResultClass.UNKNOWN:
        return None
    never_class: Never = result_class
    raise OutcomeLabelError(f"unhandled result class: {never_class!r}")


def _side_atom(
    winner_side: WinnerSide,
    a_atom: TerminalAtom,
    b_atom: TerminalAtom,
) -> TerminalAtom:
    if winner_side is WinnerSide.A:
        return a_atom
    if winner_side is WinnerSide.B:
        return b_atom
    never_side: Never = winner_side
    raise OutcomeLabelError(f"unhandled winner side: {never_side!r}")


def _decisive_terminal_atom(
    method: MethodLabel,
    winner_side: WinnerSide,
) -> TerminalAtom | None:
    """Map a decisive method onto a contract TerminalAtom member."""
    if method is MethodLabel.KO_TKO:
        return _side_atom(winner_side, TerminalAtom.A_KO_TKO, TerminalAtom.B_KO_TKO)
    if method is MethodLabel.SUBMISSION:
        return _side_atom(
            winner_side, TerminalAtom.A_SUBMISSION, TerminalAtom.B_SUBMISSION
        )
    if method is MethodLabel.DECISION:
        return _side_atom(winner_side, TerminalAtom.A_DECISION, TerminalAtom.B_DECISION)
    if method is MethodLabel.OTHER_STOPPAGE:
        return _side_atom(
            winner_side, TerminalAtom.A_OTHER_STOPPAGE, TerminalAtom.B_OTHER_STOPPAGE
        )
    if method is MethodLabel.TECHNICAL_DECISION:
        return _side_atom(winner_side, TerminalAtom.A_DECISION, TerminalAtom.B_DECISION)
    if method is MethodLabel.TECHNICAL_DRAW:
        return None
    never_method: Never = method
    raise OutcomeLabelError(f"unhandled method label: {never_method!r}")


def terminal_atom(
    result_class: ResultClass,
    method: MethodLabel | None,
    winner_side: WinnerSide | None,
) -> TerminalAtom | None:
    """Map decisive+draw outcomes onto contract atoms.

    ``SettlementOnlyLabel`` (``no_contest``, ``void``) and overturned/pending/
    unknown are excluded from win fitting. Void has no UFCStats method token.
    """
    if result_class is ResultClass.DRAW:
        return TerminalAtom.DRAW
    if result_class in SETTLEMENT_ONLY_RESULT_CLASSES:
        return None
    if result_class is ResultClass.DECISIVE:
        if winner_side is None or method is None:
            return None
        return _decisive_terminal_atom(method, winner_side)
    never_class: Never = result_class
    raise OutcomeLabelError(f"unhandled result class: {never_class!r}")


def swap_terminal_atom(atom: TerminalAtom) -> TerminalAtom:
    """Swap fighter-scoped atoms; draw is unchanged."""
    if atom is TerminalAtom.DRAW:
        return TerminalAtom.DRAW
    if atom is TerminalAtom.A_KO_TKO:
        return TerminalAtom.B_KO_TKO
    if atom is TerminalAtom.A_SUBMISSION:
        return TerminalAtom.B_SUBMISSION
    if atom is TerminalAtom.A_OTHER_STOPPAGE:
        return TerminalAtom.B_OTHER_STOPPAGE
    if atom is TerminalAtom.A_DECISION:
        return TerminalAtom.B_DECISION
    if atom is TerminalAtom.B_KO_TKO:
        return TerminalAtom.A_KO_TKO
    if atom is TerminalAtom.B_SUBMISSION:
        return TerminalAtom.A_SUBMISSION
    if atom is TerminalAtom.B_OTHER_STOPPAGE:
        return TerminalAtom.A_OTHER_STOPPAGE
    if atom is TerminalAtom.B_DECISION:
        return TerminalAtom.A_DECISION
    never_atom: Never = atom
    raise OutcomeLabelError(f"unhandled terminal atom: {never_atom!r}")


def _require_version_kind(kind: VersionKind) -> None:
    if kind is VersionKind.EVENT_NIGHT or kind is VersionKind.CURRENT:
        return
    never_kind: Never = kind
    raise OutcomeLabelError(f"unhandled version kind: {never_kind!r}")


def label_from_facts(
    *,
    method_raw: str | None,
    result_class: ResultClass | None,
    winner_side: WinnerSide | None,
    source_version_kind: VersionKind | None = None,
) -> OutcomeLabel:
    """Build a closed outcome label from a method token and optional result class."""
    if source_version_kind is not None:
        _require_version_kind(source_version_kind)

    normalized = normalize_outcome_from_method(method_raw)
    resolved_class = result_class
    resolved_method = normalized.method
    status = normalized.status

    if normalized.status is NormalizationStatus.VALID:
        if resolved_class is not None and resolved_class is not normalized.result_class:
            raise OutcomeLabelError(
                "result_class contradicts exact method token: "
                f"{resolved_class.value} vs {normalized.result_class.value}"
            )
        resolved_class = normalized.result_class
    elif normalized.status is NormalizationStatus.MISSING:
        if resolved_class is None:
            resolved_class = ResultClass.PENDING
            status = NormalizationStatus.PENDING
        else:
            status = NormalizationStatus.VALID
            resolved_method = None
    elif normalized.status is NormalizationStatus.PENDING:
        if resolved_class is None:
            resolved_class = ResultClass.PENDING
        resolved_method = None
        status = NormalizationStatus.PENDING
    elif normalized.status is NormalizationStatus.UNKNOWN:
        # Malformed method: never guess KO/sub/decision from a substring.
        resolved_method = None
        if resolved_class is None:
            resolved_class = ResultClass.UNKNOWN
        elif resolved_class is ResultClass.DECISIVE:
            resolved_class = ResultClass.UNKNOWN
    else:
        never_status: Never = normalized.status
        raise OutcomeLabelError(f"unhandled normalization status: {never_status!r}")

    binary = binary_winner_label(resolved_class, winner_side)
    atom = terminal_atom(resolved_class, resolved_method, winner_side)
    return OutcomeLabel(
        result_class=resolved_class,
        method=resolved_method,
        winner_side=winner_side if binary is not None else None,
        binary_winner=binary,
        terminal_atom=atom,
        status=status,
        source_version_kind=source_version_kind,
    )


def settlement_label(event_night_version: ResultVersion) -> OutcomeLabel:
    """Event-night settlement label. Reversals must not change this."""
    _require_version_kind(event_night_version.version_kind)
    if event_night_version.version_kind is not VersionKind.EVENT_NIGHT:
        raise OutcomeLabelError(
            "settlement_label requires version_kind='event_night'"
        )
    return label_from_facts(
        method_raw=event_night_version.method_raw,
        result_class=event_night_version.result_class,
        winner_side=event_night_version.winner_side,
        source_version_kind=event_night_version.version_kind,
    )


def _training_sort_key(version: ResultVersion) -> tuple[datetime, datetime, int]:
    return (version.effective_at, version.observed_at, version.revision)


def training_label(
    versions: Sequence[ResultVersion],
    cutoff: datetime,
) -> OutcomeLabel:
    """Latest result whose ``effective_at < cutoff`` and ``observed_at <= cutoff``.

    A later reversal is invisible to an earlier cutoff. Event-night settlement
    is a separate function and is never rewritten here.
    """
    eligible: list[ResultVersion] = []
    for version in versions:
        _require_version_kind(version.version_kind)
        if mutable_fact_allowed_at_cutoff(
            effective_at=version.effective_at,
            observed_at=version.observed_at,
            cutoff=cutoff,
        ):
            eligible.append(version)
    if not eligible:
        return OutcomeLabel(
            result_class=ResultClass.PENDING,
            method=None,
            winner_side=None,
            binary_winner=None,
            terminal_atom=None,
            status=NormalizationStatus.PENDING,
            source_version_kind=None,
        )
    chosen = max(eligible, key=_training_sort_key)
    return label_from_facts(
        method_raw=chosen.method_raw,
        result_class=chosen.result_class,
        winner_side=chosen.winner_side,
        source_version_kind=chosen.version_kind,
    )
