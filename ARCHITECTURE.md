# FlowGuard AI 최종 아키텍처 완성본

FlowGuard는 KB·토스의 자산관리 서비스처럼 **금융데이터가 갱신되면 종합 분석을 다시 수행하고, 저장된 최신 결과를 여러 화면에 나누어 보여주는 구조**로 설계합니다.

사용자가 화면을 열 때마다 에이전트를 실행하지 않습니다.

```text
금융데이터 갱신
→ 종합 유동성 분석
→ 결과 저장
→ 대시보드·위험카드·추천안 갱신
```

---

## 1. 전체 아키텍처

```text
┌───────────────────────────────────────────────────────────────┐
│                        Data Sources                           │
│                                                               │
│  실제 서비스: MyData API                                     │
│  공모전 MVP: 합성 CSV                                         │
│  사용자 입력: 예정수입 · 보호자금 · 필수지출 확인             │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                   Financial Data Adapter                      │
│                                                               │
│  데이터 형식 통합 · 중복 제거 · 거래 분류 · 계좌 연결         │
│  카드대금 · 할부 · 예정수입 · 반복지출 정규화                  │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                        PostgreSQL                             │
│                                                               │
│  계좌 · 카드 · 거래 · 할부 · 예정수입 · 사용자 설정           │
└──────────────────────────────┬────────────────────────────────┘
                               │ 데이터 갱신 이벤트
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                    Analysis Orchestrator                      │
│                                                               │
│  분석 작업 생성 · 실행 상태 관리 · 재현성 정보 기록           │
│  snapshot_id · analysis_run_id · 모델·정책 버전 관리          │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                 Financial Snapshot Builder                    │
│                                                               │
│  특정 시점의 전체 금융상태 고정                               │
│                                                               │
│  계좌잔액 · 카드대금 · 할부 · 예정수입                        │
│  필수지출 · 선택지출 · 세금 보호금액 · 최소 안전잔액          │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│               Deterministic Financial Core                    │
│                    기준 상태 분석                             │
│                                                               │
│  13주 현금흐름 · 예정수입 지연 시나리오                       │
│  Safe-to-Spend · 부족금액 범위 · 위험 시점                    │
│  결제계좌 부족 / 전체 유동성 부족 구분                        │
│  내부 위험확률 · 데이터 신뢰도                                │
└──────────────────────────────┬────────────────────────────────┘
                               │ 기준 분석 결과
                               ▼
┌───────────────────────────────────────────────────────────────┐
│              Liquidity Investigator Agent                     │
│                                                               │
│  위험 원인 조사 · 필요한 근거 선택                            │
│  거래처 지급이력 확인 · 위험 가설 수립                        │
│  후보 대응안 구성 · 결과 재검토                               │
│                                                               │
│  ※ 숫자를 직접 계산하지 않음                                 │
│  ※ 사용자와 대화하는 챗봇이 아님                             │
└──────────────────────────────┬────────────────────────────────┘
                               │ MCP 호출
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                  FlowGuard Core MCP Server                    │
│                                                               │
│  금융 컨텍스트 조회 · 거래처 근거 조회                        │
│  금융 이벤트 조회 · 현금흐름 계산                             │
│  Safe-to-Spend 계산 · 대응안 평가 · 정책 검증                 │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│             Action Plan Evaluation Pipeline                   │
│                                                               │
│  후보 대응안 스키마 검사                                      │
│  금융 스냅숏 복사본에 가상 적용                              │
│  13주 현금흐름 재계산                                         │
│  적용 전후 비교 · 반동위험 검사 · 금융정책 검증               │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                 Risk Presentation Mapper                      │
│                                                               │
│  내부 계산값                                                   │
│  확률 · 부족금액 · 위험시점 · 신뢰도                          │
│                         ↓                                     │
│  사용자 표현                                                   │
│  상태 · 날짜 · 영향 · 원인 · 추천 행동                        │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                 Analysis Report Store                         │
│                                                               │
│  최신 종합분석 · 위험카드 · 13주 타임라인                     │
│  Safe-to-Spend · 위험 근거 · 추천안 · 적용 전후 결과          │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                     Application API                           │
│                                                               │
│  최신 결과 조회 · 분석 상태 조회 · 데이터 수정               │
│  추천안 승인·거절 · 다른 대응안 요청 · 알림 관리              │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                     FlowGuard Web App                         │
│                                                               │
│  홈 · 13주 현금흐름 · 위험 상세 · 예정수입                    │
│  할부부담 · 추천안 · 적용 전후 · 사용자 승인                  │
└───────────────────────────────────────────────────────────────┘
```

---

# 2. 핵심 실행 흐름

## 데이터 갱신 및 종합분석

