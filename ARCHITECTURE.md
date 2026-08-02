# FlowGuard AI 목표 아키텍처와 MVP 실행 프로필

FlowGuard는 KB·토스의 자산관리 서비스처럼 **금융데이터가 갱신되면 종합 분석을 다시 수행하고, 저장된 최신 결과를 여러 화면에 나누어 보여주는 구조**로 설계합니다.

사용자가 화면을 열 때마다 에이전트를 실행하지 않습니다.

```text
금융데이터 갱신
→ 종합 유동성 분석
→ 결과 저장
→ 대시보드·위험카드·추천안 갱신
```

## 0. 문서 범위와 현재 실행 프로필

이 문서는 도메인 책임을 설명하는 논리 아키텍처와 운영 확장 목표를 함께 다룹니다.
상자나 컴포넌트 이름이 항상 현재의 독립 프로세스나 컨테이너를 뜻하지는 않습니다.

| 구성 | 현재 공모전 MVP 구현 | 목표 확장 |
|---|---|---|
| Web | Next.js 프로세스 | `flowguard-web` 컨테이너 |
| API/분석 | FastAPI 요청 안에서 동기 분석 | API와 durable worker 분리 |
| AI 해석 | 같은 Python distribution의 별도 FastAPI 진입점, 단일 worker | 공유 멱등 저장소 기반 다중 worker 서비스 |
| 코어/MCP 도구 | 일곱 도구와 FastMCP 진입점 구현; 분석 경로는 동일 구현을 in-process 호출 | 별도 MCP 프로세스와 transport |
| 저장소 | SQLite 기본·테스트 완료; PostgreSQL 설정 경로 제공 | 검증된 PostgreSQL 배포와 마이그레이션 |
| 큐/스케줄러 | 없음 | Redis queue, lease, 정기 실행 |
| Docker | Dockerfile·Compose 없음 | 11절의 목표 토폴로지 |

자동 검증은 SQLite, Fake OpenAI client와 MockTransport를 사용하며 E2E에서는 AI
연결 실패 시 결정론적 폴백을 확인합니다. 실제 OpenAI 호출, API와 AI 프로세스 사이의
성공 경로, PostgreSQL, Redis, Docker 토폴로지와 프로세스 간 MCP transport는 아직
통합 검증하지 않았습니다.

---

## 1. 전체 아키텍처

아래 그림은 책임과 데이터 흐름을 나타내는 **논리 컴포넌트 그림**입니다. 현재 MVP의
프로세스 배치는 위 표를 기준으로 하며, PostgreSQL·별도 worker·별도 MCP 서버는 목표
확장 구성입니다.

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
│               Persistence (MVP: SQLite)                      │
│                                                               │
│  계좌 · 카드 · 거래 · 할부 · 예정수입 · 사용자 설정           │
│  운영 확장 목표: PostgreSQL                                   │
└──────────────────────────────┬────────────────────────────────┘
                               │ 분석 요청 (MVP: API 호출)
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
│          Backend Recommendation Builder                       │
│                                                               │
│  위험 원인 조사 · 필요한 근거 선택                            │
│  거래처 지급이력 확인 · 위험 가설 수립                        │
│  후보 대응안 구성 · 결과 재검토                               │
│                                                               │
│  ※ MVP: API 프로세스 · 확장 목표: backend worker              │
│  ※ 숫자를 직접 계산하지 않음                                 │
└──────────────────────────────┬────────────────────────────────┘
                               │ 코어 도구 호출 (MCP 계약)
                               ▼
┌───────────────────────────────────────────────────────────────┐
│              FlowGuard Core Tool / MCP Boundary               │
│                                                               │
│  금융 컨텍스트 조회 · 거래처 근거 조회                        │
│  금융 이벤트 조회 · 현금흐름 계산                             │
│  Safe-to-Spend 계산 · 대응안 평가 · 정책 검증                 │
│  MVP: in-process 호출 · 확장 목표: 별도 MCP transport         │
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
                               │ 최소 facts/evidence/actionCandidates
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                 AI Interpretation Service                     │
│                                                               │
│  OpenAI 호출 · 후보 순위화 · 설명 생성                        │
│  DB 직접 접근 없음 · MCP 직접 호출 없음                       │
│  request/response 계약 검증 · 타임아웃 · 재시도 · 폴백        │
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
AI 서비스 구조화 해석 요청
        ↓
