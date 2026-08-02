from __future__ import annotations

from flowguard.services.agent_trace import TRACE_DISCLOSURE, build_decision_trace


def test_deterministic_trace_shape_and_copy_are_unchanged() -> None:
    state = {
        "risk": {
            "type": "PAYMENT_ACCOUNT",
            "date": "2026-08-25",
            "shortageAmount": 250_000,
        },
        "risk_hypotheses": [
            {
                "source": "baseline_result.risk_metrics",
                "triggering_event_ids": ["event-1"],
            }
        ],
        "tool_calls": [
            {"tool_name": "get_financial_context"},
            {"tool_name": "validate_financial_policy", "error": {"code": "unsafe"}},
        ],
        "actionCandidates": [
            {"action_id": "action-1", "type": "TRANSFER", "feasible": True},
            {
                "action_id": "action-2",
                "type": "ADJUST_DISCRETIONARY_BUDGET",
                "feasible": False,
            },
        ],
        "recommended_plan": {
            "action_id": "action-1",
            "rationale": "안전정책을 충족하는 첫 번째 대응안입니다.",
        },
    }

    assert build_decision_trace(state, mode="DETERMINISTIC") == {
        "mode": "DETERMINISTIC",
        "model": None,
        "status": "COMPLETED",
        "fallback_reason": None,
        "disclosure": TRACE_DISCLOSURE,
        "steps": [
            {
                "sequence": 1,
                "kind": "RISK_HYPOTHESIS",
                "title": "위험 가설 수립",
                "summary": (
                    "2026-08-25의 PAYMENT_ACCOUNT 위험, 예상 부족액 250,000원을 우선 확인했습니다."
                ),
                "status": "COMPLETED",
                "tool_name": None,
                "candidate_id": None,
                "evidence_ids": ["event-1"],
            },
            {
                "sequence": 2,
                "kind": "TOOL_CALL",
                "title": "금융 맥락 조회",
                "summary": "계좌, 카드, 예정 수입, 필수지출과 보호자금을 확인했습니다.",
                "status": "COMPLETED",
                "tool_name": "get_financial_context",
                "candidate_id": None,
                "evidence_ids": [],
            },
            {
                "sequence": 3,
                "kind": "TOOL_CALL",
                "title": "금융 안전정책 검증",
                "summary": "필요한 근거를 확인하지 못해 해당 결과를 추천 판단에서 제외했습니다.",
                "status": "FAILED",
                "tool_name": "validate_financial_policy",
                "candidate_id": None,
                "evidence_ids": [],
            },
            {
                "sequence": 4,
                "kind": "CANDIDATE_EVALUATION",
                "title": "TRANSFER 검증",
                "summary": "금융 코어와 안전정책 검증을 통과했습니다.",
                "status": "COMPLETED",
                "tool_name": None,
                "candidate_id": "action-1",
                "evidence_ids": [],
            },
            {
                "sequence": 5,
                "kind": "CANDIDATE_EVALUATION",
                "title": "ADJUST_DISCRETIONARY_BUDGET 검증",
                "summary": "금융 코어 또는 안전정책 검증을 통과하지 못했습니다.",
                "status": "REJECTED",
                "tool_name": None,
                "candidate_id": "action-2",
                "evidence_ids": [],
            },
            {
                "sequence": 6,
                "kind": "FINAL_SELECTION",
                "title": "최종 대응안 선택",
                "summary": "안전정책을 충족하는 첫 번째 대응안입니다.",
                "status": "COMPLETED",
                "tool_name": None,
                "candidate_id": "action-1",
                "evidence_ids": [],
            },
        ],
        "usage": None,
    }