MVP에서는 사용자가 CSV를 새로 업로드하거나 금융정보를 수정하면 전체 분석 작업을 실행합니다.

```text
CSV 업로드 또는 데이터 수정
        ↓
금융데이터 정규화 및 저장
        ↓
최초 설정의 환경설정·후보 결정을 한 번에 확정
        ↓
새 금융 스냅숏 생성
        ↓
금융 코어의 기준 상태 계산
        ↓
에이전트의 위험 원인 조사
        ↓
후보 대응안 생성 및 수치 검증
        ↓
사용자용 리포트 생성
        ↓
최신 분석 결과로 교체
```

최초 설정 확정과 분석은 분리합니다. 환경설정과 여러 후보 결정은 먼저 하나의
트랜잭션으로 저장하며, 전부 성공한 경우에만 분석을 한 번 실행합니다. 분석 중에는
외부 모델 호출을 포함할 수 있으므로 데이터베이스 트랜잭션을 열어 두지 않습니다.

이 흐름 자체는 마이데이터 자산관리 서비스와 유사합니다. FlowGuard의 특징은 분석 대상이 자산 구성이나 과거 소비가 아니라 **향후 13주의 유동성 위험과 대응 행동**이라는 점입니다.

---

## 사용자가 화면을 조회할 때

페이지를 열 때는 금융 분석이나 에이전트 실행을 다시 하지 않습니다.

```text
사용자가 홈 화면 접속
        ↓
Application API
        ↓
저장된 최신 분석 결과 조회
        ↓
즉시 화면 표시
```

따라서 홈 화면의 응답속도가 LLM 호출 속도에 좌우되지 않습니다.

분석이 실행 중이라면 기존 결과를 보여주면서 다음과 같이 표시합니다.

> 새로운 금융정보를 반영하고 있습니다.
> 최근 분석: 오늘 오전 8시 30분 기준

---

## 추천안 승인

```text
검증된 추천안 표시
        ↓
사용자 승인
        ↓
MVP에서는 가상 적용
        ↓
변경된 상태의 스냅숏 생성
        ↓
종합 분석 재실행
        ↓
적용 후 리포트 표시
```

분석 동의와 행동 승인은 분리합니다.

* **분석 동의:** 금융정보를 수집하고 분석하는 것에 대한 허용
* **행동 승인:** 특정 추천안을 적용하는 것에 대한 허용

---

# 3. 금융 코어와 에이전트의 역할 분리

## Deterministic Financial Core

숫자와 금융 상태를 계산합니다.

* 날짜별 계좌 잔액
* 예정 수입의 지연 가능성
* 13주 현금흐름
* 부족 발생 시점
* 예상 부족금액 범위
* Safe-to-Spend
* 결제계좌 부족 여부
* 전체 유동성 부족 여부
* 대응안 적용 전후 결과
* 반동위험
* 정책 위반 여부

동일한 입력과 설정에는 동일한 결과가 나오도록 구현합니다.

## Liquidity Investigator Agent

계산 결과를 바탕으로 무엇을 추가로 확인하고 어떤 대응안을 검토할지 결정합니다.

* 주요 위험 원인 가설 생성
* 필요한 거래처 지급 근거 조회
* 관련 금융 이벤트 선택
* 해결 가능한 대응 행동 조합
* 여러 후보 대응안 구성
* 평가 결과를 보고 대응안 수정
* 최종 추천 근거 구조화

에이전트는 다음과 같은 값을 직접 만들어내지 않습니다.

```text
잔액
부족금액
위험확률
Safe-to-Spend
할부 부담액
대응안 적용 결과
```

### MVP 실행 프로필

`FLOWGUARD_AGENT_MODE=auto`가 기본값입니다. `OPENAI_API_KEY`가 있으면
`gpt-5.6-luna`가 Responses API의 함수 호출을 통해 필요한 조회 도구와
백엔드 생성 후보를 선택합니다. 키가 없거나 모델 호출이 실패하면 기존 결정론적
조사기로 전환하고 그 사유를 분석 리포트에 기록합니다.

모델은 금액·날짜·확률을 직접 계산하지 않으며, 금융 코어가 가상 적용과 정책
검증을 마친 후보만 최종 추천으로 사용할 수 있습니다. 화면에는 숨은 사고과정이
아니라 위험 가설, 실제 도구 호출, 후보 검증 결과, 최종 선택 이유로 구성된 공개
감사 추적만 표시합니다.

---

# 4. MCP 도구 최종 구성

## 조회 도구

### `get_financial_context`

* 계좌 잔액
* 카드대금
* 할부 일정
* 예정 수입
* 필수지출
* 보호자금
* 최소 안전잔액
* 데이터 누락 정보

### `get_counterparty_evidence`

