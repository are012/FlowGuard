from __future__ import annotations

import json
from urllib import error

import pytest

from flowguard.services import analysis_support
from flowguard.services.analysis_support import AIInterpretationClient
from flowguard.storage import FlowGuardRepository


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


def _valid_payload() -> dict[str, object]:
    return {
        "snapshotRevision": "revision-1",
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
        "actionCandidates": [{"actionId": "transfer-1", "type": "transfer", "amount": 240000}],
    }


def test_ai_interpretation_client_falls_back_when_server_unavailable() -> None:
    client = AIInterpretationClient(base_url="http://127.0.0.1:1", timeout_seconds=0.01)

    result = client.interpret(
        analysis_id="analysis-001",
        payload=_valid_payload(),
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
        payload=_valid_payload(),
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
        payload=_valid_payload(),
    )

    assert result["source"] == "fallback"
    assert result["fallbackReason"] == "unknown_ranked_action"


def test_ai_interpretation_client_retries_retryable_failure_once_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    client = AIInterpretationClient(
        base_url="http://localhost:8001",
        repository=repository,
        max_retries=1,
        retry_backoff_seconds=0,
    )
    calls = {"count": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise error.URLError("temporary")
        return FakeResponse(
            {
                "schemaVersion": "1.0",
                "analysisId": "analysis-001",
                "riskExplanation": "ok",
                "rankedActions": [
                    {"actionId": "transfer-1", "priority": 1, "reason": "best option"}
                ],
                "userMessage": "ok",
            }
        )

    monkeypatch.setattr(analysis_support.request, "urlopen", fake_urlopen)

    result = client.interpret(
        analysis_id="analysis-001",
        user_id="demo-user",
        snapshot_revision="revision-1",
        correlation_id="req-1",
        payload=_valid_payload(),
    )

    assert calls["count"] == 2
    assert result["source"] == "ai"
    assert result["attemptCount"] == 2
    tracked = repository.get_ai_interpretation(result["aiRequestId"])
    assert tracked["status"] == "SUCCEEDED"
    assert tracked["attempt_count"] == 2


def test_ai_interpretation_client_reuses_cached_result_for_same_request_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    client = AIInterpretationClient(
        base_url="http://localhost:8001",
        repository=repository,
    )
    calls = {"count": 0}

    def fake_urlopen(*_args, **_kwargs):
        calls["count"] += 1
        return FakeResponse(
            {
                "schemaVersion": "1.0",
                "analysisId": "analysis-001",
                "riskExplanation": "ok",
                "rankedActions": [],
                "userMessage": "ok",
            }
        )

    monkeypatch.setattr(analysis_support.request, "urlopen", fake_urlopen)
    payload = _valid_payload()

    first = client.interpret(
        analysis_id="analysis-001",
        user_id="demo-user",
        snapshot_revision="revision-1",
        correlation_id="req-1",
        payload=payload,
    )
    second = client.interpret(
        analysis_id="analysis-001",
        user_id="demo-user",
        snapshot_revision="revision-1",
        correlation_id="req-1",
        payload=payload,
    )

    assert calls["count"] == 1
    assert first["aiRequestId"] == second["aiRequestId"]
    assert second["source"] == "ai"
