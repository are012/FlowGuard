"""Synchronous, deterministic end-to-end analysis orchestration."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from threading import Event, Lock
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from flowguard.config import (
    AI_CONNECT_TIMEOUT_SECONDS,
    AI_CONTRACT_VERSION,
    AI_DEFAULT_LOCALE,
    AI_MAX_RETRIES,
    AI_PROMPT_VERSION,
    AI_RESPONSE_TIMEOUT_SECONDS,
    AI_SCHEMA_VERSION,
    AI_TOTAL_TIMEOUT_SECONDS,
    DEFAULT_SIMULATION_SEED,
    FINANCIAL_CORE_VERSION,
    POLICY_VERSION,
    RISK_RULE_VERSION,
)
from flowguard.core import map_recommendation_presentation, map_risk_presentation
from flowguard.domain import CashflowAnalysis, FinancialSnapshot
from flowguard.observability import log_event
from flowguard.storage import (
    FlowGuardRepository,
    InvalidAnalysisTransition,
    jsonable,
    utc_now,
)

from .agent_trace import build_decision_trace
from .analysis_support import AIInterpretationClient
from .errors import ServiceError
from .investigator import LiquidityInvestigator
from .snapshots import SnapshotBuilder
from .tools import CoreToolService

STATUS_MESSAGES = {
    "SNAPSHOT_BUILDING": "현재 금융정보로 불변 스냅숏을 생성하고 있습니다.",
    "BASELINE_ANALYZING": "13주 기준 현금흐름을 계산하고 있습니다.",
    "AGENT_INVESTIGATING": "유동성 위험의 원인과 근거를 조사하고 있습니다.",
    "PLAN_EVALUATING": "후보 대응안을 가상 적용하고 정책을 검증하고 있습니다.",
    "REPORT_BUILDING": "사용자용 분석 리포트를 만들고 있습니다.",
    "INTERPRETATION_REQUESTING": "검증된 결과의 AI 해석을 요청하고 있습니다.",
    "INTERPRETATION_VALIDATING": "AI 해석 응답의 계약과 후보를 검증하고 있습니다.",
    "COMPLETED": "분석이 완료되었습니다.",
    "FAILED": "분석에 실패했습니다.",
}
logger = logging.getLogger("flowguard.analysis")
_OMITTED_AS_OF = object()


@dataclass(slots=True)
class _AnalysisInFlight:
    completion: Event
    response: dict[str, Any] | None = None
    exception: BaseException | None = None


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
        self._in_flight_lock = Lock()
        self._in_flight: dict[tuple[str, str, str, object], _AnalysisInFlight] = {}
        self.ai_client = ai_client or AIInterpretationClient(
            base_url=os.getenv("FLOWGUARD_AI_SERVER_URL", "http://localhost:8001"),
            connect_timeout_seconds=float(
                os.getenv("FLOWGUARD_AI_CONNECT_TIMEOUT_SECONDS", AI_CONNECT_TIMEOUT_SECONDS)
            ),
            read_timeout_seconds=float(
                os.getenv("FLOWGUARD_AI_RESPONSE_TIMEOUT_SECONDS", AI_RESPONSE_TIMEOUT_SECONDS)
            ),
            total_timeout_seconds=float(
                os.getenv("FLOWGUARD_AI_TOTAL_TIMEOUT_SECONDS", AI_TOTAL_TIMEOUT_SECONDS)
            ),
            max_retries=int(os.getenv("FLOWGUARD_AI_MAX_RETRIES", AI_MAX_RETRIES)),
        )

    def run(
        self,
        user_id: str,
        *,
        trigger_type: str = "MANUAL",
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        request_key = (
            user_id,
            self.repository.current_state_revision(user_id),
            trigger_type,
            self._as_of_key(as_of),
        )
        with self._in_flight_lock:
            pending = self._in_flight.get(request_key)
            if pending is None:
                pending = _AnalysisInFlight(completion=Event())
                self._in_flight[request_key] = pending
                owns_request = True
            else:
                owns_request = False

        if not owns_request:
            pending.completion.wait()
            with self._in_flight_lock:
                response = pending.response
                exception = pending.exception
            if exception is not None:
                raise exception
            if response is None:
                raise RuntimeError("analysis owner completed without a response")
            return response

        try:
            response = self._run_once(user_id, trigger_type=trigger_type, as_of=as_of)
            with self._in_flight_lock:
                pending.response = response
            return response
        except BaseException as exc:
            with self._in_flight_lock:
                pending.exception = exc
            raise
        finally:
            with self._in_flight_lock:
                self._in_flight.pop(request_key, None)
                pending.completion.set()

    @staticmethod
    def _as_of_key(as_of: datetime | None) -> object:
        if as_of is None:
            return _OMITTED_AS_OF
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            return as_of.isoformat()
        return as_of.astimezone(ZoneInfo("Asia/Seoul")).isoformat()

    def _run_once(
        self,
        user_id: str,
        *,
        trigger_type: str,
        as_of: datetime | None,
    ) -> dict[str, Any]:
        analysis_revision: str | None = None
        run = self.repository.create_analysis(
            user_id,
            trigger_type=trigger_type,
            metadata=self._metadata(is_virtual=False),
        )
        analysis_id = run["analysis_id"]
        log_event(
            logger,
            logging.INFO,
            "analysis_started",
            analysis_id=analysis_id,
            trigger_type=trigger_type,
            is_virtual=False,
        )
        try:
            self._transition(analysis_id, "SNAPSHOT_BUILDING")
            snapshot, current_state_revision = self.snapshot_builder.build_with_revision(
                user_id, as_of=as_of
            )
            analysis_revision = current_state_revision
            result = self._analyze_snapshot(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot=snapshot,
                is_virtual=False,
                base_snapshot_id=None,
                current_state_revision=current_state_revision,
            )
        except ServiceError as exc:
            result = self._fail(analysis_id, exc)
        except Exception as exc:
            result = self._fail_unexpected(analysis_id, exc)
        response = self._with_revision_contract(
            user_id,
            result,
            analysis_revision=analysis_revision,
        )
        log_event(
            logger,
            logging.INFO,
            "analysis_finished",
            analysis_id=analysis_id,
            analysis_status=str(response.get("analysis_status", response.get("status"))),
            interpretation_status=str(response.get("interpretation_status", "NOT_REQUESTED")),
            snapshot_revision=analysis_revision,
            is_virtual=False,
        )
        return response

    def run_virtual(
        self,
        user_id: str,
        *,
        base_snapshot_id: str,
        actions: list[dict[str, Any]],
        expected_current_state_revision: str,
    ) -> dict[str, Any]:
        """Apply approved actions to a new snapshot and fully reanalyze it.

        Mutable current records are intentionally untouched.
        """

        run = self.repository.create_analysis(
            user_id,
            trigger_type="RECOMMENDATION_APPROVAL",
            metadata=self._metadata(is_virtual=True, base_snapshot_id=base_snapshot_id),
        )
        analysis_id = run["analysis_id"]
        log_event(
            logger,
            logging.INFO,
            "analysis_started",
            analysis_id=analysis_id,
            trigger_type="RECOMMENDATION_APPROVAL",
            is_virtual=True,
        )
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
                    "기준 금융 스냅숏을 찾을 수 없습니다.",
                    details={"snapshot_id": base_snapshot_id},
                    http_status=404,
                )
            self.repository.create_snapshot(
                user_id,
                as_of=snapshot.as_of,
                snapshot_id=virtual_snapshot_id,
                payload=snapshot.model_dump(mode="json"),
            )
            result = self._analyze_snapshot(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot=snapshot,
                is_virtual=True,
                base_snapshot_id=base_snapshot_id,
                current_state_revision=live_revision,
            )
        except ServiceError as exc:
            result = self._fail(analysis_id, exc)
        except Exception as exc:
            result = self._fail_unexpected(analysis_id, exc)
        log_event(
            logger,
            logging.INFO,
            "analysis_finished",
            analysis_id=analysis_id,
            analysis_status=str(result.get("analysis_status", result.get("status"))),
            interpretation_status=str(result.get("interpretation_status", "NOT_REQUESTED")),
            snapshot_revision=result.get("snapshot_revision"),
            is_virtual=True,
        )
        return result

    def _analyze_snapshot(
        self,
        *,
        analysis_id: str,
        user_id: str,
        snapshot: FinancialSnapshot,
        is_virtual: bool,
        base_snapshot_id: str | None,
        current_state_revision: str,
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
        if "decision_trace" not in agent_state:
            agent_state["decision_trace"] = build_decision_trace(
                agent_state,
                mode=getattr(self.investigator, "agent_mode", "DETERMINISTIC"),
                model=getattr(self.investigator, "agent_model", None),
                fallback_reason=getattr(self.investigator, "fallback_reason", None),
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
            recommendation_presentation = map_recommendation_presentation(
                agent_state["recommended_plan"],
                analysis_model.risk_metrics,
            )
            derivation, comparison_candidates = self._recommendation_explanation(
                agent_state=agent_state,
                baseline=baseline,
                snapshot=snapshot,
                current_state_revision=current_state_revision,
            )
            recommendations.append(
                self.repository.save_recommendation(
                    analysis_id=analysis_id,
                    user_id=user_id,
                    payload={
                        **agent_state["recommended_plan"],
                        **recommendation_presentation,
                        "snapshot_id": snapshot.snapshot_id,
                        "current_state_revision": current_state_revision,
                        "virtual_only": True,
                        "derivation": derivation,
                        "comparison_candidates": comparison_candidates,
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
                "agent_mode": getattr(self.investigator, "agent_mode", "DETERMINISTIC"),
                "agent_model": getattr(self.investigator, "agent_model", None),
                "agent_prompt_version": getattr(
                    self.investigator,
                    "prompt_version",
                    "rule-based-investigator-1",
                ),
                "tool_version": baseline.get("tool_version"),
                "policy_version": POLICY_VERSION,
                "presentation_rule_version": RISK_RULE_VERSION,
                "simulation_seed": DEFAULT_SIMULATION_SEED,
            },
            "created_at": utc_now().isoformat(),
        }
        interpretation_request = self._build_ai_request_payload(
            analysis_id=analysis_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_revision=current_state_revision,
            report=report,
        )
        agent_state["interpretation_request_id"] = (
            interpretation_request.get("requestId") if interpretation_request else None
        )
        self.repository.save_report(
            analysis_id=analysis_id,
            user_id=user_id,
            snapshot_id=snapshot.snapshot_id,
            payload=report,
        )
        interpretation = self._interpret_analysis(interpretation_request)
        if interpretation.pop("_pending", False):
            return self.repository.get_analysis(analysis_id)
        return self.repository.complete_analysis_report(
            user_id=user_id,
            analysis_id=analysis_id,
            snapshot_revision=current_state_revision,
            message=STATUS_MESSAGES["COMPLETED"],
            result={
                "report_stored": True,
                "is_virtual": is_virtual,
                "base_snapshot_id": base_snapshot_id,
                "interpretation_status": interpretation["status"],
            },
            interpretation_id=interpretation.get("interpretation_id"),
        )

    def _with_revision_contract(
        self,
        user_id: str,
        analysis: dict[str, Any],
        *,
        analysis_revision: str | None,
    ) -> dict[str, Any]:
        revision = self.repository.current_state_revision(user_id)
        analysis_status = analysis.get("analysis_status")
        is_stale = analysis_revision is not None and analysis_revision != revision
        return {
            **analysis,
            "revision": revision,
            "analysis_revision": analysis_revision,
            "report_revision": analysis_revision,
            "latest_data_revision": revision,
            "is_stale": is_stale,
            "refresh_status": analysis_status,
            "analysis_required": (
                analysis_status not in {"SUCCEEDED", "SUPERSEDED"}
                or analysis_revision is None
                or is_stale
            ),
        }

    @staticmethod
    def _recommendation_explanation(
        *,
        agent_state: dict[str, Any],
        baseline: dict[str, Any],
        snapshot: FinancialSnapshot,
        current_state_revision: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        recommendation = agent_state["recommended_plan"]
        selected_id = str(recommendation.get("action_id") or recommendation.get("id"))
        metrics = baseline["risk_metrics"]
        recommendation_type = recommendation.get("type")
        recommendation_amount = recommendation.get("amount")
        if not isinstance(recommendation_amount, int):
            recommendation_amount = metrics["expected_gap_max"]
        amount_comes_from_event = recommendation_type == "DELAY_PURCHASE"
        evidence_ids = [
            *metrics.get("triggering_event_ids", []),
            *recommendation.get("source_evidence_ids", []),
        ]
        derivation = {
            "amount": recommendation_amount,
            "currency": "KRW",
            "source_field": (
                "scheduled_events[].amount"
                if amount_comes_from_event
                else "risk_metrics.expected_gap_max"
            ),
            "source_value": recommendation_amount,
            "formula": (
                "조정 가능한 구매 예정 금액을 사용합니다."
                if amount_comes_from_event
                else "예상 최대 부족액만큼 결제 재원을 보충합니다."
            ),
            "evidence_ids": list(dict.fromkeys(str(item) for item in evidence_ids)),
            "snapshot_id": snapshot.snapshot_id,
            "revision": current_state_revision,
            "tool_version": baseline.get("tool_version"),
            "policy_version": POLICY_VERSION,
        }
        comparison_candidates = [
            AnalysisOrchestrator._comparison_candidate(
                candidate,
                selected_id=selected_id,
            )
            for candidate in agent_state.get("actionCandidates", [])
        ]
        return derivation, comparison_candidates

    @staticmethod
    def _comparison_candidate(
        candidate: dict[str, Any],
        *,
        selected_id: str,
    ) -> dict[str, Any]:
        evaluation = candidate.get("evaluation") or {}
        policy = candidate.get("policy_result") or {}
        risk_shift = evaluation.get("risk_shift")
        raw_violations = [
            *evaluation.get("policy_violations", []),
            *policy.get("violations", []),
        ]
        policy_violations: list[dict[str, Any]] = []
        seen_violations: set[tuple[Any, Any, Any]] = set()
        for violation in raw_violations:
            identity = (
                violation.get("code"),
                violation.get("message"),
                violation.get("action_id"),
            )
            if identity in seen_violations:
                continue
            seen_violations.add(identity)
            policy_violations.append(violation)
        feasible = candidate.get("feasible") is True
        rejection_reason = None
        if not feasible:
            if policy_violations:
                rejection_reason = str(
                    policy_violations[0].get("message") or policy_violations[0].get("code")
                )
            elif isinstance(risk_shift, dict) and risk_shift.get("detected"):
                reasons = risk_shift.get("reasons") or []
                rejection_reason = (
                    ", ".join(str(reason) for reason in reasons)
                    or "이후 기간에 새로운 위험이 발생합니다."
                )
            else:
                rejection_reason = "가상 적용 또는 금융 안전정책 검증을 통과하지 못했습니다."
        candidate_id = str(candidate.get("action_id") or candidate.get("id"))
        return {
            "candidate_id": candidate_id,
            "type": candidate.get("type"),
            "amount": candidate.get("amount"),
            "selected": candidate_id == selected_id,
            "feasible": feasible,
            "after_expected_gap_max": (
                evaluation.get("after", {}).get("risk_metrics", {}).get("expected_gap_max")
            ),
            "risk_shift": risk_shift,
            "policy_violations": policy_violations,
            "rejection_reason": rejection_reason,
        }

    def _interpret_analysis(
        self,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if payload is None:
            return {
                "status": "NOT_REQUESTED",
                "interpretation_id": None,
                "attempt_count": 0,
                "fallback_used": False,
                "latency_ms": 0,
                "response_payload": None,
                "error_code": None,
            }
        run: dict[str, Any] | None = None
        try:
            run, created = self.repository.create_or_get_interpretation_run(
                analysis_id=payload["analysisId"],
                snapshot_id=payload["snapshotId"],
                snapshot_revision=payload["snapshotRevision"],
                request_id=payload["requestId"],
                idempotency_key=payload["idempotencyKey"],
                contract_version=payload["contractVersion"],
                prompt_version=payload["promptVersion"],
                model_name=os.getenv("FLOWGUARD_AI_MODEL_NAME", "gpt-5.6-luna"),
            )
            if not created:
                log_event(
                    logger,
                    logging.INFO,
                    "ai_interpretation_reused",
                    analysis_id=payload["analysisId"],
                    ai_request_id=payload["requestId"],
                    interpretation_status=str(run["status"]),
                )
                return {
                    **run,
                    "_pending": run["status"] in {"QUEUED", "RUNNING"},
                }
            self._transition(payload["analysisId"], "INTERPRETATION_REQUESTING")
            self.repository.update_interpretation_run(
                run["interpretation_id"],
                status="RUNNING",
            )
            outcome = self.ai_client.interpret(payload)
            self._transition(payload["analysisId"], "INTERPRETATION_VALIDATING")
            updated = self.repository.update_interpretation_run(
                run["interpretation_id"],
                status=outcome.status,
                attempt_count=outcome.attempt_count,
                fallback_used=outcome.fallback_used,
                latency_ms=outcome.latency_ms,
                response_payload=outcome.response_payload,
                error_code=outcome.error_code,
            )
            log_event(
                logger,
                logging.INFO if outcome.status == "SUCCEEDED" else logging.WARNING,
                "ai_interpretation_finished",
                analysis_id=payload["analysisId"],
                ai_request_id=payload["requestId"],
                interpretation_status=outcome.status,
                attempt_count=outcome.attempt_count,
                latency_ms=outcome.latency_ms,
                error_code=outcome.error_code,
            )
            return updated
        except Exception as exc:
            error_code = (
                "interpretation_internal_error"
                if run is not None
                else "interpretation_persistence_error"
            )
            log_event(
                logger,
                logging.ERROR,
                "ai_interpretation_failed",
                analysis_id=payload["analysisId"],
                ai_request_id=payload["requestId"],
                error_code=error_code,
                exception_type=type(exc).__name__,
            )
            failed = {
                "status": "FAILED",
                "interpretation_id": run.get("interpretation_id") if run else None,
                "attempt_count": 0,
                "fallback_used": False,
                "latency_ms": 0,
                "response_payload": None,
                "error_code": error_code,
            }
            if run is not None:
                try:
                    return self.repository.update_interpretation_run(
                        run["interpretation_id"],
                        status="FAILED",
                        attempt_count=0,
                        fallback_used=False,
                        latency_ms=0,
                        error_code=error_code,
                    )
                except Exception:
                    pass
            return failed

    def _build_ai_request_payload(
        self,
        *,
        analysis_id: str,
        snapshot_id: str,
        snapshot_revision: str,
        report: dict[str, Any],
    ) -> dict[str, Any] | None:
        next_risk = self._build_next_risk_payload(report.get("risk_metrics", {}))
        cashflow_summary = self._build_cashflow_summary(report.get("cashflow", {}))
        calculated_at = report.get("created_at")
        safe_to_spend_payload = report.get("safe_to_spend")
        safe_to_spend = (
            safe_to_spend_payload
            if isinstance(safe_to_spend_payload, int)
            else safe_to_spend_payload.get("safe_to_spend")
            if isinstance(safe_to_spend_payload, dict)
            else None
        )
        if (
            calculated_at is None
            or not isinstance(safe_to_spend, int)
            or next_risk is None
            or cashflow_summary is None
        ):
            return None
        locale = os.getenv("FLOWGUARD_AI_LOCALE", AI_DEFAULT_LOCALE)
        idempotency_key = (
            f"{analysis_id}:{snapshot_revision}:contract-{AI_CONTRACT_VERSION}:"
            f"prompt-{AI_PROMPT_VERSION}:{locale}"
        )
        request_id = f"ai-request-{uuid5(NAMESPACE_URL, idempotency_key)}"
        return {
            "schemaVersion": AI_SCHEMA_VERSION,
            "contractVersion": AI_CONTRACT_VERSION,
            "promptVersion": AI_PROMPT_VERSION,
            "requestId": request_id,
            "idempotencyKey": idempotency_key,
            "analysisId": analysis_id,
            "snapshotId": snapshot_id,
            "snapshotRevision": snapshot_revision,
            "locale": locale,
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
            policy = candidate.get("policy_result") or {}
            evaluation = candidate.get("evaluation") or {}
            if (
                not isinstance(action_id, str)
                or not action_id
                or candidate.get("feasible") is not True
                or evaluation.get("valid") is not True
                or policy.get("valid") is not True
            ):
                continue
            built_candidates.append(
                {
                    "actionId": action_id,
                    "type": candidate.get("type"),
                    "amount": candidate.get("amount"),
                    "feasible": True,
                }
            )
        return built_candidates

    def _metadata(
        self,
        *,
        is_virtual: bool,
        base_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "model_name": "flowguard-deterministic-core",
            "model_version": FINANCIAL_CORE_VERSION,
            "prompt_version": "rule-based-investigator-1",
            "agent_mode": getattr(self.investigator, "agent_mode", "DETERMINISTIC"),
            "agent_model": getattr(self.investigator, "agent_model", None),
            "agent_prompt_version": getattr(
                self.investigator,
                "prompt_version",
                "rule-based-investigator-1",
            ),
            "policy_version": POLICY_VERSION,
            "simulation_seed": DEFAULT_SIMULATION_SEED,
            "is_virtual": is_virtual,
            "base_snapshot_id": base_snapshot_id,
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
        log_event(
            logger,
            logging.ERROR,
            "analysis_failed",
            analysis_id=analysis_id,
            error_code=error.code,
            status_code=error.http_status,
            retryable=error.retryable,
        )
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
