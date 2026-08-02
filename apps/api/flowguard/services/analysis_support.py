"""Small analysis support services that keep the orchestrator thin."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from flowguard.ai_contract import AIToBackendResponse, BackendToAIRequest

RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
CONTRACT_VERSION = "1.1"
ISO_DATE_PATTERN = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
KOREAN_DATE_PATTERN = re.compile(r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일")
WON_AMOUNT_PATTERN = re.compile(r"(?<![\d,])(-?\d[\d,]*)\s*(만원|만\s*원|천원|천\s*원|원)")


@dataclass(frozen=True)
class InterpretationOutcome:
    status: Literal["SUCCEEDED", "FALLBACK", "FAILED"]
    response_payload: dict[str, Any]
    attempt_count: int
    latency_ms: int
    error_code: str | None = None

    @property
    def fallback_used(self) -> bool:
        return self.status == "FALLBACK"

    def public_payload(self) -> dict[str, Any]:
        return {
            **self.response_payload,
            "source": "AI" if self.status == "SUCCEEDED" else "DETERMINISTIC_FALLBACK",
            "fallbackReason": self.error_code,
        }


class AIInterpretationClient:
    """Call the isolated AI service, validate its contract, and fall back safely."""

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

    def interpret(self, payload: Mapping[str, Any]) -> InterpretationOutcome:
        started_at = time.monotonic()
        try:
            request_body = BackendToAIRequest.model_validate(payload)
        except Exception:
            return self._fallback(payload, "invalid_payload", 0, started_at)

        attempt_count = 0
        deadline = started_at + self.total_timeout_seconds
        with httpx.Client(transport=self.transport, trust_env=False) as client:
            while attempt_count <= self.max_retries:
                attempt_count += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "total_timeout",
                        attempt_count - 1,
                        started_at,
                    )
                timeout = httpx.Timeout(
                    timeout=remaining,
                    connect=min(self.connect_timeout_seconds, remaining),
                    read=min(self.read_timeout_seconds, remaining),
                    write=min(self.connect_timeout_seconds, remaining),
                    pool=min(self.connect_timeout_seconds, remaining),
                )
                try:
                    response = client.post(
                        f"{self.base_url}/interpret",
                        json=request_body.model_dump(mode="json"),
                        timeout=timeout,
                    )
                except httpx.ConnectError:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "connection_failed",
                        attempt_count,
                        started_at,
                    )
                except httpx.TimeoutException:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "timeout",
                        attempt_count,
                        started_at,
                    )
                except httpx.HTTPError:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "http_client_error",
                        attempt_count,
                        started_at,
                    )

                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt_count <= self.max_retries:
                        continue
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        f"http_{response.status_code}",
                        attempt_count,
                        started_at,
                    )
                if not 200 <= response.status_code < 300:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        f"http_{response.status_code}",
                        attempt_count,
                        started_at,
                    )

                try:
                    response_payload = response.json()
                except ValueError:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "invalid_json",
                        attempt_count,
                        started_at,
                    )
                try:
                    validated = AIToBackendResponse.model_validate(response_payload)
                except Exception:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "invalid_response",
                        attempt_count,
                        started_at,
                    )

                mismatch = self._contract_mismatch(request_body, validated)
                if mismatch is not None:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        mismatch,
                        attempt_count,
                        started_at,
                    )
                candidate_ids = {
                    item.actionId for item in request_body.actionCandidates if item.feasible is True
                }
                ranked_ids = {item.actionId for item in validated.rankedActions}
                if not ranked_ids.issubset(candidate_ids):
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        "unknown_ranked_action",
                        attempt_count,
                        started_at,
                    )
                unsupported_claim = self._unsupported_numeric_claim(request_body, validated)
                if unsupported_claim is not None:
                    return self._fallback(
                        request_body.model_dump(mode="json"),
                        unsupported_claim,
                        attempt_count,
                        started_at,
                    )
                return InterpretationOutcome(
                    status="SUCCEEDED",
                    response_payload=validated.model_dump(mode="json"),
                    attempt_count=attempt_count,
                    latency_ms=self._elapsed_ms(started_at),
                )

        return self._fallback(
            request_body.model_dump(mode="json"),
            "retry_exhausted",
            attempt_count,
            started_at,
        )

    @staticmethod
    def _contract_mismatch(
        request_body: BackendToAIRequest,
        response_body: AIToBackendResponse,
    ) -> str | None:
        fields = (
            "contractVersion",
            "promptVersion",
            "requestId",
            "idempotencyKey",
            "analysisId",
            "snapshotId",
            "snapshotRevision",
            "locale",
        )
        for field in fields:
            if getattr(request_body, field) != getattr(response_body, field):
                return f"{field}_mismatch"
        return None

    @staticmethod
    def _unsupported_numeric_claim(
        request_body: BackendToAIRequest,
        response_body: AIToBackendResponse,
    ) -> str | None:
        """Reject explicit amount/date claims that were absent from the request."""

        supplied_amounts: set[int] = set()
        supplied_dates: set[str] = set()

        def collect(value: Any) -> None:
            if isinstance(value, bool):
                return
            if isinstance(value, int):
                supplied_amounts.add(value)
                supplied_amounts.add(abs(value))
                return
            if isinstance(value, str):
                supplied_dates.update(ISO_DATE_PATTERN.findall(value))
                return
            if isinstance(value, Mapping):
                for item in value.values():
                    collect(item)
                return
            if isinstance(value, list):
                for item in value:
                    collect(item)

        collect(
            {
                "facts": request_body.facts.model_dump(mode="json"),
                "evidence": request_body.evidence,
                "actionCandidates": [
                    item.model_dump(mode="json") for item in request_body.actionCandidates
                ],
            }
        )
        supplied_month_days = {
            (int(date_text[5:7]), int(date_text[8:10])) for date_text in supplied_dates
        }
        generated_text = "\n".join(
            [
                response_body.riskExplanation,
                response_body.userMessage,
                *(item.reason for item in response_body.rankedActions),
            ]
        )
        if any(
            date_text not in supplied_dates
            for date_text in ISO_DATE_PATTERN.findall(generated_text)
        ):
            return "unknown_date_claim"
        for year, month, day in KOREAN_DATE_PATTERN.findall(generated_text):
            if (int(month), int(day)) not in supplied_month_days:
                return "unknown_date_claim"
            if year and not any(
                date_text.startswith(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
                for date_text in supplied_dates
            ):
                return "unknown_date_claim"
        multipliers = {"원": 1, "천원": 1_000, "만원": 10_000}
        for raw_amount, raw_unit in WON_AMOUNT_PATTERN.findall(generated_text):
            unit = raw_unit.replace(" ", "")
            amount = int(raw_amount.replace(",", "")) * multipliers[unit]
            if amount not in supplied_amounts:
                return "unknown_amount_claim"
        return None

    def _fallback(
        self,
        payload: Mapping[str, Any],
        error_code: str,
        attempt_count: int,
        started_at: float,
    ) -> InterpretationOutcome:
        envelope = {
            field: payload.get(field)
            for field in (
                "schemaVersion",
                "contractVersion",
                "promptVersion",
                "requestId",
                "idempotencyKey",
                "analysisId",
                "snapshotId",
                "snapshotRevision",
                "locale",
            )
            if payload.get(field) is not None
        }
        return InterpretationOutcome(
            status="FALLBACK",
            response_payload={
                **envelope,
                "riskExplanation": "규칙 기반으로 위험을 요약했습니다.",
                "rankedActions": [],
                "userMessage": "금융 위험을 규칙 기반으로 확인했습니다.",
            },
            attempt_count=attempt_count,
            latency_ms=self._elapsed_ms(started_at),
            error_code=error_code,
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((time.monotonic() - started_at) * 1000))


__all__ = [
    "AIInterpretationClient",
    "CONTRACT_VERSION",
    "InterpretationOutcome",
    "RETRYABLE_STATUS_CODES",
]
