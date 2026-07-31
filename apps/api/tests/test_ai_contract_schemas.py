from __future__ import annotations

from flowguard.api.schemas import AIToBackendResponse, BackendToAIRequest


def test_backend_to_ai_request_accepts_expected_contract() -> None:
    payload = {
        "schemaVersion": "1.0",
        "analysisId": "analysis-001",
        "snapshotRevision": "revision-7",
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
        "actionCandidates": [],
    }

    model = BackendToAIRequest.model_validate(payload)

    assert model.analysisId == "analysis-001"
    assert model.snapshotRevision == "revision-7"
    assert model.facts.safeToSpend == 180000
    assert model.facts.nextRisk.shortageAmount == 240000


def test_ai_to_backend_response_accepts_ranked_actions() -> None:
    payload = {
        "schemaVersion": "1.0",
        "analysisId": "analysis-001",
        "riskExplanation": "결제계좌 잔액이 빠르게 줄어들고 있습니다.",
        "rankedActions": [
            {
                "actionId": "transfer-1",
                "priority": 1,
                "reason": "결제계좌 부족을 가장 빠르게 해소합니다.",
            }
        ],
        "userMessage": "결제계좌 잔액이 24만 원 부족해질 수 있습니다.",
    }

    model = AIToBackendResponse.model_validate(payload)

    assert model.rankedActions[0].actionId == "transfer-1"
    assert model.rankedActions[0].priority == 1
