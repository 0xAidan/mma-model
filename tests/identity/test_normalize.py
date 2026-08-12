"""Identity name normalization (DWCS-104)."""

from __future__ import annotations

from mma_model.identity.normalize import normalize_person_name, name_tokens


def test_nfkc_casefold_preserves_diacritics() -> None:
    assert normalize_person_name("José Mauro") == "josé mauro"
    assert normalize_person_name("JOSÉ MAURO") == "josé mauro"
    assert "é" in normalize_person_name("José")


def test_collapses_whitespace_and_preserves_meaningful_tokens() -> None:
    assert normalize_person_name("  John   Smith  ") == "john smith"
    assert name_tokens("Mary-Jane O'Connor") == ("mary-jane", "o'connor")
    assert normalize_person_name("Mary-Jane O'Connor") == "mary-jane o'connor"


def test_empty_and_non_ascii_hangul_preserved() -> None:
    assert normalize_person_name("") == ""
    assert normalize_person_name("김동현") == "김동현"
