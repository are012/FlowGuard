"""Rules that map internal risk numbers to stable user-facing states."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flowguard.config import (
    RISK_ACT_NOW_DAYS_THRESHOLD,
    RISK_ACT_NOW_PROBABILITY_THRESHOLD,
    RISK_PREPARE_DAYS_THRESHOLD,
    RISK_PREPARE_PROBABILITY_THRESHOLD,
    RISK_RULE_VERSION,
    RISK_VERIFY_CONFIDENCE_THRESHOLD,
)
from flowguard.domain import (
    CashflowAnalysis,
    RiskMetrics,
    RiskPresentation,
    RiskStatus,
    ShortfallType,
)

_STATUS_LABELS = {
    RiskStatus.STABLE: "현재는 안전해요",
    RiskStatus.VERIFY: "확인할 정보가 있어요",
    RiskStatus.PREPARE: "미리 준비가 필요해요",
    RiskStatus.ACT_NOW: "지금 조치가 필요해요",
}


def select_risk_status(metrics: RiskMetrics) -> RiskStatus:
    """Apply the documented four-state rule to any risk-metric window."""

    if metrics.first_risk_date is None:
        if (
            metrics.requires_verification
            or metrics.data_confidence < RISK_VERIFY_CONFIDENCE_THRESHOLD
        ):
            return RiskStatus.VERIFY
        return RiskStatus.STABLE

    days = metrics.days_until_risk if metrics.days_until_risk is not None else 0
    if metrics.shortfall_probability >= RISK_ACT_NOW_PROBABILITY_THRESHOLD or (
        metrics.has_essential_risk and days <= RISK_ACT_NOW_DAYS_THRESHOLD
    ):
        return RiskStatus.ACT_NOW
    if (
        metrics.shortfall_probability >= RISK_PREPARE_PROBABILITY_THRESHOLD
        or days <= RISK_PREPARE_DAYS_THRESHOLD
    ):
        return RiskStatus.PREPARE
    return RiskStatus.VERIFY


def map_recommendation_presentation(
    recommendation: Mapping[str, Any],
    metrics: RiskMetrics,
) -> dict[str, str]:
    """Build deterministic user-facing copy for every saved recommendation."""

    recommendation_type = str(recommendation.get("type") or "")
    raw_amount = recommendation.get("amount")
    amount = raw_amount if isinstance(raw_amount, int) else metrics.expected_gap_max
    amount_phrase = f"{amount:,}원" if amount > 0 else "필요한 금액"
    risk_date_phrase = (
        f"{metrics.first_risk_date.isoformat()} 결제 전에"
        if metrics.first_risk_date is not None
        else "다음 결제 전에"
    )

    if recommendation_type == "TRANSFER":
        return {
            "title": f"결제계좌에 {amount_phrase}을 미리 옮기세요",
            "summary": f"{risk_date_phrase} 결제계좌의 부족 가능성을 줄이는 우선 추천안입니다.",
            "rationale": (
                f"예상 최대 부족액 {amount_phrase}을 기준으로 다른 가용계좌의 자금을 "
                "결제계좌로 옮기면 예정된 결제를 안전잔액 안에서 준비할 수 있습니다."
            ),
        }
    if recommendation_type == "PAUSE_SAVINGS":
        return {
            "title": "조정 가능한 저축 이체를 잠시 멈추세요",
            "summary": f"{risk_date_phrase} 가용자금을 확보하는 우선 추천안입니다.",
            "rationale": (
                "필수지출은 유지하고 사용자가 조정 가능하다고 표시한 저축 일정만 "
                "잠시 미루면 부족 가능성을 낮출 수 있습니다."
            ),
        }
    if recommendation_type == "DELAY_PURCHASE":
        return {
            "title": f"예정된 {amount_phrase} 구매를 늦추세요",
            "summary": f"{risk_date_phrase} 필수지출 재원을 먼저 확보하는 우선 추천안입니다.",
            "rationale": (
                "미확정이고 조정 가능한 구매 일정만 뒤로 옮겨 필수지출에 필요한 "
                "가용자금을 보존할 수 있습니다."
            ),
        }
    if recommendation_type == "ADJUST_DISCRETIONARY_BUDGET":
        return {
            "title": f"선택지출 예산을 {amount_phrase} 조정하세요",
            "summary": f"{risk_date_phrase} 필수지출 재원을 확보하는 우선 추천안입니다.",
            "rationale": (
                f"예상 최대 부족액 {amount_phrase}만큼 선택지출 한도를 조정하면 "
                "필수지출과 보호자금을 유지할 수 있습니다."
            ),
        }
    return {
        "title": "현금흐름 대응안을 검토하세요",
        "summary": f"{risk_date_phrase} 부족 가능성을 줄이기 위한 우선 추천안입니다.",
        "rationale": "금융 코어 계산과 안전정책 검증을 통과한 대응안입니다.",
    }


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.8:
        return "분석 신뢰도 높음"
    if confidence >= 0.6:
        return "분석 신뢰도 보통"
    return "예정 수입을 확인할수록 분석이 정밀해져요"


def map_risk_presentation(analysis: CashflowAnalysis) -> RiskPresentation:
    """Convert internal metrics into the four documented user states."""

    metrics = analysis.risk_metrics
    status = select_risk_status(metrics)
    if status == RiskStatus.STABLE:
        return RiskPresentation(
            status=status,
            status_label=_STATUS_LABELS[status],
            title="향후 13주 현금흐름이 안정적입니다",
            impact="예상된 필수지출과 안전잔액을 유지할 수 있습니다",
            cause="현재 확인된 유동성 부족이 없습니다",
            recommended_action="현재 계획을 유지하세요",
            confidence_label=_confidence_label(metrics.data_confidence),
            rule_version=RISK_RULE_VERSION,
        )
    if metrics.first_risk_date is None:
        return RiskPresentation(
            status=status,
            status_label=_STATUS_LABELS[status],
            title="분석에 필요한 정보를 확인하세요",
            impact="일부 금융정보가 없거나 최신 상태가 아닙니다",
            cause="데이터 신뢰도가 충분하지 않습니다",
            recommended_action="예정 수입과 최근 금융정보를 확인하세요",
            confidence_label=_confidence_label(metrics.data_confidence),
            rule_version=RISK_RULE_VERSION,
        )

    days = metrics.days_until_risk or 0
    date_phrase = "오늘" if days == 0 else f"{days}일 뒤"
    if metrics.expected_gap_min == metrics.expected_gap_max:
        impact = f"약 {metrics.expected_gap_max:,}원이 부족할 수 있습니다"
    else:
        impact = (
            f"약 {metrics.expected_gap_min:,}~{metrics.expected_gap_max:,}원이 부족할 수 있습니다"
        )
    if metrics.shortfall_type == ShortfallType.PAYMENT_ACCOUNT:
        cause = "전체 자금은 있지만 결제계좌의 가용잔액이 부족할 수 있습니다"
        action = "결제계좌에 필요한 금액을 미리 확보하세요"
    else:
        cause = "비보호 가용자금을 합쳐도 필수지출이 부족할 수 있습니다"
        action = "선택지출 조정이나 구매 연기를 검토하세요"
    return RiskPresentation(
        status=status,
        status_label=_STATUS_LABELS[status],
        title=f"{date_phrase} 필요한 자금을 준비하세요",
        impact=impact,
        cause=cause,
        recommended_action=action,
        confidence_label=_confidence_label(metrics.data_confidence),
        rule_version=RISK_RULE_VERSION,
    )
