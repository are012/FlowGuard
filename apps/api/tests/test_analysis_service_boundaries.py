from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from flowguard.services.analysis import AnalysisOrchestrator
from flowguard.services.analysis_support import AIInterpretationClient
from flowguard.storage import FlowGuardRepository


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
            {"actionId": "transfer-1", "type": "TRANSFER", "amount": 240000, "feasible": True}
        ],
    }


def response_payload(**changes: Any) -> dict[str, Any]:
    request = request_payload()
    response = {
        key: request[key]
        for key in (
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
    }
    response.update(
        {
            "riskExplanation": "결제계좌 잔액이 부족할 수 있습니다.",
            "rankedActions": [
                {"actionId": "transfer-1", "priority": 1, "reason": "검증된 이체입니다."}
            ],
            "userMessage": "검증된 대응안을 확인해 주세요.",
            **changes,
        }
    )
    return response


def client_for(handler: httpx.MockTransport) -> AIInterpretationClient:
    return AIInterpretationClient(
        base_url="http://ai.test",
        connect_timeout_seconds=0.01,
        read_timeout_seconds=0.01,
        total_timeout_seconds=0.1,
        transport=handler,
    )


def test_ai_request_identifiers_are_stable_for_same_logical_input() -> None:
    orchestrator = AnalysisOrchestrator(FlowGuardRepository("sqlite:///:memory:"))
    report = {
        "created_at": "2026-07-31T12:00:00+09:00",
        "safe_to_spend": {"safe_to_spend": 180_000},
        "risk_metrics": {
            "first_risk_date": "2026-08-03",
            "expected_gap_max": 240_000,
            "shortfall_type": "PAYMENT_ACCOUNT_SHORTAGE",
        },
        "cashflow": {"daily_positions": [{"date": "2026-08-03", "available_balance": -240_000}]},
        "agent": {"gathered_evidence": [], "actionCandidates": []},
    }

    first = orchestrator._build_ai_request_payload(  # noqa: SLF001
        analysis_id="analysis-001",
        snapshot_id="snapshot-007",
        snapshot_revision="rev-7",
        report=report,
    )
    second = orchestrator._build_ai_request_payload(  # noqa: SLF001
        analysis_id="analysis-001",
        snapshot_id="snapshot-007",
        snapshot_revision="rev-7",
        report=report,
    )

    assert first is not None
    assert second is not None
    assert first["idempotencyKey"] == second["idempotencyKey"]
    assert first["requestId"] == second["requestId"]


def test_existing_pending_interpretation_is_not_executed_or_finalized_twice() -> None:
    class MustNotCallAI:
        @staticmethod
        def interpret(_payload: dict[str, Any]) -> None:
            raise AssertionError("the existing interpretation owner must make the call")

    repository = FlowGuardRepository("sqlite:///:memory:")
    snapshot = repository.create_snapshot(
        "user-1",
        as_of=datetime.fromisoformat("2026-07-31T12:00:00+09:00"),
    )
    analysis = repository.create_analysis("user-1", trigger_type="MANUAL")
    for status in (
        "SNAPSHOT_BUILDING",
        "BASELINE_ANALYZING",
        "AGENT_INVESTIGATING",
        "PLAN_EVALUATING",
        "REPORT_BUILDING",
    ):
        analysis = repository.transition_analysis(
            analysis["analysis_id"],
            status,
            message=status,
            snapshot_id=snapshot["snapshot_id"] if status == "SNAPSHOT_BUILDING" else None,
        )
    payload = request_payload()
    payload.update(
        {
            "analysisId": analysis["analysis_id"],
            "snapshotId": snapshot["snapshot_id"],
        }
    )
    repository.create_or_get_interpretation_run(
        analysis_id=payload["analysisId"],
        snapshot_id=payload["snapshotId"],
        snapshot_revision=payload["snapshotRevision"],
        request_id=payload["requestId"],
        idempotency_key=payload["idempotencyKey"],
        contract_version=payload["contractVersion"],
        prompt_version=payload["promptVersion"],
    )
    repository.transition_analysis(
        analysis["analysis_id"],
        "INTERPRETATION_REQUESTING",
        message="requesting",
    )
    orchestrator = AnalysisOrchestrator(
        repository,
        ai_client=MustNotCallAI(),  # type: ignore[arg-type]
    )

    outcome = orchestrator._interpret_analysis(payload)  # noqa: SLF001

    assert outcome["_pending"] is True
    current = repository.get_analysis(analysis["analysis_id"])
    assert current["status"] == "INTERPRETATION_REQUESTING"
    assert current["analysis_status"] == "RUNNING"
    assert current["interpretation_status"] == "RUNNING"


def test_ai_interpretation_client_retries_connection_failure_once() -> None:
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("unavailable", request=request)

    outcome = client_for(httpx.MockTransport(fail)).interpret(request_payload())

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "connection_failed"
    assert outcome.attempt_count == 2
    assert calls == 2


def test_ai_interpretation_client_falls_back_on_invalid_payload_without_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response_payload())

    outcome = client_for(httpx.MockTransport(handler)).interpret({"facts": {"safeToSpend": 180000}})

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "invalid_payload"
    assert outcome.attempt_count == 0
    assert calls == 0


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_retryable_status_is_attempted_exactly_twice(status_code: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code)

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == f"http_{status_code}"
    assert outcome.attempt_count == 2
    assert calls == 2


def test_retry_reuses_identical_contract_identifiers() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=response_payload())

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "SUCCEEDED"
    assert outcome.attempt_count == 2
    assert bodies[0] == bodies[1]


def test_infeasible_action_cannot_be_ranked() -> None:
    payload = request_payload()
    payload["actionCandidates"][0]["feasible"] = False

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload())

    outcome = client_for(httpx.MockTransport(handler)).interpret(payload)

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == "unknown_ranked_action"
    assert outcome.attempt_count == 1


@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        (httpx.Response(400), "http_400"),
        (httpx.Response(200, content=b"not-json"), "invalid_json"),
        (
            httpx.Response(200, json=response_payload(requestId="different")),
            "requestId_mismatch",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(
                    rankedActions=[{"actionId": "unknown-1", "priority": 1, "reason": "알 수 없음"}]
                ),
            ),
            "unknown_ranked_action",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(riskExplanation="제공되지 않은 999,999원 위험을 설명합니다."),
            ),
            "unknown_amount_claim",
        ),
        (
            httpx.Response(
                200,
                json=response_payload(userMessage="2027-01-01에 조치해 주세요."),
            ),
            "unknown_date_claim",
        ),
    ],
)
def test_non_retryable_response_falls_back_without_retry(
    response: httpx.Response,
    error_code: str,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    outcome = client_for(httpx.MockTransport(handler)).interpret(request_payload())

    assert outcome.status == "FALLBACK"
    assert outcome.error_code == error_code
    assert outcome.attempt_count == 1
    assert calls == 1