* 거래처별 지급 횟수
* 예정일과 실제 입금일 차이
* 평균·최대 지연일
* 최근 지급 패턴
* 데이터 충분성

### `query_financial_events`

* 특정 기간의 수입·지출
* 카드대금
* 할부금
* 예정 입금
* 필수지출과 선택지출
* 계좌이동

## 계산·평가 도구

### `simulate_cashflow`

기준 상태나 주어진 조건에서 13주 현금흐름을 계산합니다.

### `calculate_safe_to_spend`

필수지출과 보호자금을 유지하면서 오늘 사용할 수 있는 금액을 계산합니다.

### `evaluate_action_plan`

후보 대응안을 가상 적용하고, 적용 전후 결과와 반동위험을 반환합니다.

## 안전 도구

### `validate_financial_policy`

* 세금 보호금액 침범 여부
* 필수생활비 보호 여부
* 최소 안전잔액 유지 여부
* 과도한 금융비용 발생 여부
* 기존 할부 누락 여부
* 데이터 최신성
* 사용자 승인 필요 여부

---

# 5. 추천안 생성 및 검증

에이전트는 사전에 정의된 행동을 조합합니다.

```text
transfer
reserve_funds
adjust_discretionary_budget
pause_savings
shift_payment_date
delay_purchase
add_installment
confirm_receivable
```

예를 들어:

```json
{
  "actions": [
    {
      "type": "transfer",
      "from_account_id": "account-2",
      "to_account_id": "payment-account",
      "amount": 100000
    },
    {
      "type": "adjust_discretionary_budget",
      "amount": 40000,
      "duration_days": 14
    }
  ]
}
```

검증 과정은 다음과 같습니다.

```text
후보 대응안 생성
        ↓
행동 형식 및 입력값 검사
        ↓
현재 스냅숏의 복사본에 가상 적용
        ↓
13주 현금흐름 재계산
        ↓
부족 시점·부족금액·위험확률 비교
        ↓
미래 구간으로 위험이 이동했는지 확인
        ↓
금융안전 정책 검사
        ↓
통과한 대응안만 저장
```

---

# 6. 위험정보 표현

금융 엔진은 내부적으로 확률값을 유지합니다.

```json
{
  "shortfall_probability": 0.37,
  "payment_account_shortfall_probability": 0.68,
  "expected_gap_min": 80000,
  "expected_gap_max": 150000,
  "risk_date": "2026-08-25",
  "data_confidence": 0.76
}
```

`Risk Presentation Mapper`가 사용자용 정보로 변환합니다.

```json
{
  "status": "PREPARE",
  "status_label": "미리 준비가 필요해요",
  "title": "8일 뒤 카드대금을 준비하세요",
  "impact": "약 8만~15만 원이 부족할 수 있습니다",
  "cause": "거래처 B의 입금 일정이 불확실합니다",
  "recommended_action": "결제계좌에 10만 원을 미리 확보하세요",
  "confidence_label": "분석 신뢰도 보통"
}
```

사용자 상태는 다음 네 단계로 통일합니다.

| 내부 상태     | 사용자 표시      |
| --------- | ----------- |
| `STABLE`  | 현재는 안전해요    |
| `VERIFY`  | 확인할 정보가 있어요 |
| `PREPARE` | 미리 준비가 필요해요 |
| `ACT_NOW` | 지금 조치가 필요해요 |

상태 판정에는 확률뿐 아니라 다음 요소를 함께 사용합니다.

```text
부족 가능성
+ 부족금액
+ 위험일까지 남은 시간
+ 필수결제 여부
+ 가용자금
+ 데이터 신뢰도
```

---

# 7. 프런트엔드 구성

전체 분석 결과를 하나의 긴 리포트로만 보여주지 않고, 토스와 같은 방식으로 기능별 화면에 분산합니다.

## 홈

```text
오늘 안심하고 쓸 수 있는 돈
170,000원

현재 상태
미리 준비가 필요해요

다음 위험
8일 뒤 카드대금

추천 행동
결제계좌에 10만 원 확보하기
```

## 13주 현금흐름

* 주차별 예상 가용잔액
* 예정 수입과 필수지출
* 위험구간
* 분석 기준시점
* 기준·지연·악화 시나리오

## 위험 상세

* 어떤 결제에서 문제가 생기는지
* 예상 부족금액 범위
* 위험의 주요 원인
* 계좌 배치 문제인지 전체 자금 부족인지
* 분석에 사용한 데이터
* 분석 신뢰도

## 예정수입

* 거래처
* 예정 금액
* 예정일
* 과거 지급 지연
* 사용자 확인 상태

## 할부부담

* 남은 회차
* 월별 납부액
* 향후 현금흐름과 겹치는 기간
* 신규 할부의 중기 영향