사용자용 리포트 생성
        ↓
최신 revision 결과만 최신 리포트로 승격
```

최초 설정 확정과 분석은 분리합니다. 환경설정과 여러 후보 결정은 먼저 하나의
트랜잭션으로 저장하며, 전부 성공한 경우에만 분석을 한 번 실행합니다. 분석 중에는
외부 AI 호출을 포함할 수 있으므로 데이터베이스 트랜잭션을 열어 두지 않습니다.

분석 결과 저장 순서는 다음 원칙을 따릅니다.

```text
백엔드 계산 완료
        ↓
기준 분석 결과 저장
        ↓
AI 해석 요청 및 저장
        ↓
snapshotRevision이 최신일 때만 latest report 승격
```

오래된 revision의 분석이나 AI 응답이 늦게 도착하더라도 실행 기록은 저장하고,
`latest report` 포인터는 더 최신 `snapshotRevision`이 가진 결과만 가리킵니다.

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

## Backend Recommendation Builder

계산 결과를 바탕으로 무엇을 추가로 확인하고 어떤 대응안을 검토할지 결정합니다.
이 구성요소는 백엔드 분석 책임이며 별도 AI 서비스가 아닙니다. 현재 MVP에서는
API 프로세스 안에서 실행하고 운영 확장 시 `flowguard-worker`로 분리합니다.

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

### 현재 MVP 실행 프로필

공모전 MVP는 `flowguard-api` 요청 안에서 스냅숏 생성, 금융 코어 계산, 위험 조사,
후보 대응안 생성, 정책 검증과 상태 저장을 동기 실행합니다. 이 문서의
`flowguard-worker`는 현재 별도 프로세스가 아니라 이 백엔드 책임을 뜻합니다.

- 코어 도구와 FastMCP 진입점은 백엔드가 소유합니다. 현재 분석 경로는
  `CoreToolService`를 in-process로 호출합니다.
- AI Interpretation Service는 같은 Python distribution을 사용하지만 포트와
  환경변수가 분리된 별도 FastAPI 프로세스로 실행합니다.
- AI 서비스는 백엔드가 전달한 `facts`, `evidence`, `actionCandidates`만 받아
  OpenAI를 호출하고 순위화·설명 생성만 수행합니다.
- OpenAI API 키는 AI 서비스 프로세스만 읽습니다.
- AI 서비스는 DB나 MCP 도구에 접근하지 않습니다.
- SQLite가 기본 저장소이며 Redis queue와 정기 scheduler는 사용하지 않습니다.

### 운영 확장 프로필

부하 분산과 durable recovery가 필요해지면 API에서 `flowguard-worker`를 분리하고,
`flowguard-mcp`, PostgreSQL, Redis와 공유 멱등 저장소를 독립 배포합니다. 이 구성은
현재 저장소에서 통합 검증된 MVP 배포가 아니라 11절의 목표 토폴로지입니다.

따라서 모델은 금액·날짜·확률을 직접 계산하지 않으며, 금융 코어가 가상 적용과 정책
검증을 마친 후보만 최종 추천으로 사용할 수 있습니다. 화면에는 숨은 사고과정이
아니라 위험 가설, 실제 도구 호출, 후보 검증 결과, 최종 선택 이유로 구성된 공개
감사 추적만 표시합니다.

## AI 해석 상태 분리

FlowGuard는 금융 분석 성공과 AI 해석 성공을 같은 상태로 취급하지 않습니다.

```text
analysis_status
→ 금융 코어와 후보 검증의 성공 여부

