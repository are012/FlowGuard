from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from flowguard.ai_contract import InvestigationTool
from flowguard.services.observation_projection import (
    ObservationProjectionError,
    project_observation,
)


def counterparty_result() -> dict[str, Any]:
    return {
        "counterparty_id": "counterparty-a",
        "payment_history_count": 4,
        "on_time_rate": 0.75,
        "average_delay_days": 2.5,
        "median_delay_days": 2.0,
        "maximum_delay_days": 6,
        "recent_trend": "STABLE",
        "data_confidence": 0.8,
        "counterparty_name": "투영되면 안 되는 거래처명",
        "unexpected_future_field": {"account_number": "secret"},
    }


def event_query_result() -> dict[str, Any]:
    return {
        "snapshot_id": "snapshot-a",
        "events": [
            {
                "event_id": "event-a",
                "event_type": "RENT",
                "expected_date": "2026-08-25",
                "amount": 320_000,
                "is_essential": True,
                "description": "원천 설명",
                "counterparty_name": "민감 거래처명",
                "counterparty_id": "counterparty-a",
                "account_id": "account-a",
                "card_id": "card-a",
                "source": "CSV",
                "source_reference_id": "source-a",
            }
        ],
    }


def financial_context_result() -> dict[str, Any]:
    return {
        "snapshot_id": "snapshot-a",
        "as_of": "2026-08-02T09:00:00+09:00",
        "accounts": [
            {"account_id": "account-a", "name": "결제계좌", "balance": 800_000},
            {"account_id": "account-b", "name": "비상계좌", "balance": 400_000},
        ],
        "cards": [{"card_id": "card-a", "name": "민감 카드명"}],
        "protected_funds": [
            {"protected_fund_id": "fund-a", "amount": 100_000},
            {"protected_fund_id": "fund-b", "amount": 250_000},
        ],
        "scheduled_events": [
            {"event_id": "event-a", "amount": 300_000, "is_essential": True},
            {"event_id": "event-b", "amount": 90_000, "is_essential": False},
            {"event_id": "event-c", "amount": 40_000, "is_essential": True},
        ],
    }


def test_counterparty_projection_uses_an_exact_statistical_whitelist() -> None:
    raw = counterparty_result()

    projected = project_observation(InvestigationTool.GET_COUNTERPARTY_EVIDENCE, raw)

    assert projected == {
        "counterparty_id": "counterparty-a",
        "payment_history_count": 4,
        "on_time_rate": 0.75,
        "average_delay_days": 2.5,
        "median_delay_days": 2.0,
        "maximum_delay_days": 6,
        "recent_trend": "STABLE",
        "data_confidence": 0.8,
    }
    assert "counterparty_name" not in projected
    assert "unexpected_future_field" not in projected


def test_event_projection_renames_only_the_five_approved_fields() -> None:
    raw = event_query_result()

    projected = project_observation(InvestigationTool.QUERY_FINANCIAL_EVENTS, raw)

    assert projected == {
        "events": [
            {
                "event_id": "event-a",
                "type": "RENT",
                "date": "2026-08-25",
                "amount": 320_000,
                "is_essential": True,
            }
        ]
    }
    assert "snapshot_id" not in projected
    assert set(projected["events"][0]) == {
        "event_id",
        "type",
        "date",
        "amount",
        "is_essential",
    }


def test_financial_context_projection_returns_aggregates_only() -> None:
    raw = financial_context_result()

    projected = project_observation(InvestigationTool.GET_FINANCIAL_CONTEXT, raw)

    assert projected == {
        "account_count": 2,
        "protected_funds_total": 350_000,
        "essential_scheduled_events_total": 340_000,
    }
    assert not ({"accounts", "cards", "snapshot_id", "as_of"} & projected.keys())


@pytest.mark.parametrize(
    "tool",
    [
        "simulate_cashflow",
        "calculate_safe_to_spend",
        "evaluate_action_plan",
        "validate_financial_policy",
        "future_tool",
    ],
)
def test_undefined_tools_are_rejected_by_default(tool: str) -> None:
    with pytest.raises(ObservationProjectionError) as caught:
        project_observation(tool, {})

    assert caught.value.code == "unsupported_tool"


@pytest.mark.parametrize(
    ("tool", "raw_result"),
    [
        ("get_counterparty_evidence", []),
        ("get_counterparty_evidence", {"counterparty_id": "counterparty-a"}),
        ("query_financial_events", {"events": {}}),
        ("query_financial_events", {"events": ["not-an-event"]}),
        ("get_financial_context", {"accounts": [], "protected_funds": []}),
        (
            "get_financial_context",
            {"accounts": ["not-an-account"], "protected_funds": [], "scheduled_events": []},
        ),
    ],
)
def test_missing_fields_and_wrong_containers_fail_closed(tool: str, raw_result: Any) -> None:
    with pytest.raises(ObservationProjectionError) as caught:
        project_observation(tool, raw_result)

    assert caught.value.code == "malformed_tool_output"


@pytest.mark.parametrize(
    ("tool", "payload_path"),
    [
        ("get_financial_context", "protected_funds"),
        ("query_financial_events", "events"),
    ],
)
def test_boolean_amounts_are_never_counted_as_integers(tool: str, payload_path: str) -> None:
    if tool == "get_financial_context":
        raw = financial_context_result()
        raw[payload_path][0]["amount"] = True
    else:
        raw = event_query_result()
        raw[payload_path][0]["amount"] = True

    with pytest.raises(ObservationProjectionError) as caught:
        project_observation(tool, raw)

    assert caught.value.code == "malformed_tool_output"


@pytest.mark.parametrize(
    ("tool", "raw"),
    [
        ("get_counterparty_evidence", counterparty_result()),
        ("query_financial_events", event_query_result()),
        ("get_financial_context", financial_context_result()),
    ],
)
def test_projected_results_are_json_safe_independent_copies(tool: str, raw: dict[str, Any]) -> None:
    original = deepcopy(raw)

    projected = project_observation(tool, raw)
    encoded = json.dumps(projected, allow_nan=False)

    assert json.loads(encoded) == projected
    assert raw == original
    assert projected is not raw
    if tool == "query_financial_events":
        assert projected["events"] is not raw["events"]
        assert projected["events"][0] is not raw["events"][0]
        raw["events"][0]["amount"] = 1
        assert projected["events"][0]["amount"] == 320_000
