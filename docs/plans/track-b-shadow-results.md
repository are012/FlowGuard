# 트랙 B shadow 실측 결과

측정일: 2026-08-02

측정 브랜치: `feature/track-b-ai-investigation` (`2733eac` 기준 프로덕션 코드)

모델: `gpt-5.6-luna`

## 측정 조건

- `FLOWGUARD_AI_INVESTIGATION=shadow`와 동일한 `AnalysisOrchestrator` 경로를 사용했다.
- AI 서비스의 실제 `/investigate/plan`·`/investigate/conclude` 엔드포인트를 호출했다.
- 조사 한도는 구현값 그대로 `MAX_PHASES=2`, `MAX_TOOL_CALLS=6`,
  `PHASE_TIMEOUT=5s`, `TOTAL_BUDGET=8s`를 사용했다.
- 여섯 표본의 조사 지연을 분리하기 위해 기존 `/interpret`는 대체 클라이언트로
  비활성화했다. 별도 한 표본에서는 `/interpret`까지 실제 호출해 전체 동기 분석
  시간을 추가 측정했다.
- 기본 합성 CSV에서 기준일, 조사 대상 거래처 수, 결제계좌 부족 규모와 위험 유형을
  달리한 여섯 입력을 사용했다. 측정값을 좋게 만들기 위한 재시도나 프롬프트·한도
  조정은 하지 않았다.

## 원시 결과

| 표본 | 위험 유형 | 조사 상태 | 턴 상태 | 실행 도구 수 | 추가 조사 | 조사 지연 | 분석 경과 |
|---|---|---|---|---:|---|---:|---:|
| base-jul | PAYMENT_ACCOUNT | PARTIAL | Succeeded → Succeeded → Failed | 5 | 예 | 8,013ms | 8,906ms |
| base-aug | PAYMENT_ACCOUNT | PARTIAL | Succeeded → Rejected | 3 | 아니오 | 7,549ms | 8,001ms |
| two-targets | PAYMENT_ACCOUNT | PARTIAL | Succeeded → Rejected | 3 | 아니오 | 5,152ms | 6,048ms |
| one-target | PAYMENT_ACCOUNT | SUCCEEDED | Succeeded → Succeeded | 3 | 아니오 | 6,467ms | 6,813ms |
| larger-payment-gap | PAYMENT_ACCOUNT | FAILED | Failed | 0 | 아니오 | 5,004ms | 5,391ms |
| total-liquidity | TOTAL_LIQUIDITY | FAILED | Failed | 0 | 아니오 | 5,020ms | 5,236ms |

모든 표본에서 금융 분석은 `SUCCEEDED`였고, AI 조사 실패·부분 실패는 결정론 경로로
폴백했다.

## 집계

| 지표 | 결과 | 목표/판정 |
|---|---:|---|
| 조사 완주율 | 1/6 = **16.7%** | 목표 80% 미달 |
| 2차 추가 조사 발생률 | 1/6 = **16.7%** | 목표 30% 미달 |
| 턴 거부율 | 2/11 = **18.2%** | 20% 미만 충족 |
| 턴 실패율 | 3/11 = **27.3%** | 별도 관찰값 |
| 조사 다양성 | **3종** | 목표 3종 충족 |
| 조사 포함 분석 경과 | 평균 6,732ms · p50 6,430ms · 최대 8,906ms | `/interpret` 제외 |
| 조사 루프 지연 | 평균 6,201ms · 최대 8,013ms | 8초 예산 경계에서 종료 |

실제로 실행된 서로 다른 도구 순서는 다음 세 종류였다.

1. 금융 맥락 → 금융 이벤트 → 거래처 근거 3회
2. 금융 맥락 → 거래처 근거 → 금융 이벤트
3. 금융 맥락 → 거래처 근거 2회

추가 조사가 발생한 표본은 마지막 결론 호출 전에 총예산을 소진했다. 두 표본은 첫
계획 호출이 턴 제한을 넘겨 종료됐고, 두 표본은 결론 응답이 계약 또는 파라미터
검증에서 거부됐다.

## 전체 동기 분석 1회 확인

기존 `/interpret`까지 실제 호출한 별도 기본 표본의 결과는 다음과 같다.

```text
analysis_status             SUCCEEDED
investigation_status        PARTIAL
investigation_latency_ms    8006
interpretation_status       SUCCEEDED
interpretation_latency_ms   3249
full_analysis_wall_ms       12093
```

8초는 트랙 B 조사 루프 예산이지 `/analyses` 요청 전체의 hard timeout이 아니다. 기존
`/interpret`는 별도 15초 예산을 유지하므로 전체 동기 요청은 8초를 넘을 수 있다.

## 판정

현재 결과로는 `on` 전환 조건을 충족하지 못한다. 기본값을 `off`로 유지하고,
`shadow`는 감사·측정 용도로만 사용해야 한다. 2차 추가 조사 발생률 16.7%는 0은
아니지만 목표보다 낮고, 추가 조사가 실제로 발생한 표본도 총예산 안에 결론을
완료하지 못했다. 따라서 현재 동기 예산에서는 2단계 설계의 효과가 입증되지 않았다.
다음 변경을 검토할 때는 단발성 계획으로의 축소와 비동기 분석 전환을 먼저 비교하고,
이번 측정값을 개선하기 위한 사후 프롬프트 조정은 하지 않는다.
