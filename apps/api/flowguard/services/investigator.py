"""Deterministic, rule-based liquidity investigation over recorded core-tool calls."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from flowguard.ai_contract import (
    Investigation,
    InvestigationPlanRequest,
)
from flowguard.config import (
    AI_DEFAULT_LOCALE,
    AI_INVESTIGATION_CONTRACT_VERSION,
    AI_INVESTIGATION_PROMPT_VERSION,
    AI_INVESTIGATION_SCHEMA_VERSION,
)
from flowguard.observability import log_event
from flowguard.storage import FlowGuardRepository

from .errors import ServiceError
from .investigation_loop import (
    MAX_PHASES,
    PHASE_TIMEOUT,
    TOTAL_BUDGET,
    InvestigationAIClient,
    InvestigationLoop,
    InvestigationLoopOutcome,
)
from .tools import CoreToolService

MAX_TOOL_CALLS = 10


def _investigation_total_budget() -> float:
    """조사 루프 총예산. 비동기 실행에서는 더 넓게 줄 수 있다.

    동기 실행은 요청이 그만큼 점유되므로 기본값을 좁게 두지만,
    FLOWGUARD_ANALYSIS_ASYNC=on 이면 요청 밖에서 돌기 때문에
    지연이 사용자 대기로 이어지지 않는다.
    """

    raw = os.getenv("FLOWGUARD_AI_INVESTIGATION_BUDGET_SECONDS", "").strip()
    if not raw:
        return TOTAL_BUDGET
    try:
        budget = float(raw)
    except ValueError:
        return TOTAL_BUDGET
    return max(budget, PHASE_TIMEOUT * MAX_PHASES)


MAX_CANDIDATE_PLANS = 5
MAX_EVALUATED_CANDIDATES = 3
MAX_EXPOSED_ALTERNATIVES = 2
logger = logging.getLogger("flowguard.policy")
InvestigationMode = Literal["off", "shadow", "on"]
ToolBudget = Literal["deterministic", "investigation"]


@dataclass(frozen=True)
class _InvestigationExecution:
    status: str
    investigation_id: str | None = None
    model_name: str | None = None
    audit_persisted: bool = False
    observations: tuple[dict[str, Any], ...] = ()
    hypotheses: tuple[dict[str, Any], ...] = ()
    priorities: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    additional_investigation_requested: bool = False
    tool_call_count: int = 0
    total_latency_ms: int = 0
    error_code: str | None = None
    trace_requests: tuple[dict[str, Any], ...] = ()


def deterministic_hypotheses(
    shortfall_type: str,
    metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    """Preserve the original rule-based hypothesis path as the universal fallback."""

    if shortfall_type == "PAYMENT_ACCOUNT":
        summary = "전체 자금보다 결제계좌 배치가 주요 위험 원인일 수 있습니다."
    else:
        summary = "보호자금을 제외한 전체 가용자금이 필수 유출보다 부족할 수 있습니다."
    return [
        {
            "hypothesis_id": f"hypothesis-{uuid4()}",
            "type": shortfall_type,
            "summary": summary,
            "source": "baseline_result.risk_metrics",
            "triggering_event_ids": metrics.get("triggering_event_ids", []),
        }
    ]


class RiskEvidenceBuilder:
    """Build structured risk and evidence summaries for downstream interpretation."""

    def __init__(self, tools: CoreToolService) -> None:
        self.tools = tools

    def build(
        self,
        snapshot_id: str,
        baseline_result: dict[str, Any],
        call: Callable[[str, dict[str, Any], Callable[[], dict[str, Any]]], dict[str, Any]],
    ) -> dict[str, Any]:
        context = call(
            "get_financial_context",
            {"snapshot_id": snapshot_id},
            lambda: self.tools.get_financial_context(snapshot_id),
        )
        metrics = baseline_result.get("risk_metrics")
        if not isinstance(metrics, dict):
            raise ServiceError(
                "AGENT_RUN_FAILED",
                "기준 분석 결과에 위험지표가 없습니다.",
                http_status=422,
            )

        gathered_evidence, evidence_gaps = self._build_evidence(
            snapshot_id,
            context,
            call,
        )
        return {
            "risk": self._build_risk(metrics),
            "gathered_evidence": gathered_evidence,
            "evidence_gaps": evidence_gaps,
            "context": context,
        }

    @staticmethod
    def _build_risk(metrics: dict[str, Any]) -> dict[str, Any]:
        shortfall_type = metrics.get("shortfall_type")
        risk_date = metrics.get("first_risk_date")
        gap = metrics.get("expected_gap_max")
        if not shortfall_type or not risk_date or not isinstance(gap, int) or gap <= 0:
            return {}
        return {
            "type": shortfall_type,
            "date": risk_date,
            "shortageAmount": gap,
        }

    def _build_evidence(
        self,
        snapshot_id: str,
        context: dict[str, Any],
        call: Callable[[str, dict[str, Any], Callable[[], dict[str, Any]]], dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        evidence: list[dict[str, Any]] = []
        evidence_gaps: list[dict[str, Any]] = []
        seen_counterparty_ids: set[str] = set()
        for receivable in context.get("receivables", [])[:2]:
            counterparty_id = receivable.get("counterparty_id")
            if not counterparty_id or counterparty_id in seen_counterparty_ids:
                continue
            seen_counterparty_ids.add(counterparty_id)
            try:
                evidence.append(
                    call(
                        "get_counterparty_evidence",
                        {
                            "snapshot_id": snapshot_id,
                            "counterparty_id": counterparty_id,
                        },
                        lambda counterparty_id=counterparty_id: (
                            self.tools.get_counterparty_evidence(
                                snapshot_id,
                                counterparty_id,
                            )
                        ),
                    )
                )
            except ServiceError as exc:
                if exc.code == "INSUFFICIENT_DATA":
                    evidence_gaps.append(
                        {
                            "counterparty_id": counterparty_id,
                            "code": exc.code,
                            "requires_verification": True,
                        }
                    )
                else:
                    raise
        return evidence, evidence_gaps


class ActionCandidateService:
    """Generate backend-verifiable action candidates."""

    def __init__(self, tools: CoreToolService) -> None:
        self.tools = tools

    def create(
        self,
        *,
        context: dict[str, Any],
        shortfall_type: str,
        risk_date: str,
        gap: int,
    ) -> list[dict[str, Any]]:
        parsed_risk_date = date.fromisoformat(risk_date)
        as_of = datetime.fromisoformat(context["as_of"]).date()
        if shortfall_type == "PAYMENT_ACCOUNT":
            payment_account = next(
                (
                    account
                    for account in context.get("accounts", [])
                    if account.get("is_payment_account")
                ),
                None,
            )
            sources = [
                account
                for account in context.get("accounts", [])
                if account.get("is_available_for_transfer")
                and not account.get("is_payment_account")
                and account.get("available_balance", 0) >= gap
            ]
            if payment_account is None:
                return []
            candidates = [
                {
                    "action_id": f"action-{uuid4()}",
                    "id": f"action-{uuid4()}",
                    "type": "TRANSFER",
                    "amount": gap,
                    "feasible": True,
                    "riskResolved": True,
                    "actions": [
                        {
                            "type": "transfer",
                            "parameters": {
                                "from_account_id": source["account_id"],
                                "to_account_id": payment_account["account_id"],
                                "amount": gap,
                                "execution_date": min(parsed_risk_date, as_of).isoformat(),
                            },
                        }
                    ],
                    "requires_user_approval": True,
                    "assumptions": [
                        "amount는 baseline_result.risk_metrics.expected_gap_max에서 가져왔습니다."
                    ],
                    "source_evidence_ids": [
                        source["account_id"],
                        payment_account["account_id"],
                    ],
                }
                for source in sources
            ]
            blocked_sources = [
                account
                for account in context.get("accounts", [])
                if not account.get("is_payment_account")
                and account not in sources
                and account.get("balance", 0) > 0
            ]
            if blocked_sources:
                source = blocked_sources[0]
                candidates.append(
                    {
                        "action_id": f"action-{uuid4()}",
                        "id": f"action-{uuid4()}",
                        "type": "TRANSFER",
                        "amount": gap,
                        "feasible": False,
                        "riskResolved": False,
                        "actions": [
                            {
                                "type": "transfer",
                                "parameters": {
                                    "from_account_id": source["account_id"],
                                    "to_account_id": payment_account["account_id"],
                                    "amount": gap,
                                    "execution_date": min(parsed_risk_date, as_of).isoformat(),
                                },
                            }
                        ],
                        "requires_user_approval": True,
                        "assumptions": [
                            "사용 불가 또는 보호 자금 계좌를 비교한 뒤 정책 검증으로 제외합니다."
                        ],
                        "source_evidence_ids": [
                            source["account_id"],
                            payment_account["account_id"],
                        ],
                    }
                )
            return candidates[:MAX_CANDIDATE_PLANS]

        savings = [
            event
            for event in context.get("scheduled_events", [])
            if event.get("event_type") == "SAVINGS" and event.get("is_adjustable")
        ]
        actions: list[dict[str, Any]] = []
        if savings:
            actions.append(
                {
                    "action_id": f"action-{uuid4()}",
                    "id": f"action-{uuid4()}",
                    "type": "PAUSE_SAVINGS",
                    "amount": gap,
                    "feasible": True,
                    "riskResolved": True,
                    "actions": [
                        {
                            "type": "pause_savings",
                            "parameters": {
                                "event_ids": [event["event_id"] for event in savings],
                                "start_date": as_of.isoformat(),
                                "end_date": max(parsed_risk_date, as_of).isoformat(),
                            },
                        }
                    ],
                    "requires_user_approval": True,
                    "assumptions": ["조정 가능하다고 표시된 저축 이벤트만 선택했습니다."],
                    "source_evidence_ids": [event["event_id"] for event in savings],
                }
            )
        actions.append(
            {
                "action_id": f"action-{uuid4()}",
                "id": f"action-{uuid4()}",
                "type": "ADJUST_DISCRETIONARY_BUDGET",
                "amount": gap,
                "feasible": True,
                "riskResolved": True,
                "actions": [
                    {
                        "type": "adjust_discretionary_budget",
                        "parameters": {
                            "amount": gap,
                            "start_date": as_of.isoformat(),
                            "end_date": max(parsed_risk_date, as_of).isoformat(),
                        },
                    }
                ],
                "requires_user_approval": True,
                "assumptions": [
                    "amount는 baseline_result.risk_metrics.expected_gap_max에서 가져왔습니다."
                ],
                "source_evidence_ids": list(
                    context.get("data_quality", {}).get("unconfirmed_items", [])
                ),
            }
        )
        for event in context.get("scheduled_events", []):
            if (
                event.get("event_type") == "DISCRETIONARY_EXPENSE"
                and event.get("is_adjustable")
                and event.get("certainty") != "CONFIRMED"
            ):
                actions.append(
                    {
                        "action_id": f"action-{uuid4()}",
                        "id": f"action-{uuid4()}",
                        "type": "DELAY_PURCHASE",
                        "amount": event["amount"],
                        "feasible": True,
                        "riskResolved": True,
                        "actions": [
                            {
                                "type": "delay_purchase",
                                "parameters": {
                                    "amount": event["amount"],
                                    "from_date": event["expected_date"],
                                    "to_date": max(
                                        parsed_risk_date,
                                        as_of,
                                        date.fromisoformat(event["expected_date"])
                                        + timedelta(days=30),
                                    ).isoformat(),
                                },
                            }
                        ],
                        "requires_user_approval": True,
                        "assumptions": ["미확정·조정 가능 구매 계획만 선택했습니다."],
                        "source_evidence_ids": [event["event_id"]],
                    }
                )
        return actions[:MAX_CANDIDATE_PLANS]

    def evaluate(
        self,
        snapshot_id: str,
        candidates: list[dict[str, Any]],
        call: Callable[[str, dict[str, Any], Callable[[], dict[str, Any]]], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        evaluated_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            evaluation = call(
                "evaluate_action_plan",
                {
                    "snapshot_id": snapshot_id,
                    "actions": candidate["actions"],
                    "seed": 42,
                },
                lambda candidate=candidate: self.tools.evaluate_action_plan(
                    snapshot_id, actions=candidate["actions"], seed=42
                ),
            )
            policy = call(
                "validate_financial_policy",
                {
                    "snapshot_id": snapshot_id,
                    "actions": candidate["actions"],
                    "evaluation_result": evaluation,
                },
                lambda candidate=candidate, evaluation=evaluation: (
                    self.tools.validate_financial_policy(
                        snapshot_id,
                        actions=candidate["actions"],
                        evaluation_result=evaluation,
                    )
                ),
            )
            risk_shift = evaluation.get("risk_shift", {})
            feasible = (
                evaluation.get("valid") is True
                and policy.get("valid") is True
                and risk_shift.get("detected") is not True
            )
            evaluated_candidates.append(
                {
                    **candidate,
                    "evaluation": evaluation,
                    "policy_result": policy,
                    "feasible": feasible,
                    "riskResolved": feasible,
                }
            )
        return evaluated_candidates


class LiquidityInvestigator:
    """Select evidence and closed-catalog actions without performing finance math."""

    def __init__(
        self,
        repository: FlowGuardRepository,
        tools: CoreToolService,
        *,
        agent_mode: str = "DETERMINISTIC",
        agent_model: str | None = None,
        fallback_reason: str | None = None,
        investigation_mode: InvestigationMode = "off",
        investigation_client: InvestigationAIClient | None = None,
        investigation_locale: str = AI_DEFAULT_LOCALE,
        investigation_model_name: str | None = None,
    ) -> None:
        self.repository = repository
        self.tools = tools
        self.risk_builder = RiskEvidenceBuilder(tools)
        self.action_service = ActionCandidateService(tools)
        self.agent_mode = agent_mode
        self.agent_model = agent_model
        self.fallback_reason = fallback_reason
        self.prompt_version = "rule-based-investigator-1"
        self.investigation_mode = investigation_mode
        self.investigation_client = investigation_client
        self.investigation_locale = investigation_locale
        self.investigation_model_name = investigation_model_name

    def investigate(
        self,
        *,
        analysis_id: str,
        user_id: str | None = None,
        snapshot_id: str,
        snapshot_revision: str | None = None,
        baseline_result: dict[str, Any],
        is_virtual: bool = False,
        investigation_targets: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        agent_run_id = f"agent-{uuid4()}"
        state: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "analysis_run_id": analysis_id,
            "agent_run_id": agent_run_id,
            "trigger_context": {"type": "DATA_REFRESH"},
            "baseline_result": baseline_result,
            "risk_hypotheses": [],
            "risk": {},
            "gathered_evidence": [],
            "evidence_gaps": [],
            "candidate_plans": [],
            "evaluated_plans": [],
            "actionCandidates": [],
            "recommended_plan": None,
            "policy_result": None,
            "analysis_complete": False,
        }
        sequence = 0
        deterministic_call_count = 0
        deterministic_sequences: set[int] = set()
        investigation_execution = _InvestigationExecution(status="NOT_REQUESTED")

        def call(
            tool_name: str,
            payload: dict[str, Any],
            function: Callable[[], dict[str, Any]],
            *,
            budget: ToolBudget = "deterministic",
        ) -> dict[str, Any]:
            nonlocal deterministic_call_count, sequence
            if budget == "deterministic":
                if deterministic_call_count >= MAX_TOOL_CALLS:
                    raise ServiceError(
                        "AGENT_RUN_FAILED",
                        "에이전트 MCP 호출 한도를 초과했습니다.",
                        details={"max_tool_calls": MAX_TOOL_CALLS},
                        http_status=422,
                    )
                deterministic_call_count += 1
            elif budget != "investigation":  # pragma: no cover - closed internal call sites
                raise ValueError(f"unsupported tool-call budget {budget}")
            sequence += 1
            if budget == "deterministic":
                deterministic_sequences.add(sequence)
            try:
                output = function()
            except Exception as exc:
                self.repository.record_tool_execution(
                    analysis_id=analysis_id,
                    agent_run_id=agent_run_id,
                    sequence=sequence,
                    tool_name=tool_name,
                    input_payload=payload,
                    error=self._tool_error(exc),
                )
                raise
            self.repository.record_tool_execution(
                analysis_id=analysis_id,
                agent_run_id=agent_run_id,
                sequence=sequence,
                tool_name=tool_name,
                input_payload=payload,
                output_payload=output,
            )
            return output

        def execute_investigation_tool(investigation: Investigation) -> dict[str, Any]:
            return self._execute_investigation_tool(
                snapshot_id,
                investigation,
                call,
            )

        def run_investigation() -> _InvestigationExecution:
            return self._run_ai_investigation(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot_id=snapshot_id,
                snapshot_revision=snapshot_revision,
                baseline_result=baseline_result,
                targets=investigation_targets,
                execute_tool=execute_investigation_tool,
            )

        def finish() -> dict[str, Any]:
            nonlocal investigation_execution
            state["analysis_complete"] = True
            if self.investigation_mode == "shadow" and not is_virtual:
                investigation_execution = run_investigation()
            all_tool_calls = self.repository.list_tool_executions(analysis_id, agent_run_id)
            state["tool_calls"] = [
                execution
                for execution in all_tool_calls
                if execution.get("sequence") in deterministic_sequences
            ]
            if self.investigation_mode != "off" and not is_virtual:
                self._apply_investigation_state(
                    state,
                    investigation_execution,
                    applied=(
                        self.investigation_mode == "on"
                        and investigation_execution.status == "SUCCEEDED"
                        and investigation_execution.audit_persisted
                    ),
                )
            return state

        if self.investigation_mode == "on" and not is_virtual:
            investigation_execution = run_investigation()

        metrics = baseline_result.get("risk_metrics")
        applies_ai_result = (
            self.investigation_mode == "on"
            and investigation_execution.status == "SUCCEEDED"
            and investigation_execution.audit_persisted
            and isinstance(metrics, dict)
        )
        if applies_ai_result:
            context = call(
                "get_financial_context",
                {"snapshot_id": snapshot_id},
                lambda: self.tools.get_financial_context(snapshot_id),
            )
            evidence_state = {
                "risk": RiskEvidenceBuilder._build_risk(metrics),
                "gathered_evidence": list(investigation_execution.observations),
                "evidence_gaps": [],
                "context": context,
            }
        else:
            evidence_state = self.risk_builder.build(snapshot_id, baseline_result, call)
        state["risk"] = evidence_state["risk"]
        state["gathered_evidence"] = evidence_state["gathered_evidence"]
        state["evidence_gaps"] = evidence_state["evidence_gaps"]
        context = evidence_state["context"]

        metrics = baseline_result.get("risk_metrics")
        shortfall_type = metrics.get("shortfall_type")
        risk_date = metrics.get("first_risk_date")
        gap = metrics.get("expected_gap_max")
        if not shortfall_type or not risk_date or not isinstance(gap, int) or gap <= 0:
            return finish()

        state["risk_hypotheses"] = (
            self._ai_hypotheses(
                analysis_id,
                metrics,
                investigation_execution.hypotheses,
            )
            if applies_ai_result
            else deterministic_hypotheses(shortfall_type, metrics)
        )
        action_candidates = self.action_service.create(
            context=context,
            shortfall_type=shortfall_type,
            risk_date=risk_date,
            gap=gap,
        )
        if not action_candidates:
            return finish()

        evaluated_candidates = self.action_service.evaluate(
            snapshot_id,
            action_candidates[:MAX_EVALUATED_CANDIDATES],
            call,
        )
        state["candidate_plans"] = action_candidates
        state["evaluated_plans"] = [
            {
                "plan_id": candidate["action_id"],
                "evaluation": candidate["evaluation"],
                "policy_result": candidate["policy_result"],
                "feasible": candidate["feasible"],
                "riskResolved": candidate["riskResolved"],
            }
            for candidate in evaluated_candidates
        ]
        state["actionCandidates"] = evaluated_candidates

        for candidate in evaluated_candidates:
            policy = candidate.get("policy_result") or {}
            if policy.get("valid") is True:
                continue
            violations = policy.get("violations") or []
            codes = sorted(
                {
                    str(violation.get("code"))
                    for violation in violations
                    if isinstance(violation, dict) and violation.get("code")
                }
            )
            log_event(
                logger,
                logging.WARNING,
                "policy_candidate_rejected",
                analysis_id=analysis_id,
                action_id=str(candidate.get("action_id", "unknown")),
                violation_count=len(violations),
                violation_codes=",".join(codes),
            )

        safe_candidates = [candidate for candidate in evaluated_candidates if candidate["feasible"]]
        if safe_candidates:
            state["policy_result"] = safe_candidates[0]["policy_result"]
            state["recommended_plan"] = {
                **safe_candidates[0],
                "alternatives": safe_candidates[1 : 1 + MAX_EXPOSED_ALTERNATIVES],
            }

        return finish()

    def _run_ai_investigation(
        self,
        *,
        analysis_id: str,
        user_id: str | None,
        snapshot_id: str,
        snapshot_revision: str | None,
        baseline_result: dict[str, Any],
        targets: Mapping[str, Any] | None,
        execute_tool: Callable[[Investigation], dict[str, Any]],
    ) -> _InvestigationExecution:
        try:
            request = self._build_investigation_request(
                analysis_id=analysis_id,
                snapshot_id=snapshot_id,
                snapshot_revision=snapshot_revision,
                baseline_result=baseline_result,
                targets=targets,
            )
        except Exception:
            return _InvestigationExecution(
                status="FAILED",
                error_code="investigation_request_failed",
            )
        if request is None:
            return _InvestigationExecution(status="NOT_REQUESTED")
        if user_id is None or self.investigation_client is None:
            return _InvestigationExecution(
                status="NOT_REQUESTED",
                error_code="investigation_client_unconfigured",
            )

        payload = request.model_dump(mode="json")
        try:
            run, created = self.repository.create_or_get_investigation_run(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot_id=snapshot_id,
                snapshot_revision=str(request.snapshotRevision),
                request_id=request.requestId,
                idempotency_key=request.idempotencyKey,
                schema_version=request.schemaVersion,
                contract_version=request.contractVersion,
                prompt_version=request.promptVersion,
                mode=self.investigation_mode.upper(),
                model_name=self.investigation_model_name,
            )
        except Exception:
            return _InvestigationExecution(
                status="FAILED",
                error_code="investigation_persistence_error",
            )

        investigation_id = str(run["investigation_id"])
        if not created and run.get("status") == "IN_PROGRESS":
            return _InvestigationExecution(
                status="FAILED",
                investigation_id=investigation_id,
                error_code="investigation_already_in_progress",
            )
        if not created and run.get("status") in {
            "SUCCEEDED",
            "PARTIAL",
            "REJECTED",
            "FAILED",
        }:
            try:
                turns = self.repository.list_investigation_turns(investigation_id)
                return self._stored_investigation_execution(run, turns)
            except Exception:
                return _InvestigationExecution(
                    status="FAILED",
                    investigation_id=investigation_id,
                    error_code="investigation_persistence_error",
                )

        try:
            outcome = InvestigationLoop(
                self.investigation_client,
                execute_tool,
                total_budget=_investigation_total_budget(),
            ).run(payload)
        except Exception as exc:
            self._fail_investigation_run(
                investigation_id,
                error_code="investigation_internal_error",
            )
            log_event(
                logger,
                logging.ERROR,
                "ai_investigation_failed",
                analysis_id=analysis_id,
                investigation_id=investigation_id,
                exception_type=type(exc).__name__,
            )
            return _InvestigationExecution(
                status="FAILED",
                investigation_id=investigation_id,
                error_code="investigation_internal_error",
            )

        try:
            for turn in outcome.turns:
                self.repository.record_investigation_turn(
                    investigation_id=investigation_id,
                    turn_sequence=turn.turn_sequence,
                    phase=turn.phase,
                    endpoint=turn.endpoint,
                    status=turn.status,
                    request_payload=turn.request_payload,
                    response_payload=turn.response_payload,
                    error_code=turn.error_code,
                    attempt_count=turn.attempt_count,
                    latency_ms=turn.latency_ms,
                )
            if outcome.status in {"SUCCEEDED", "PARTIAL"}:
                self.repository.complete_investigation_run(
                    investigation_id,
                    status=outcome.status,
                    observations=outcome.observations,
                    hypotheses=outcome.hypotheses,
                    priorities=outcome.priorities,
                    unresolved=outcome.unresolved,
                    tool_call_count=outcome.tool_call_count,
                    total_latency_ms=outcome.total_latency_ms,
                    additional_investigation_requested=(outcome.additional_investigation_requested),
                    error_code=outcome.error_code,
                )
            else:
                self.repository.fail_investigation_run(
                    investigation_id,
                    status=outcome.status,
                    observations=outcome.observations,
                    tool_call_count=outcome.tool_call_count,
                    total_latency_ms=outcome.total_latency_ms,
                    additional_investigation_requested=(outcome.additional_investigation_requested),
                    error_code=outcome.error_code or "investigation_failed",
                )
        except Exception as exc:
            self._fail_investigation_run(
                investigation_id,
                error_code="investigation_persistence_error",
                outcome=outcome,
            )
            log_event(
                logger,
                logging.ERROR,
                "ai_investigation_persistence_failed",
                analysis_id=analysis_id,
                investigation_id=investigation_id,
                exception_type=type(exc).__name__,
            )
            return _InvestigationExecution(
                status="FAILED",
                investigation_id=investigation_id,
                error_code="investigation_persistence_error",
            )

        return self._loop_investigation_execution(
            investigation_id,
            outcome,
            model_name=self.investigation_model_name,
        )

    def _build_investigation_request(
        self,
        *,
        analysis_id: str,
        snapshot_id: str,
        snapshot_revision: str | None,
        baseline_result: Mapping[str, Any],
        targets: Mapping[str, Any] | None,
    ) -> InvestigationPlanRequest | None:
        metrics = baseline_result.get("risk_metrics")
        if not isinstance(metrics, Mapping):
            return None
        shortfall_type = metrics.get("shortfall_type")
        risk_date = metrics.get("first_risk_date")
        shortage_amount = metrics.get("expected_gap_max")
        if (
            not isinstance(shortfall_type, str)
            or not shortfall_type
            or risk_date is None
            or type(shortage_amount) is not int
            or shortage_amount <= 0
        ):
            return None
        if snapshot_revision is None or targets is None:
            raise ValueError("investigation identity and targets are required")

        idempotency_key = (
            f"{analysis_id}:{snapshot_revision}:contract-"
            f"{AI_INVESTIGATION_CONTRACT_VERSION}:"
            f"{AI_INVESTIGATION_PROMPT_VERSION}:{self.investigation_locale}:"
            f"{self.investigation_mode}"
        )
        request_id = f"ai-investigation-{uuid5(NAMESPACE_URL, idempotency_key)}"
        return InvestigationPlanRequest.model_validate(
            {
                "schemaVersion": AI_INVESTIGATION_SCHEMA_VERSION,
                "contractVersion": AI_INVESTIGATION_CONTRACT_VERSION,
                "promptVersion": AI_INVESTIGATION_PROMPT_VERSION,
                "requestId": request_id,
                "idempotencyKey": idempotency_key,
                "analysisId": analysis_id,
                "snapshotId": snapshot_id,
                "snapshotRevision": snapshot_revision,
                "locale": self.investigation_locale,
                "baseline": {
                    "type": shortfall_type,
                    "date": risk_date,
                    "shortageAmount": shortage_amount,
                },
                "targets": dict(targets),
            }
        )

    def _execute_investigation_tool(
        self,
        snapshot_id: str,
        investigation: Investigation,
        call: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        tool_name = investigation.tool.value
        params = investigation.params
        if tool_name == "get_financial_context":
            return call(
                tool_name,
                {"snapshot_id": snapshot_id},
                lambda: self.tools.get_financial_context(snapshot_id),
                budget="investigation",
            )
        if tool_name == "get_counterparty_evidence" and params.counterpartyId is not None:
            counterparty_id = params.counterpartyId
            return call(
                tool_name,
                {
                    "snapshot_id": snapshot_id,
                    "counterparty_id": counterparty_id,
                },
                lambda: self.tools.get_counterparty_evidence(
                    snapshot_id,
                    counterparty_id,
                ),
                budget="investigation",
            )
        if (
            tool_name == "query_financial_events"
            and params.dateFrom is not None
            and params.dateTo is not None
        ):
            date_from = params.dateFrom
            date_to = params.dateTo
            return call(
                tool_name,
                {
                    "snapshot_id": snapshot_id,
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                },
                lambda: self.tools.query_financial_events(
                    snapshot_id,
                    date_from=date_from,
                    date_to=date_to,
                ),
                budget="investigation",
            )
        raise ServiceError(
            "AGENT_RUN_FAILED",
            "허용되지 않은 AI 조사 도구 요청입니다.",
            details={"tool_name": tool_name},
            http_status=422,
        )

    def _fail_investigation_run(
        self,
        investigation_id: str,
        *,
        error_code: str,
        outcome: InvestigationLoopOutcome | None = None,
    ) -> None:
        with suppress(Exception):
            self.repository.fail_investigation_run(
                investigation_id,
                status="FAILED",
                observations=outcome.observations if outcome is not None else (),
                tool_call_count=outcome.tool_call_count if outcome is not None else 0,
                total_latency_ms=outcome.total_latency_ms if outcome is not None else 0,
                additional_investigation_requested=(
                    outcome.additional_investigation_requested if outcome is not None else False
                ),
                error_code=error_code,
            )

    @classmethod
    def _loop_investigation_execution(
        cls,
        investigation_id: str,
        outcome: InvestigationLoopOutcome,
        *,
        model_name: str | None,
    ) -> _InvestigationExecution:
        turns = [
            {
                "phase": turn.phase,
                "endpoint": turn.endpoint,
                "status": turn.status,
                "response_payload": turn.response_payload,
            }
            for turn in outcome.turns
        ]
        return _InvestigationExecution(
            status=outcome.status,
            investigation_id=investigation_id,
            model_name=model_name,
            audit_persisted=True,
            observations=outcome.observations,
            hypotheses=outcome.hypotheses,
            priorities=outcome.priorities,
            unresolved=outcome.unresolved,
            additional_investigation_requested=(outcome.additional_investigation_requested),
            tool_call_count=outcome.tool_call_count,
            total_latency_ms=outcome.total_latency_ms,
            error_code=outcome.error_code,
            trace_requests=cls._investigation_trace_requests(
                turns,
                tool_call_count=outcome.tool_call_count,
            ),
        )

    @classmethod
    def _stored_investigation_execution(
        cls,
        run: Mapping[str, Any],
        turns: list[dict[str, Any]],
    ) -> _InvestigationExecution:
        if run.get("status") == "SUCCEEDED" and not cls._has_complete_success_audit(run, turns):
            return _InvestigationExecution(
                status="FAILED",
                investigation_id=str(run["investigation_id"]),
                model_name=(str(run["model_name"]) if run.get("model_name") else None),
                error_code="investigation_audit_incomplete",
            )
        observations = tuple(dict(item) for item in run.get("observations") or ())
        hypotheses = tuple(dict(item) for item in run.get("hypotheses") or ())
        priorities = tuple(str(item) for item in run.get("priorities") or ())
        unresolved = tuple(str(item) for item in run.get("unresolved") or ())
        tool_call_count = int(run.get("tool_call_count") or 0)
        return _InvestigationExecution(
            status=str(run["status"]),
            investigation_id=str(run["investigation_id"]),
            model_name=(str(run["model_name"]) if run.get("model_name") else None),
            audit_persisted=True,
            observations=observations,
            hypotheses=hypotheses,
            priorities=priorities,
            unresolved=unresolved,
            additional_investigation_requested=(
                run.get("additional_investigation_requested") is True
            ),
            tool_call_count=tool_call_count,
            total_latency_ms=int(run.get("total_latency_ms") or 0),
            error_code=(str(run["error_code"]) if run.get("error_code") else None),
            trace_requests=cls._investigation_trace_requests(
                turns,
                tool_call_count=tool_call_count,
            ),
        )

    @classmethod
    def _has_complete_success_audit(
        cls,
        run: Mapping[str, Any],
        turns: list[dict[str, Any]],
    ) -> bool:
        additional_requested = run.get("additional_investigation_requested") is True
        expected_sequences = [1, 2, 3] if additional_requested else [1, 2]
        if [turn.get("turn_sequence") for turn in turns] != expected_sequences:
            return False
        if (
            turns[0].get("phase") != 1
            or turns[0].get("endpoint") != "/investigate/plan"
            or turns[0].get("status") != "SUCCEEDED"
        ):
            return False
        if any(
            turn.get("phase") != 2
            or turn.get("endpoint") != "/investigate/conclude"
            or turn.get("status") != "SUCCEEDED"
            for turn in turns[1:]
        ):
            return False
        if additional_requested:
            additional_response = turns[1].get("response_payload")
            if not isinstance(additional_response, Mapping) or not isinstance(
                additional_response.get("additionalInvestigations"), list
            ):
                return False
        final_response = turns[-1].get("response_payload")
        if not isinstance(final_response, Mapping) or not isinstance(
            final_response.get("conclusion"), Mapping
        ):
            return False
        tool_call_count = int(run.get("tool_call_count") or 0)
        observations = run.get("observations")
        hypotheses = run.get("hypotheses")
        if (
            not isinstance(observations, list)
            or len(observations) != tool_call_count
            or not isinstance(hypotheses, list)
            or not hypotheses
        ):
            return False
        trace_requests = cls._investigation_trace_requests(
            turns,
            tool_call_count=None,
        )
        return len(trace_requests) == tool_call_count

    @staticmethod
    def _investigation_trace_requests(
        turns: list[dict[str, Any]],
        *,
        tool_call_count: int | None,
    ) -> tuple[dict[str, Any], ...]:
        requests: list[dict[str, Any]] = []
        for turn in turns:
            if turn.get("status") != "SUCCEEDED":
                continue
            response = turn.get("response_payload")
            if not isinstance(response, Mapping):
                continue
            items = (
                response.get("investigations")
                if turn.get("endpoint") == "/investigate/plan"
                else response.get("additionalInvestigations")
            )
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                requests.append(
                    {
                        "source": "AI",
                        "phase": int(turn["phase"]),
                        "tool": item.get("tool"),
                        "params": item.get("params"),
                        "reason": item.get("reason"),
                    }
                )
        return tuple(requests if tool_call_count is None else requests[:tool_call_count])

    def _apply_investigation_state(
        self,
        state: dict[str, Any],
        execution: _InvestigationExecution,
        *,
        applied: bool,
    ) -> None:
        state["investigation"] = {
            "investigation_id": execution.investigation_id,
            "mode": self.investigation_mode.upper(),
            "status": execution.status,
            "applied": applied,
            "observations": list(execution.observations),
            "hypotheses": list(execution.hypotheses),
            "priorities": list(execution.priorities),
            "unresolved": list(execution.unresolved),
            "additional_investigation_requested": (execution.additional_investigation_requested),
            "tool_call_count": execution.tool_call_count,
            "total_latency_ms": execution.total_latency_ms,
            "error_code": execution.error_code,
        }
        state["investigation_trace"] = list(execution.trace_requests)
        if applied:
            state["decision_mode"] = "AI_INVESTIGATED"
            state["decision_model"] = execution.model_name
            state["decision_fallback_reason"] = None
            state["decision_prompt_version"] = AI_INVESTIGATION_PROMPT_VERSION
        elif self.investigation_mode == "on" and execution.status == "PARTIAL":
            state["decision_mode"] = "AI_PARTIAL"
            state["decision_model"] = execution.model_name
            state["decision_fallback_reason"] = execution.error_code
            state["decision_prompt_version"] = AI_INVESTIGATION_PROMPT_VERSION
        else:
            state["decision_mode"] = "DETERMINISTIC"
            state["decision_model"] = None
            state["decision_fallback_reason"] = (
                execution.error_code if self.investigation_mode == "on" else None
            )
            state["decision_prompt_version"] = "rule-based-investigator-1"

    @staticmethod
    def _ai_hypotheses(
        analysis_id: str,
        metrics: Mapping[str, Any],
        hypotheses: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        triggering_event_ids = list(metrics.get("triggering_event_ids") or ())
        projected: list[dict[str, Any]] = []
        for index, item in enumerate(
            sorted(hypotheses, key=lambda hypothesis: int(hypothesis["priority"])),
            start=1,
        ):
            identity = f"{analysis_id}:{index}:{item['type']}"
            projected.append(
                {
                    "hypothesis_id": f"hypothesis-ai-{uuid5(NAMESPACE_URL, identity)}",
                    "type": item["type"],
                    "summary": item["summary"],
                    "priority": item["priority"],
                    "source": "AI_INVESTIGATION",
                    "triggering_event_ids": triggering_event_ids,
                }
            )
        return projected

    @staticmethod
    def _tool_error(exception: Exception) -> dict[str, Any]:
        if isinstance(exception, ServiceError):
            return exception.to_dict()
        return {
            "code": "TOOL_EXECUTION_FAILED",
            "message": "금융 도구 실행 중 예상하지 못한 오류가 발생했습니다.",
            "details": {"exception_type": type(exception).__name__},
            "retryable": False,
        }

    _hypotheses = staticmethod(deterministic_hypotheses)


__all__ = [
    "RiskEvidenceBuilder",
    "ActionCandidateService",
    "InvestigationMode",
    "LiquidityInvestigator",
    "MAX_CANDIDATE_PLANS",
    "MAX_EVALUATED_CANDIDATES",
    "MAX_EXPOSED_ALTERNATIVES",
    "MAX_TOOL_CALLS",
    "deterministic_hypotheses",
]
