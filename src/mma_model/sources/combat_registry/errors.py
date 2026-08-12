"""Combat Registry public snapshot parser errors."""


class ParserSchemaDriftError(ValueError):
    """Raised when required Combat Registry labels/columns are missing or malformed."""
