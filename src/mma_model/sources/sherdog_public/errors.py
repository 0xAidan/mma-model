"""Sherdog public snapshot parser errors."""


class ParserSchemaDriftError(ValueError):
    """Raised when required Sherdog labels/columns are missing or malformed."""
