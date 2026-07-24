"""Stable application errors shared by REST and MCP boundaries."""

from __future__ import annotations

from typing import Any


class ServiceError(RuntimeError):
    """A fail-closed error with a public code and safe details."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "retryable": self.retryable,
        }


def not_found(resource: str, resource_id: str) -> ServiceError:
    return ServiceError(
        "RESOURCE_NOT_FOUND",
        "요청한 데이터를 찾을 수 없습니다.",
        details={"resource": resource, "resource_id": resource_id},
        http_status=404,
    )
