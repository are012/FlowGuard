from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from flowguard.ai_contract import Investigation
from flowguard.services import investigation_loop as loop_module
from flowguard.services.investigation_loop import (
    AIInvestigationCallOutcome,
    AIInvestigationClient,
    InvestigationLoop,
)

ECHO_FIELDS = (
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


def plan_request() -> dict[str, Any]:
    return {
        "schemaVersion": "1.3",
        "contractVersion": "1.3",
        "promptVersion": "invest-1",
        "requestId": "investigation-request-a",
        "idempotencyKey": "analysis-a:revision-a:investigation-plan",
        "analysisId": "analysis-a",
        "snapshotId": "snapshot-a",
        "snapshotRevision": "revision-a",
        "locale": "ko-KR",
        "baseline": {
            "type": "PAYMENT_ACCOUNT",
            "date": "2026-08-25",
            "shortageAmount": 250_000,
        },
        "targets": {
            "counterpartyIds": ["counterparty-a", "counterparty-b"],
            "eventWindow": {"dateFrom": "2026-08-02", "dateTo": "2026-10-31"},
            "actionTypes": ["transfer", "confirm_receivable"],
        },
    }


def investigation(
    *,
    tool: str = "get_counterparty_evidence",
    params: dict[str, Any] | None = None,
    reason: str = "입금 예정 거래처의 지급 이력을 확인합니다.",
) -> dict[str, Any]:
    return {
        "tool": tool,
        "params": {"counterpartyId": "counterparty-a"} if params is None else params,
        "reason": reason,
    }


def conclusion() -> dict[str, Any]:
    return {
        "conclusion": {
            "hypotheses": [
                {
                    "type": "COUNTERPARTY_DELAY",
                    "summary": "거래처 지급 변동이 유동성 위험을 키울 수 있습니다.",
                    "priority": 1,
                }
            ],
            "candidatePriorities": ["confirm_receivable", "transfer"],
            "unresolved": ["최근 지급 이력의 대표성을 확인하지 못했습니다."],
        }
    }


def counterparty_result(counterparty_id: str = "counterparty-a") -> dict[str, Any]:
    return {
        "counterparty_id": counterparty_id,
        "payment_history_count": 4,
        "on_time_rate": 0.75,
        "average_delay_days": 1.5,
        "median_delay_days": 1,
        "maximum_delay_days": 3,
        "recent_trend": "STABLE",
        "data_confidence": 0.8,
        "secret_account_number": "never-project-this",
    }


def financial_context_result() -> dict[str, Any]:
    return {
        "accounts": [{"account_id": "secret-account", "balance": 900_000}],
        "protected_funds": [{"amount": 100_000, "reason": "secret-reason"}],
        "scheduled_events": [
            {"amount": 200_000, "is_essential": True, "description": "secret-event"}
        ],
    }


def event_result() -> dict[str, Any]:
    return {
        "events": [
            {
                "event_id": "event-a",
                "event_type": "RECEIVABLE",
                "expected_date": "2026-08-20",
                "amount": 300_000,
                "is_essential": True,
                "description": "never-project-this",
            }
        ]
    }


def raw_result(request: Investigation) -> dict[str, Any]:
    if request.tool.value == "get_counterparty_evidence":
        return counterparty_result(request.params.counterpartyId or "counterparty-a")
    if request.tool.value == "get_financial_context":
        return financial_context_result()
    if request.tool.value == "query_financial_events":
        return event_result()
    raise AssertionError("unexpected tool")


def successful_response(
    request: Mapping[str, Any],
    content: Mapping[str, Any],
) -> AIInvestigationCallOutcome:
    response = {field: request[field] for field in ECHO_FIELDS}
    response.update(deepcopy(dict(content)))
    return AIInvestigationCallOutcome(
        status="SUCCEEDED",
        response_payload=response,
        attempt_count=1,
        latency_ms=25,
    )


ScriptedResult = Mapping[str, Any] | AIInvestigationCallOutcome


@dataclass
class ScriptedClient:
    plan_result: ScriptedResult
    conclude_results: list[ScriptedResult] = field(default_factory=list)
    advance_clock: Any | None = None
    advance_seconds: list[float] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any], float]] = field(default_factory=list)

    def _advance(self) -> None:
        if self.advance_clock is not None and self.advance_seconds:
            self.advance_clock(self.advance_seconds.pop(0))

    def plan(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome:
        copied = deepcopy(dict(payload))
        self.calls.append(("plan", copied, timeout_seconds))
        self._advance()
        if isinstance(self.plan_result, AIInvestigationCallOutcome):
            return self.plan_result
        return successful_response(copied, self.plan_result)

    def conclude(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> AIInvestigationCallOutcome:
        copied = deepcopy(dict(payload))
        self.calls.append(("conclude", copied, timeout_seconds))
        self._advance()
        scripted = self.conclude_results.pop(0)
        if isinstance(scripted, AIInvestigationCallOutcome):
            return scripted
        return successful_response(copied, scripted)


@dataclass
class ManualClock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_bounds_are_fixed_for_the_synchronous_two_phase_loop() -> None:
    assert loop_module.MAX_PHASES == 2
    assert loop_module.MAX_TOOL_CALLS == 6
    assert loop_module.PHASE_TIMEOUT == 5.0
    assert loop_module.TOTAL_BUDGET == 8.0


def test_initial_observation_can_produce_a_successful_conclusion() -> None:
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[conclusion()],
    )

    outcome = InvestigationLoop(client, raw_result).run(plan_request())

    assert outcome.status == "SUCCEEDED"
    assert outcome.error_code is None
    assert outcome.tool_call_count == 1
    assert outcome.additional_investigation_requested is False
    assert outcome.priorities == ("confirm_receivable", "transfer")
    assert outcome.hypotheses[0]["type"] == "COUNTERPARTY_DELAY"
    assert [turn.turn_sequence for turn in outcome.turns] == [1, 2]
    assert [turn.phase for turn in outcome.turns] == [1, 2]
    assert [turn.endpoint for turn in outcome.turns] == [
        "/investigate/plan",
        "/investigate/conclude",
    ]
    assert "secret_account_number" not in outcome.observations[0]["result"]
    assert "never-project-this" not in repr(outcome)


def test_additional_investigation_is_executed_before_forced_conclusion() -> None:
    client = ScriptedClient(
        plan_result={
            "investigations": [
                investigation(
                    tool="get_financial_context", params={}, reason="자금 구성을 확인합니다."
                )
            ]
        },
        conclude_results=[
            {
                "additionalInvestigations": [
                    investigation(
                        tool="query_financial_events",
                        params={"dateFrom": "2026-08-02", "dateTo": "2026-10-31"},
                        reason="예정된 필수 거래의 구성을 확인합니다.",
                    )
                ]
            },
            conclusion(),
        ],
    )

    outcome = InvestigationLoop(client, raw_result).run(plan_request())

    assert outcome.status == "SUCCEEDED"
    assert outcome.additional_investigation_requested is True
    assert outcome.tool_call_count == 2
    assert len(outcome.observations) == 2
    assert [turn.turn_sequence for turn in outcome.turns] == [1, 2, 3]
    assert [turn.phase for turn in outcome.turns] == [1, 2, 2]
    first_conclude = client.calls[1][1]
    final_conclude = client.calls[2][1]
    assert first_conclude["allowAdditionalInvestigations"] is True
    assert final_conclude["allowAdditionalInvestigations"] is False
    assert first_conclude["idempotencyKey"] != final_conclude["idempotencyKey"]
    assert len(final_conclude["observations"]) == 2


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        (
            AIInvestigationCallOutcome("FAILED", {}, 1, 10, "connection_failed"),
            "FAILED",
        ),
        (
            AIInvestigationCallOutcome("REJECTED", {}, 1, 10, "invalid_investigation_response"),
            "REJECTED",
        ),
    ],
)
def test_first_turn_failure_has_no_observations_or_tool_calls(
    failure: AIInvestigationCallOutcome,
    expected_status: str,
) -> None:
    client = ScriptedClient(plan_result=failure)

    outcome = InvestigationLoop(client, raw_result).run(plan_request())

    assert outcome.status == expected_status
    assert outcome.error_code == failure.error_code
    assert outcome.observations == ()
    assert outcome.tool_call_count == 0
    assert len(outcome.turns) == 1


