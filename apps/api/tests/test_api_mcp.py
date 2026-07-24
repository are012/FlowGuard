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
