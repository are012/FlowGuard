"""Build a public, user-facing trace without exposing hidden model reasoning."""

from __future__ import annotations

from typing import Any

TRACE_DISCLOSURE = (
    "이 기록은 숨겨진 모델 추론이 아니라 확인한 근거, 도구 호출, "
    "후보 평가와 최종 선택 이유를 요약한 공개 감사 기록입니다."
)


def build_decision_trace(
    state: dict[str, Any],
    *,
    mode: str,
    model: str | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    """Create a compact trace from persisted, verifiable workflow artifacts."""

    steps: list[dict[str, Any]] = []
    sequence = 0

    def add_step(
        kind: str,
        title: str,
        summary: str,
        *,
        status: str = "COMPLETED",
        tool_name: str | None = None,
        candidate_id: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> None:
        nonlocal sequence
        sequence += 1
        steps.append(
            {
                "sequence": sequence,
                "kind": kind,
                "title": title,
                "summary": summary,
                "status": status,
                "tool_name": tool_name,
                "candidate_id": candidate_id,
                "evidence_ids": evidence_ids or [],
            }
        )

    risk = state.get("risk") or {}
    hypotheses = state.get("risk_hypotheses") or []
    if risk:
        risk_type = risk.get("type", "UNKNOWN")
        risk_date = risk.get("date", "미정")
        shortage = risk.get("shortageAmount")
        amount_text = f", 예상 부족액 {shortage:,}원" if isinstance(shortage, int) else ""
        add_step(
            "RISK_HYPOTHESIS",
            "위험 가설 수립",
            f"{risk_date}의 {risk_type} 위험{amount_text}을 우선 확인했습니다.",
            evidence_ids=[
                str(item)
                for hypothesis in hypotheses
                for item in (
                    hypothesis.get("triggering_event_ids")
                    or hypothesis.get("evidence_ids")
                    or []
                )
            ],
        )
    else:
        add_step(
            "RISK_HYPOTHESIS",
            "기준 위험 확인",
            "현재 스냅숏에서 대응이 필요한 유동성 부족을 찾지 못했습니다.",
        )

    for execution in state.get("tool_calls", []):
        tool_name = str(execution.get("tool_name", "unknown_tool"))
        has_error = bool(execution.get("error"))
        add_step(
            "TOOL_CALL",
            _tool_title(tool_name),
            _tool_summary(tool_name, has_error=has_error),
            status="FAILED" if has_error else "COMPLETED",
            tool_name=tool_name,
        )

    evaluated = state.get("actionCandidates") or []
    if not evaluated:
        evaluated = [
            {
                "action_id": item.get("plan_id"),
                "feasible": item.get("feasible"),
                "riskResolved": item.get("riskResolved"),
            }
            for item in state.get("evaluated_plans", [])
        ]
    for candidate in evaluated:
        candidate_id = candidate.get("action_id") or candidate.get("id")
        candidate_type = candidate.get("type") or "대응안"
        feasible = candidate.get("feasible") is True
        add_step(
            "CANDIDATE_EVALUATION",
            f"{candidate_type} 검증",
            (
                "금융 코어와 안전정책 검증을 통과했습니다."
                if feasible
                else "금융 코어 또는 안전정책 검증을 통과하지 못했습니다."
            ),
            status="COMPLETED" if feasible else "REJECTED",
            candidate_id=str(candidate_id) if candidate_id else None,
        )

    recommendation = state.get("recommended_plan")
    if recommendation:
        candidate_id = recommendation.get("action_id") or recommendation.get("id")
        add_step(
            "FINAL_SELECTION",
            "최종 대응안 선택",
            str(
                recommendation.get("rationale")
                or recommendation.get("summary")
                or "검증을 통과한 대응안 중 우선순위가 가장 높은 안을 선택했습니다."
            ),
            candidate_id=str(candidate_id) if candidate_id else None,
        )
    elif risk:
        add_step(
            "FINAL_SELECTION",
            "추천 보류",
            "현재 근거와 안전정책을 모두 만족하는 대응안을 확정하지 못했습니다.",
            status="NEEDS_REVIEW",
        )

    return {
        "mode": mode,
        "model": model,
        "status": "COMPLETED",
        "fallback_reason": fallback_reason,
        "disclosure": TRACE_DISCLOSURE,
        "steps": steps,
        "usage": None,
    }


def _tool_title(tool_name: str) -> str:
    return {
        "get_financial_context": "금융 맥락 조회",
        "get_counterparty_evidence": "거래처 지급 근거 조회",
        "query_financial_events": "위험 구간 이벤트 조회",
        "evaluate_action_plan": "대응안 가상 적용",
        "validate_financial_policy": "금융 안전정책 검증",
    }.get(tool_name, "금융 도구 실행")


def _tool_summary(tool_name: str, *, has_error: bool) -> str:
    if has_error:
        return "필요한 근거를 확인하지 못해 해당 결과를 추천 판단에서 제외했습니다."
    return {
        "get_financial_context": "계좌, 카드, 예정 수입, 필수지출과 보호자금을 확인했습니다.",
        "get_counterparty_evidence": "과거 지급 이력과 입금 지연 근거를 확인했습니다.",
        "query_financial_events": "위험일 전후의 수입·지출 이벤트를 확인했습니다.",
        "evaluate_action_plan": "대응안을 가상 적용해 적용 전후 현금흐름을 비교했습니다.",
        "validate_financial_policy": "보호자금과 최소잔액 등 금융 안전정책을 검증했습니다.",
    }.get(tool_name, "검증 가능한 금융 데이터를 조회했습니다.")


__all__ = ["TRACE_DISCLOSURE", "build_decision_trace"]
