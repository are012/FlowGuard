"""Small structured-log helpers shared by the API and AI service."""

from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("flowguard_request_id", default=None)


def configure_logging() -> None:
    """Configure FlowGuard logging while preserving an embedding host's handlers."""

    configured = os.getenv("FLOWGUARD_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelNamesMapping().get(configured, logging.INFO)
    application_logger = logging.getLogger("flowguard")
    application_logger.setLevel(level)
    if not application_logger.hasHandlers():
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        application_logger.addHandler(handler)
        application_logger.propagate = False


def bind_request_id(request_id: str) -> Token[str | None]:
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: str | int | float | bool | None,
) -> None:
    """Emit one JSON object containing only caller-selected, scalar fields."""

    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
    }
    request_id = _request_id.get()
    if request_id is not None and "request_id" not in fields:
        payload["request_id"] = request_id
    payload.update(fields)
    logger.log(
        level,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        stacklevel=2,
    )


__all__ = [
    "bind_request_id",
    "configure_logging",
    "log_event",
    "reset_request_id",
]
