"""Recommendation decision audit and virtual-only application services."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from flowguard.storage import FlowGuardRepository, RecordNotFound, StorageConflict

from .analysis import AnalysisOrchestrator
from .errors import ServiceError, not_found
from .tools import CoreToolService


class RecommendationService:
    def __init__(
        self,
        repository: FlowGuardRepository,
        tools: CoreToolService,
        analyses: AnalysisOrchestrator | None = None,
    ) -> None:
        self.repository = repository
        self.tools = tools
        self.analyses = analyses or AnalysisOrchestrator(repository, tools=tools)

    def list(self, user_id: str) -> list[dict[str, Any]]:
        try:
            latest = self.repository.latest_report(user_id)
        except RecordNotFound:
            return []
        recommendation_ids = {
            item["recommendation_id"]
            for item in latest.get("recommendations", [])
            if item.get("recommendation_id")
        }
        return [
            item
            for item in self.repository.list_recommendations(user_id)
            if item["recommendation_id"] in recommendation_ids and item["status"] == "PENDING"
        ]

    def get(self, user_id: str, recommendation_id: str) -> dict[str, Any]:
        try:
            return self.repository.get_recommendation(user_id, recommendation_id)
        except RecordNotFound as exc:
            raise not_found("recommendation", recommendation_id) from exc

    def approve(
        self,
        user_id: str,
        recommendation_id: str,
        *,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        recommendation = self.get(user_id, recommendation_id)
        self._ensure_current(user_id, recommendation)
        actions = recommendation.get("actions", [])
        evaluation = self.tools.evaluate_action_plan(
            recommendation["snapshot_id"], actions=actions, seed=42
        )
        policy = self.tools.validate_financial_policy(
            recommendation["snapshot_id"],
            actions=actions,
            evaluation_result=evaluation,
        )
        if (
            evaluation.get("valid") is not True
            or policy.get("valid") is not True
            or evaluation.get("risk_shift", {}).get("detected") is True
        ):
            raise ServiceError(
                "POLICY_VALIDATION_FAILED",
                "최신 데이터에서 추천안을 안전하게 가상 적용할 수 없습니다.",
                details={
                    "policy_violations": policy.get("violations", []),
                    "risk_shift": evaluation.get("risk_shift", {}),
                },
                http_status=409,
            )
        expected_revision = recommendation.get("current_state_revision")
        if not isinstance(expected_revision, str):
            raise ServiceError(
                "SNAPSHOT_STALE",
                "추천안에 현재 상태 revision이 없어 다시 분석해야 합니다.",
                http_status=409,
            )
        virtual_analysis = self.analyses.run_virtual(
            user_id,
            base_snapshot_id=recommendation["snapshot_id"],
            actions=actions,
            expected_current_state_revision=expected_revision,
            request_id=request_id,
        )
        if virtual_analysis["status"] != "COMPLETED":
            raise ServiceError(
                "ANALYSIS_FAILED",
                "추천안 가상 적용 후 종합분석에 실패했습니다.",
                details={
                    "analysis_id": virtual_analysis["analysis_id"],
                    "error": virtual_analysis.get("error"),
                },
                http_status=422,
            )
        latest = self.repository.latest_report(user_id)
        if latest["analysis_id"] != virtual_analysis["analysis_id"]:
            raise ServiceError(
                "ANALYSIS_FAILED",
                "가상 적용 리포트를 확인할 수 없습니다.",
                http_status=500,
            )
        try:
            decided = self.repository.record_recommendation_decision(
                user_id,
                recommendation_id,
                decision="APPROVED",
                details={
                    "reason": reason,
                    "confirmed_virtual_only": True,
                    "external_actions_executed": False,
                    "evaluation": evaluation,
                    "policy": policy,
                },
            )
        except StorageConflict as exc:
            raise ServiceError(
                "ACTION_NOT_FEASIBLE",
                "이미 최종 결정된 추천안입니다.",
                http_status=409,
            ) from exc
        return {
            "recommendation": decided,
            "virtual_application": {
                "before": evaluation["before"],
                "after": evaluation["after"],
                "risk_shift": evaluation["risk_shift"],
                "external_actions_executed": False,
            },
            "virtual_analysis": virtual_analysis,
            "virtual_report": latest,
        }

    def reject(
        self,
        user_id: str,
        recommendation_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.get(user_id, recommendation_id)
        try:
            return self.repository.record_recommendation_decision(
                user_id,
                recommendation_id,
                decision="REJECTED",
                details={"reason": reason, "external_actions_executed": False},
            )
        except StorageConflict as exc:
            raise ServiceError(
                "ACTION_NOT_FEASIBLE",
                "이미 최종 결정된 추천안입니다.",
                http_status=409,
            ) from exc

    def alternatives(
        self,
        user_id: str,
        recommendation_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        recommendation = self.get(user_id, recommendation_id)
        if recommendation["status"] != "PENDING":
            raise ServiceError(
                "ACTION_NOT_FEASIBLE",
                "최종 결정된 추천안에는 다른 대응안을 요청할 수 없습니다.",
                http_status=409,
            )
        self.repository.record_recommendation_decision(
            user_id,
            recommendation_id,
            decision="ALTERNATIVES_REQUESTED",
            details={"reason": reason, "external_actions_executed": False},
        )
        return {
            "recommendation_id": recommendation_id,
            "alternatives": recommendation.get("alternatives", [])[:2],
            "alternative_state": (
                "AVAILABLE" if recommendation.get("alternatives") else "NO_SAFE_ALTERNATIVE"
            ),
            "message": (
                None
                if recommendation.get("alternatives")
                else "현재 데이터에서 정책 검증을 통과한 다른 대응안이 없습니다."
            ),
            "virtual_only": True,
        }

    def installment_precheck(
        self,
        user_id: str,
        *,
        purchase_amount: int,
        installment_months: int,
        first_payment_date: date,
        card_id: str,
    ) -> dict[str, Any]:
        try:
            latest = self.repository.latest_report(user_id)
        except RecordNotFound as exc:
            raise ServiceError(
                "SNAPSHOT_NOT_FOUND",
                "할부 사전점검 전에 종합 분석이 필요합니다.",
                http_status=404,
            ) from exc
        action = {
            "action_id": f"action-{uuid4()}",
            "type": "add_installment",
            "parameters": {
                "purchase_amount": purchase_amount,
                "installment_months": installment_months,
                "first_payment_date": first_payment_date.isoformat(),
                "card_id": card_id,
            },
            "requires_user_approval": True,
            "assumptions": ["카드 수수료 없이 원금 균등 분할을 사용합니다."],
            "source_evidence_ids": [card_id],
        }
        evaluation = self.tools.evaluate_action_plan(
            latest["snapshot_id"], actions=[action], seed=42
        )
        policy = self.tools.validate_financial_policy(
            latest["snapshot_id"],
            actions=[action],
            evaluation_result=evaluation,
        )
        return {
            "snapshot_id": latest["snapshot_id"],
            "action": action,
            "evaluation": evaluation,
            "policy": policy,
            "virtual_only": True,
            "external_actions_executed": False,
        }

    def _ensure_current(self, user_id: str, recommendation: dict[str, Any]) -> None:
        try:
            latest = self.repository.latest_report(user_id)
        except RecordNotFound as exc:
            raise ServiceError(
                "SNAPSHOT_STALE",
                "최신 성공 분석이 없어 추천안을 승인할 수 없습니다.",
                http_status=409,
            ) from exc
        if latest["analysis_id"] != recommendation["analysis_id"]:
            raise ServiceError(
                "SNAPSHOT_STALE",
                "오래된 분석의 추천안입니다. 최신 분석에서 다시 확인하세요.",
                details={
                    "recommendation_analysis_id": recommendation["analysis_id"],
                    "latest_analysis_id": latest["analysis_id"],
                },
                http_status=409,
            )
        expected_revision = recommendation.get("current_state_revision")
        current_revision = self.repository.current_state_revision(user_id)
        if expected_revision != current_revision:
            raise ServiceError(
                "SNAPSHOT_STALE",
                "추천안 생성 이후 금융정보가 변경되었습니다.",
                details={
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
                http_status=409,
            )
        if recommendation["status"] != "PENDING":
            raise ServiceError(
                "ACTION_NOT_FEASIBLE",
                "이미 최종 결정된 추천안입니다.",
                http_status=409,
            )
