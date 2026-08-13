"""Outcome normalization, binary winners, atoms, and PIT result versions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mma_model.labels.outcomes import (
    EXACT_METHOD_TOKENS,
    MethodLabel,
    NormalizationStatus,
    OutcomeLabelError,
    ResultClass,
    ResultVersion,
    TerminalAtom,
    VersionKind,
    WinnerSide,
    binary_winner_label,
    label_from_facts,
    match_exact_method_token,
    normalize_outcome_from_method,
    settlement_label,
    swap_terminal_atom,
    terminal_atom,
    training_label,
)

EVENT_NIGHT = datetime(2019, 6, 1, 2, 0, tzinfo=UTC)
ADJUDICATED = datetime(2019, 8, 15, 12, 0, tzinfo=UTC)
PRE_CUTOFF = datetime(2019, 7, 1, 0, 0, tzinfo=UTC)
POST_CUTOFF = datetime(2019, 9, 1, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "result_class", "method"),
    [
        ("KO/TKO", ResultClass.DECISIVE, MethodLabel.KO_TKO),
        ("KO", ResultClass.DECISIVE, MethodLabel.KO_TKO),
        ("TKO", ResultClass.DECISIVE, MethodLabel.KO_TKO),
        ("SUB", ResultClass.DECISIVE, MethodLabel.SUBMISSION),
        ("Submission", ResultClass.DECISIVE, MethodLabel.SUBMISSION),
        ("U-DEC", ResultClass.DECISIVE, MethodLabel.DECISION),
        ("S-DEC", ResultClass.DECISIVE, MethodLabel.DECISION),
        ("M-DEC", ResultClass.DECISIVE, MethodLabel.DECISION),
        ("DEC", ResultClass.DECISIVE, MethodLabel.DECISION),
        ("Decision", ResultClass.DECISIVE, MethodLabel.DECISION),
        ("DQ", ResultClass.DECISIVE, MethodLabel.OTHER_STOPPAGE),
        ("Technical Decision", ResultClass.DECISIVE, MethodLabel.TECHNICAL_DECISION),
        ("Technical Draw", ResultClass.DRAW, MethodLabel.TECHNICAL_DRAW),
        ("Draw", ResultClass.DRAW, None),
        ("NC", ResultClass.NO_CONTEST, None),
        ("CNC", ResultClass.NO_CONTEST, None),
        ("No Contest", ResultClass.NO_CONTEST, None),
        ("Could Not Continue", ResultClass.NO_CONTEST, None),
        ("Overturned", ResultClass.OVERTURNED, None),
    ],
)
def test_exact_tokens_normalize(
    raw: str,
    result_class: ResultClass,
    method: MethodLabel | None,
) -> None:
    got = normalize_outcome_from_method(raw)
    assert got.status is NormalizationStatus.VALID
    assert got.result_class is result_class
    assert got.method is method
    assert match_exact_method_token(raw) in EXACT_METHOD_TOKENS


def test_malformed_method_fail_closed() -> None:
    got = normalize_outcome_from_method("maybe a KO?")
    assert got.status is NormalizationStatus.UNKNOWN
    assert got.result_class is ResultClass.UNKNOWN
    assert got.method is None
    assert match_exact_method_token("maybe a KO?") is None
    assert match_exact_method_token("Nicole Decker") is None
    assert match_exact_method_token("Kona Diaz") is None
    assert match_exact_method_token("Nick Diaz") is None


def test_missing_method_is_pending() -> None:
    got = normalize_outcome_from_method(None)
    assert got.status is NormalizationStatus.MISSING
    assert got.result_class is ResultClass.PENDING
    assert label_from_facts(
        method_raw=None, result_class=None, winner_side=None
    ).result_class is ResultClass.PENDING


def test_draw_never_binary_winner() -> None:
    assert (
        binary_winner_label(ResultClass.DRAW, None) is None
    )
    label = label_from_facts(
        method_raw="Draw",
        result_class=None,
        winner_side=None,
    )
    assert label.binary_winner is None
    assert label.terminal_atom is TerminalAtom.DRAW


def test_no_contest_never_binary_winner() -> None:
    assert binary_winner_label(ResultClass.NO_CONTEST, None) is None
    label = label_from_facts(
        method_raw="NC",
        result_class=None,
        winner_side=None,
    )
    assert label.binary_winner is None
    assert label.terminal_atom is None


def test_draw_or_nc_with_winner_raises() -> None:
    with pytest.raises(OutcomeLabelError, match="draw cannot carry a winner"):
        binary_winner_label(ResultClass.DRAW, WinnerSide.A)
    with pytest.raises(OutcomeLabelError, match="no_contest cannot carry a winner"):
        binary_winner_label(ResultClass.NO_CONTEST, WinnerSide.B)


def test_overturned_and_pending_never_binary_winner() -> None:
    assert binary_winner_label(ResultClass.OVERTURNED, WinnerSide.A) is None
    assert binary_winner_label(ResultClass.PENDING, WinnerSide.B) is None
    assert binary_winner_label(ResultClass.UNKNOWN, WinnerSide.A) is None


def test_technical_decision_distinguished_but_pools_to_decision_atom() -> None:
    label = label_from_facts(
        method_raw="Technical Decision",
        result_class=None,
        winner_side=WinnerSide.A,
    )
    assert label.method is MethodLabel.TECHNICAL_DECISION
    assert label.result_class is ResultClass.DECISIVE
    assert label.binary_winner is WinnerSide.A
    assert label.terminal_atom is TerminalAtom.A_DECISION


def test_dq_maps_to_other_stoppage() -> None:
    label = label_from_facts(
        method_raw="DQ",
        result_class=None,
        winner_side=WinnerSide.B,
    )
    assert label.method is MethodLabel.OTHER_STOPPAGE
    assert label.terminal_atom is TerminalAtom.B_OTHER_STOPPAGE


def test_swap_safe_ab_atom_mapping() -> None:
    atom = terminal_atom(ResultClass.DECISIVE, MethodLabel.KO_TKO, WinnerSide.A)
    assert atom is TerminalAtom.A_KO_TKO
    swapped = swap_terminal_atom(atom)
    assert swapped is TerminalAtom.B_KO_TKO
    assert swap_terminal_atom(swapped) is TerminalAtom.A_KO_TKO
    for member in TerminalAtom:
        twice = swap_terminal_atom(swap_terminal_atom(member))
        assert twice is member
    assert swap_terminal_atom(TerminalAtom.DRAW) is TerminalAtom.DRAW


def test_decisive_without_winner_has_no_binary_or_atom() -> None:
    label = label_from_facts(
        method_raw="KO/TKO",
        result_class=None,
        winner_side=None,
    )
    assert label.result_class is ResultClass.DECISIVE
    assert label.binary_winner is None
    assert label.terminal_atom is None


def _event_night_ko() -> ResultVersion:
    return ResultVersion(
        version_kind=VersionKind.EVENT_NIGHT,
        effective_at=EVENT_NIGHT,
        observed_at=EVENT_NIGHT,
        winner_side=WinnerSide.A,
        method_raw="KO/TKO",
        result_class=ResultClass.DECISIVE,
        revision=1,
    )


def _reversal_overturned() -> ResultVersion:
    return ResultVersion(
        version_kind=VersionKind.CURRENT,
        effective_at=ADJUDICATED,
        observed_at=ADJUDICATED,
        winner_side=None,
        method_raw="Overturned",
        result_class=ResultClass.OVERTURNED,
        revision=1,
    )


def test_event_night_settlement_unchanged_after_reversal() -> None:
    night = _event_night_ko()
    before = settlement_label(night)
    versions = [night, _reversal_overturned()]
    after_settlement = settlement_label(night)
    assert after_settlement == before
    assert after_settlement.binary_winner is WinnerSide.A
    assert after_settlement.terminal_atom is TerminalAtom.A_KO_TKO
    assert after_settlement.source_version_kind is VersionKind.EVENT_NIGHT
    post = training_label(versions, POST_CUTOFF)
    assert post.result_class is ResultClass.OVERTURNED
    assert post.binary_winner is None
    assert settlement_label(night).binary_winner is WinnerSide.A


def test_training_label_pre_adjudication_cutoff_unchanged() -> None:
    night = _event_night_ko()
    versions_before = [night]
    pre_before = training_label(versions_before, PRE_CUTOFF)
    versions_after_append = [night, _reversal_overturned()]
    pre_after = training_label(versions_after_append, PRE_CUTOFF)
    assert pre_after == pre_before
    assert pre_after.binary_winner is WinnerSide.A
    assert pre_after.terminal_atom is TerminalAtom.A_KO_TKO
    assert pre_after.source_version_kind is VersionKind.EVENT_NIGHT


def test_training_label_after_adjudication_uses_reversal() -> None:
    versions = [_event_night_ko(), _reversal_overturned()]
    post = training_label(versions, POST_CUTOFF)
    assert post.result_class is ResultClass.OVERTURNED
    assert post.binary_winner is None
    assert post.terminal_atom is None
    assert post.source_version_kind is VersionKind.CURRENT
    # effective_at == cutoff is excluded (strictly before).
    at_adjudication = training_label(versions, ADJUDICATED)
    assert at_adjudication.binary_winner is WinnerSide.A
    assert at_adjudication.source_version_kind is VersionKind.EVENT_NIGHT


def test_settlement_label_rejects_current_version() -> None:
    with pytest.raises(OutcomeLabelError, match="event_night"):
        settlement_label(_reversal_overturned())


def test_training_label_requires_observed_at_at_or_before_cutoff() -> None:
    night = _event_night_ko()
    late_obs = ResultVersion(
        version_kind=VersionKind.CURRENT,
        effective_at=ADJUDICATED,
        observed_at=ADJUDICATED + timedelta(days=10),
        winner_side=None,
        method_raw="Overturned",
        result_class=ResultClass.OVERTURNED,
        revision=1,
    )
    cutoff = ADJUDICATED + timedelta(days=1)
    got = training_label([night, late_obs], cutoff)
    assert got.source_version_kind is VersionKind.EVENT_NIGHT
    assert got.binary_winner is WinnerSide.A


def test_conflicting_result_class_and_method_raises() -> None:
    with pytest.raises(OutcomeLabelError, match="contradicts"):
        label_from_facts(
            method_raw="KO/TKO",
            result_class=ResultClass.DRAW,
            winner_side=None,
        )