interpretation_status
→ AI 응답, 규칙 기반 폴백, 또는 해석 실패 여부
```

예를 들어 AI 서비스가 실패해도 다음 상태는 정상입니다.

```json
{
  "analysisStatus": "SUCCEEDED",
  "interpretationStatus": "FALLBACK",
  "interpretation": {
    "source": "DETERMINISTIC_FALLBACK"
  }
}
```

즉, AI 실패는 금융 분석 전체를 `FAILED`로 바꾸지 않습니다.

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

# 9. 목표 PostgreSQL 논리 데이터 모델

아래 목록은 운영 확장 시 정규화할 논리 데이터 모델입니다. 현재 MVP는 SQLite와
SQLAlchemy를 사용하며 일부 실행 산출물을 JSON payload로 저장합니다. 현재 실제
테이블과 제약은 `apps/api/flowguard/storage.py`가 기준입니다.

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

# 10. 분석 실행과 목표 백그라운드 작업

현재 MVP에서는 복잡한 ‘의미 있는 변화 판정기’, queue 또는 scheduler를 두지
않습니다. 데이터 변경 응답이 `analysis_required=true`를 반환하면 클라이언트가 분석
API를 호출하고, API 요청 안에서 분석을 동기 실행합니다.

```text
CSV 업로드
또는 사용자가 금융정보 수정
        ↓
analysis_required = true
        ↓
POST /api/v1/analyses
        ↓
API 요청 안에서 스냅숏 생성·종합 분석
        ↓
revision이 최신인 리포트만 승격
```

운영 확장 단계에서는 durable worker queue와 하루 한 번의 정기 분석을 추가할 수
있습니다.

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

# 11. 목표 확장용 Docker 토폴로지

> 아래 일곱 컨테이너는 현재 저장소에서 실행·검증된 배포 구성이 아니라 운영 확장을
> 위한 목표안입니다. 현재 MVP에는 Dockerfile과 Compose가 없으며, 구현된 로컬 실행
> 구성은 Web, 동기 분석과 코어 도구를 포함한 API, 선택적 AI Interpretation Service와
> SQLite입니다. 자동 검증 경계는 0절을 따르며 Docker 사용 여부와 배포 방식은
> `SPECIFICATION.md` 27절의 Open Decision으로 유지합니다.

```text
flowguard-web
flowguard-api
flowguard-worker
flowguard-ai-service
flowguard-mcp
flowguard-postgres
flowguard-redis
```

각 목표 컴포넌트의 역할은 다음과 같습니다.

| 목표 컴포넌트             | 역할                    |
| -------------------- | --------------------- |
| `flowguard-web`      | Next.js 사용자 화면        |
| `flowguard-api`      | FastAPI, 조회·수정·승인 API |
| `flowguard-worker`   | 스냅숏·분석 작업·후보 생성·정기 실행 |
| `flowguard-ai-service` | OpenAI 호출·설명 생성·후보 순위화 |
| `flowguard-mcp`      | 백엔드 소유 코어/MCP 도구      |
| `flowguard-postgres` | 원천 데이터·분석 결과·감사 로그    |
| `flowguard-redis`    | 작업 큐·분석 진행상태·에이전트 상태  |

---

# 12. 현재 MVP와 목표 확장 실행 구조

## 현재 MVP

```text
합성 CSV·사용자 입력
        ↓
FlowGuard Web App
        ↓
Application API
  ├─ SQLite
  ├─ 동기 Analysis Orchestrator
  ├─ Deterministic Financial Core
  └─ in-process CoreToolService
        ↓
기준 분석 리포트 저장 (SQLite)
        ↓
선택적 AI Interpretation Service (별도 프로세스·단일 worker)
        ↓
해석 실행 기록 저장
        ↓
최신 revision만 latest report로 승격
        ↓
FlowGuard Web App에서 저장된 결과 조회
```

## 목표 운영 확장

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
Backend Recommendation Builder
        ↓
MCP를 통한 근거 조회·대응안 평가
        ↓
정책 검증 및 반동위험 확인
        ↓
AI Interpretation Service
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
