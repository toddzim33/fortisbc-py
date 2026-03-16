"""FortisBC library exceptions."""


class FortisbcError(Exception):
    """Base exception."""


class FortisbcAuthError(FortisbcError):
    """Authentication failed."""


class FortisbcParseError(FortisbcError):
    """Failed to parse portal response."""
