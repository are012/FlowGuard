"""Endpoint-specific, process-local request limiting for the synchronous MVP."""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from flowguard.observability import bind_request_id, log_event, reset_request_id

logger = logging.getLogger("flowguard.requests")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True)
class RateLimitRule:
    method: str
    path: str
    limit: int
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.limit < 1 or self.window_seconds <= 0:
            raise ValueError("rate-limit values must be positive")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int | None = None


class InMemoryRateLimiter:
    """Sliding-window limiter scoped to one API process.

    This is intentionally not presented as a distributed security boundary. The
    expansion profile must replace it with an authenticated, shared limiter.
    """

    def __init__(
        self,
        rules: list[RateLimitRule],
        *,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 10_000,
    ) -> None:
        self._rules = {(rule.method.upper(), rule.path): rule for rule in rules}
        self._clock = clock
        self._max_buckets = max_buckets
        self._buckets: dict[tuple[str, str, str], deque[float]] = {}
        self._lock = Lock()

    def check(self, method: str, path: str, identity: str) -> RateLimitDecision | None:
        rule = self._rules.get((method.upper(), path))
        if rule is None:
            return None
        now = self._clock()
        key = (rule.method.upper(), rule.path, identity)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_buckets:
                    oldest_key = min(
                        self._buckets,
                        key=lambda item: (
                            self._buckets[item][-1] if self._buckets[item] else float("-inf")
                        ),
                    )
                    self._buckets.pop(oldest_key, None)
                bucket = deque()
                self._buckets[key] = bucket
            cutoff = now - rule.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= rule.limit:
                retry_after = max(1, math.ceil(bucket[0] + rule.window_seconds - now))
                return RateLimitDecision(
                    allowed=False,
                    limit=rule.limit,
                    remaining=0,
                    retry_after_seconds=retry_after,
                )
            bucket.append(now)
            return RateLimitDecision(
                allowed=True,
                limit=rule.limit,
                remaining=rule.limit - len(bucket),
            )


def limiter_from_environment() -> InMemoryRateLimiter | None:
    enabled = os.getenv("FLOWGUARD_RATE_LIMIT_ENABLED", "true").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        return None
    return InMemoryRateLimiter(
        [
            RateLimitRule(
                "POST",
                "/api/v1/imports/transactions",
                _positive_int("FLOWGUARD_IMPORT_RATE_LIMIT_PER_MINUTE", 10),
            ),
            RateLimitRule(
                "POST",
                "/api/v1/analyses",
                _positive_int("FLOWGUARD_ANALYSIS_RATE_LIMIT_PER_MINUTE", 5),
            ),
        ]
    )


class OperationalMiddleware:
    """Attach correlation IDs, structured request logs, and costly-route limits."""

    def __init__(self, app: ASGIApp, limiter: InMemoryRateLimiter | None = None) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        supplied_request_id = headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else f"request-{uuid4()}"
        )
        scope.setdefault("state", {})["request_id"] = request_id
        token = bind_request_id(request_id)
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        identity = headers.get("x-user-id", "").strip() or os.getenv(
            "FLOWGUARD_DEMO_USER_ID", "demo-user"
        )
        decision = self.limiter.check(method, path, identity) if self.limiter else None
        started_at = time.monotonic()
        if decision is not None and not decision.allowed:
            assert decision.retry_after_seconds is not None
            log_event(
                logger,
                logging.WARNING,
                "rate_limit_exceeded",
                method=method,
                path=path,
                retry_after_seconds=decision.retry_after_seconds,
            )
            response = JSONResponse(
                status_code=429,
                content={
                    "code": "RATE_LIMITED",
                    "message": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
                    "details": {"retry_after_seconds": decision.retry_after_seconds},
                    "retryable": True,
                },
                headers={
                    "X-Request-ID": request_id,
                    "RateLimit-Limit": str(decision.limit),
                    "RateLimit-Remaining": "0",
                    "Retry-After": str(decision.retry_after_seconds),
                },
            )
            await response(scope, receive, send)
            log_event(
                logger,
                logging.INFO,
                "request_completed",
                method=method,
                path=path,
                status_code=429,
                duration_ms=_elapsed_ms(started_at),
            )
            reset_request_id(token)
            return

        status_code = 500

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                if decision is not None:
                    response_headers["RateLimit-Limit"] = str(decision.limit)
                    response_headers["RateLimit-Remaining"] = str(decision.remaining)
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            log_event(
                logger,
                logging.ERROR,
                "request_failed",
                method=method,
                path=path,
                exception_type=type(exc).__name__,
                duration_ms=_elapsed_ms(started_at),
            )
            raise
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", path)
            log_event(
                logger,
                logging.INFO,
                "request_completed",
                method=method,
                path=str(route_path),
                status_code=status_code,
                duration_ms=_elapsed_ms(started_at),
            )
            reset_request_id(token)


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.monotonic() - started_at) * 1000))


__all__ = [
    "InMemoryRateLimiter",
    "OperationalMiddleware",
    "RateLimitDecision",
    "RateLimitRule",
    "limiter_from_environment",
]
