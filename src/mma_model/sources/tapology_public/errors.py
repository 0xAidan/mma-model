"""Tapology public snapshot parser errors."""


class ParserSchemaDriftError(ValueError):
    """Raised when required Tapology labels/columns are missing or malformed."""
