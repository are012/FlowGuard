from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest

from flowguard.ai_contract import (
    InvestigationConcludeRequest,
    InvestigationConcludeResponse,
    InvestigationPlanRequest,
    InvestigationPlanResponse,
)
from flowguard.services.investigation_validation import (
    ALLOWED_INVESTIGATION_TOOLS,
    INVESTIGATION_ECHO_FIELDS,
    validate_investigation_conclude_response,
    validate_investigation_plan_response,
)


def envelope() -> dict[str, str]:
    return {
        "schemaVersion": "1.3",
        "contractVersion": "1.3",
        "promptVersion": "invest-1",
        "requestId": "request-a",
        "idempotencyKey": "analysis-a:revision-a:invest-plan",
        "analysisId": "analysis-a",
        "snapshotId": "snapshot-a",
        "snapshotRevision": "revision-a",
        "locale": "ko-KR",
    }


def plan_request_payload() -> dict[str, Any]:
    return {
        **envelope(),
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


def plan_response_payload(
    investigations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        **envelope(),
        "investigations": investigations or [investigation()],
    }


def conclude_request_payload(*, allow_additional: bool = True) -> dict[str, Any]:
    return {
        **plan_request_payload(),
        "observations": [
            {
                **investigation(),
                "result": {"counterparty_id": "counterparty-a"},
            }
        ],
        "allowAdditionalInvestigations": allow_additional,
    }


def conclusion() -> dict[str, Any]:
    return {
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


def conclude_response_payload() -> dict[str, Any]:
    return {**envelope(), "conclusion": conclusion()}


def validate_plan(
    response_payload: dict[str, Any],
    request_payload: dict[str, Any] | None = None,
) -> str | None:
    request = InvestigationPlanRequest.model_validate(request_payload or plan_request_payload())
    response = InvestigationPlanResponse.model_validate(response_payload)
    return validate_investigation_plan_response(request, response)


def validate_conclusion(
    response_payload: dict[str, Any],
    *,
    allow_additional: bool = True,
) -> str | None:
    request = InvestigationConcludeRequest.model_validate(
        conclude_request_payload(allow_additional=allow_additional)
    )
    response = InvestigationConcludeResponse.model_validate(response_payload)
    return validate_investigation_conclude_response(request, response)


def test_valid_plan_and_conclusion_are_accepted() -> None:
    assert validate_plan(plan_response_payload()) is None
    assert validate_conclusion(conclude_response_payload()) is None


@pytest.mark.parametrize("field", INVESTIGATION_ECHO_FIELDS)
def test_all_nine_contract_echo_values_must_match(field: str) -> None:
    request = InvestigationPlanRequest.model_validate(plan_request_payload())
    response = InvestigationPlanResponse.model_validate(plan_response_payload())
    setattr(response, field, "different")

    assert validate_investigation_plan_response(request, response) == f"{field}_mismatch"


def test_only_the_three_approved_lookup_tools_are_allowed() -> None:
    assert {tool.value for tool in ALLOWED_INVESTIGATION_TOOLS} == {
        "get_financial_context",
        "get_counterparty_evidence",
        "query_financial_events",
    }
    request = InvestigationPlanRequest.model_validate(plan_request_payload())
    response = InvestigationPlanResponse.model_validate(plan_response_payload())
    response.investigations[0].tool = cast(Any, "simulate_cashflow")

    assert validate_investigation_plan_response(request, response) == "unsupported_tool"


def test_counterparty_must_be_one_of_the_request_targets() -> None:
    response = plan_response_payload(
        [investigation(params={"counterpartyId": "counterparty-unknown"})]
    )

    assert validate_plan(response) == "unknown_counterparty_id"


@pytest.mark.parametrize(
    "params",
    [
        {"dateFrom": "2026-08-01", "dateTo": "2026-08-20"},
        {"dateFrom": "2026-10-20", "dateTo": "2026-11-01"},
    ],
)
def test_query_dates_must_stay_inside_the_request_window(params: dict[str, str]) -> None:
    response = plan_response_payload([investigation(tool="query_financial_events", params=params)])

    assert validate_plan(response) == "date_out_of_range"


def test_action_type_scope_error_precedes_tool_parameter_error() -> None:
    outside_targets = plan_response_payload(
        [investigation(tool="get_financial_context", params={"actionType": "reserve_funds"})]
    )
    inside_targets = plan_response_payload(
        [investigation(tool="get_financial_context", params={"actionType": "transfer"})]
    )

    assert validate_plan(outside_targets) == "unknown_action_type"
    assert validate_plan(inside_targets) == "invalid_tool_params"


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("get_financial_context", {}),
        ("get_counterparty_evidence", {"counterpartyId": "counterparty-b"}),
        (
            "query_financial_events",
            {"dateFrom": "2026-08-02", "dateTo": "2026-10-31"},
        ),
    ],
)
def test_each_tool_accepts_only_its_meaningful_parameter_shape(
    tool: str,
    params: dict[str, str],
) -> None:
    response = plan_response_payload([investigation(tool=tool, params=params)])

    assert validate_plan(response) is None


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("get_financial_context", {"counterpartyId": "counterparty-a"}),
        ("get_counterparty_evidence", {}),
        (
            "get_counterparty_evidence",
            {
                "counterpartyId": "counterparty-a",
                "dateFrom": "2026-08-02",
                "dateTo": "2026-10-31",
            },
        ),
        ("query_financial_events", {}),
        (
            "query_financial_events",
            {
                "dateFrom": "2026-08-02",
                "dateTo": "2026-10-31",
                "counterpartyId": "counterparty-a",
            },
        ),
    ],
)
def test_irrelevant_or_missing_tool_parameters_are_rejected(
    tool: str,
    params: dict[str, str],
) -> None:
    response = plan_response_payload([investigation(tool=tool, params=params)])

    assert validate_plan(response) == "invalid_tool_params"


