"""Typed errors raised by the HaaS SDK."""

from __future__ import annotations

from typing import Any


class HaasError(Exception):
    """Base class for every SDK error."""


class HaasConnectionError(HaasError):
    """The API could not be reached (after retries)."""


class APIError(HaasError):
    """The API answered with an RFC 9457 problem (or another non-2xx response)."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str | None = None,
        request_id: str | None = None,
        body: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
        self.body = body
        self.retry_after = retry_after

    def __str__(self) -> str:
        suffix = f" (request_id={self.request_id})" if self.request_id else ""
        return f"{self.status}: {self.args[0]}{suffix}"


class BadRequestError(APIError):
    pass


class AuthenticationError(APIError):
    pass


class PermissionDeniedError(APIError):
    pass


class NotFoundError(APIError):
    pass


class ConflictError(APIError):
    pass


class ValidationError(APIError):
    pass


class RateLimitError(APIError):
    pass


class ServerError(APIError):
    pass


class StreamClosedError(HaasError):
    """The server closed an event stream (for example: re-authentication failed)."""


class WebhookVerificationError(HaasError):
    """A webhook delivery failed signature, timestamp or payload verification."""


_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
}


def error_class(status: int) -> type[APIError]:
    if status >= 500:
        return ServerError
    return _BY_STATUS.get(status, APIError)
