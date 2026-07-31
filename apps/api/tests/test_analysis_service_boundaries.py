from __future__ import annotations

import json

import pytest

from flowguard.services import analysis_support
from flowguard.services.analysis_support import AIInterpretationClient


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    monkeypatch.setattr(
        analysis_support.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )


def test_ai_interpretation_client_falls_back_when_server_unavailable() -> None:
    client = AIInterpretationClient(base_url="http://127.0.0.1:1", timeout_seconds=0.01)

    result = client.interpret(
        analysis_id="analysis-001",
        payload={
            "calculatedAt": "2026-07-31T12:00:00+09:00",
            "facts": {
                "safeToSpend": 180000,
                "nextRisk": {
                    "type": "PAYMENT_ACCOUNT",
                    "date": "2026-08-03",
                    "shortageAmount": 240000,
                },
                "cashflowSummary": {
                    "lowestBalance": -240000,
                    "lowestBalanceDate": "2026-08-03",
                },
            },
            "actionCandidates": [{"actionId": "transfer-1"}],
        },
    )

    assert result["source"] == "fallback"
    assert result["fallbackReason"] == "timeout_or_error"
    assert result["rankedActions"] == []


def test_ai_interpretation_client_falls_back_on_invalid_payload() -> None:
    client = AIInterpretationClient(base_url="http://127.0.0.1:1", timeout_seconds=0.01)

    result = client.interpret(
        analysis_id="analysis-001",
        payload={"facts": {"safeToSpend": 180000}},
    )

    assert result["source"] == "fallback"
    assert result["fallbackReason"] == "invalid_payload"


def test_ai_interpretation_client_rejects_mismatched_analysis_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AIInterpretationClient(base_url="http://localhost:8001")
    patch_urlopen(
        monkeypatch,
        {
            "schemaVersion": "1.0",
            "analysisId": "analysis-other",
            "riskExplanation": "ok",
            "rankedActions": [{"actionId": "transfer-1", "priority": 1, "reason": "ok"}],
            "userMessage": "ok",
        },
    )

    result = client.interpret(
        analysis_id="analysis-001",
        payload={
            "calculatedAt": "2026-07-31T12:00:00+09:00",
            "facts": {
                "safeToSpend": 180000,
                "nextRisk": {
                    "type": "PAYMENT_ACCOUNT",
                    "date": "2026-08-03",
                    "shortageAmount": 240000,
                },
                "cashflowSummary": {
                    "lowestBalance": -240000,
                    "lowestBalanceDate": "2026-08-03",
                },
            },
            "actionCandidates": [{"actionId": "transfer-1"}],
        },
    )

    assert result["source"] == "fallback"
    assert result["fallbackReason"] == "analysis_id_mismatch"


def test_ai_interpretation_client_rejects_unknown_ranked_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AIInterpretationClient(base_url="http://localhost:8001")
    patch_urlopen(
        monkeypatch,
        {
            "schemaVersion": "1.0",
            "analysisId": "analysis-001",
            "riskExplanation": "ok",
            "rankedActions": [{"actionId": "unknown-1", "priority": 1, "reason": "ok"}],
            "userMessage": "ok",
        },
    )

    result = client.interpret(
        analysis_id="analysis-001",
        payload={
            "calculatedAt": "2026-07-31T12:00:00+09:00",
            "facts": {
                "safeToSpend": 180000,
                "nextRisk": {
                    "type": "PAYMENT_ACCOUNT",
                    "date": "2026-08-03",
                    "shortageAmount": 240000,
                },
                "cashflowSummary": {
                    "lowestBalance": -240000,
                    "lowestBalanceDate": "2026-08-03",
                },
            },
            "actionCandidates": [{"actionId": "transfer-1"}],
        },
    )

    assert result["source"] == "fallback"
    assert result["fallbackReason"] == "unknown_ranked_action"
