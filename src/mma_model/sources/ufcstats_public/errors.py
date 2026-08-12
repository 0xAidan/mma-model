"""UFCStats public snapshot parser errors."""


class ParserSchemaDriftError(ValueError):
    """Raised when required UFCStats labels/columns are missing or malformed."""


class ParticipantError(ParserSchemaDriftError):
    """Raised when participant count/identity is invalid."""
