"""Bounded GPT-5.6 Luna investigator backed by deterministic financial tools."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from flowguard.storage import FlowGuardRepository

from .agent_trace import build_decision_trace
from .errors import ServiceError
from .investigator import (
    MAX_EVALUATED_CANDIDATES,
    MAX_EXPOSED_ALTERNATIVES,
    MAX_TOOL_CALLS,
    ActionCandidateService,
    LiquidityInvestigator,
)
from .tools import CoreToolService

MAX_LUNA_TURNS = 6
MAX_LUNA_TOOL_CALLS = 8

LUNA_INSTRUCTIONS = """
You are FlowGuard's liquidity investigation agent.

Goal:
- Identify the most important near-term liquidity risk.
- Select only the financial tools needed to verify the risk.
- Compare backend-generated action candidates.
- Finish by calling submit_decision exactly once.

Hard boundaries:
- Never calculate, guess, or modify balances, dates, probabilities, or shortage amounts.
- Treat tool outputs as authoritative for financial math and policy validation.
- Recommend only a candidate that evaluate_action_candidate reports as feasible.
- Do not propose actions outside list_action_candidates.
- Do not execute real financial actions.
- Keep user-facing summaries concise and in Korean.
- Do not reveal private chain-of-thought. Return only short, evidence-backed public rationales.

