"""FastMCP exposure of FlowGuard's seven versioned core tools."""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from flowguard.services.tools import CoreToolService
from flowguard.storage import FlowGuardRepository

mcp = FastMCP("FlowGuard Core")
_repository: FlowGuardRepository | None = None
_tools: CoreToolService | None = None


def tool_service() -> CoreToolService:
    global _repository, _tools
    if _tools is None:
        _repository = FlowGuardRepository()
        _tools = CoreToolService(_repository)
    return _tools


@mcp.tool()
def get_financial_context(snapshot_id: str) -> dict[str, Any]:
    """Return the immutable financial context for one snapshot."""

    return tool_service().get_financial_context(snapshot_id)


@mcp.tool()
def get_counterparty_evidence(snapshot_id: str, counterparty_id: str) -> dict[str, Any]:
    """Return deterministic payment-delay evidence for one counterparty."""

    return tool_service().get_counterparty_evidence(snapshot_id, counterparty_id)


@mcp.tool()
def query_financial_events(
    snapshot_id: str,
    date_from: str,
    date_to: str,
    directions: list[str] | None = None,
    event_types: list[str] | None = None,
) -> dict[str, Any]:
    """Query scheduled events inside an inclusive date range."""

    return tool_service().query_financial_events(
        snapshot_id,
        date_from=date.fromisoformat(date_from),
        date_to=date.fromisoformat(date_to),
        directions=directions,
        event_types=event_types,
    )


@mcp.tool()
def simulate_cashflow(
    snapshot_id: str,
    horizon_days: int = 91,
    scenario: dict[str, Any] | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Calculate the versioned 13-week baseline and delay scenarios."""

    return tool_service().simulate_cashflow(
        snapshot_id,
        horizon_days=horizon_days,
        scenario=scenario,
        seed=seed,
    )


@mcp.tool()
def calculate_safe_to_spend(
    snapshot_id: str,
    horizon_days: int = 91,
    protection_level: float | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Calculate today's additional safe spending capacity."""

    return tool_service().calculate_safe_to_spend(
        snapshot_id,
        horizon_days=horizon_days,
        protection_level=protection_level,
        seed=seed,
    )


@mcp.tool()
def evaluate_action_plan(
    snapshot_id: str,
    actions: list[dict[str, Any]],
    seed: int = 42,
) -> dict[str, Any]:
    """Virtually apply a closed-catalog action plan and compare before/after."""

    return tool_service().evaluate_action_plan(snapshot_id, actions=actions, seed=seed)


@mcp.tool()
def validate_financial_policy(
    snapshot_id: str,
    actions: list[dict[str, Any]],
    evaluation_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate protected funds, reserves, obligations, and approval requirements."""

    return tool_service().validate_financial_policy(
        snapshot_id,
        actions=actions,
        evaluation_result=evaluation_result,
    )


if __name__ == "__main__":
    mcp.run()
