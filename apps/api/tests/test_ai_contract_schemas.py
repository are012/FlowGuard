from __future__ import annotations

import pytest
from pydantic import ValidationError

from flowguard.api.schemas import AIToBackendResponse, BackendToAIRequest


def envelope() -> dict[str, str]:
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
    }


def request_payload() -> dict[str, object]:
    return {
        **envelope(),
        "calculatedAt": "2026-07-26T21:30:00+09:00",
        "facts": {
            "safeToSpend": 180000,
            "nextRisk": {
                "type": "PAYMENT_ACCOUNT_SHORTAGE",
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
            {"actionId": "transfer-1", "type": "TRANSFER", "amount": 240000, "feasible": True}
        ],
    }


def response_payload() -> dict[str, object]:
    return {
        **envelope(),
        "riskExplanation": "결제계좌 잔액이 빠르게 줄어들고 있습니다.",
        "rankedActions": [
            {
                "actionId": "transfer-1",
                "priority": 1,
                "reason": "결제계좌 부족을 가장 빠르게 해소합니다.",
            }
        ],
        "userMessage": "결제계좌 잔액이 부족해질 수 있습니다.",
    }


def test_backend_to_ai_request_accepts_version_1_1_contract() -> None:
    model = BackendToAIRequest.model_validate(request_payload())

    assert model.analysisId == "analysis-001"
    assert model.snapshotRevision == "rev-7"
    assert model.facts.safeToSpend == 180000
    assert model.actionCandidates[0].actionId == "transfer-1"


def test_ai_to_backend_response_accepts_ranked_actions() -> None:
    model = AIToBackendResponse.model_validate(response_payload())

    assert model.rankedActions[0].actionId == "transfer-1"
    assert model.rankedActions[0].priority == 1


def test_version_1_0_and_missing_correlation_fields_are_rejected() -> None:
    payload = request_payload()
    payload["schemaVersion"] = "1.0"
    payload.pop("requestId")

    with pytest.raises(ValidationError):
        BackendToAIRequest.model_validate(payload)


@pytest.mark.parametrize("duplicate_field", ["actionId", "priority"])
def test_response_rejects_duplicate_rankings(duplicate_field: str) -> None:
    payload = response_payload()
    second = {
        "actionId": "transfer-2",
        "priority": 2,
        "reason": "다른 안전 후보입니다.",
    }
    second[duplicate_field] = payload["rankedActions"][0][duplicate_field]  # type: ignore[index]
    payload["rankedActions"].append(second)  # type: ignore[union-attr]

    with pytest.raises(ValidationError):
        AIToBackendResponse.model_validate(payload)


def test_request_requires_timezone_aware_calculated_at() -> None:
    payload = request_payload()
    payload["calculatedAt"] = "2026-07-26T21:30:00"

    with pytest.raises(ValidationError):
        BackendToAIRequest.model_validate(payload)
