"""Validate AI investigation responses against their originating requests."""

from __future__ import annotations

from collections.abc import Iterable

from flowguard.ai_contract import (
    Investigation,
    InvestigationConcludeRequest,
    InvestigationConcludeResponse,
    InvestigationObservation,
    InvestigationParams,
    InvestigationPlanRequest,
    InvestigationPlanResponse,
    InvestigationTool,
)
from flowguard.ai_numeric_policy import contains_numeric_expression

INVESTIGATION_ECHO_FIELDS = (
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
ALLOWED_INVESTIGATION_TOOLS = frozenset(
    {
        InvestigationTool.GET_FINANCIAL_CONTEXT,
        InvestigationTool.GET_COUNTERPARTY_EVIDENCE,
        InvestigationTool.QUERY_FINANCIAL_EVENTS,
    }
)

InvestigationItem = Investigation | InvestigationObservation
CanonicalInvestigation = tuple[str, tuple[tuple[str, str], ...]]


def validate_investigation_plan_response(
    request_body: InvestigationPlanRequest,
    response_body: InvestigationPlanResponse,
) -> str | None:
    """Return a stable error code when a plan response violates its request."""

    mismatch = _contract_mismatch(request_body, response_body)
    if mismatch is not None:
        return mismatch
    return _validate_investigations(
        request_body,
        response_body.investigations,
        previous=(),
    )


def validate_investigation_conclude_response(
    request_body: InvestigationConcludeRequest,
    response_body: InvestigationConcludeResponse,
) -> str | None:
    """Return a stable error code when a conclude response violates its request."""

    mismatch = _contract_mismatch(request_body, response_body)
    if mismatch is not None:
        return mismatch

    has_additional = response_body.additionalInvestigations is not None
    has_conclusion = response_body.conclusion is not None
    if has_additional == has_conclusion:
        return "invalid_outcome"

    if has_additional:
        if not request_body.allowAdditionalInvestigations:
            return "additional_investigations_not_allowed"
        return _validate_investigations(
            request_body,
            response_body.additionalInvestigations or (),
            previous=request_body.observations,
        )

    conclusion = response_body.conclusion
    if conclusion is None:
        return "invalid_outcome"
    if len(conclusion.hypotheses) > 3:
        return "too_many_hypotheses"
    if any(
        contains_numeric_expression(hypothesis.summary) for hypothesis in conclusion.hypotheses
    ) or any(contains_numeric_expression(item) for item in conclusion.unresolved):
        return "unsupported_numeric_claim"

    allowed_action_types = set(request_body.targets.actionTypes)
    if any(item not in allowed_action_types for item in conclusion.candidatePriorities):
        return "unknown_action_type"
    return None


def _contract_mismatch(
    request_body: InvestigationPlanRequest | InvestigationConcludeRequest,
    response_body: InvestigationPlanResponse | InvestigationConcludeResponse,
) -> str | None:
    for field in INVESTIGATION_ECHO_FIELDS:
        if getattr(request_body, field) != getattr(response_body, field):
            return f"{field}_mismatch"
    return None


def _validate_investigations(
    request_body: InvestigationPlanRequest | InvestigationConcludeRequest,
    investigations: Iterable[Investigation],
    *,
    previous: Iterable[InvestigationItem],
) -> str | None:
    seen = {_canonical_investigation(item) for item in previous}
    for investigation in investigations:
        if investigation.tool not in ALLOWED_INVESTIGATION_TOOLS:
            return "unsupported_tool"

        params = investigation.params
        scope_error = _validate_params_scope(request_body, params)
        if scope_error is not None:
            return scope_error
        if not _params_match_tool(investigation.tool, params):
            return "invalid_tool_params"
        if contains_numeric_expression(investigation.reason):
            return "unsupported_numeric_claim"

        canonical = _canonical_investigation(investigation)
        if canonical in seen:
            return "duplicate_investigation"
        seen.add(canonical)
    return None


def _validate_params_scope(
    request_body: InvestigationPlanRequest | InvestigationConcludeRequest,
    params: InvestigationParams,
) -> str | None:
    targets = request_body.targets
    if params.counterpartyId is not None and params.counterpartyId not in targets.counterpartyIds:
        return "unknown_counterparty_id"

    window = targets.eventWindow
    if (
        params.dateFrom is not None and not window.dateFrom <= params.dateFrom <= window.dateTo
    ) or (params.dateTo is not None and not window.dateFrom <= params.dateTo <= window.dateTo):
        return "date_out_of_range"

    if params.actionType is not None and params.actionType not in targets.actionTypes:
        return "unknown_action_type"
    return None


def _params_match_tool(tool: InvestigationTool, params: InvestigationParams) -> bool:
    present = {
        field
        for field in ("counterpartyId", "dateFrom", "dateTo", "actionType")
        if getattr(params, field) is not None
    }
    expected = {
        InvestigationTool.GET_FINANCIAL_CONTEXT: set(),
        InvestigationTool.GET_COUNTERPARTY_EVIDENCE: {"counterpartyId"},
        InvestigationTool.QUERY_FINANCIAL_EVENTS: {"dateFrom", "dateTo"},
    }
    return present == expected.get(tool)


def _canonical_investigation(item: InvestigationItem) -> CanonicalInvestigation:
    params = item.params.model_dump(mode="json", exclude_none=True)
    return item.tool.value, tuple(sorted((key, str(value)) for key, value in params.items()))


__all__ = [
    "ALLOWED_INVESTIGATION_TOOLS",
    "INVESTIGATION_ECHO_FIELDS",
    "validate_investigation_conclude_response",
    "validate_investigation_plan_response",
]
