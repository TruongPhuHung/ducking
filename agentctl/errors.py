from __future__ import annotations

from typing import Any


class AgentCtlError(Exception):
    """Expected user-facing failure with a stable machine code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "agentctl_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
