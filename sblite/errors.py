"""sblite error types."""


class SbError(Exception):
    """Base exception for all sblite sandbox errors."""

    pass


class SbTimeout(SbError):
    """Raised when sandbox execution exceeds the configured timeout."""

    pass


class SbCancelled(SbError):
    """Raised when sandbox execution is cancelled externally."""

    pass


class SbValidationError(SbError):
    """Raised when AST validation rejects code before compilation."""

    def __init__(self, message: str, lineno: int | None = None, col: int | None = None):
        self.lineno = lineno
        self.col = col
        super().__init__(message)