def test_second_turn_rejection_preserves_only_projected_observations() -> None:
    rejection = AIInvestigationCallOutcome(
        "REJECTED",
        {},
        1,
        18,
        "invalid_investigation_response",
    )
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[rejection],
    )

    outcome = InvestigationLoop(client, raw_result).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "invalid_investigation_response"
    assert len(outcome.observations) == 1
    assert outcome.turns[-1].status == "REJECTED"
    assert "secret_account_number" not in repr(outcome)


def test_duplicate_additional_investigation_is_rejected_by_the_loop() -> None:
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[
            {
                "additionalInvestigations": [
                    investigation(reason="같은 거래처의 지급 변동을 다시 확인합니다.")
                ]
            }
        ],
    )

    outcome = InvestigationLoop(client, raw_result).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "duplicate_investigation"
    assert outcome.tool_call_count == 1
    assert len(outcome.observations) == 1
    assert outcome.turns[-1].status == "REJECTED"


def test_backend_tool_limit_stops_additional_batch_and_keeps_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(loop_module, "MAX_TOOL_CALLS", 1)
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[
            {
                "additionalInvestigations": [
                    investigation(
                        params={"counterpartyId": "counterparty-b"},
                        reason="다른 거래처의 지급 이력을 확인합니다.",
                    )
                ]
            }
        ],
    )

    outcome = InvestigationLoop(client, raw_result).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "tool_call_limit_exceeded"
    assert outcome.tool_call_count == 1
    assert len(outcome.observations) == 1
    assert outcome.turns[-1].error_code == "tool_call_limit_exceeded"


