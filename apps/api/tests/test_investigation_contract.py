from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from flowguard.ai_contract import (
    InvestigationActionType,
    InvestigationConcludeRequest,
    InvestigationConcludeResponse,
    InvestigationParams,
    InvestigationPlanRequest,
    InvestigationPlanResponse,
    InvestigationShortfallType,
    InvestigationTool,
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


def investigation_envelope() -> dict[str, str]:
    return {
        "schemaVersion": "1.3",
        "contractVersion": "1.3",
        "promptVersion": "invest-1",
        "requestId": "ai-investigation-request-a",
        "idempotencyKey": "analysis-a:revision-a:contract-1.3:invest-1:ko-KR",
        "analysisId": "analysis-a",
        "snapshotId": "snapshot-a",
        "snapshotRevision": "revision-a",
        "locale": "ko-KR",
    }


def investigation() -> dict[str, Any]:
    return {
        "tool": "get_counterparty_evidence",
        "params": {"counterpartyId": "counterparty-a"},
        "reason": "입금 예정 거래처의 지급 이력을 확인합니다.",
    }


def plan_request() -> dict[str, Any]:
    return {
        **investigation_envelope(),
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


def plan_response() -> dict[str, Any]:
    return {**investigation_envelope(), "investigations": [investigation()]}


def conclude_request() -> dict[str, Any]:
    return {
        **plan_request(),
        "allowAdditionalInvestigations": True,
        "observations": [
            {
                **investigation(),
                "result": {
                    "counterparty_id": "counterparty-a",
                    "average_delay_days": 4,
                },
            }
        ],
    }


def conclude_response() -> dict[str, Any]:
    return {
        **investigation_envelope(),
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
        },
    }


def test_investigation_contract_accepts_version_1_3_plan_and_conclusion() -> None:
    request = InvestigationPlanRequest.model_validate(plan_request())
    planned = InvestigationPlanResponse.model_validate(plan_response())
    observed = InvestigationConcludeRequest.model_validate(conclude_request())
    concluded = InvestigationConcludeResponse.model_validate(conclude_response())

    assert request.baseline.shortageAmount == 250_000
    assert planned.investigations[0].tool == InvestigationTool.GET_COUNTERPARTY_EVIDENCE
    assert observed.observations[0].result["average_delay_days"] == 4
    assert concluded.conclusion is not None
    assert concluded.conclusion.candidatePriorities == ["confirm_receivable", "transfer"]


def test_conclude_request_distinguishes_initial_and_final_calls() -> None:
    initial_payload = conclude_request()
    final_payload = conclude_request()
    final_payload["allowAdditionalInvestigations"] = False

    initial = InvestigationConcludeRequest.model_validate(initial_payload)
    final = InvestigationConcludeRequest.model_validate(final_payload)

    assert initial.allowAdditionalInvestigations is True
    assert final.allowAdditionalInvestigations is False


@pytest.mark.parametrize("invalid_value", [None, 0, 1, "true", "false"])
def test_conclude_request_requires_a_strict_boolean_control_flag(invalid_value: object) -> None:
    payload = conclude_request()
    if invalid_value is None:
        payload.pop("allowAdditionalInvestigations")
    else:
        payload["allowAdditionalInvestigations"] = invalid_value

    with pytest.raises(ValidationError):
        InvestigationConcludeRequest.model_validate(payload)


@pytest.mark.parametrize("field", ECHO_FIELDS)
def test_every_contract_echo_identifier_is_required(field: str) -> None:
    payload = plan_response()
    payload.pop(field)

    with pytest.raises(ValidationError):
        InvestigationPlanResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schemaVersion", "1.2"),
        ("contractVersion", "1.2"),
        ("promptVersion", "invest-legacy"),
    ],
)
def test_investigation_contract_rejects_wrong_versions(field: str, value: str) -> None:
    payload = plan_request()
    payload[field] = value

    with pytest.raises(ValidationError):
        InvestigationPlanRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["baseline", "targets"])
def test_plan_request_rejects_missing_required_payload(field: str) -> None:
    payload = plan_request()
    payload.pop(field)

    with pytest.raises(ValidationError):
        InvestigationPlanRequest.model_validate(payload)


def test_plan_response_requires_at_least_one_investigation() -> None:
    payload = plan_response()
    payload["investigations"] = []

    with pytest.raises(ValidationError):
        InvestigationPlanResponse.model_validate(payload)


def test_only_three_lookup_tools_are_in_the_contract() -> None:
    assert {tool.value for tool in InvestigationTool} == {
        "get_financial_context",
        "get_counterparty_evidence",
        "query_financial_events",
    }

    payload = plan_response()
    payload["investigations"][0]["tool"] = "simulate_cashflow"
    with pytest.raises(ValidationError):
        InvestigationPlanResponse.model_validate(payload)


def test_investigation_action_types_match_the_domain_catalog() -> None:
    assert {action_type.value for action_type in InvestigationActionType} == {
        "transfer",
        "reserve_funds",
        "adjust_discretionary_budget",
        "pause_savings",
        "shift_payment_date",
        "delay_purchase",
        "add_installment",
        "confirm_receivable",
    }


def test_investigation_shortfall_types_match_the_domain_catalog() -> None:
    assert {shortfall_type.value for shortfall_type in InvestigationShortfallType} == {
        "PAYMENT_ACCOUNT",
        "TOTAL_LIQUIDITY",
    }

    payload = plan_request()
    payload["baseline"]["type"] = "PAYMENT_ACCOUNT_SHORTAGE"
    with pytest.raises(ValidationError):
        InvestigationPlanRequest.model_validate(payload)


