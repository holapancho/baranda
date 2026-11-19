"""Error types shared by every tool.

Every failure a tool reports has the same shape, so the model always gets a ``kind`` it can act on:
fix its arguments, tell the user, or stop.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    INVALID_INPUT = "invalid_input"
    AUTH = "auth"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    INTERNAL = "internal"
    CIRCUIT_OPEN = "circuit_open"


class ToolError(Exception):
    """An error meant to be returned to the model as a structured result."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "kind": str(self.kind),
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.status is not None:
            error["status"] = self.status
        return {"error": error}


class Denied(ToolError):
    """A policy decision. Not a failure to retry: the request is not allowed."""

    def __init__(self, message: str, *, layer: str | None = None) -> None:
        super().__init__(ErrorKind.DENIED, message)
        self.layer = layer


class PolicyError(Exception):
    """A policy file is missing, malformed or uses keys its layer mode does not allow."""