def test_projection_failure_after_an_observation_returns_partial_without_raw_data() -> None:
    requests = [
        investigation(),
        investigation(
            tool="get_financial_context",
            params={},
            reason="자금 구성을 이어서 확인합니다.",
        ),
    ]
    client = ScriptedClient(plan_result={"investigations": requests})

    def execute(request: Investigation) -> dict[str, Any]:
        if request.tool.value == "get_financial_context":
            return {"private_raw_value": "must-not-survive"}
        return raw_result(request)

    outcome = InvestigationLoop(client, execute).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "malformed_tool_output"
    assert outcome.tool_call_count == 2
    assert len(outcome.observations) == 1
    assert "private_raw_value" not in repr(outcome)


def test_final_additional_request_is_rejected_and_preserves_both_batches() -> None:
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[
            {
                "additionalInvestigations": [
                    investigation(
                        params={"counterpartyId": "counterparty-b"},
                        reason="다른 거래처의 지급 이력을 확인합니다.",
                    )
                ]
            },
            {
                "additionalInvestigations": [
                    investigation(
                        tool="get_financial_context",
                        params={},
                        reason="자금 구성도 추가로 확인합니다.",
                    )
                ]
            },
        ],
    )

    outcome = InvestigationLoop(client, raw_result).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "additional_investigations_not_allowed"
    assert outcome.tool_call_count == 2
    assert len(outcome.observations) == 2
    assert outcome.turns[-1].turn_sequence == 3
    assert outcome.turns[-1].status == "REJECTED"


