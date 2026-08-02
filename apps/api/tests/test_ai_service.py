from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flowguard.ai_service import create_app


def request_payload() -> dict[str, Any]:
    return {
        "schemaVersion": "1.1",
        "contractVersion": "1.1",
        "promptVersion": "3",
        "requestId": "ai-request-001",
        "idempotencyKey": "analysis-001:rev-7:contract-1.1:prompt-3:ko-KR",
        "analysisId": "analysis-001",
        "snapshotId": "snapshot-007",
        "snapshotRevision": "rev-7",
        "locale": "ko-KR",
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
        "evidence": [],
        "actionCandidates": [
            {"actionId": "transfer-1", "type": "TRANSFER", "amount": 240000, "feasible": True},
            {"actionId": "blocked-1", "type": "TRANSFER", "amount": 240000, "feasible": False},
        ],
    }


class FakeResponses:
    def __init__(self, action_id: str = "transfer-1") -> None:
        self.action_id = action_id
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed={
                "riskExplanation": "검증된 위험 정보를 설명합니다.",
                "rankedActions": [
                    {"actionId": self.action_id, "priority": 1, "reason": "안전 검증 완료"}
                ],
                "userMessage": "검증된 대응안을 확인해 주세요.",
            }
        )


class FakeOpenAI:
    def __init__(self, action_id: str = "transfer-1") -> None:
        self.responses = FakeResponses(action_id)


def test_ai_service_echoes_contract_and_uses_no_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FakeOpenAI()
    caplog.set_level(logging.INFO, logger="flowguard")
    with TestClient(create_app(openai_client=fake, model="test-model")) as client:
        response = client.post("/interpret", json=request_payload())

    assert response.status_code == 200
    payload = response.json()
    assert payload["requestId"] == "ai-request-001"
    assert payload["snapshotRevision"] == "rev-7"
    assert payload["rankedActions"][0]["actionId"] == "transfer-1"
    call = fake.responses.calls[0]
    assert call["model"] == "test-model"
    assert call["store"] is False
    assert "tools" not in call
    sent_candidates = json.loads(call["input"])["actionCandidates"]
    assert [item["actionId"] for item in sent_candidates] == ["transfer-1"]
    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "flowguard.ai_service"
    ]
    assert events[-1]["event"] == "ai_service_interpretation_finished"
    assert events[-1]["ai_request_id"] == "ai-request-001"


def test_ai_service_reuses_success_for_same_idempotency_key() -> None:
    fake = FakeOpenAI()
    with TestClient(create_app(openai_client=fake)) as client:
        first = client.post("/interpret", json=request_payload())
        second = client.post("/interpret", json=request_payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert len(fake.responses.calls) == 1


def test_ai_service_rejects_changed_payload_for_same_idempotency_key() -> None:
    fake = FakeOpenAI()
    changed = request_payload()
    changed["locale"] = "en-US"
    with TestClient(create_app(openai_client=fake)) as client:
        assert client.post("/interpret", json=request_payload()).status_code == 200
        conflict = client.post("/interpret", json=changed)

    assert conflict.status_code == 409
    assert len(fake.responses.calls) == 1


def test_ai_service_rejects_action_that_backend_did_not_approve() -> None:
    fake = FakeOpenAI(action_id="blocked-1")
    with TestClient(create_app(openai_client=fake)) as client:
        response = client.post("/interpret", json=request_payload())

    assert response.status_code == 502


def test_ai_service_requires_openai_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app(openai_client=None)) as client:
        response = client.post("/interpret", json=request_payload())

    assert response.status_code == 503
