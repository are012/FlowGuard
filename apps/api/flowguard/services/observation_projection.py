"""Minimize backend tool results before sending observations to AI."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from flowguard.ai_contract import InvestigationTool

COUNTERPARTY_EVIDENCE_FIELDS = (
    "counterparty_id",
    "payment_history_count",
    "on_time_rate",
    "average_delay_days",
    "median_delay_days",
    "maximum_delay_days",
    "recent_trend",
    "data_confidence",
)


class ObservationProjectionError(ValueError):
    """Reject unsupported tools and malformed tool results with stable codes."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def project_observation(
    tool: InvestigationTool | str,
    raw_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the explicit AI-visible projection for one backend tool result."""

    tool_name = tool.value if isinstance(tool, InvestigationTool) else tool
    projector = _PROJECTORS.get(tool_name)
    if projector is None:
        raise ObservationProjectionError("unsupported_tool")
    if not isinstance(raw_result, Mapping):
        raise ObservationProjectionError("malformed_tool_output")
    return _json_safe_copy(projector(raw_result))


def _project_counterparty_evidence(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    projected = {field: _required(raw_result, field) for field in COUNTERPARTY_EVIDENCE_FIELDS}

    _require_text(projected["counterparty_id"])
    _require_nonnegative_int(projected["payment_history_count"])
    _require_rate(projected["on_time_rate"])
    _require_nonnegative_number(projected["average_delay_days"])
    _require_nonnegative_number(projected["median_delay_days"])
    _require_nonnegative_int(projected["maximum_delay_days"])
    _require_text(projected["recent_trend"])
    _require_rate(projected["data_confidence"])
    return projected


def _project_financial_events(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    events = _require_mapping_list(raw_result, "events")
    projected_events: list[dict[str, Any]] = []
    for event in events:
        event_id = _required(event, "event_id")
        event_type = _required(event, "event_type")
        expected_date = _required(event, "expected_date")
        amount = _required(event, "amount")
        is_essential = _required(event, "is_essential")

        _require_text(event_id)
        _require_text(event_type)
        _require_iso_date(expected_date)
        _require_nonnegative_int(amount)
        if type(is_essential) is not bool:
            raise ObservationProjectionError("malformed_tool_output")

        projected_events.append(
            {
                "event_id": event_id,
                "type": event_type,
                "date": expected_date,
                "amount": amount,
                "is_essential": is_essential,
            }
        )
    return {"events": projected_events}


def _project_financial_context(raw_result: Mapping[str, Any]) -> dict[str, Any]:
    accounts = _require_mapping_list(raw_result, "accounts")
    protected_funds = _require_mapping_list(raw_result, "protected_funds")
    scheduled_events = _require_mapping_list(raw_result, "scheduled_events")

    protected_funds_total = sum(
        _require_nonnegative_int(_required(fund, "amount")) for fund in protected_funds
    )
    essential_scheduled_events_total = 0
    for event in scheduled_events:
        amount = _require_nonnegative_int(_required(event, "amount"))
        is_essential = _required(event, "is_essential")
        if type(is_essential) is not bool:
            raise ObservationProjectionError("malformed_tool_output")
        if is_essential:
            essential_scheduled_events_total += amount

    return {
        "account_count": len(accounts),
        "protected_funds_total": protected_funds_total,
        "essential_scheduled_events_total": essential_scheduled_events_total,
    }


def _required(raw_result: Mapping[str, Any], field: str) -> Any:
    try:
        return raw_result[field]
    except KeyError as exc:
        raise ObservationProjectionError("malformed_tool_output") from exc


def _require_mapping_list(raw_result: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    value = _required(raw_result, field)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ObservationProjectionError("malformed_tool_output")
    return value


def _require_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ObservationProjectionError("malformed_tool_output")
    return value


def _require_nonnegative_int(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ObservationProjectionError("malformed_tool_output")
    return value


def _require_nonnegative_number(value: Any) -> int | float:
    if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
        raise ObservationProjectionError("malformed_tool_output")
    return value


def _require_rate(value: Any) -> int | float:
    number = _require_nonnegative_number(value)
    if number > 1:
        raise ObservationProjectionError("malformed_tool_output")
    return number


def _require_iso_date(value: Any) -> str:
    text = _require_text(value)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ObservationProjectionError("malformed_tool_output") from exc
    return text


def _json_safe_copy(value: dict[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ObservationProjectionError("malformed_tool_output") from exc
    if not isinstance(copied, dict):  # pragma: no cover - projectors always return mappings
        raise ObservationProjectionError("malformed_tool_output")
    return copied


Projector = Callable[[Mapping[str, Any]], dict[str, Any]]
_PROJECTORS: dict[str, Projector] = {
    InvestigationTool.GET_COUNTERPARTY_EVIDENCE.value: _project_counterparty_evidence,
    InvestigationTool.QUERY_FINANCIAL_EVENTS.value: _project_financial_events,
    InvestigationTool.GET_FINANCIAL_CONTEXT.value: _project_financial_context,
}


__all__ = ["ObservationProjectionError", "project_observation"]
