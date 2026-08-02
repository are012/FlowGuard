"""Validate and call the isolated AI label-grouping contract."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from flowguard.ai_contract import LabelClassificationRequest, LabelClassificationResponse
from flowguard.ai_numeric_policy import contains_numeric_expression

RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
CLASSIFICATION_CONTRACT_VERSION = "1.2"
CLASSIFICATION_PROMPT_VERSION = "classify-1"
CLASSIFICATION_ECHO_FIELDS = (
    "schemaVersion",
    "contractVersion",
    "promptVersion",
    "requestId",
    "idempotencyKey",
    "importId",
    "locale",
)
INFLOW_CATEGORY_HINTS = {"RECEIVABLE", "OTHER_INFLOW"}


@dataclass(frozen=True)
class LabelClassificationOutcome:
    status: Literal["SUCCEEDED", "REJECTED", "FAILED"]
    response_payload: dict[str, Any]
    attempt_count: int
    latency_ms: int
    error_code: str | None = None


def validate_label_group_response(
    request_body: LabelClassificationRequest,
    response_body: LabelClassificationResponse,
) -> str | None:
    """Return a stable error code when a syntactically valid response violates the request."""

    for field in CLASSIFICATION_ECHO_FIELDS:
        if getattr(request_body, field) != getattr(response_body, field):
            return f"{field}_mismatch"

    directions = {item.labelId: item.direction for item in request_body.labels}
    requested_ids = set(directions)
    seen_ids: set[str] = set()

    for group in response_body.groups:
        group_ids = group.labelIds
        if any(label_id not in requested_ids for label_id in group_ids):
            return "unknown_label_id"
        if len(group_ids) != len(set(group_ids)) or any(
            label_id in seen_ids for label_id in group_ids
        ):
            return "duplicate_label_id"
        if len({directions[label_id] for label_id in group_ids}) != 1:
            return "mixed_direction_group"
        direction = directions[group_ids[0]]
        category_hint = group.categoryHint.value
        if (direction == "INFLOW") != (category_hint in INFLOW_CATEGORY_HINTS):
            return "category_direction_mismatch"
        if contains_numeric_expression(group.normalizedName) or contains_numeric_expression(
            group.reason
        ):
            return "unsupported_numeric_claim"
        seen_ids.update(group_ids)

    for label_id in response_body.ungrouped:
        if label_id not in requested_ids:
            return "unknown_label_id"
        if label_id in seen_ids:
            return "duplicate_label_id"
        seen_ids.add(label_id)

    if seen_ids != requested_ids:
        return "missing_label_id"
    return None


class AILabelClassificationClient:
    """Call the isolated classifier and distinguish transport from contract failures."""

    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout_seconds: float = 3.0,
        read_timeout_seconds: float = 10.0,
        total_timeout_seconds: float = 15.0,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.max_retries = max_retries
        self.transport = transport

    def classify(self, payload: Mapping[str, Any]) -> LabelClassificationOutcome:
        started_at = time.monotonic()
        try:
            request_body = LabelClassificationRequest.model_validate(payload)
        except Exception:
            return self._outcome("REJECTED", "invalid_payload", 0, started_at)

        attempt_count = 0
        deadline = started_at + self.total_timeout_seconds
        with httpx.Client(transport=self.transport, trust_env=False) as client:
            while attempt_count <= self.max_retries:
                attempt_count += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._outcome("FAILED", "total_timeout", attempt_count - 1, started_at)
                timeout = httpx.Timeout(
                    timeout=remaining,
                    connect=min(self.connect_timeout_seconds, remaining),
                    read=min(self.read_timeout_seconds, remaining),
                    write=min(self.connect_timeout_seconds, remaining),
                    pool=min(self.connect_timeout_seconds, remaining),
                )
                try:
                    response = client.post(
                        f"{self.base_url}/classify/labels",
                        json=request_body.model_dump(mode="json"),
                        timeout=timeout,
                    )
                except httpx.ConnectError:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._outcome("FAILED", "connection_failed", attempt_count, started_at)
                except httpx.TimeoutException:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._outcome("FAILED", "timeout", attempt_count, started_at)
                except httpx.HTTPError:
                    return self._outcome("FAILED", "http_client_error", attempt_count, started_at)

                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._outcome(
                        "FAILED", f"http_{response.status_code}", attempt_count, started_at
                    )
                if response.status_code == 422 and self._is_contract_rejection(response):
                    return self._outcome(
                        "REJECTED",
                        "invalid_classification_response",
                        attempt_count,
                        started_at,
                    )
                if not 200 <= response.status_code < 300:
                    return self._outcome(
                        "FAILED", f"http_{response.status_code}", attempt_count, started_at
                    )
                try:
                    response_payload = response.json()
                except ValueError:
                    return self._outcome("REJECTED", "invalid_json", attempt_count, started_at)
                try:
                    validated = LabelClassificationResponse.model_validate(response_payload)
                except Exception:
                    return self._outcome("REJECTED", "invalid_response", attempt_count, started_at)
                error_code = validate_label_group_response(request_body, validated)
                if error_code is not None:
                    return self._outcome("REJECTED", error_code, attempt_count, started_at)
                return LabelClassificationOutcome(
                    status="SUCCEEDED",
                    response_payload=validated.model_dump(mode="json"),
                    attempt_count=attempt_count,
                    latency_ms=self._elapsed_ms(started_at),
                )

        return self._outcome("FAILED", "retry_exhausted", attempt_count, started_at)

    def _outcome(
        self,
        status: Literal["REJECTED", "FAILED"],
        error_code: str,
        attempt_count: int,
        started_at: float,
    ) -> LabelClassificationOutcome:
        return LabelClassificationOutcome(
            status=status,
            response_payload={},
            attempt_count=attempt_count,
            latency_ms=self._elapsed_ms(started_at),
            error_code=error_code,
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((time.monotonic() - started_at) * 1000))

    @staticmethod
    def _is_contract_rejection(response: httpx.Response) -> bool:
        try:
            detail = response.json().get("detail")
        except (AttributeError, ValueError):
            return False
        return (
            isinstance(detail, Mapping) and detail.get("code") == "invalid_classification_response"
        )


__all__ = [
    "AILabelClassificationClient",
    "CLASSIFICATION_CONTRACT_VERSION",
    "CLASSIFICATION_PROMPT_VERSION",
    "LabelClassificationOutcome",
    "validate_label_group_response",
]
