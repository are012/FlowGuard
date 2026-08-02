"""Shared API test configuration."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def deterministic_analysis_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트는 분석 결과를 검증하지 전송 방식이나 AI 연결을 검증하지 않는다.

    프로덕션 기본값은 비동기 실행(`FLOWGUARD_ANALYSIS_ASYNC=on`)과
    AI 조사(`FLOWGUARD_AI_INVESTIGATION=on`)이지만, 테스트에서 그대로
    두면 `POST /api/v1/analyses` 가 202 만 반환하고 조사 경로가 외부
    호출에 의존해 결과 단언이 깨진다.

    두 축을 다루는 테스트는 이 값을 직접 덮어써서 의도를 명시한다.
    """

    monkeypatch.setenv("FLOWGUARD_ANALYSIS_ASYNC", "off")
    monkeypatch.setenv("FLOWGUARD_AI_INVESTIGATION", "off")
