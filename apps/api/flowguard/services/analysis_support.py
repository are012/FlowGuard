"""Small analysis support services that keep the orchestrator thin."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Any
from urllib import error, request

from flowguard.storage import FlowGuardRepository

logger = logging.getLogger(__name__)


class _AITransportError(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.details = details or {}


class AIInterpretationClient:
    """Call the AI server, validate the response schema, and fall back safely."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 3.0,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.25,
        max_response_bytes: int = 32_768,
        prompt_version: str = "ai-interpretation-v1",
        model_name: str = "ai-service",
        max_concurrent_requests: int = 2,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_open_seconds: float = 30.0,
        repository: FlowGuardRepository | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.max_response_bytes = max(1_024, max_response_bytes)
        self.prompt_version = prompt_version
        self.model_name = model_name
        self.repository = repository
        self._concurrency = threading.BoundedSemaphore(max(1, max_concurrent_requests))
        self._circuit_breaker_threshold = max(1, circuit_breaker_threshold)
        self._circuit_breaker_open_seconds = max(1.0, circuit_breaker_open_seconds)
        self._failure_lock = threading.Lock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def interpret(
        self,
        *,
        analysis_id: str,
        payload: dict[str, Any],
        user_id: str | None = None,
        snapshot_revision: str | None = None,
        correlation_id: str | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        body = self._validate_request_payload(analysis_id, payload)
        if body is None:
            return self._fallback(
                analysis_id,
                "invalid_payload",
                facts=payload.get("facts"),
                action_candidates=payload.get("actionCandidates"),
                correlation_id=correlation_id,
            )

        active_prompt_version = prompt_version or self.prompt_version
        active_model_name = model_name or self.model_name
        ai_request_id: str | None = None
        if self.repository is not None and user_id is not None and snapshot_revision is not None:
            tracked, created = self.repository.begin_ai_interpretation(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot_revision=snapshot_revision,
                contract_version=body.schemaVersion,
                prompt_version=active_prompt_version,
                correlation_id=correlation_id,
                model_name=active_model_name,
                request_payload=body.model_dump(mode="json"),
            )
            ai_request_id = tracked["ai_request_id"]
            if not created:
                cached = tracked.get("response_payload")
                if tracked["status"] in {"SUCCEEDED", "FALLBACK"} and isinstance(cached, dict):
                    return dict(cached)
                return self._fallback(
                    analysis_id,
                    "duplicate_inflight",
                    facts=payload.get("facts"),
                    action_candidates=payload.get("actionCandidates"),
                    correlation_id=correlation_id,
                    ai_request_id=ai_request_id,
                    contract_version=body.schemaVersion,
                    prompt_version=active_prompt_version,
                    model_name=active_model_name,
                )

        if self._is_circuit_open():
            fallback = self._fallback(
                analysis_id,
                "circuit_open",
                facts=payload.get("facts"),
                action_candidates=payload.get("actionCandidates"),
                correlation_id=correlation_id,
                ai_request_id=ai_request_id,
                contract_version=body.schemaVersion,
                prompt_version=active_prompt_version,
                model_name=active_model_name,
            )
            self._finalize(
                ai_request_id,
                status="FALLBACK",
                response_payload=fallback,
                error={"reason": "circuit_open"},
                attempt_count=0,
            )
            self._log(
                "ai_interpretation_fallback",
                analysis_id=analysis_id,
                snapshot_revision=body.snapshotRevision,
                ai_request_id=ai_request_id,
                correlation_id=correlation_id,
                reason="circuit_open",
                prompt_version=active_prompt_version,
                contract_version=body.schemaVersion,
                model=active_model_name,
            )
            return fallback

        if not self._concurrency.acquire(timeout=self.timeout_seconds):
            fallback = self._fallback(
                analysis_id,
                "concurrency_limited",
                facts=payload.get("facts"),
                action_candidates=payload.get("actionCandidates"),
                correlation_id=correlation_id,
                ai_request_id=ai_request_id,
                contract_version=body.schemaVersion,
                prompt_version=active_prompt_version,
                model_name=active_model_name,
            )
            self._finalize(
                ai_request_id,
                status="FALLBACK",
                response_payload=fallback,
                error={"reason": "concurrency_limited"},
                attempt_count=0,
            )
            return fallback

        try:
            response_payload: dict[str, Any] | None = None
            attempt_count = 0
            for attempt in range(self.max_retries + 1):
                attempt_count = attempt + 1
                try:
                    self._log(
                        "ai_interpretation_attempt",
                        analysis_id=analysis_id,
                        snapshot_revision=body.snapshotRevision,
                        ai_request_id=ai_request_id,
                        correlation_id=correlation_id,
                        attempt=attempt_count,
                        prompt_version=active_prompt_version,
                        contract_version=body.schemaVersion,
                        model=active_model_name,
                    )
                    response_payload = self._send_request(body.model_dump(mode="json"))
                    break
                except _AITransportError as exc:
                    if exc.retryable and attempt < self.max_retries:
                        self._sleep_before_retry(attempt)
                        continue
                    self._register_failure()
                    fallback = self._fallback(
                        analysis_id,
                        exc.reason,
                        facts=payload.get("facts"),
                        action_candidates=payload.get("actionCandidates"),
                        correlation_id=correlation_id,
                        ai_request_id=ai_request_id,
                        contract_version=body.schemaVersion,
                        prompt_version=active_prompt_version,
                        model_name=active_model_name,
                    )
                    self._finalize(
                        ai_request_id,
                        status="FALLBACK",
                        response_payload=fallback,
                        error={
                            "reason": exc.reason,
                            "retryable": exc.retryable,
                            "details": exc.details,
                        },
                        attempt_count=attempt_count,
                    )
                    self._log(
                        "ai_interpretation_fallback",
                        analysis_id=analysis_id,
                        snapshot_revision=body.snapshotRevision,
                        ai_request_id=ai_request_id,
                        correlation_id=correlation_id,
                        attempt=attempt_count,
                        reason=exc.reason,
                        prompt_version=active_prompt_version,
                        contract_version=body.schemaVersion,
                        model=active_model_name,
                    )
                    return fallback
        finally:
            self._concurrency.release()

        validated = self._validate_response_payload(response_payload)
        if validated is None:
            self._register_failure()
            fallback = self._fallback(
                analysis_id,
                "invalid_response",
                facts=payload.get("facts"),
                action_candidates=payload.get("actionCandidates"),
                correlation_id=correlation_id,
                ai_request_id=ai_request_id,
                contract_version=body.schemaVersion,
                prompt_version=active_prompt_version,
                model_name=active_model_name,
            )
            self._finalize(
                ai_request_id,
                status="FALLBACK",
                response_payload=fallback,
                error={"reason": "invalid_response"},
                attempt_count=attempt_count,
            )
            return fallback

        if validated.analysisId != analysis_id:
            self._register_failure()
            fallback = self._fallback(
                analysis_id,
                "analysis_id_mismatch",
                facts=payload.get("facts"),
                action_candidates=payload.get("actionCandidates"),
                correlation_id=correlation_id,
                ai_request_id=ai_request_id,
                contract_version=body.schemaVersion,
                prompt_version=active_prompt_version,
                model_name=active_model_name,
            )
            self._finalize(
                ai_request_id,
                status="FALLBACK",
                response_payload=fallback,
                error={"reason": "analysis_id_mismatch"},
                attempt_count=attempt_count,
            )
            return fallback

        candidate_ids = self._candidate_ids(body.actionCandidates)
        ranked_ids = {item.actionId for item in validated.rankedActions}
        if ranked_ids and not ranked_ids.issubset(candidate_ids):
            self._register_failure()
            fallback = self._fallback(
                analysis_id,
                "unknown_ranked_action",
                facts=payload.get("facts"),
                action_candidates=payload.get("actionCandidates"),
                correlation_id=correlation_id,
                ai_request_id=ai_request_id,
                contract_version=body.schemaVersion,
                prompt_version=active_prompt_version,
                model_name=active_model_name,
            )
            self._finalize(
                ai_request_id,
                status="FALLBACK",
                response_payload=fallback,
                error={"reason": "unknown_ranked_action"},
                attempt_count=attempt_count,
            )
            return fallback

        result = {
            "analysisId": validated.analysisId,
            "riskExplanation": validated.riskExplanation,
            "rankedActions": [
                {
                    "actionId": item.actionId,
                    "priority": item.priority,
                    "reason": item.reason,
                }
                for item in validated.rankedActions
            ],
            "userMessage": validated.userMessage,
            "source": "ai",
            "aiRequestId": ai_request_id,
            "contractVersion": body.schemaVersion,
            "promptVersion": active_prompt_version,
            "model": active_model_name,
            "attemptCount": attempt_count,
        }
        self._reset_failures()
        self._finalize(
            ai_request_id,
            status="SUCCEEDED",
            response_payload=result,
            attempt_count=attempt_count,
        )
        self._log(
            "ai_interpretation_succeeded",
            analysis_id=analysis_id,
            snapshot_revision=body.snapshotRevision,
            ai_request_id=ai_request_id,
            correlation_id=correlation_id,
            attempt=attempt_count,
            prompt_version=active_prompt_version,
            contract_version=body.schemaVersion,
            model=active_model_name,
        )
        return result

    @staticmethod
    def _validate_request_payload(analysis_id: str, payload: dict[str, Any]) -> Any | None:
        try:
            from flowguard.api.schemas import BackendToAIRequest

            return BackendToAIRequest.model_validate(
                {
                    "schemaVersion": "1.0",
                    "analysisId": analysis_id,
                    "snapshotRevision": payload.get("snapshotRevision"),
                    "calculatedAt": payload.get("calculatedAt", "2026-01-01T00:00:00+09:00"),
                    "facts": payload.get("facts", {}),
                    "evidence": payload.get("evidence", []),
                    "actionCandidates": payload.get("actionCandidates", []),
                }
            )
        except Exception:
            return None

    @staticmethod
    def _validate_response_payload(response_payload: dict[str, Any] | None) -> Any | None:
        if response_payload is None:
            return None
        try:
            from flowguard.api.schemas import AIToBackendResponse

            return AIToBackendResponse.model_validate(response_payload)
        except Exception:
            return None

    def _send_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        http_request = request.Request(
            f"{self.base_url}/interpret",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                raw_body = response.read(self.max_response_bytes + 1)
                if len(raw_body) > self.max_response_bytes:
                    raise _AITransportError(
                        "response_too_large",
                        retryable=False,
                        details={"max_response_bytes": self.max_response_bytes},
                    )
                return json.loads(raw_body.decode("utf-8"))
        except error.HTTPError as exc:
            retryable = exc.code in {429, 502, 503, 504}
            raise _AITransportError(
                "timeout_or_error",
                retryable=retryable,
                details={"http_status": exc.code},
            ) from exc
        except (error.URLError, TimeoutError) as exc:
            raise _AITransportError("timeout_or_error", retryable=True) from exc
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise _AITransportError("invalid_response", retryable=False) from exc

    def _fallback(
        self,
        analysis_id: str,
        reason: str,
        *,
        facts: Any = None,
        action_candidates: Any = None,
        correlation_id: str | None = None,
        ai_request_id: str | None = None,
        contract_version: str | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        risk = facts.get("nextRisk", {}) if isinstance(facts, dict) else {}
        risk_date = risk.get("date")
        shortage_amount = risk.get("shortageAmount")
        risk_type = risk.get("type")
        explanation = "규칙 기반 분석으로 위험 요약을 제공합니다."
        if isinstance(risk_date, str) and isinstance(shortage_amount, int):
            if risk_type:
                explanation = f"{risk_date}에 {shortage_amount:,}원 부족 위험이 예상됩니다."
            else:
                explanation = f"{risk_date}에 자금 부족 가능성이 있습니다."
        message = "AI 해석 대신 백엔드 계산 결과를 기준으로 안내합니다."
        first_action = self._first_feasible_action(action_candidates)
        if first_action is not None:
            action_type = first_action.get("type")
            amount = first_action.get("amount")
            if action_type == "transfer" and isinstance(amount, int):
                message = f"검증된 이체 후보 {amount:,}원을 먼저 검토해 주세요."
            elif isinstance(action_type, str):
                message = f"검증된 {action_type} 대응안을 먼저 검토해 주세요."
        return {
            "analysisId": analysis_id,
            "riskExplanation": explanation,
            "rankedActions": [],
            "userMessage": message,
            "source": "fallback",
            "fallbackReason": reason,
            "aiRequestId": ai_request_id,
            "correlationId": correlation_id,
            "contractVersion": contract_version,
            "promptVersion": prompt_version,
            "model": model_name,
        }

    @staticmethod
    def _candidate_ids(action_candidates: Iterable[dict[str, Any]]) -> set[str]:
        ids: set[str] = set()
        for candidate in action_candidates:
            raw_id = candidate.get("actionId")
            if isinstance(raw_id, str) and raw_id:
                ids.add(raw_id)
        return ids

    @staticmethod
    def _first_feasible_action(action_candidates: Any) -> dict[str, Any] | None:
        if not isinstance(action_candidates, list):
            return None
        for candidate in action_candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("feasible") is False:
                continue
            return candidate
        return None

    def _finalize(
        self,
        ai_request_id: str | None,
        *,
        status: str,
        response_payload: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        attempt_count: int | None = None,
    ) -> None:
        if self.repository is None or ai_request_id is None:
            return
        self.repository.finalize_ai_interpretation(
            ai_request_id,
            status=status,
            response_payload=response_payload,
            error=error,
            attempt_count=attempt_count,
        )

    def _is_circuit_open(self) -> bool:
        with self._failure_lock:
            return time.monotonic() < self._circuit_open_until

    def _register_failure(self) -> None:
        with self._failure_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_breaker_threshold:
                self._circuit_open_until = time.monotonic() + self._circuit_breaker_open_seconds

    def _reset_failures(self) -> None:
        with self._failure_lock:
            self._consecutive_failures = 0
            self._circuit_open_until = 0.0

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff_seconds * (2**attempt)
        jitter = random.uniform(0, max(0.01, self.retry_backoff_seconds / 2))
        time.sleep(delay + jitter)

    @staticmethod
    def _log(event: str, **fields: Any) -> None:
        joined = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
        logger.info("%s %s", event, joined)