def test_same_tool_and_canonical_params_cannot_repeat_in_one_plan() -> None:
    first = investigation(reason="거래처 지급 이력을 확인합니다.")
    repeated = investigation(reason="거래처 입금 변동을 확인합니다.")

    assert validate_plan(plan_response_payload([first, repeated])) == "duplicate_investigation"


def test_additional_investigation_cannot_repeat_an_observation() -> None:
    response = {
        **envelope(),
        "additionalInvestigations": [
            investigation(reason="이미 확인한 거래처 근거를 다시 살핍니다.")
        ],
    }

    assert validate_conclusion(response) == "duplicate_investigation"


@pytest.mark.parametrize(
    ("location", "unsafe_text"),
    [
        ("reason", "거래처 지급이 사흘 늦는지 확인합니다."),
        ("summary", "위험이 삼십 퍼센트 높을 수 있습니다."),
        ("unresolved", "지급 이력이 2건뿐입니다."),
    ],
)
def test_free_text_numeric_claims_are_defensively_rejected(
    location: str,
    unsafe_text: str,
) -> None:
    if location == "reason":
        request = InvestigationPlanRequest.model_validate(plan_request_payload())
        response = InvestigationPlanResponse.model_validate(plan_response_payload())
        response.investigations[0].reason = unsafe_text
        error = validate_investigation_plan_response(request, response)
    else:
        request = InvestigationConcludeRequest.model_validate(conclude_request_payload())
        response = InvestigationConcludeResponse.model_validate(conclude_response_payload())
        assert response.conclusion is not None
        if location == "summary":
            response.conclusion.hypotheses[0].summary = unsafe_text
        else:
            response.conclusion.unresolved[0] = unsafe_text
        error = validate_investigation_conclude_response(request, response)

    assert error == "unsupported_numeric_claim"


def test_conclusion_candidate_priorities_must_be_request_targets() -> None:
    payload = conclude_response_payload()
    payload["conclusion"]["candidatePriorities"] = ["reserve_funds"]

    assert validate_conclusion(payload) == "unknown_action_type"


def test_hypothesis_limit_is_defensively_checked_after_schema_validation() -> None:
    request = InvestigationConcludeRequest.model_validate(conclude_request_payload())
    response = InvestigationConcludeResponse.model_validate(conclude_response_payload())
    assert response.conclusion is not None
    hypothesis = response.conclusion.hypotheses[0]
    response.conclusion.hypotheses = [deepcopy(hypothesis) for _ in range(4)]

    assert validate_investigation_conclude_response(request, response) == "too_many_hypotheses"


@pytest.mark.parametrize("outcome", ["both", "neither"])
def test_conclude_xor_is_defensively_checked_after_schema_validation(outcome: str) -> None:
    request = InvestigationConcludeRequest.model_validate(conclude_request_payload())
    response = InvestigationConcludeResponse.model_validate(conclude_response_payload())
    if outcome == "both":
        response.additionalInvestigations = [
            InvestigationPlanResponse.model_validate(plan_response_payload()).investigations[0]
        ]
    else:
        response.conclusion = None

    assert validate_investigation_conclude_response(request, response) == "invalid_outcome"


def test_final_conclude_request_rejects_additional_investigations() -> None:
    payload = {
        **envelope(),
        "additionalInvestigations": [investigation(params={"counterpartyId": "counterparty-b"})],
    }

    assert (
        validate_conclusion(payload, allow_additional=False)
        == "additional_investigations_not_allowed"
    )


def test_nonfinal_conclude_request_accepts_a_new_additional_investigation() -> None:
    payload = {
        **envelope(),
        "additionalInvestigations": [investigation(params={"counterpartyId": "counterparty-b"})],
    }

    assert validate_conclusion(payload, allow_additional=True) is None
