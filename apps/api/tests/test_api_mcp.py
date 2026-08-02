from datetime import date
from unittest.mock import Mock

import flowguard.mcp_server as server
from flowguard.mcp_server import mcp


def test_fastmcp_exposes_exactly_the_seven_financial_tools() -> None:
    assert set(mcp._tool_manager._tools) == {  # noqa: SLF001
        "get_financial_context",
        "get_counterparty_evidence",
        "query_financial_events",
        "simulate_cashflow",
        "calculate_safe_to_spend",
        "evaluate_action_plan",
        "validate_financial_policy",
    }


def test_mcp_functions_forward_typed_arguments_to_core_service(monkeypatch) -> None:
    service = Mock()
    for method in (
        "get_financial_context",
        "get_counterparty_evidence",
        "query_financial_events",
        "simulate_cashflow",
        "calculate_safe_to_spend",
        "evaluate_action_plan",
        "validate_financial_policy",
    ):
        getattr(service, method).return_value = {"method": method}
    monkeypatch.setattr(server, "_tools", service)

    assert server.get_financial_context("snapshot") == {"method": "get_financial_context"}
    assert server.get_counterparty_evidence("snapshot", "client") == {
        "method": "get_counterparty_evidence"
    }
    assert server.query_financial_events(
        "snapshot",
        "2026-08-01",
        "2026-08-31",
        directions=["OUTFLOW"],
        event_types=["RENT"],
    ) == {"method": "query_financial_events"}
    service.query_financial_events.assert_called_once_with(
        "snapshot",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        directions=["OUTFLOW"],
        event_types=["RENT"],
    )
    assert server.simulate_cashflow("snapshot", seed=7) == {"method": "simulate_cashflow"}
    assert server.calculate_safe_to_spend("snapshot", protection_level=0.8) == {
        "method": "calculate_safe_to_spend"
    }
    actions = [{"type": "confirm_receivable", "parameters": {"receivable_id": "r"}}]
    assert server.evaluate_action_plan("snapshot", actions, seed=9) == {
        "method": "evaluate_action_plan"
    }
    evaluation = {"snapshot_id": "snapshot"}
    assert server.validate_financial_policy("snapshot", actions, evaluation) == {
        "method": "validate_financial_policy"
    }


def test_tool_service_lazily_builds_and_reuses_repository(monkeypatch) -> None:
    repository = object()
    service = object()
    repository_factory = Mock(return_value=repository)
    service_factory = Mock(return_value=service)
    monkeypatch.setattr(server, "_repository", None)
    monkeypatch.setattr(server, "_tools", None)
    monkeypatch.setattr(server, "FlowGuardRepository", repository_factory)
    monkeypatch.setattr(server, "CoreToolService", service_factory)

    assert server.tool_service() is service
    assert server.tool_service() is service
    repository_factory.assert_called_once_with(create_schema=False)
    service_factory.assert_called_once_with(repository)