def test_each_ai_call_receives_the_smaller_phase_or_global_deadline() -> None:
    clock = ManualClock()
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[conclusion()],
        advance_clock=clock.advance,
        advance_seconds=[4.0, 0.0],
    )

    def execute(request: Investigation) -> dict[str, Any]:
        clock.advance(1.0)
        return raw_result(request)

    outcome = InvestigationLoop(client, execute, clock=clock).run(plan_request())

    assert outcome.status == "SUCCEEDED"
    assert client.calls[0][2] == 5.0
    assert client.calls[1][2] == 3.0
    assert outcome.total_latency_ms == 5_000


def test_tool_returning_after_the_global_deadline_blocks_the_next_ai_call() -> None:
    clock = ManualClock()
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[conclusion()],
    )

    def slow_execute(request: Investigation) -> dict[str, Any]:
        clock.advance(8.0)
        return raw_result(request)

    outcome = InvestigationLoop(client, slow_execute, clock=clock).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "total_budget_exhausted"
    assert outcome.tool_call_count == 1
    assert len(outcome.observations) == 1
    assert [call[0] for call in client.calls] == ["plan"]


def test_successful_plan_returned_after_the_deadline_is_failed() -> None:
    clock = ManualClock()
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        advance_clock=clock.advance,
        advance_seconds=[8.0],
    )

    outcome = InvestigationLoop(client, raw_result, clock=clock).run(plan_request())

    assert outcome.status == "FAILED"
    assert outcome.error_code == "total_budget_exhausted"
    assert outcome.tool_call_count == 0
    assert outcome.turns[0].status == "FAILED"
    assert outcome.turns[0].error_code == "total_budget_exhausted"


def test_successful_first_conclusion_returned_after_the_deadline_is_partial() -> None:
    clock = ManualClock()
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[conclusion()],
        advance_clock=clock.advance,
        advance_seconds=[0.0, 8.0],
    )

    outcome = InvestigationLoop(client, raw_result, clock=clock).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "total_budget_exhausted"
    assert len(outcome.observations) == 1
    assert outcome.turns[-1].status == "FAILED"


def test_successful_final_conclusion_returned_after_the_deadline_is_partial() -> None:
    clock = ManualClock()
    client = ScriptedClient(
        plan_result={"investigations": [investigation()]},
        conclude_results=[
            {
                "additionalInvestigations": [
                    investigation(
                        params={"counterpartyId": "counterparty-b"},
                        reason="다른 거래처의 지급 이력을 확인합니다.",
                    )
                ]
            },
            conclusion(),
        ],
        advance_clock=clock.advance,
        advance_seconds=[0.0, 0.0, 8.0],
    )

    outcome = InvestigationLoop(client, raw_result, clock=clock).run(plan_request())

    assert outcome.status == "PARTIAL"
    assert outcome.error_code == "total_budget_exhausted"
    assert len(outcome.observations) == 2
    assert outcome.turns[-1].turn_sequence == 3
    assert outcome.turns[-1].status == "FAILED"


def test_http_client_prevalidates_requests_without_making_a_call() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500)

    client = AIInvestigationClient(
        base_url="http://ai.test",
        transport=httpx.MockTransport(handler),
    )
    invalid = plan_request()
    invalid.pop("analysisId")

    outcome = client.plan(invalid, timeout_seconds=2.0)

    assert outcome.status == "REJECTED"
    assert outcome.error_code == "invalid_payload"
    assert outcome.attempt_count == 0
    assert call_count == 0


def test_http_client_treats_the_service_contract_422_as_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": {"code": "invalid_investigation_response"}},
        )

    client = AIInvestigationClient(
        base_url="http://ai.test",
        transport=httpx.MockTransport(handler),
    )

    outcome = client.plan(plan_request(), timeout_seconds=2.0)

    assert outcome.status == "REJECTED"
    assert outcome.error_code == "invalid_investigation_response"
    assert outcome.attempt_count == 1


