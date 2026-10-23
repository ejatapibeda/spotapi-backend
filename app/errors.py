"""Contract error envelope: {ok:false, error:{code, message, retryable}}."""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_COOKIES = "INVALID_COOKIES"
    SPOTIFY_SESSION_INVALID = "SPOTIFY_SESSION_INVALID"
    SPOTIFY_SESSION_MISSING = "SPOTIFY_SESSION_MISSING"
    NO_RESULTS = "NO_RESULTS"
    NO_ACTIVE_DEVICE = "NO_ACTIVE_DEVICE"
    RATE_LIMITED = "RATE_LIMITED"
    COMMAND_FAILED = "COMMAND_FAILED"
    SPOTIFY_UPSTREAM = "SPOTIFY_UPSTREAM"
    TOKEN_UNAVAILABLE = "TOKEN_UNAVAILABLE"


# code -> (http status, retryable)
_DEFAULTS: dict[ErrorCode, tuple[int, bool]] = {
    ErrorCode.BAD_REQUEST: (400, False),
    ErrorCode.UNAUTHORIZED: (401, False),
    ErrorCode.INVALID_COOKIES: (400, False),
    ErrorCode.SPOTIFY_SESSION_INVALID: (401, False),
    ErrorCode.SPOTIFY_SESSION_MISSING: (403, False),
    ErrorCode.NO_RESULTS: (404, False),
    ErrorCode.NO_ACTIVE_DEVICE: (409, True),
    ErrorCode.RATE_LIMITED: (429, True),
    ErrorCode.COMMAND_FAILED: (502, True),
    ErrorCode.SPOTIFY_UPSTREAM: (502, True),
    ErrorCode.TOKEN_UNAVAILABLE: (503, True),
}


class ApiError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status: int | None = None,
        retryable: bool | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        d_status, d_retry = _DEFAULTS[code]
        self.code = code
        self.message = message
        self.status = status or d_status
        self.retryable = d_retry if retryable is None else retryable
        self.retry_after = retry_after

    def body(self) -> dict:
        return {
            "ok": False,
            "error": {"code": self.code.value, "message": self.message, "retryable": self.retryable},
        }

    def headers(self) -> dict[str, str] | None:
        if self.retry_after is not None:
            return {"Retry-After": str(int(self.retry_after))}
        return None
