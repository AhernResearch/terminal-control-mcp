"""Stable errors returned by terminal-control MCP tools."""

from dataclasses import dataclass
from typing import Any


@dataclass
class TerminalControlError(Exception):
    """Expected, machine-readable terminal-control failure."""

    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def ok_response(target: str | None = None, **payload: Any) -> dict[str, Any]:
    """Build a successful response with a stable envelope."""
    result: dict[str, Any] = {"status": "ok"}
    if target is not None:
        result["target"] = target
    result.update(payload)
    return result


def error_response(
    error: TerminalControlError,
    target: str | None = None,
) -> dict[str, Any]:
    """Build an error response without exposing exception internals."""
    result: dict[str, Any] = {
        "status": "error",
        "error": {"code": error.code, "message": error.message},
    }
    if target is not None:
        result["target"] = target
    return result
