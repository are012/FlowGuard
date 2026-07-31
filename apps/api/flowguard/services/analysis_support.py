"""Small analysis support services that keep the orchestrator thin."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib import error, request


class AIInterpretationClient:
    """Call the AI server, validate the response schema, and fall back safely."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def interpret(self, *, analysis_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from flowguard.api.schemas import BackendToAIRequest

            body = BackendToAIRequest.model_validate(
                {
                    "schemaVersion": "1.0",
                    "analysisId": analysis_id,
                    "calculatedAt": payload.get("calculatedAt", "2026-01-01T00:00:00+09:00"),
                    "facts": payload.get("facts", {}),
                    "evidence": payload.get("evidence", []),
                    "actionCandidates": payload.get("actionCandidates", []),
                }
            )
        except Exception:
            body = None

        if body is None:
            return self._fallback(analysis_id, "invalid_payload")

        try:
            http_request = request.Request(
                f"{self.base_url}/interpret",
                data=json.dumps(body.model_dump(mode="json")).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except (error.URLError, error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
            return self._fallback(analysis_id, "timeout_or_error")

        try:
            from flowguard.api.schemas import AIToBackendResponse

            validated = AIToBackendResponse.model_validate(response_payload)
        except Exception:
            return self._fallback(analysis_id, "invalid_response")

        if validated.analysisId != analysis_id:
            return self._fallback(analysis_id, "analysis_id_mismatch")

        candidate_ids = self._candidate_ids(body.actionCandidates)
        ranked_ids = {item.actionId for item in validated.rankedActions}
        if ranked_ids and not ranked_ids.issubset(candidate_ids):
            return self._fallback(analysis_id, "unknown_ranked_action")

        return {
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
        }

    def _fallback(self, analysis_id: str, reason: str) -> dict[str, Any]:
        return {
            "analysisId": analysis_id,
            "riskExplanation": "규칙 기반으로 위험을 요약했습니다.",
            "rankedActions": [],
            "userMessage": "금융 위험을 규칙 기반으로 확인했습니다.",
            "source": "fallback",
            "fallbackReason": reason,
        }

    @staticmethod
    def _candidate_ids(action_candidates: Iterable[dict[str, Any]]) -> set[str]:
        ids: set[str] = set()
        for candidate in action_candidates:
            raw_id = candidate.get("actionId")
            if isinstance(raw_id, str) and raw_id:
                ids.add(raw_id)
        return ids
