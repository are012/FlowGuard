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


class LiquidityInvestigator:
    """Select evidence and closed-catalog actions without performing finance math."""

    def __init__(self, repository: FlowGuardRepository, tools: CoreToolService) -> None:
        self.repository = repository
        self.tools = tools

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
            "gathered_evidence": [],
            "evidence_gaps": [],
            "candidate_plans": [],
            "evaluated_plans": [],
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

        shortfall_type = metrics.get("shortfall_type")
        risk_date = metrics.get("first_risk_date")
        gap = metrics.get("expected_gap_max")
        if not shortfall_type or not risk_date or not isinstance(gap, int) or gap <= 0:
            state["analysis_complete"] = True
            state["tool_calls"] = self.repository.list_tool_executions(analysis_id, agent_run_id)
            return state

        state["risk_hypotheses"] = self._hypotheses(shortfall_type, metrics)
        for receivable in context.get("receivables", [])[:2]:
            counterparty_id = receivable.get("counterparty_id")
            if not counterparty_id:
                continue
            try:
                evidence = call(
                    "get_counterparty_evidence",
                    {
                        "snapshot_id": snapshot_id,
                        "counterparty_id": counterparty_id,
                    },
                    lambda counterparty_id=counterparty_id: self.tools.get_counterparty_evidence(
                        snapshot_id, counterparty_id
                    ),
                )
            except ServiceError as exc:
                if exc.code != "INSUFFICIENT_DATA":
                    raise
                state["evidence_gaps"].append(
                    {
                        "counterparty_id": counterparty_id,
                        "code": exc.code,
                        "requires_verification": True,
                    }
                )
                continue
            state["gathered_evidence"].append(evidence)

        actions = self._candidate_actions(
            context=context,
            shortfall_type=shortfall_type,
            risk_date=risk_date,
            gap=gap,
        )
        if not actions:
            state["analysis_complete"] = True
            state["tool_calls"] = self.repository.list_tool_executions(analysis_id, agent_run_id)
            return state

        remaining_plan_slots = min(MAX_CANDIDATE_PLANS, (MAX_TOOL_CALLS - sequence) // 2)
        safe_plans: list[dict[str, Any]] = []
        for action in actions[:remaining_plan_slots]:
            plan = {
                "plan_id": f"plan-{uuid4()}",
                "actions": [action],
                "reason": (
                    "기준 분석의 최대 예상 부족액과 최초 위험일을 사용한 규칙 기반 후보입니다."
                ),
            }
            state["candidate_plans"].append(plan)
            evaluation = call(
                "evaluate_action_plan",
                {
                    "snapshot_id": snapshot_id,
                    "actions": plan["actions"],
                    "seed": 42,
                },
                lambda plan=plan: self.tools.evaluate_action_plan(
                    snapshot_id, actions=plan["actions"], seed=42
                ),
            )
            policy = call(
                "validate_financial_policy",
                {
                    "snapshot_id": snapshot_id,
                    "actions": plan["actions"],
                    "evaluation_result": evaluation,
                },
                lambda plan=plan, evaluation=evaluation: self.tools.validate_financial_policy(
                    snapshot_id,
                    actions=plan["actions"],
                    evaluation_result=evaluation,
                ),
            )
            evaluated = {
                "plan_id": plan["plan_id"],
                "evaluation": evaluation,
                "policy_result": policy,
            }
            state["evaluated_plans"].append(evaluated)
            risk_shift = evaluation.get("risk_shift", {})
            if (
                evaluation.get("valid") is True
                and policy.get("valid") is True
                and risk_shift.get("detected") is not True
            ):
                safe_plans.append(
                    {
                        **plan,
                        "evaluation": evaluation,
                        "policy_result": policy,
                    }
                )

        if safe_plans:
            state["policy_result"] = safe_plans[0]["policy_result"]
            state["recommended_plan"] = {
                **safe_plans[0],
                "alternatives": safe_plans[1 : 1 + MAX_EXPOSED_ALTERNATIVES],
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

    @staticmethod
    def _candidate_actions(
        *,
        context: dict[str, Any],
        shortfall_type: str,
        risk_date: str,
        gap: int,
    ) -> list[dict[str, Any]]:
        parsed_risk_date = date.fromisoformat(risk_date)
        as_of = datetime.fromisoformat(context["as_of"]).date()
        if shortfall_type == "PAYMENT_ACCOUNT":
            payment = next(
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
            if payment is None:
                return []
            return [
                {
                    "action_id": f"action-{uuid4()}",
                    "type": "transfer",
                    "parameters": {
                        "from_account_id": source["account_id"],
                        "to_account_id": payment["account_id"],
                        "amount": gap,
                        "execution_date": min(parsed_risk_date, as_of).isoformat(),
                    },
                    "requires_user_approval": True,
                    "assumptions": [
                        ("amount는 baseline_result.risk_metrics.expected_gap_max에서 가져왔습니다.")
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
                    "type": "pause_savings",
                    "parameters": {
                        "event_ids": [event["event_id"] for event in savings],
                        "start_date": as_of.isoformat(),
                        "end_date": max(parsed_risk_date, as_of).isoformat(),
                    },
                    "requires_user_approval": True,
                    "assumptions": ["조정 가능하다고 표시된 저축 이벤트만 선택했습니다."],
                    "source_evidence_ids": [event["event_id"] for event in savings],
                }
            )
        actions.append(
            {
                "action_id": f"action-{uuid4()}",
                "type": "adjust_discretionary_budget",
                "parameters": {
                    "amount": gap,
                    "start_date": as_of.isoformat(),
                    "end_date": max(parsed_risk_date, as_of).isoformat(),
                },
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
                        "type": "delay_purchase",
                        "parameters": {
                            "amount": event["amount"],
                            "from_date": event["expected_date"],
                            "to_date": max(
                                parsed_risk_date,
                                as_of,
                                date.fromisoformat(event["expected_date"]) + timedelta(days=30),
                            ).isoformat(),
                        },
                        "requires_user_approval": True,
                        "assumptions": ["미확정·조정 가능 구매 계획만 선택했습니다."],
                        "source_evidence_ids": [event["event_id"]],
                    }
                )
        return actions[:MAX_CANDIDATE_PLANS]


__all__ = [
    "LiquidityInvestigator",
    "MAX_CANDIDATE_PLANS",
    "MAX_EXPOSED_ALTERNATIVES",
    "MAX_TOOL_CALLS",
]