@pytest.mark.parametrize("location", ["target", "params", "priorities"])
def test_unapproved_action_type_is_rejected(location: str) -> None:
    if location == "target":
        payload = plan_request()
        payload["targets"]["actionTypes"] = ["withdraw_cash"]
        model = InvestigationPlanRequest
    elif location == "params":
        payload = plan_response()
        payload["investigations"][0]["params"]["actionType"] = "withdraw_cash"
        model = InvestigationPlanResponse
    else:
        payload = conclude_response()
        payload["conclusion"]["candidatePriorities"] = ["withdraw_cash"]
        model = InvestigationConcludeResponse

    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_tool_params_have_only_the_four_approved_fields() -> None:
    assert set(InvestigationParams.model_fields) == {
        "counterpartyId",
        "dateFrom",
        "dateTo",
        "actionType",
    }

    payload = plan_response()
    payload["investigations"][0]["params"].update(
        {
            "amount": 250_000,
            "date": "2026-08-25",
            "riskGrade": "HIGH",
        }
    )
    with pytest.raises(ValidationError):
        InvestigationPlanResponse.model_validate(payload)


def test_output_contract_has_no_financial_claim_fields_outside_tool_params() -> None:
    def property_paths(schema: dict[str, Any], prefix: str = "") -> set[str]:
        paths: set[str] = set()
        for name, child in schema.get("properties", {}).items():
            path = f"{prefix}.{name}" if prefix else name
            paths.add(path)
            if isinstance(child, dict):
                paths.update(property_paths(child, path))
        for name, child in schema.get("$defs", {}).items():
            if isinstance(child, dict):
                paths.update(property_paths(child, name))
        return paths

    response_paths = property_paths(InvestigationConcludeResponse.model_json_schema())
    forbidden_names = {"amount", "date", "riskGrade", "riskLevel", "probability"}

    assert not any(path.rsplit(".", 1)[-1] in forbidden_names for path in response_paths)
    assert any(path.endswith("InvestigationParams.dateFrom") for path in response_paths)
    assert any(path.endswith("InvestigationParams.dateTo") for path in response_paths)


@pytest.mark.parametrize(
    ("location", "unsafe_text"),
    [
        ("reason", "거래처가 사흘 늦을 가능성을 확인합니다."),
        ("reason", "결제 예정일은 2026-08-25입니다."),
        ("summary", "부족액이 이십만원에 이를 수 있습니다."),
        ("unresolved", "지급 이력이 2건뿐입니다."),
    ],
)
def test_response_free_text_rejects_numeric_expressions(
    location: str,
    unsafe_text: str,
) -> None:
    if location == "reason":
        payload = plan_response()
        payload["investigations"][0]["reason"] = unsafe_text
        model = InvestigationPlanResponse
    else:
        payload = conclude_response()
        conclusion = payload["conclusion"]
        if location == "summary":
            conclusion["hypotheses"][0]["summary"] = unsafe_text
        else:
            conclusion["unresolved"] = [unsafe_text]
        model = InvestigationConcludeResponse

    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("outcomes", [(), ("additionalInvestigations", "conclusion")])
def test_conclude_response_requires_exactly_one_outcome(outcomes: tuple[str, ...]) -> None:
    payload = investigation_envelope()
    if "additionalInvestigations" in outcomes:
        payload["additionalInvestigations"] = [investigation()]
    if "conclusion" in outcomes:
        payload["conclusion"] = conclude_response()["conclusion"]

    with pytest.raises(ValidationError):
        InvestigationConcludeResponse.model_validate(payload)


def test_conclude_response_limits_additional_investigations_to_two() -> None:
    payload: dict[str, Any] = {
        **investigation_envelope(),
        "additionalInvestigations": [deepcopy(investigation()) for _ in range(3)],
    }

    with pytest.raises(ValidationError):
        InvestigationConcludeResponse.model_validate(payload)


def test_conclusion_limits_hypotheses_to_three() -> None:
    payload = conclude_response()
    hypothesis = payload["conclusion"]["hypotheses"][0]
    payload["conclusion"]["hypotheses"] = [
        {**deepcopy(hypothesis), "type": f"TYPE-{index}", "priority": index}
        for index in range(1, 5)
    ]

    with pytest.raises(ValidationError):
        InvestigationConcludeResponse.model_validate(payload)


def test_event_window_rejects_reversed_dates() -> None:
    payload = plan_request()
    payload["targets"]["eventWindow"] = {
        "dateFrom": "2026-10-31",
        "dateTo": "2026-08-02",
    }

    with pytest.raises(ValidationError):
        InvestigationPlanRequest.model_validate(payload)


@pytest.mark.parametrize(
    "params",
    [
        {"dateFrom": "2026-08-02"},
        {"dateTo": "2026-10-31"},
        {"dateFrom": "2026-10-31", "dateTo": "2026-08-02"},
    ],
)
def test_tool_params_require_a_complete_ordered_date_range(params: dict[str, str]) -> None:
    payload = plan_response()
    payload["investigations"][0]["params"] = params

    with pytest.raises(ValidationError):
        InvestigationPlanResponse.model_validate(payload)


def test_tool_params_accept_a_complete_ordered_date_range() -> None:
    payload = plan_response()
    payload["investigations"][0]["params"] = {
        "dateFrom": "2026-08-02",
        "dateTo": "2026-10-31",
        "actionType": "transfer",
    }

    model = InvestigationPlanResponse.model_validate(payload)

    assert model.investigations[0].params.dateFrom.isoformat() == "2026-08-02"
