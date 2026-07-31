"""Deterministic, rule-based liquidity investigation over recorded core-tool calls."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from flowguard.storage import FlowGuardRepository

from .errors import ServiceError
from .tools import CoreToolService

MAX_TOOL_CALLS = 10
MAX_CANDIDATE_PLANS = 5
MAX_EXPOSED_ALTERNATIVES = 2


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
        for receivable in context.get("receivables", [])[:2]:
            counterparty_id = receivable.get("counterparty_id")
            if not counterparty_id:
                continue
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
            return [
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
                    "source_evidence_ids": list(
                        context.get("data_quality", {}).get("unconfirmed_items", [])
                    ),
                }
                for source in sources
            ]

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
    ) -> None:
        self.repository = repository
        self.tools = tools
        self.risk_builder = RiskEvidenceBuilder(tools)
        self.action_service = ActionCandidateService(tools)
        self.agent_mode = agent_mode
        self.agent_model = agent_model
        self.fallback_reason = fallback_reason
        self.prompt_version = "rule-based-investigator-1"

    def investigate(
        self,
        *,
        analysis_id: str,
        snapshot_id: str,
        baseline_result: dict[str, Any],
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

        def call(
            tool_name: str,
            payload: dict[str, Any],
            function: Callable[[], dict[str, Any]],
        ) -> dict[str, Any]:
            nonlocal sequence
            if sequence >= MAX_TOOL_CALLS:
                raise ServiceError(
                    "AGENT_RUN_FAILED",
                    "에이전트 MCP 호출 한도를 초과했습니다.",
                    details={"max_tool_calls": MAX_TOOL_CALLS},
                    http_status=422,
                )
            sequence += 1
            try:
                output = function()
            except ServiceError as exc:
                self.repository.record_tool_execution(
                    analysis_id=analysis_id,
                    agent_run_id=agent_run_id,
                    sequence=sequence,
                    tool_name=tool_name,
                    input_payload=payload,
                    error=exc.to_dict(),
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
            state["analysis_complete"] = True
            state["tool_calls"] = self.repository.list_tool_executions(analysis_id, agent_run_id)
            return state

        state["risk_hypotheses"] = self._hypotheses(shortfall_type, metrics)
        action_candidates = self.action_service.create(
            context=context,
            shortfall_type=shortfall_type,
            risk_date=risk_date,
            gap=gap,
        )
        if not action_candidates:
            state["analysis_complete"] = True
            state["tool_calls"] = self.repository.list_tool_executions(analysis_id, agent_run_id)
            return state

        evaluated_candidates = self.action_service.evaluate(snapshot_id, action_candidates, call)
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

        safe_candidates = [candidate for candidate in evaluated_candidates if candidate["feasible"]]
        if safe_candidates:
            state["policy_result"] = safe_candidates[0]["policy_result"]
            state["recommended_plan"] = {
                **safe_candidates[0],
                "alternatives": safe_candidates[1 : 1 + MAX_EXPOSED_ALTERNATIVES],
            }

        state["analysis_complete"] = True
        state["tool_calls"] = self.repository.list_tool_executions(analysis_id, agent_run_id)
        return state

    @staticmethod
    def _hypotheses(shortfall_type: str, metrics: dict[str, Any]) -> list[dict[str, Any]]:
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


__all__ = [
    "RiskEvidenceBuilder",
    "ActionCandidateService",
    "LiquidityInvestigator",
    "MAX_CANDIDATE_PLANS",
    "MAX_EXPOSED_ALTERNATIVES",
    "MAX_TOOL_CALLS",
]
