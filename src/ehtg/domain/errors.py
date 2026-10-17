from typing import Any


class AppError(Exception):
    """An error whose deliberately safe message may be returned to the owner."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 422,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details or {}
