"""Unicode-preserving person-name normalization (DWCS-104)."""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")


def normalize_person_name(name: str) -> str:
    """NFKC + casefold; preserve diacritics and meaningful tokens; collapse whitespace."""
    if name is None:
        raise ValueError("display_name/name must not be None")
    text = unicodedata.normalize("NFKC", str(name)).casefold().strip()
    return _WS_RE.sub(" ", text)


def name_tokens(name: str) -> tuple[str, ...]:
    """Tokenize a normalized person name on whitespace."""
    normalized = normalize_person_name(name)
    if not normalized:
        return ()
    return tuple(normalized.split(" "))
