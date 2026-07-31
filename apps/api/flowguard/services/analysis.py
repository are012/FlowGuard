"""Synchronous, deterministic end-to-end analysis orchestration."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from uuid import uuid4

from flowguard.config import (
    DEFAULT_SIMULATION_SEED,
    FINANCIAL_CORE_VERSION,
    POLICY_VERSION,
    RISK_RULE_VERSION,
)
from flowguard.core import map_risk_presentation
from flowguard.domain import CashflowAnalysis, FinancialSnapshot
from flowguard.storage import (
    FlowGuardRepository,
    InvalidAnalysisTransition,
    jsonable,
    utc_now,
)

from .analysis_support import AIInterpretationClient
from .errors import ServiceError
from .investigator import LiquidityInvestigator
from .snapshots import SnapshotBuilder
from .tools import CoreToolService

STATUS_MESSAGES = {
    "SNAPSHOT_BUILDING": "현재 금융정보로 분석 스냅샷을 생성하고 있습니다.",
    "BASELINE_ANALYZING": "13주 기준 현금흐름을 계산하고 있습니다.",
    "AGENT_INVESTIGATING": "유동성 위험의 원인과 근거를 조사하고 있습니다.",
    "PLAN_EVALUATING": "후보 대응안을 가상 적용하고 정책을 검증하고 있습니다.",
    "REPORT_BUILDING": "사용자용 분석 보고서를 만들고 있습니다.",
    "COMPLETED": "분석이 완료되었습니다.",
    "FAILED": "분석이 실패했습니다.",
}


class AnalysisOrchestrator:
    """Run the complete workflow inline while persisting every public status."""

    def __init__(
        self,
        repository: FlowGuardRepository,
        *,
        snapshot_builder: SnapshotBuilder | None = None,
        tools: CoreToolService | None = None,
        investigator: LiquidityInvestigator | None = None,
        ai_client: AIInterpretationClient | None = None,
    ) -> None:
        self.repository = repository
        self.snapshot_builder = snapshot_builder or SnapshotBuilder(repository)
        self.tools = tools or CoreToolService(repository)
        self.investigator = investigator or LiquidityInvestigator(repository, self.tools)
        self.cashflow_service = CashflowAnalysisService(self.tools)
        self.ai_client = ai_client or AIInterpretationClient(
            base_url=os.getenv("FLOWGUARD_AI_SERVER_URL", "http://localhost:8001"),
            timeout_seconds=float(os.getenv("FLOWGUARD_AI_TIMEOUT_SECONDS", "3")),
            max_retries=int(os.getenv("FLOWGUARD_AI_MAX_RETRIES", "1")),
            retry_backoff_seconds=float(
                os.getenv("FLOWGUARD_AI_RETRY_BACKOFF_SECONDS", "0.25")
            ),
            prompt_version=os.getenv("FLOWGUARD_AI_PROMPT_VERSION", "ai-interpretation-v1"),
            model_name=os.getenv("FLOWGUARD_AI_MODEL_NAME", "ai-service"),
            max_concurrent_requests=int(
                os.getenv("FLOWGUARD_AI_MAX_CONCURRENT_REQUESTS", "2")
            ),
            circuit_breaker_threshold=int(
                os.getenv("FLOWGUARD_AI_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
            ),
            circuit_breaker_open_seconds=float(
                os.getenv("FLOWGUARD_AI_CIRCUIT_BREAKER_OPEN_SECONDS", "30")
            ),
            repository=repository,
        )

    def run(
        self,
        user_id: str,
        *,
        trigger_type: str = "MANUAL",
        as_of: datetime | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        run = self.repository.create_analysis(
            user_id,
            trigger_type=trigger_type,
            metadata=self._metadata(is_virtual=False, request_id=request_id),
        )
        analysis_id = run["analysis_id"]
        try:
            self._transition(analysis_id, "SNAPSHOT_BUILDING")
            snapshot, current_state_revision = self.snapshot_builder.build_with_revision(
                user_id, as_of=as_of
            )
            return self._analyze_snapshot(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot=snapshot,
                is_virtual=False,
                base_snapshot_id=None,
                current_state_revision=current_state_revision,
                correlation_id=request_id,
            )
        except ServiceError as exc:
            return self._fail(analysis_id, exc)
        except Exception as exc:
            return self._fail_unexpected(analysis_id, exc)

    def run_virtual(
        self,
        user_id: str,
        *,
        base_snapshot_id: str,
        actions: list[dict[str, Any]],
        expected_current_state_revision: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply approved actions to a new snapshot and fully reanalyze it.

        Mutable current records are intentionally untouched.
        """

        run = self.repository.create_analysis(
            user_id,
            trigger_type="RECOMMENDATION_APPROVAL",
            metadata=self._metadata(
                is_virtual=True,
                base_snapshot_id=base_snapshot_id,
                request_id=request_id,
            ),
        )
        analysis_id = run["analysis_id"]
        try:
            live_revision = self.repository.current_state_revision(user_id)
            if live_revision != expected_current_state_revision:
                raise ServiceError(
                    "SNAPSHOT_STALE",
                    "추천안 생성 이후 금융정보가 변경되었습니다.",
                    details={
                        "expected_revision": expected_current_state_revision,
                        "current_revision": live_revision,
                    },
                    http_status=409,
                )
            self._transition(
                analysis_id,
                "SNAPSHOT_BUILDING",
                details={"is_virtual": True, "base_snapshot_id": base_snapshot_id},
            )
            virtual_snapshot_id = f"snapshot-virtual-{uuid4()}"
            snapshot = self.tools.apply_actions_virtual(
                base_snapshot_id,
                actions=actions,
                new_snapshot_id=virtual_snapshot_id,
            )
            if snapshot.user_id != user_id:
                raise ServiceError(
                    "SNAPSHOT_NOT_FOUND",
                    "기준 금융 스냅샷을 찾을 수 없습니다.",
                    details={"snapshot_id": base_snapshot_id},
                    http_status=404,
                )
            self.repository.create_snapshot(
                user_id,
                as_of=snapshot.as_of,
                snapshot_id=virtual_snapshot_id,
                payload=snapshot.model_dump(mode="json"),
            )
            return self._analyze_snapshot(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot=snapshot,
                is_virtual=True,
                base_snapshot_id=base_snapshot_id,
                current_state_revision=live_revision,
                correlation_id=request_id,
            )
        except ServiceError as exc:
            return self._fail(analysis_id, exc)
        except Exception as exc:
            return self._fail_unexpected(analysis_id, exc)

    def _analyze_snapshot(
        self,
        *,
        analysis_id: str,
        user_id: str,
        snapshot: FinancialSnapshot,
        is_virtual: bool,
        base_snapshot_id: str | None,
        current_state_revision: str,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        self.repository.transition_analysis(
            analysis_id,
            "BASELINE_ANALYZING",
            message=STATUS_MESSAGES["BASELINE_ANALYZING"],
            snapshot_id=snapshot.snapshot_id,
            details={
                "is_virtual": is_virtual,
                "base_snapshot_id": base_snapshot_id,
            },
        )
        baseline = self.cashflow_service.analyze(snapshot.snapshot_id, seed=DEFAULT_SIMULATION_SEED)
        safe_to_spend = self.cashflow_service.safe_to_spend(
            snapshot.snapshot_id,
            protection_level=snapshot.preferences.protection_level,
            seed=DEFAULT_SIMULATION_SEED,
        )

        self._transition(analysis_id, "AGENT_INVESTIGATING")
        agent_state = self.investigator.investigate(
            analysis_id=analysis_id,
            snapshot_id=snapshot.snapshot_id,
            baseline_result=baseline,
        )
        self._transition(
            analysis_id,
            "PLAN_EVALUATING",
            details={
                "candidate_plan_count": len(agent_state["candidate_plans"]),
                "evaluated_plan_count": len(agent_state["evaluated_plans"]),
            },
        )
        self._transition(analysis_id, "REPORT_BUILDING")

        analysis_model = CashflowAnalysis.model_validate(baseline)
        presentation = jsonable(map_risk_presentation(analysis_model))
        recommendations: list[dict[str, Any]] = []
        if agent_state["recommended_plan"] is not None:
            recommendations.append(
                self.repository.save_recommendation(
                    analysis_id=analysis_id,
                    user_id=user_id,
                    payload={
                        **agent_state["recommended_plan"],
                        "snapshot_id": snapshot.snapshot_id,
                        "current_state_revision": current_state_revision,
                        "virtual_only": True,
                    },
                )
            )

        report = {
            "analysis_id": analysis_id,
            "snapshot_id": snapshot.snapshot_id,
            "base_snapshot_id": base_snapshot_id,
            "current_state_revision": current_state_revision,
            "is_virtual": is_virtual,
            "as_of": snapshot.as_of.isoformat(),
            "presentation": presentation,
            "risk_metrics": baseline["risk_metrics"],
            "cashflow": {
                "analysis_horizon_days": baseline["analysis_horizon_days"],
                "daily_positions": baseline["daily_positions"],
                "scenarios": baseline["scenarios"],
            },
            "safe_to_spend": safe_to_spend,
            "agent": agent_state,
            "recommendations": recommendations,
            "data_quality": snapshot.data_quality.model_dump(mode="json"),
            "versions": {
                "model_name": "flowguard-deterministic-core",
                "model_version": baseline.get("model_version", FINANCIAL_CORE_VERSION),
                "prompt_version": "rule-based-investigator-1",
                "ai_prompt_version": os.getenv(
                    "FLOWGUARD_AI_PROMPT_VERSION", "ai-interpretation-v1"
                ),
                "tool_version": baseline.get("tool_version"),
                "policy_version": POLICY_VERSION,
                "presentation_rule_version": RISK_RULE_VERSION,
                "simulation_seed": DEFAULT_SIMULATION_SEED,
            },
            "trace": {"correlation_id": correlation_id},
            "created_at": utc_now().isoformat(),
        }
        interpretation = self._interpret_analysis(
            analysis_id,
            user_id,
            report,
            current_state_revision=current_state_revision,
            correlation_id=correlation_id,
        )
        report["ai_interpretation"] = interpretation
        report["interpretation_status"] = interpretation["source"].upper()
        latest_report_eligible = (
            self.repository.current_state_revision(user_id) == current_state_revision
        )
        report["latest_report_eligible"] = latest_report_eligible
        self.repository.save_report(
            analysis_id=analysis_id,
            user_id=user_id,
            snapshot_id=snapshot.snapshot_id,
            payload=report,
        )
        completed = self.repository.transition_analysis(
            analysis_id,
            "COMPLETED",
            message=STATUS_MESSAGES["COMPLETED"],
            result={
                "report_available": True,
                "is_virtual": is_virtual,
                "base_snapshot_id": base_snapshot_id,
                "latest_report_promoted": latest_report_eligible,
                "interpretation_status": report["interpretation_status"],
            },
        )
        if latest_report_eligible:
            self.repository.promote_latest_report(user_id=user_id, analysis_id=analysis_id)
        return completed

    def _interpret_analysis(
        self,
        analysis_id: str,
        user_id: str,
        report: dict[str, Any],
        *,
        current_state_revision: str,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        payload = self._build_ai_request_payload(report, snapshot_revision=current_state_revision)
        if payload is None:
            return {
                "analysisId": analysis_id,
                "riskExplanation": "규칙 기반으로 위험을 요약했습니다.",
                "rankedActions": [],
                "userMessage": "AI 해석을 생략하고 규칙 기반 분석 결과를 제공합니다.",
                "source": "fallback",
                "fallbackReason": "no_risk_context",
                "correlationId": correlation_id,
            }
        return self.ai_client.interpret(
            analysis_id=analysis_id,
            payload=payload,
            user_id=user_id,
            snapshot_revision=current_state_revision,
            correlation_id=correlation_id,
            prompt_version=report["versions"].get("ai_prompt_version"),
        )

    def _build_ai_request_payload(
        self,
        report: dict[str, Any],
        *,
        snapshot_revision: str,
    ) -> dict[str, Any] | None:
        next_risk = self._build_next_risk_payload(report.get("risk_metrics", {}))
        cashflow_summary = self._build_cashflow_summary(report.get("cashflow", {}))
        calculated_at = report.get("created_at")
        safe_to_spend = report.get("safe_to_spend")
        if (
            calculated_at is None
            or not isinstance(safe_to_spend, int)
            or next_risk is None
            or cashflow_summary is None
        ):
            return None
        return {
            "snapshotRevision": snapshot_revision,
            "calculatedAt": calculated_at,
            "facts": {
                "safeToSpend": safe_to_spend,
                "nextRisk": next_risk,
                "cashflowSummary": cashflow_summary,
            },
            "evidence": report.get("agent", {}).get("gathered_evidence", []),
            "actionCandidates": self._build_action_candidates(report.get("agent", {})),
        }

    @staticmethod
    def _build_next_risk_payload(risk_metrics: dict[str, Any]) -> dict[str, Any] | None:
        risk_date = risk_metrics.get("first_risk_date")
        shortage_amount = risk_metrics.get("expected_gap_max")
        risk_type = risk_metrics.get("shortfall_type")
        if (
            not isinstance(risk_date, str)
            or not isinstance(shortage_amount, int)
            or shortage_amount <= 0
            or not isinstance(risk_type, str)
            or not risk_type
        ):
            return None
        return {
            "type": risk_type,
            "date": risk_date,
            "shortageAmount": shortage_amount,
        }

    @staticmethod
    def _build_cashflow_summary(cashflow: dict[str, Any]) -> dict[str, Any] | None:
        daily_positions = cashflow.get("daily_positions")
        if not isinstance(daily_positions, list) or not daily_positions:
            return None
        valid_positions = [
            position
            for position in daily_positions
            if isinstance(position.get("available_balance"), int)
            and isinstance(position.get("date"), str)
        ]
        if not valid_positions:
            return None
        lowest_position = min(valid_positions, key=lambda position: position["available_balance"])
        return {
            "lowestBalance": lowest_position["available_balance"],
            "lowestBalanceDate": lowest_position["date"],
        }

    @staticmethod
    def _build_action_candidates(agent_state: dict[str, Any]) -> list[dict[str, Any]]:
        candidates = agent_state.get("actionCandidates", [])
        if not isinstance(candidates, list):
            return []
        built_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            action_id = candidate.get("action_id")
            if not isinstance(action_id, str) or not action_id:
                continue
            built_candidates.append(
                {
                    "actionId": action_id,
                    "type": candidate.get("type"),
                    "amount": candidate.get("amount"),
                    "feasible": candidate.get("feasible"),
                }
            )
        return built_candidates

    @staticmethod
    def _metadata(
        *,
        is_virtual: bool,
        base_snapshot_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "model_name": "flowguard-deterministic-core",
            "model_version": FINANCIAL_CORE_VERSION,
            "prompt_version": "rule-based-investigator-1",
            "policy_version": POLICY_VERSION,
            "simulation_seed": DEFAULT_SIMULATION_SEED,
            "is_virtual": is_virtual,
            "base_snapshot_id": base_snapshot_id,
            "request_id": request_id,
        }

    def _fail_unexpected(self, analysis_id: str, exception: Exception) -> dict[str, Any]:
        error = ServiceError(
            "ANALYSIS_FAILED",
            "종합 금융분석을 완료하지 못했습니다.",
            details={"exception_type": type(exception).__name__},
            retryable=False,
            http_status=500,
        )
        return self._fail(analysis_id, error)

    def _transition(
        self,
        analysis_id: str,
        status: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.repository.transition_analysis(
            analysis_id,
            status,
            message=STATUS_MESSAGES[status],
            details=details,
        )

    def _fail(self, analysis_id: str, error: ServiceError) -> dict[str, Any]:
        try:
            return self.repository.transition_analysis(
                analysis_id,
                "FAILED",
                message=STATUS_MESSAGES["FAILED"],
                details={"error_code": error.code},
                error=error.to_dict(),
            )
        except InvalidAnalysisTransition:
            return self.repository.get_analysis(analysis_id)


class CashflowAnalysisService:
    def __init__(self, tools: CoreToolService) -> None:
        self.tools = tools

    def analyze(self, snapshot_id: str, *, seed: int) -> dict[str, Any]:
        return self.tools.simulate_cashflow(snapshot_id, seed=seed)

    def safe_to_spend(self, snapshot_id: str, *, protection_level: float, seed: int) -> int:
        return self.tools.calculate_safe_to_spend(
            snapshot_id,
            protection_level=protection_level,
            seed=seed,
        )


__all__ = [
    "AnalysisOrchestrator",
    "CashflowAnalysisService",
    "STATUS_MESSAGES",
]