Suggested flow:
1. Call get_financial_context.
2. Inspect relevant counterparty evidence or risk-window events when useful.
3. Call list_action_candidates.
4. Evaluate at most two promising candidates.
5. Call submit_decision.
""".strip()


class PublicHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class LunaDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_hypotheses: list[PublicHypothesis] = Field(default_factory=list, max_length=3)
    selected_candidate_id: str | None
    alternative_candidate_ids: list[str] = Field(default_factory=list, max_length=2)
    recommendation_summary: str = Field(min_length=1, max_length=500)
    selection_reason: str = Field(min_length=1, max_length=800)
    evidence_summary: list[str] = Field(default_factory=list, max_length=6)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=6)


class LunaLiquidityInvestigator:
    """Let Luna choose evidence and candidates while the backend remains authoritative."""

    agent_mode = "LUNA"

    def __init__(
        self,
        repository: FlowGuardRepository,
        tools: CoreToolService,
        client: Any,
        *,
        model: str = "gpt-5.6-luna",
        reasoning_effort: str = "low",
    ) -> None:
        self.repository = repository
        self.tools = tools
        self.client = client
        self.model = model
        self.agent_model = model
        self.reasoning_effort = reasoning_effort
        self.prompt_version = "luna-investigator-1"
        self.action_service = ActionCandidateService(tools)

    def investigate(
        self,
        *,
        analysis_id: str,
        snapshot_id: str,
        baseline_result: dict[str, Any],
    ) -> dict[str, Any]:
        metrics = baseline_result.get("risk_metrics")
        if not isinstance(metrics, dict):
            raise ServiceError(
                "AGENT_RUN_FAILED",
                "기준 분석 결과에 위험지표가 없습니다.",
                http_status=422,
            )
        if not self._has_actionable_risk(metrics):
            fallback = LiquidityInvestigator(self.repository, self.tools).investigate(
                analysis_id=analysis_id,
                snapshot_id=snapshot_id,
                baseline_result=baseline_result,
            )
            fallback["decision_trace"] = build_decision_trace(
                fallback,
                mode="LUNA_NOT_REQUIRED",
                model=self.model,
            )
            return fallback

        try:
            return self._run_luna(
                analysis_id=analysis_id,
                snapshot_id=snapshot_id,
                baseline_result=baseline_result,
                metrics=metrics,
            )
        except Exception as exc:
            fallback = LiquidityInvestigator(self.repository, self.tools).investigate(
                analysis_id=analysis_id,
                snapshot_id=snapshot_id,
                baseline_result=baseline_result,
            )
            fallback["decision_trace"] = build_decision_trace(
                fallback,
                mode="DETERMINISTIC_FALLBACK",
                model=self.model,
                fallback_reason=f"LUNA_API_OR_PROTOCOL_ERROR:{type(exc).__name__}",
            )
            return fallback

    def _run_luna(
        self,
        *,
        analysis_id: str,
        snapshot_id: str,
        baseline_result: dict[str, Any],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        agent_run_id = f"agent-{uuid4()}"
        core_call_count = 0
        model_tool_call_count = 0
        context: dict[str, Any] | None = None
        candidates: dict[str, dict[str, Any]] = {}
        evaluated: dict[str, dict[str, Any]] = {}
        gathered_evidence: list[dict[str, Any]] = []
        evidence_gaps: list[dict[str, Any]] = []
        counterparty_evidence_cache: dict[str, dict[str, Any]] = {}
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        def call(
            tool_name: str,
            payload: dict[str, Any],
            function: Any,
        ) -> dict[str, Any]:
            nonlocal core_call_count
            if core_call_count >= MAX_TOOL_CALLS:
                raise ServiceError(
                    "AGENT_RUN_FAILED",
                    "에이전트 금융 도구 호출 한도를 초과했습니다.",
                    details={"max_tool_calls": MAX_TOOL_CALLS},
                    http_status=422,
                )
            core_call_count += 1
            try:
                output = function()
            except Exception as exc:
                self.repository.record_tool_execution(
                    analysis_id=analysis_id,
                    agent_run_id=agent_run_id,
                    sequence=core_call_count,
                    tool_name=tool_name,
                    input_payload=payload,
                    error=LiquidityInvestigator._tool_error(exc),
                )
                raise
            self.repository.record_tool_execution(
                analysis_id=analysis_id,
                agent_run_id=agent_run_id,
                sequence=core_call_count,
                tool_name=tool_name,
                input_payload=payload,
                output_payload=output,
            )
            return output

        def get_context() -> dict[str, Any]:
            nonlocal context
            if context is None:
                context = call(
                    "get_financial_context",
                    {"snapshot_id": snapshot_id},
                    lambda: self.tools.get_financial_context(snapshot_id),
                )
            return context

        def ensure_candidates() -> None:
            if candidates:
                return
            built = self.action_service.create(
                context=get_context(),
                shortfall_type=str(metrics["shortfall_type"]),
                risk_date=str(metrics["first_risk_date"]),
                gap=int(metrics["expected_gap_max"]),
            )
            candidates.update({str(item["action_id"]): item for item in built})

        def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            nonlocal model_tool_call_count
            model_tool_call_count += 1
            if model_tool_call_count > MAX_LUNA_TOOL_CALLS:
                raise ServiceError(
                    "AGENT_RUN_FAILED",
                    "Luna 도구 선택 한도를 초과했습니다.",
                    details={"max_model_tool_calls": MAX_LUNA_TOOL_CALLS},
                    http_status=422,
                )
            if name == "get_financial_context":
                return get_context()
            if name == "get_counterparty_evidence":
                counterparty_id = str(arguments["counterparty_id"])
                if counterparty_id in counterparty_evidence_cache:
                    return counterparty_evidence_cache[counterparty_id]
                try:
                    evidence = call(
                        "get_counterparty_evidence",
                        {
                            "snapshot_id": snapshot_id,
                            "counterparty_id": counterparty_id,
                        },
                        lambda: self.tools.get_counterparty_evidence(snapshot_id, counterparty_id),
                    )
                except ServiceError as exc:
                    if exc.code != "INSUFFICIENT_DATA":
                        raise
                    gap = {
                        "counterparty_id": counterparty_id,
                        "code": exc.code,
                        "requires_verification": True,
                    }
                    evidence_gaps.append(gap)
                    result = {"error": gap}
                    counterparty_evidence_cache[counterparty_id] = result
                    return result
                gathered_evidence.append(evidence)
                counterparty_evidence_cache[counterparty_id] = evidence
                return evidence
            if name == "query_financial_events":
                date_from = date.fromisoformat(str(arguments["date_from"]))
                date_to = date.fromisoformat(str(arguments["date_to"]))
                return call(
                    "query_financial_events",
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
                )
            if name == "list_action_candidates":
                ensure_candidates()
                return {"candidates": [_compact_candidate(item) for item in candidates.values()]}
            if name == "evaluate_action_candidate":
                candidate_id = str(arguments["candidate_id"])
                if candidate_id not in candidates:
                    ensure_candidates()
                candidate = candidates.get(candidate_id)
                if candidate is None:
                    return {"error": "UNKNOWN_CANDIDATE", "candidate_id": candidate_id}
                if candidate_id not in evaluated:
                    if (
                        len(evaluated) >= MAX_EVALUATED_CANDIDATES
                        or core_call_count > MAX_TOOL_CALLS - 2
                    ):
                        return {
                            "error": "EVALUATION_BUDGET_EXHAUSTED",
                            "max_evaluated_candidates": MAX_EVALUATED_CANDIDATES,
                        }
                    evaluated[candidate_id] = self.action_service.evaluate(
                        snapshot_id,
                        [candidate],
                        call,
                    )[0]
                return _compact_evaluation(evaluated[candidate_id])
            raise ServiceError(
                "AGENT_RUN_FAILED",
                "Luna가 지원하지 않는 도구를 요청했습니다.",
                details={"tool_name": name},
                http_status=422,
            )

        input_items: list[Any] = [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "snapshot_id": snapshot_id,
                        "baseline_risk_metrics": metrics,
                        "instruction": (
                            "검증 가능한 근거와 후보만 사용해 가장 안전한 대응안을 선택하세요."
                        ),
                    },
                    ensure_ascii=False,
                ),
            }
        ]
        decision: LunaDecision | None = None
        safety_identifier = self._safety_identifier(snapshot_id)

        for _turn in range(MAX_LUNA_TURNS):
            response = self.client.responses.create(
                model=self.model,
                instructions=LUNA_INSTRUCTIONS,
                input=input_items,
                tools=LUNA_TOOLS,
                tool_choice="auto",
                parallel_tool_calls=False,
                reasoning={"effort": self.reasoning_effort, "context": "current_turn"},
                max_output_tokens=2000,
                store=False,
                safety_identifier=safety_identifier,
            )
            _accumulate_usage(usage, getattr(response, "usage", None))
            input_items += list(response.output)
            function_calls = [
                item for item in response.output if getattr(item, "type", None) == "function_call"
            ]
            if not function_calls:
                raise ValueError("Luna response did not contain a function call")
            for tool_call in function_calls:
                arguments = json.loads(tool_call.arguments)
                if tool_call.name == "submit_decision":
                    decision = LunaDecision.model_validate(arguments)
                    break
                result = execute_tool(tool_call.name, arguments)
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
            if decision is not None:
                break
        if decision is None:
            raise ValueError("Luna did not submit a final decision")

        selected = self._validated_selection(
            decision,
            candidates=candidates,
            evaluated=evaluated,
        )
        safe_candidates = [item for item in evaluated.values() if item.get("feasible") is True]
        alternatives = self._safe_alternatives(
            decision,
            selected=selected,
            safe_candidates=safe_candidates,
        )
        recommended_plan = None
        if selected is not None:
            recommended_plan = {
                **selected,
                "summary": decision.recommendation_summary,
                "rationale": decision.selection_reason,
                "alternatives": alternatives,
                "selected_by": self.model,
            }

        state = {
            "snapshot_id": snapshot_id,
            "analysis_run_id": analysis_id,
            "agent_run_id": agent_run_id,
            "trigger_context": {"type": "DATA_REFRESH"},
            "baseline_result": baseline_result,
            "risk_hypotheses": [
                {
                    "hypothesis_id": f"hypothesis-{uuid4()}",
                    **hypothesis.model_dump(mode="json"),
                }
                for hypothesis in decision.risk_hypotheses
            ],
            "risk": {
                "type": metrics["shortfall_type"],
                "date": metrics["first_risk_date"],
                "shortageAmount": metrics["expected_gap_max"],
            },
            "gathered_evidence": gathered_evidence,
            "evidence_gaps": evidence_gaps,
            "candidate_plans": list(candidates.values()),
            "evaluated_plans": [
                {
                    "plan_id": item["action_id"],
                    "evaluation": item["evaluation"],
                    "policy_result": item["policy_result"],
                    "feasible": item["feasible"],
                    "riskResolved": item["riskResolved"],
                }
                for item in evaluated.values()
            ],
            "actionCandidates": list(evaluated.values()),
            "recommended_plan": recommended_plan,
            "policy_result": selected.get("policy_result") if selected else None,
            "analysis_complete": True,
            "tool_calls": self.repository.list_tool_executions(analysis_id, agent_run_id),
        }
        trace = build_decision_trace(state, mode="LUNA", model=self.model)
        trace["usage"] = usage
        trace["evidence_summary"] = decision.evidence_summary
        trace["unresolved_questions"] = decision.unresolved_questions
        state["decision_trace"] = trace
        return state

    def _validated_selection(
        self,
        decision: LunaDecision,
        *,
        candidates: dict[str, dict[str, Any]],
        evaluated: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        selected_id = decision.selected_candidate_id
        if selected_id is None:
            return None
        if selected_id not in candidates:
            return next(
                (item for item in evaluated.values() if item.get("feasible") is True),
                None,
            )
        selected = evaluated.get(selected_id)
        if selected and selected.get("feasible") is True:
            return selected
        return next(
            (item for item in evaluated.values() if item.get("feasible") is True),
            None,
        )

    @staticmethod
    def _safe_alternatives(
        decision: LunaDecision,
        *,
        selected: dict[str, Any] | None,
        safe_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected_id = selected.get("action_id") if selected else None
        by_id = {str(item["action_id"]): item for item in safe_candidates}
        ordered_ids = [
            *decision.alternative_candidate_ids,
            *(str(item["action_id"]) for item in safe_candidates),
        ]
        alternatives: list[dict[str, Any]] = []
        for candidate_id in ordered_ids:
            candidate = by_id.get(candidate_id)
            if candidate is None or candidate_id == selected_id or candidate in alternatives:
                continue
            alternatives.append(candidate)
            if len(alternatives) >= MAX_EXPOSED_ALTERNATIVES:
                break
        return alternatives

    def _safety_identifier(self, snapshot_id: str) -> str:
        snapshot = self.repository.get_snapshot(snapshot_id)
        user_id = str(snapshot.get("user_id", "unknown"))
        return hashlib.sha256(f"flowguard:{user_id}".encode()).hexdigest()[:32]

    @staticmethod
    def _has_actionable_risk(metrics: dict[str, Any]) -> bool:
        return bool(
            metrics.get("shortfall_type")
            and metrics.get("first_risk_date")
            and isinstance(metrics.get("expected_gap_max"), int)
            and metrics["expected_gap_max"] > 0
        )


def _compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate["action_id"],
        "type": candidate.get("type"),
        "amount": candidate.get("amount"),
        "actions": candidate.get("actions", []),
        "assumptions": candidate.get("assumptions", []),
        "source_evidence_ids": candidate.get("source_evidence_ids", []),
    }


def _compact_evaluation(candidate: dict[str, Any]) -> dict[str, Any]:
    evaluation = candidate.get("evaluation", {})
    policy = candidate.get("policy_result", {})
    return {
        **_compact_candidate(candidate),
        "feasible": candidate.get("feasible") is True,
        "risk_resolved": candidate.get("riskResolved") is True,
        "before_risk": evaluation.get("before", {}).get("risk_metrics"),
        "after_risk": evaluation.get("after", {}).get("risk_metrics"),
        "risk_shift": evaluation.get("risk_shift"),
        "policy_valid": policy.get("valid") is True,
        "policy_violations": policy.get("violations", []),
    }


def _accumulate_usage(total: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    for key in total:
        value = getattr(usage, key, 0)
        if isinstance(value, int):
            total[key] += value


LUNA_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_financial_context",
        "description": (
            "현재 스냅숏의 계좌, 카드, 예정 수입, 필수지출, 할부, 보호자금과 "
            "데이터 품질을 조회합니다."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_counterparty_evidence",
        "description": "특정 거래처의 과거 지급 이력과 입금 지연 근거를 조회합니다.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "counterparty_id": {
                    "type": "string",
                    "description": "금융 맥락에 포함된 거래처 ID",
                }
            },
            "required": ["counterparty_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "query_financial_events",
        "description": "지정한 기간의 수입·지출·카드대금·할부 이벤트를 조회합니다.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD 시작일"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD 종료일"},
            },
            "required": ["date_from", "date_to"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "list_action_candidates",
        "description": (
            "금융 코어가 현재 위험과 닫힌 행동 카탈로그로 생성한 대응안 후보를 조회합니다."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "evaluate_action_candidate",
        "description": (
            "후보를 가상 적용하고 현금흐름 변화, 반동위험과 금융 안전정책을 검증합니다."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string",
                    "description": "list_action_candidates가 반환한 후보 ID",
                }
            },
            "required": ["candidate_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "submit_decision",
        "description": (
            "검증을 마친 뒤 공개 가능한 위험 가설, 근거 요약과 최종 대응안 선택을 제출합니다."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "risk_hypotheses": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "summary": {"type": "string"},
                            "evidence_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["type", "summary", "evidence_ids"],
                        "additionalProperties": False,
                    },
                },
                "selected_candidate_id": {"type": ["string", "null"]},
                "alternative_candidate_ids": {
                    "type": "array",
                    "maxItems": 2,
                    "items": {"type": "string"},
                },
                "recommendation_summary": {"type": "string"},
                "selection_reason": {"type": "string"},
                "evidence_summary": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {"type": "string"},
                },
                "unresolved_questions": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {"type": "string"},
                },
            },
            "required": [
                "risk_hypotheses",
                "selected_candidate_id",
                "alternative_candidate_ids",
                "recommendation_summary",
                "selection_reason",
                "evidence_summary",
                "unresolved_questions",
            ],
            "additionalProperties": False,
        },
    },
]


__all__ = [
    "LUNA_INSTRUCTIONS",
    "LUNA_TOOLS",
    "LunaDecision",
    "LunaLiquidityInvestigator",
]