def test_http_client_applies_semantic_validation_to_a_success_response() -> None:
    request_payload = plan_request()

    def handler(request: httpx.Request) -> httpx.Response:
        response = {field: request_payload[field] for field in ECHO_FIELDS}
        response["analysisId"] = "wrong-analysis"
        response["investigations"] = [investigation()]
        return httpx.Response(200, json=response)

    client = AIInvestigationClient(
        base_url="http://ai.test",
        transport=httpx.MockTransport(handler),
    )

    outcome = client.plan(request_payload, timeout_seconds=2.0)

    assert outcome.status == "REJECTED"
    assert outcome.error_code == "analysisId_mismatch"
    assert outcome.response_payload["analysisId"] == "wrong-analysis"


def test_http_client_applies_the_caller_timeout_to_httpx() -> None:
    request_payload = plan_request()
    timeout_extensions: list[dict[str, float]] = []
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        timeout_extensions.append(request.extensions["timeout"])
        return httpx.Response(
            200,
            json=successful_response(
                request_payload,
                {"investigations": [investigation()]},
            ).response_payload,
        )

    client = AIInvestigationClient(
        base_url="http://ai.test",
        connect_timeout_seconds=3.0,
        read_timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )

    outcome = client.plan(request_payload, timeout_seconds=1.25)

    assert outcome.status == "SUCCEEDED"
    assert timeout_extensions == [{"connect": 1.25, "read": 1.25, "write": 1.25, "pool": 1.25}]


def test_http_client_retries_with_the_same_deadline_and_reduced_timeout() -> None:
    request_payload = plan_request()
    clock = ManualClock()
    timeout_extensions: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout_extensions.append(request.extensions["timeout"])
        if len(timeout_extensions) == 1:
            clock.advance(0.75)
            return httpx.Response(503)
        return httpx.Response(
            200,
            json=successful_response(
                request_payload,
                {"investigations": [investigation()]},
            ).response_payload,
        )

    client = AIInvestigationClient(
        base_url="http://ai.test",
        connect_timeout_seconds=3.0,
        read_timeout_seconds=5.0,
        max_retries=1,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )

    outcome = client.plan(request_payload, timeout_seconds=2.0)

    assert outcome.status == "SUCCEEDED"
    assert outcome.attempt_count == 2
    assert timeout_extensions == [
        {"connect": 2.0, "read": 2.0, "write": 2.0, "pool": 2.0},
        {"connect": 1.25, "read": 1.25, "write": 1.25, "pool": 1.25},
    ]


def test_http_client_does_not_retry_after_its_deadline() -> None:
    clock = ManualClock()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        clock.advance(2.0)
        return httpx.Response(503)

    client = AIInvestigationClient(
        base_url="http://ai.test",
        max_retries=1,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )

    outcome = client.plan(plan_request(), timeout_seconds=2.0)

    assert outcome.status == "FAILED"
    assert outcome.error_code == "total_timeout"
    assert outcome.attempt_count == 1
    assert call_count == 1


def test_http_client_rejects_a_success_returned_after_its_phase_deadline() -> None:
    request_payload = plan_request()
    clock = ManualClock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.advance(3.0)
        return httpx.Response(
            200,
            json=successful_response(
                request_payload,
                {"investigations": [investigation()]},
            ).response_payload,
        )

    client = AIInvestigationClient(
        base_url="http://ai.test",
        transport=httpx.MockTransport(handler),
        clock=clock,
    )

    outcome = client.plan(request_payload, timeout_seconds=2.0)

    assert outcome.status == "FAILED"
    assert outcome.error_code == "total_timeout"
    assert outcome.attempt_count == 1


def test_http_client_reports_total_timeout_when_transport_fails_after_deadline() -> None:
    clock = ManualClock()
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        clock.advance(2.0)
        raise httpx.ConnectError("late connection failure", request=request)

    client = AIInvestigationClient(
        base_url="http://ai.test",
        max_retries=1,
        transport=httpx.MockTransport(handler),
        clock=clock,
    )

    outcome = client.plan(plan_request(), timeout_seconds=2.0)

    assert outcome.status == "FAILED"
    assert outcome.error_code == "total_timeout"
    assert outcome.attempt_count == 1
    assert call_count == 1