## 추천안

* 추천 행동
* 필요한 금액
* 적용 전후 상태
* 이후 13주의 반동위험
* 승인·거절·다른 대응안 요청

---

# 8. 주요 API

## 데이터

```text
POST  /api/v1/imports/transactions
GET   /api/v1/transactions
PATCH /api/v1/transactions/{transaction_id}

GET   /api/v1/scheduled-events
POST  /api/v1/scheduled-events
PATCH /api/v1/scheduled-events/{event_id}
```

## 분석

```text
POST /api/v1/analyses
GET  /api/v1/analyses/{analysis_id}
GET  /api/v1/analyses/{analysis_id}/events
```

## 사용자 화면

```text
GET /api/v1/dashboard
GET /api/v1/reports/latest
GET /api/v1/cashflow/timeline
GET /api/v1/risks/next
GET /api/v1/receivables
GET /api/v1/installments
```

## 추천안

```text
GET  /api/v1/recommendations
GET  /api/v1/recommendations/{recommendation_id}

POST /api/v1/recommendations/{recommendation_id}/approve
POST /api/v1/recommendations/{recommendation_id}/reject
POST /api/v1/recommendations/{recommendation_id}/alternatives
```

대화형 질의 API는 포함하지 않습니다.

---

# 9. 데이터베이스 핵심 테이블

```text
users
user_consents
user_preferences

accounts
cards
transactions
counterparties
scheduled_cash_events
installment_plans

financial_snapshots
snapshot_accounts
snapshot_events

analysis_runs
analysis_results
risk_metrics
presentation_results

agent_runs
agent_hypotheses
tool_executions

recommendations
recommendation_actions
action_evaluations
policy_validations
approval_records

notifications
```

재현성을 위해 다음 정보를 저장합니다.

```text
snapshot_id
analysis_run_id
agent_run_id
model_name
model_version
prompt_version
tool_version
policy_version
simulation_seed
created_at
```

---

# 10. 백그라운드 작업

MVP에서는 복잡한 ‘의미 있는 변화 판정기’를 두지 않습니다.

```text
CSV 업로드
또는 사용자가 금융정보 수정
        ↓
분석 작업 큐 등록
        ↓
새 금융 스냅숏 생성
        ↓
FlowGuard 종합 분석
        ↓
최신 리포트 교체
```

추가로 하루 한 번 정기 분석을 실행할 수 있습니다.

```text
매일 지정 시간
        ↓
현재 데이터를 기준으로 새 스냅숏 생성
        ↓
예정 수입 지연·결제일 접근 여부 확인
        ↓
리포트와 알림 갱신
```

실제 마이데이터 서비스로 확장되면 데이터 동기화 완료 이벤트가 CSV 업로드를 대신합니다.

---

# 11. Docker 구성

```text
flowguard-web
flowguard-api
flowguard-worker
flowguard-mcp
flowguard-postgres
flowguard-redis
```

각 컨테이너의 역할은 다음과 같습니다.

| 컨테이너                 | 역할                    |
| -------------------- | --------------------- |
| `flowguard-web`      | Next.js 사용자 화면        |
| `flowguard-api`      | FastAPI, 조회·수정·승인 API |
| `flowguard-worker`   | 스냅숏·분석 작업·정기 실행       |
| `flowguard-mcp`      | 에이전트용 MCP 도구          |
| `flowguard-postgres` | 원천 데이터·분석 결과·감사 로그    |
| `flowguard-redis`    | 작업 큐·분석 진행상태·에이전트 상태  |

---

# 12. 최종 실행 구조

```text
MyData API 또는 MVP용 CSV
        ↓
Financial Data Adapter
        ↓
PostgreSQL
        ↓
Financial Snapshot Builder
        ↓
Deterministic Financial Core
        ↓
Liquidity Investigator Agent
        ↓
MCP를 통한 근거 조회·대응안 평가
        ↓
정책 검증 및 반동위험 확인
        ↓
Risk Presentation Mapper
        ↓
Analysis Report Store
        ↓
Application API
        ↓
FlowGuard Web App
```

사용자 경험은 다음처럼 정리됩니다.

```text
금융정보 연결 또는 갱신
        ↓
FlowGuard가 전체 유동성을 분석
        ↓
최신 결과가 각 화면에 반영
        ↓
필요한 위험과 추천 행동 확인
        ↓
추천안 승인
        ↓
적용 이후 상태 재분석
```

이 구조에서는 **마이데이터 기반 자산관리 서비스의 익숙한 갱신 방식**을 활용하면서, 그 위에 FlowGuard의 핵심인 **예정 수입 불확실성 분석, 13주 유동성 예측, Safe-to-Spend, 대응안 검증, 반동위험 탐지**를 추가합니다.