def test_ai_investigated_trace_exposes_requests_hypothesis_and_unresolved() -> None:
    hypothesis_reason = "지급 이력 때문에 거래처 입금 지연 가능성을 우선 확인했습니다."
    first_reason = "거래처 지급 이력을 확인합니다."
    second_reason = "위험 구간의 이벤트 구성을 확인합니다."
    unresolved = ["거래처가 실제 지급일을 확정했는지는 확인하지 못했습니다."]
    state = {
        "risk": {
            "type": "PAYMENT_ACCOUNT",
            "date": "2026-08-25",
            "shortageAmount": 250_000,
        },
        "risk_hypotheses": [
            {
                "type": "COUNTERPARTY_DELAY",
                "summary": hypothesis_reason,
                "priority": 1,
                "source": "AI_INVESTIGATION",
                "triggering_event_ids": ["event-1"],
            }
        ],
        "investigation_trace": [
            {
                "source": "AI",
                "phase": 1,
                "tool": "get_counterparty_evidence",
                "reason": first_reason,
            },
            {
                "source": "AI",
                "phase": 2,
                "tool": "query_financial_events",
                "reason": second_reason,
            },
        ],
        "investigation": {"status": "SUCCEEDED", "unresolved": unresolved},
        "tool_calls": [{"tool_name": "get_financial_context"}],
        "actionCandidates": [],
        "recommended_plan": None,
    }

    trace = build_decision_trace(state, mode="AI_INVESTIGATED", model="test-model")

    assert trace["steps"][0] == {
        "sequence": 1,
        "kind": "RISK_HYPOTHESIS",
        "title": "위험 가설 수립",
        "summary": (
            "2026-08-25의 PAYMENT_ACCOUNT 위험, 예상 부족액 250,000원을 우선 확인했습니다."
        ),
        "status": "COMPLETED",
        "tool_name": None,
        "candidate_id": None,
        "evidence_ids": ["event-1"],
    }
    assert trace["steps"][1] == {
        "sequence": 2,
        "kind": "TOOL_CALL",
        "title": "거래처 지급 근거 조회",
        "summary": "과거 지급 이력과 입금 지연 근거를 확인했습니다.",
        "status": "COMPLETED",
        "tool_name": "get_counterparty_evidence",
        "candidate_id": None,
        "evidence_ids": [],
        "source": "AI",
        "phase": 1,
        "reason": first_reason,
    }
    assert trace["steps"][2]["source"] == "AI"
    assert trace["steps"][2]["phase"] == 2
    assert trace["steps"][2]["reason"] == second_reason
    assert trace["steps"][3] == {
        "sequence": 4,
        "kind": "RISK_HYPOTHESIS",
        "title": "AI 조사 가설 정리",
        "summary": hypothesis_reason,
        "status": "COMPLETED",
        "tool_name": None,
        "candidate_id": None,
        "evidence_ids": ["event-1"],
        "source": "AI",
        "phase": 2,
        "reason": hypothesis_reason,
    }
    assert trace["steps"][4]["tool_name"] == "get_financial_context"
    assert "source" not in trace["steps"][4]
    assert trace["steps"][5]["kind"] == "FINAL_SELECTION"
    assert trace["unresolved_questions"] == unresolved


def test_ai_partial_observations_are_explicitly_audit_only() -> None:
    reason = "거래처 지급 이력을 확인합니다."
    state = {
        "risk": {
            "type": "PAYMENT_ACCOUNT",
            "date": "2026-08-25",
            "shortageAmount": 250_000,
        },
        "risk_hypotheses": [
            {
                "source": "baseline_result.risk_metrics",
                "summary": "결정론적 가설입니다.",
                "triggering_event_ids": ["event-1"],
            }
        ],
        "investigation_trace": [
            {
                "source": "AI",
                "phase": None,
                "tool": "unapproved_tool",
                "reason": None,
            },
            {
                "source": "AI",
                "phase": 1,
                "tool": "get_counterparty_evidence",
                "reason": reason,
            },
            {
                "source": "AI",
                "phase": 1,
                "tool": "query_financial_events",
                "reason": "위험 구간의 이벤트 구성을 확인합니다.",
            },
        ],
        "investigation": {
            "status": "PARTIAL",
            "observations": [{"tool": "get_counterparty_evidence"}],
            "unresolved": [],
        },
        "tool_calls": [{"tool_name": "get_financial_context"}],
        "actionCandidates": [],
        "recommended_plan": None,
    }

    trace = build_decision_trace(state, mode="AI_PARTIAL")

    hypothesis_step = trace["steps"][0]
    assert "source" not in hypothesis_step
    assert "phase" not in hypothesis_step
    assert "reason" not in hypothesis_step
    ai_tool_step = trace["steps"][1]
    assert ai_tool_step["status"] == "AUDIT_ONLY"
    assert ai_tool_step["summary"] == (
        "과거 지급 이력과 입금 지연 근거를 확인했습니다. 다만 AI 조사가 부분 완료되어 "
        "이 관찰은 감사 기록으로만 보존하고 최종 결정 근거에는 사용하지 않았습니다."
    )
    assert ai_tool_step["source"] == "AI"
    assert ai_tool_step["phase"] == 1
    assert ai_tool_step["reason"] == reason
    failed_ai_tool_step = trace["steps"][2]
    assert failed_ai_tool_step["tool_name"] == "query_financial_events"
    assert failed_ai_tool_step["status"] == "FAILED"
    assert failed_ai_tool_step["summary"] == (
        "필요한 근거를 확인하지 못해 해당 결과를 추천 판단에서 제외했습니다."
    )
    assert failed_ai_tool_step["source"] == "AI"
    deterministic_tool_step = trace["steps"][3]
    assert deterministic_tool_step["status"] == "COMPLETED"
    assert "source" not in deterministic_tool_step


def test_shadow_trace_does_not_expose_unapplied_ai_requests() -> None:
    state = {
        "risk": {},
        "risk_hypotheses": [],
        "investigation_trace": [
            {
                "source": "AI",
                "phase": 1,
                "tool": "get_financial_context",
                "reason": "현재 자금 구성을 확인합니다.",
            }
        ],
        "investigation": {"status": "SUCCEEDED", "unresolved": []},
        "tool_calls": [],
        "actionCandidates": [],
        "recommended_plan": None,
    }

    trace = build_decision_trace(state, mode="DETERMINISTIC")

    assert len(trace["steps"]) == 1
    assert trace["steps"][0]["title"] == "기준 위험 확인"
    assert "source" not in trace["steps"][0]
