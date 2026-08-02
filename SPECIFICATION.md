# FlowGuard AI Implementation Specification

> 상태: Draft

---

## 1. 표기 규칙

이 문서에서 `TBD`로 표시한 항목은 구현 전에 팀 합의가 필요한 미정 사항이다.

---

# 2. MVP 범위

## 2.1 포함 범위

- 합성 금융거래 CSV 업로드
- 계좌, 카드, 거래, 할부, 예정 수입, 필수지출 정규화
- 반복 수입, 고정지출, 할부 후보 탐지
- 사용자 확인 및 수정
- 금융 스냅숏 생성
- 향후 13주 현금흐름 분석
- 1~4주 상세 분석
- 5~13주 위험구간 분석
- 결제계좌 부족과 전체 유동성 부족 구분
- 오늘 안심하고 쓸 수 있는 돈 계산
- 예정 수입 지연 시나리오 반영
- 거래처 지급 이력 기반 근거 조회
- 유동성 조사 에이전트의 위험 가설 생성
- MCP 도구 선택
- 후보 대응안 생성
- 대응안 가상 적용 및 전후 비교
- 반동위험 탐지
- 금융 안전정책 검증
- 사용자용 위험 상태와 설명 생성
- 추천안 승인, 거절, 다른 대응안 요청
- 승인된 추천안의 가상 적용
- 분석 결과 저장 및 최신 리포트 조회

## 2.2 제외 범위

- 실제 마이데이터 API 연동
- 실제 계좌이체
- 실제 카드 결제일 변경
- 실제 금융상품 신청 또는 계약
- 실제 대출 실행
- 자동으로 외부 금융기관에 명령 전송
- 범용 금융 챗봇
- 자연어 기반 자유 질의 API
- 신용평가 또는 대출 거절 판단
- 사용자의 승인 없는 행동 적용

## 2.3 현재 공모전 MVP 실행 프로필

이 명세의 `flowguard-worker`, `flowguard-mcp`, PostgreSQL, Redis, 컨테이너 이름은
책임 경계 또는 운영 확장 목표를 표현한다. 현재 저장소에서 구현한 MVP 실행 프로필은
다음과 같다. 자동 테스트는 SQLite와 대체 AI client를 사용하며, API와 별도 AI
프로세스 사이의 성공 경로는 포함하지 않는다. 상세 검증 범위는 `DEVELOPMENT.md`를
따른다.

| 구성 | 현재 MVP 실행 방식 | 운영 확장 목표 |
|---|---|---|
| Web | Next.js 프로세스 | 독립 Web 컨테이너 |
| API와 분석 | FastAPI 요청 안에서 `AnalysisOrchestrator` 동기 실행 | API와 durable worker 분리 |
| 코어/MCP 도구 | MCP 계약과 같은 도구 구현을 API 프로세스에서 직접 호출; FastMCP 진입점 제공 | 별도 MCP 프로세스와 transport 연결 |
| AI 분류·해석 | 같은 Python distribution의 별도 FastAPI 프로세스, 단일 worker | 공유 멱등 저장소를 사용하는 독립 서비스 |
| 저장소 | Alembic 버전 관리 SQLite·자동 마이그레이션 테스트; PostgreSQL URL과 driver 경로 제공 | 검증된 PostgreSQL 배포·통합 테스트 |
| 큐와 스케줄러 | 사용하지 않음 | Redis queue, lease, 정기 실행 |
| 컨테이너 | Dockerfile과 Compose를 MVP 완료 조건에 포함하지 않음 | 운영 배포 방식 확정 후 구성 |

따라서 현재 MVP에서 `worker`는 별도 프로세스 이름이 아니라 백엔드 분석 책임을
가리킬 수 있다. Docker 사용 여부와 운영 배포 방식은 27절의 Open Decision으로
유지한다.

---

# 3. 핵심 용어

| 용어 | 정의 |
|---|---|
| Financial Snapshot | 특정 기준시점의 계좌, 거래, 예정 수입, 예정 지출, 보호자금, 사용자 설정을 고정한 분석 입력 |
| Analysis Run | 하나의 금융 스냅숏을 기준으로 수행한 전체 분석 실행 |
| Safe-to-Spend | 향후 13주 동안 필수지출, 보호자금, 최소 안전잔액을 유지하면서 오늘 추가로 사용할 수 있는 최대 금액 |
| Payment Account Shortfall | 전체 가용자금은 충분하지만 실제 결제계좌의 잔액이 부족한 상태 |
| Total Liquidity Shortfall | 사용 가능한 전체 자금을 합쳐도 필수지출과 보호기준을 충족하지 못하는 상태 |
| Rebound Risk | 대응안이 현재 위험을 낮추면서 이후 기간의 위험을 새로 만들거나 키우는 상태 |
| Protected Fund | 세금 예비비 등 총자산에는 포함되지만 일반 지출에 사용할 수 없는 자금 |
| Essential Expense | 월세, 보험료, 대출 원리금 등 임의 감축 대상으로 취급하지 않는 지출 |
| Scheduled Cash Event | 미래 특정 날짜에 발생할 것으로 예상되는 수입 또는 지출 |
| Counterparty Evidence | 거래처별 과거 지급일, 실제 입금일, 지연일수, 지급 패턴 등의 근거 |
| Risk Presentation | 내부 확률과 수치를 사용자 상태, 날짜, 영향, 원인, 추천 행동으로 변환한 결과 |

---

# 4. 공통 데이터 규칙

## 4.1 금액

- 모든 금액은 대한민국 원 단위 정수로 저장한다.
- 소수 금액은 허용하지 않는다.
- 금액 필드에 통화기호나 쉼표를 포함하지 않는다.
- 음수 금액 대신 `direction` 또는 이벤트 유형으로 유입과 유출을 구분한다.
- 외화는 MVP 범위에서 제외한다.

예시:

```json
{
  "amount": 1500000,
  "currency": "KRW"
}
```

## 4.2 날짜와 시간

- 날짜는 `YYYY-MM-DD` 형식을 사용한다.
- 날짜와 시간이 필요한 경우 ISO 8601 형식을 사용한다.
- 모든 분석 결과에 분석 기준시점 `as_of`를 포함한다.
- 시간대 기본값은 `Asia/Seoul`로 한다.
- 13주 분석 기간은 `as_of` 날짜를 포함한 91일로 정의한다.
- 내부 계산은 날짜 단위로 수행한다.
- 사용자 화면은 1~4주를 날짜 중심, 5~13주를 주차 중심으로 표시할 수 있다.

## 4.3 식별자

- 각 엔티티는 시스템 내부 고유 식별자를 가진다.
- UUID 사용 여부는 구현 선택으로 두되 외부 계약에서는 문자열로 취급한다.
- 분석 관련 객체는 최소한 다음 식별자를 사용한다.

```text
snapshot_id
analysis_run_id
agent_run_id
recommendation_id
action_evaluation_id
```

## 4.4 상태값

상태값은 대문자 스네이크 케이스를 사용한다.

예시:

```text
CONFIRMED
ESTIMATED
OVERDUE
CANCELLED
PAYMENT_ACCOUNT
TOTAL_LIQUIDITY
```

---

# 5. 표준 도메인 모델

## 5.1 Account

```json
{
  "account_id": "account-001",
  "name": "생활비 계좌",
  "account_type": "CHECKING",
  "balance": 800000,
  "minimum_balance": 100000,
  "protected_amount": 0,
  "is_payment_account": true,
  "is_available_for_transfer": true,
  "updated_at": "2026-08-17T09:00:00+09:00"
}
```

### 필드

| 필드 | 타입 | 필수 | 설명 |
|---|---:|---:|---|
| account_id | string | O | 계좌 식별자 |
| name | string | O | 사용자 표시명 |
| account_type | enum | O | `CHECKING`, `SAVINGS`, `OTHER` |
| balance | integer | O | 현재 잔액 |
| minimum_balance | integer | O | 사용자가 유지하려는 최소잔액 |
| protected_amount | integer | O | 일반 가용자금에서 제외할 금액 |
| is_payment_account | boolean | O | 카드대금·자동이체 결제계좌 여부 |
| is_available_for_transfer | boolean | O | 대응안에서 이체 출금계좌로 사용할 수 있는지 |
| updated_at | datetime | O | 데이터 최신시각 |

### 규칙

- `protected_amount`는 `balance`보다 클 수 없다.
- `minimum_balance`는 0 이상이다.
- 일반 가용잔액은 다음과 같이 정의한다.

```text
available_balance
=
max(0, balance - protected_amount - minimum_balance)
```

---

## 5.2 Card

```json
{
  "card_id": "card-001",
  "name": "KB 신용카드",
  "payment_account_id": "account-001",
  "payment_day": 25,
  "current_billing_amount": 950000,
  "billing_date": "2026-08-25",
  "updated_at": "2026-08-17T09:00:00+09:00"
}
```

### 규칙

- 카드 개별 거래와 확정 카드 청구액을 같은 기간의 현금유출로 중복 반영하지 않는다.
- 카드 청구액이 확정된 경우 해당 청구액을 결제 이벤트로 사용한다.
- 카드 청구액이 아직 확정되지 않은 경우 개별 거래 기반 예상 청구액을 사용할 수 있다.
- 확정 청구액과 예상 청구액의 우선순위는 다음과 같다.

```text
확정 카드 청구액
> 카드사 제공 예정 청구액
> 개별 거래 기반 추정액
```

---

## 5.3 Transaction

```json
{
  "transaction_id": "txn-001",
  "account_id": "account-001",
  "occurred_at": "2026-08-01T10:20:00+09:00",
  "direction": "OUTFLOW",
  "amount": 45000,
  "category": "FOOD",
  "counterparty_name": "마트",
  "transaction_type": "PURCHASE",
  "source": "CSV",
  "is_internal_transfer": false
}
```

### transaction_type 예시

```text
PURCHASE
INCOME
TRANSFER
CARD_PAYMENT
LOAN_PAYMENT
TAX
REFUND
OTHER
```

### 규칙

- 본인 계좌 간 이동은 수입·지출로 집계하지 않는다.
- 내부 이체는 출금과 입금 거래를 하나의 이동으로 연결한다.
- 카드 구매 거래는 소비 분석에는 사용할 수 있지만 카드 청구액과 함께 현금유출로 중복 계산하지 않는다.
- 취소와 환불은 원거래와 연결할 수 있어야 한다.

---

## 5.4 Counterparty

```json
{
  "counterparty_id": "client-b",
  "name": "거래처 B",
  "counterparty_type": "CLIENT",
  "is_recurring": true,
  "payment_history_count": 10,
  "updated_at": "2026-08-17T09:00:00+09:00"
}
```

---

## 5.5 Receivable

```json
{
  "receivable_id": "recv-001",
  "counterparty_id": "client-b",
  "amount": 900000,
  "expected_date": "2026-08-20",
  "status": "ESTIMATED",
  "destination_account_id": "account-001",
  "user_confirmed": true,
  "source": "USER_INPUT",
  "updated_at": "2026-08-17T09:00:00+09:00"
}
```

### status

```text
CONFIRMED
ESTIMATED
OVERDUE
RECEIVED
CANCELLED
```

### 규칙

- `RECEIVED` 상태는 미래 수입으로 계산하지 않는다.
- `CANCELLED` 상태는 현금흐름에서 제외한다.
- `CONFIRMED`와 `ESTIMATED`는 서로 다른 불확실성 수준을 가진다.
- 예정일이 지났고 실제 입금이 없으면 `OVERDUE`로 전환할 수 있다.
- 사용자 확인이 없는 예정 수입을 확정 수입으로 취급하지 않는다.

---

## 5.6 ScheduledCashEvent

```json
{
  "event_id": "event-001",
  "event_type": "RECEIVABLE",
  "direction": "INFLOW",
  "amount": 900000,
  "expected_date": "2026-08-20",
  "account_id": "account-001",
  "counterparty_id": "client-b",
  "certainty": "ESTIMATED",
  "is_essential": false,
  "is_adjustable": false,
  "source": "USER_CONFIRMED",
  "source_reference_id": "recv-001"
}
```

### event_type

```text
RECEIVABLE
CARD_BILL
INSTALLMENT_PAYMENT
RENT
INSURANCE
UTILITY
LOAN_PAYMENT
TAX
SAVINGS
DISCRETIONARY_EXPENSE
OTHER_INFLOW
OTHER_OUTFLOW
```

### certainty

```text
CONFIRMED
ESTIMATED
UNCERTAIN
```

### 규칙

- 하나의 실제 금융 의무가 여러 데이터 원천에서 들어오더라도 현금흐름에는 한 번만 반영한다.
- `source_reference_id`를 이용해 원본 데이터와 연결한다.
- 필수지출은 `is_essential=true`로 표시한다.
- 조정 가능한 지출은 `is_adjustable=true`로 표시할 수 있다.

---

## 5.7 InstallmentPlan

```json
{
  "installment_plan_id": "inst-001",
  "card_id": "card-001",
  "original_amount": 1500000,
  "monthly_payment": 250000,
  "total_months": 6,
  "remaining_months": 5,
  "next_payment_date": "2026-08-25",
  "status": "ACTIVE"
}
```

### 규칙

- 할부 원거래 총액과 월별 할부 납부액을 동시에 현금유출로 반영하지 않는다.
- 현금흐름에는 분석기간 안에 실제로 청구될 월별 할부금만 반영한다.
- 현재 카드 청구액에 이번 달 할부금이 이미 포함된 경우 이번 달 할부금을 별도 추가하지 않는다.
- 이후 회차의 할부금은 예정 지출로 생성한다.
- `remaining_months`는 0 이상이다.

---

## 5.8 ProtectedFund

```json
{
  "protected_fund_id": "protected-001",
  "account_id": "account-002",
  "fund_type": "TAX_RESERVE",
  "amount": 1000000,
  "release_date": null,
  "user_confirmed": true
}
```

### fund_type

```text
TAX_RESERVE
EMERGENCY_RESERVE
BUSINESS_RESERVE
OTHER
```

### 규칙

- 보호자금은 총자산에는 포함한다.
- 보호자금은 일반 가용자금과 Safe-to-Spend 계산에서 제외한다.
- 사용자 승인 없이 보호자금을 사용하는 대응안을 생성하지 않는다.
- MVP의 기본 정책에서는 세금 예비비를 사용하는 대응안을 허용하지 않는다.

---

## 5.9 FinancialSnapshot

```json
{
  "snapshot_id": "snapshot-001",
  "user_id": "user-001",
  "as_of": "2026-08-17T09:00:00+09:00",
  "timezone": "Asia/Seoul",
  "accounts": [],
  "cards": [],
  "transactions": [],
  "scheduled_events": [],
  "receivables": [],
  "installment_plans": [],
  "protected_funds": [],
  "preferences": {
    "protection_level": 0.9,
    "minimum_total_reserve": 0
  },
  "data_quality": {
    "missing_sources": [],
    "stale_sources": [],
    "unconfirmed_items": []
  }
}
```

### 규칙

- 하나의 Analysis Run은 하나의 `snapshot_id`를 기준으로 실행한다.
- 분석 중 원본 금융데이터가 변경되어도 기존 스냅숏의 내용은 변경하지 않는다.
- 변경된 데이터는 새로운 스냅숏으로 생성한다.
- 스냅숏 생성 이후 데이터 수정이 발생하면 새로운 분석 실행이 필요하다.

---

# 6. 데이터 입력과 정규화

## 6.1 입력 원천

MVP 입력 원천:

```text
CSV
USER_INPUT
SYSTEM_DERIVED
```

향후 확장 원천:

```text
MYDATA_API
BANK_API
CARD_API
```

## 6.2 CSV 최소 요구사항

CSV 형식은 저장소에 제공되는 샘플 파일과 스키마 문서를 기준으로 한다.

최소 필드:

```text
transaction_id
account_id
occurred_at
direction
amount
description
```

선택 필드:

```text
category
counterparty_name
transaction_type
card_id
installment_months
source_reference_id
```

## 6.3 정규화 결과

Financial Data Adapter는 외부 데이터를 다음 공통 구조로 변환한다.

```text
Account
Card
Transaction
ScheduledCashEvent
Counterparty
Receivable
InstallmentPlan
ProtectedFund
```

## 6.4 중복 제거

중복 후보는 다음 근거를 조합해 탐지한다.

- 원본 식별자
- 계좌
- 날짜와 시간
- 금액
- 거래 설명
- 카드 또는 거래처
- 출금·입금 쌍
- source_reference_id

중복 여부가 불확실한 경우 자동 삭제하지 않고 사용자 확인 대상으로 남긴다.

## 6.5 반복 후보의 라벨 그룹핑

반복 수입·고정지출 후보는 다음 순서로 탐지한다.

1. 거래의 `counterparty_name` 또는 `description`에서 고유 라벨을 만든다.
2. 기본값에서는 방향과 `strip().casefold()`한 라벨의 완전일치로 그룹핑한다.
3. `FLOWGUARD_AI_CLASSIFICATION=on`이면 검증된 AI 라벨 그룹을 사용할 수 있다.
4. 어떤 그룹을 사용하든 반복 간격 20~40일, 금액 중앙값 대비 10% 편차,
   최소 발생 횟수(수입 3회·지출 2회)는 백엔드가 결정론적으로 검증한다.
5. 할부 후보는 AI 힌트가 아니라 `installment_months > 1`인 기존 경로에서만 만든다.

기존 완전일치 로직은 `deterministic_grouping()`에 보존하며 AI 미설정, 호출 실패,
응답 거부 시 폴백으로 사용한다. `FLOWGUARD_AI_CLASSIFICATION`의 기본값과 `off`는 AI를
호출하지 않고 기존 응답·후보·데이터 revision을 변경하지 않는다. `shadow`는 호출과
감사 기록만 수행하며 후보에 적용하지 않고, `on`만 검증 성공 그룹을 적용한다.

AI 그룹으로 만든 후보에는 분류 출처, 원 라벨, 카테고리·필수지출 제안을 표시한다.
사용자는 확정 전에 카테고리·필수지출 여부를 수정하거나 그룹을 해제할 수 있으며,
백엔드는 그룹 해제 시 해당 후보를 거부 상태로 바꾸고 같은 거래에
`deterministic_grouping()`을 다시 적용한다. 그룹 해제 표식은 라벨 집합과 방향을
기준으로 보존하여 이후 거래가 추가된 업로드에서도 같은 AI 그룹을 다시 적용하지
않는다. 해제된 원래 후보는 다시 확정할 수 없고, 그룹 해제와 후보 확정은 같은 사용자
revision 트랜잭션에서 선행 상태를 확인한다. 분류된 재업로드도 준비 시점의 revision을
검증하며, 경합하면 최신 그룹 해제 표식을 다시 대조한 뒤 저장한다.

---

# 7. 금융 코어 계약

## 7.1 책임

Deterministic Financial Core는 다음 값을 계산한다.

- 날짜별 계좌 잔액
- 날짜별 전체 가용자금
- 예정 수입 지연 시나리오
- 13주 현금흐름
- 결제계좌 부족
- 전체 유동성 부족
- 최초 위험 날짜
- 예상 부족금액 범위
- 내부 부족 확률
- Safe-to-Spend
- 대응안 적용 전후 결과
- 반동위험
- 정책 위반 여부

동일한 스냅숏, 설정, 시드에는 동일한 결과를 반환해야 한다.

## 7.2 비책임

금융 코어는 다음을 수행하지 않는다.

- 자연어 추천 문장 작성
- 위험 원인에 대한 자유형 추론
- 사용자에게 보여줄 최종 문구 생성
- 외부 계좌이체
- 금융상품 추천 문구 작성
- LLM 호출

---

# 8. 13주 현금흐름 계산

## 8.1 분석 기간

```text
분석 시작일 = snapshot.as_of의 날짜
분석 종료일 = 분석 시작일 + 90일
총 분석 기간 = 91일
```

## 8.2 기본 계산

각 날짜와 계좌별 종료 잔액은 다음 이벤트를 순서대로 반영한다.

```text
종료 잔액
=
시작 잔액
+ 해당 날짜의 유입
- 해당 날짜의 유출
```

계좌 간 내부 이체:

```text
출금계좌 잔액 감소
입금계좌 잔액 증가
전체 총자산 변화 없음
```

일별 위치는 표시용 잔액과 위험 판정용 안전여유를 구분한다.

```text
liquidity_margin
=
모든 계좌의 (잔액 - 보호자금 - 계좌별 최소잔액) 합계
- 최소 총예비비

available_balance = max(0, liquidity_margin)

payment_account_margin
=
결제계좌별 (잔액 - 보호자금 - 계좌별 최소잔액) 중 최솟값
```

`liquidity_margin`과 `payment_account_margin`은 안전기준 미달을 표현할 수 있도록
음수를 허용한다. 결제계좌에는 결제계좌로 등록된 계좌, 카드 결제계좌와 분석
기간에 유출이 예정된 계좌가 포함된다. 해당 계좌가 없으면 전체 계좌의
안전여유를 사용한다.

## 8.3 이벤트 처리 순서

같은 날짜에 여러 이벤트가 있을 때의 기본 순서:

1. 확정 수입
2. 내부 계좌이동
3. 필수지출
4. 카드대금 및 할부금
5. 선택지출
6. 저축 및 기타 조정 가능한 지출

정확한 은행 처리 순서가 데이터로 제공되는 경우 해당 순서를 우선한다.

같은 날짜 내 순서가 결과에 영향을 주지만 실제 순서를 알 수 없는 경우 데이터 신뢰도를 낮추고 보수적 순서를 사용한다.

## 8.4 분석 출력

```json
{
  "snapshot_id": "snapshot-001",
  "analysis_horizon_days": 91,
  "daily_positions": [
    {
      "date": "2026-08-25",
      "account_balances": {
        "account-001": -120000,
        "account-002": 1120000
      },
      "total_balance": 1000000,
      "available_balance": 0,
      "liquidity_margin": 0,
      "payment_account_margin": -220000,
      "protected_balance": 1000000,
      "status": "ACT_NOW",
      "triggering_event_ids": ["event-card-bill-001"]
    }
  ]
}
```

`daily_positions[].status`는 네 가지 지연 시나리오의 해당 날짜 부족 가능성과
이전에 발생해 실제 비보호 잔액 적자가 아직 해소되지 않은 부족, 필수지출 여부,
임박도와 날짜별 데이터 확인 범위를 금융 백엔드가 13.3의 규칙으로 판정한 값이다.
최소잔액이나 최소 총예비비 미달만으로 발생한 부족은 결제일 이후까지 영구
전파하지 않는다. 계좌 부족은 해당 계좌의 `잔액 - 보호자금`이, 전체 유동성
부족은 모든 계좌의 `잔액 - 보호자금` 합계가 0 이상으로 회복될 때 전파를 끝낸다.
최초 부족 판정에는 설정된 최소잔액과 최소 총예비비를 그대로 적용한다.
`triggering_event_ids`에는 해당 날짜에 발생한 이벤트뿐 아니라 아직 해소되지 않은
실제 적자의 원인 이벤트도 포함해 후속 주차에서 원인을 잃지 않도록 한다.
클라이언트는 잔액만 보고 별도의 위험 상태를 만들지 않는다.

---

# 9. 예정 수입 지연 모델

## 9.1 기본 시나리오

MVP는 최소한 다음 시나리오를 지원한다.

```text
ON_TIME
DELAY_3_DAYS
DELAY_7_DAYS
DELAY_14_DAYS
```

## 9.2 거래처 근거

거래처별로 다음 값을 계산할 수 있다.

- 지급 이력 수
- 예정일과 실제 입금일 차이
- 평균 지연일
- 중앙 지연일
- 최대 지연일
- 정시 지급 비율
- 최근 지급 지연 추세
- 데이터 최신성

## 9.3 데이터 부족

거래처 지급 이력이 부족한 기준은 `TBD`다.

데이터가 부족한 경우:

- 거래처별 결과에 낮은 신뢰도를 부여한다.
- 전체 거래처 또는 동일 유형 거래처의 기본 분포를 참고할 수 있다.
- 사용자에게 추가 확인이 필요한 상태로 표시할 수 있다.
- 임의로 정시 입금으로 확정하지 않는다.

## 9.4 확률 계산

내부 확률 산출 방식은 `TBD`다.

허용 가능한 MVP 구현:

- 경험적 빈도 기반 분포
- 거래처별 분포와 전체 기본 분포의 가중 결합
- 고정된 데모 분포

선택한 방법은 코드와 테스트에서 재현 가능해야 하며 `model_version` 또는 `tool_version`으로 기록한다.

---

# 10. 부족 유형 판정

## 10.1 결제계좌 부족

다음 조건을 만족하면 `PAYMENT_ACCOUNT` 부족으로 분류한다.

```text
결제 시점의 대상 계좌 잔액 < 결제금액
AND
보호자금을 제외한 다른 계좌의 이동 가능 자금으로 부족분 충당 가능
```

예시 사용자 메시지:

```text
이 계좌에서 결제가 실패할 수 있어요.
다른 계좌에는 필요한 자금이 있습니다.
```

## 10.2 전체 유동성 부족

다음 조건을 만족하면 `TOTAL_LIQUIDITY` 부족으로 분류한다.

```text
전체 비보호 가용자금
<
분석 시점까지 필요한 필수지출과 최소 안전잔액
```

예시 사용자 메시지:

```text
다른 계좌의 돈을 모아도 부족할 수 있어요.
```

## 10.3 부족 유형 우선순위

같은 시점에 두 조건이 모두 나타나면 `TOTAL_LIQUIDITY`를 우선한다.

---

# 11. Safe-to-Spend

## 11.1 정의

Safe-to-Spend는 향후 13주 동안 다음 조건을 유지하면서 오늘 추가로 지출할 수 있는 최대 금액이다.

- 필수지출을 지급할 수 있음
- 보호자금을 침범하지 않음
- 계좌별 최소잔액을 유지함
- 사용자 설정 보호수준을 충족함
- 허용된 부족확률 기준을 충족함

## 11.2 입력

```json
{
  "snapshot_id": "snapshot-001",
  "protection_level": 0.9,
  "horizon_days": 91
}
```

## 11.3 출력

```json
{
  "snapshot_id": "snapshot-001",
  "safe_to_spend": 170000,
  "protection_level": 0.9,
  "binding_constraint": {
    "date": "2026-08-25",
    "event_id": "event-card-bill-001",
    "reason": "CARD_BILL"
  },
  "data_confidence": 0.76
}
```

## 11.4 계산 방식

정확한 탐색 알고리즘은 구현 선택으로 둔다.

권장 방식:

1. 오늘 발생하는 가상 선택지출 금액을 설정한다.
2. 전체 시나리오에서 13주 현금흐름을 다시 계산한다.
3. 보호수준 조건을 만족하는 최대 금액을 탐색한다.
4. 결과를 원 단위 정수로 반환한다.

## 11.5 기본값

- 데모 기본 보호수준: `0.90`
- 운영 기본값 확정 여부: `TBD`
- 사용자 화면 노출 방식: `TBD`

---

# 12. 내부 위험 지표

```json
{
  "shortfall_probability": 0.37,
  "payment_account_shortfall_probability": 0.68,
  "total_liquidity_shortfall_probability": 0.37,
  "first_risk_date": "2026-08-25",
  "shortfall_type": "PAYMENT_ACCOUNT",
  "expected_gap_min": 80000,
  "expected_gap_max": 150000,
  "days_until_risk": 8,
  "data_confidence": 0.76
}
```

## 12.1 필수 지표

- `shortfall_probability`
- `payment_account_shortfall_probability`
- `total_liquidity_shortfall_probability`
- `first_risk_date`
- `shortfall_type`
- `expected_gap_min`
- `expected_gap_max`
- `days_until_risk`
- `data_confidence`

## 12.2 범위 규칙

- 확률과 신뢰도는 0 이상 1 이하의 실수다.
- 부족금액은 0 이상의 원 단위 정수다.
- 위험이 없으면 `first_risk_date`와 `shortfall_type`은 `null`일 수 있다.
- 위험이 없으면 부족금액 범위는 0으로 반환한다.

---

# 13. 위험 상태 변환

## 13.1 사용자 상태

```text
STABLE
VERIFY
PREPARE
ACT_NOW
```

| 상태 | 사용자 표시 |
|---|---|
| STABLE | 현재는 안전해요 |
| VERIFY | 확인할 정보가 있어요 |
| PREPARE | 미리 준비가 필요해요 |
| ACT_NOW | 지금 조치가 필요해요 |

## 13.2 판정 입력

상태는 다음 값을 함께 사용해 결정한다.

- 부족 가능성
- 부족금액 범위
- 위험일까지 남은 기간
- 필수결제 여부
- 전체 가용자금
- 데이터 신뢰도
- 미확인 예정 수입
- 데이터 최신성

## 13.3 판정 규칙

현재 구현은 `risk-presentation-rules-v2`의 명시적 규칙을 사용한다.

위험일이 있는 경우:

- `ACT_NOW`: 위험일까지 3일 이내이고 (`부족확률 >= 0.75` 또는 필수결제 영향)임
- `PREPARE`: 부족확률이 0.25 이상이거나 위험일까지 14일 이내임
- `VERIFY`: 위험은 있으나 위 조건에 해당하지 않음

여러 조건을 동시에 만족하면 위 목록의 높은 상태를 우선한다.

위험일이 없는 경우:

- 전체 Risk Presentation은 확인이 필요한 데이터가 있거나 데이터 신뢰도가
  0.60 미만이면 `VERIFY`, 그렇지 않으면 `STABLE`로 판정한다.
- 일별 상태에서 누락되거나 오래된 데이터 원천이 있으면 전체 91일에
  `VERIFY` 조건을 적용한다. 이미 `RECEIVED` 또는 `CANCELLED`인 채권의 최신성은
  미래 현금흐름과 무관하므로 이 조건에서 제외한다.
- 일별 상태에서 미확인 예정수입은 예정일부터 최대 지연 시나리오인 14일
  뒤까지, `ESTIMATED` 예정 이벤트는 해당 예정일에만 `VERIFY` 조건을 적용한다.
- 일별 확인 범위 밖이고 다른 위험이 없으면 `STABLE`로 판정한다.

필수 원칙:

- 해당 날짜에 적용되는 데이터 부족만으로 `STABLE`을 반환하지 않는다.
- 위험확률이 낮더라도 위험일이 임박하고 필수결제 영향이 크면 높은 상태를 선택할 수 있다.
- 날짜가 한정된 불확실성을 위험과 무관한 전체 기간의 경보로 확장하지 않는다.
- 최소잔액 또는 최소 총예비비 미달만으로 지난 부족을 `ACT_NOW`로 계속 전파하지 않는다.
- 사용자 화면에는 내부 확률을 기본 정보로 노출하지 않는다.
- 내부 확률은 평가, 비교, 알림 기준, 감사 로그에 유지한다.

## 13.4 Presentation 출력

```json
{
  "status": "PREPARE",
  "status_label": "미리 준비가 필요해요",
  "title": "8일 뒤 카드대금을 준비하세요",
  "impact": "약 8만~15만 원이 부족할 수 있습니다",
  "cause": "거래처 B의 입금 일정이 불확실합니다",
  "recommended_action": "결제계좌에 10만 원을 미리 확보하세요",
  "confidence_label": "분석 신뢰도 보통",
  "rule_version": "risk-presentation-rules-v2"
}
```

---

# 14. 백엔드 조사기 계약

## 14.1 역할

Liquidity Investigator Agent는 백엔드 분석 책임을 담당하는 구성요소다. 현재 MVP에서는
`flowguard-api` 프로세스 안에서 동기 실행하고, 운영 확장 시 같은 책임을
`flowguard-worker`로 분리한다. 다음 작업을 수행한다.

- 기준 분석 결과 확인
- 주요 위험 원인 가설 생성
- 필요한 추가 근거 선택
- MCP 계약과 동일한 코어 도구 호출
- 후보 대응안 구성
- 대응안 평가 결과 검토
- 효과가 부족하거나 반동위험이 있는 계획 수정
- 정책 검증 결과 확인
- 최종 추천 근거 구조화

별도 AI 서비스가 존재하더라도, 다음 책임은 백엔드 조사기에 남는다.

- MCP 계약과 동일한 코어 도구 선택 및 호출
- 후보 대응안 생성
- 후보 대응안 평가
- 정책 검증 통과 여부 판단
- 추천 저장 및 실행 상태 기록

## 14.2 제한

에이전트는 다음 값을 직접 계산하거나 생성하지 않는다.

- 계좌 잔액
- 날짜별 현금흐름
- 부족금액
- 부족확률
- Safe-to-Spend
- 할부 납부액
- 대응안 적용 결과
- 정책 위반 여부

에이전트는 데이터베이스에 직접 접근하지 않는다.

에이전트는 정의되지 않은 행동 유형을 생성하지 않는다.

에이전트는 정책 검증을 통과하지 않은 대응안을 최종 추천으로 확정하지 않는다.

에이전트는 OpenAI API 키를 소유하지 않는다.

에이전트는 별도 AI 서비스 없이도 결정론적 후보 생성을 완료할 수 있어야 한다.

## 14.3 실행 한도

초기 기본값:

```text
최대 MCP 호출 수: 10
최대 계획 수정 횟수: 3
최대 후보 대응안 수: 5
사용자 노출 대응안 수: 우선 추천 1개 + 대안 최대 2개
```

운영 기본값 확정 여부는 `TBD`다.

---

# 15. 에이전트 상태

```json
{
  "snapshot_id": "snapshot-001",
  "analysis_run_id": "analysis-001",
  "agent_run_id": "agent-001",
  "trigger_context": {
    "type": "DATA_REFRESH"
  },
  "baseline_result": {},
  "risk_hypotheses": [],
  "gathered_evidence": [],
  "tool_calls": [],
  "candidate_plans": [],
  "evaluated_plans": [],
  "recommended_plan": null,
  "policy_result": null,
  "decision_trace": {
    "mode": "DETERMINISTIC",
    "model": null,
    "fallback_reason": null,
    "steps": [],
    "usage": null
  },
  "interpretation_request_id": null,
  "analysis_complete": false
}
```

`decision_trace.steps`는 위험 가설, 실제 도구 호출, 후보 평가, 최종 선택처럼
저장된 실행 산출물만 포함한다. 모델의 숨은 사고과정이나 원문 추론 토큰은 저장하거나
사용자에게 노출하지 않는다. AI 해석 요청 및 응답은 별도 `ai_interpretation_runs`
모델에 저장하며, 조사기 상태와 분리한다.

---

# 16. 행동 카탈로그

에이전트는 다음 행동 유형만 생성할 수 있다.

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

## 16.1 공통 행동 구조

```json
{
  "action_id": "action-001",
  "type": "transfer",
  "parameters": {},
  "requires_user_approval": true,
  "assumptions": [],
  "source_evidence_ids": []
}
```

## 16.2 transfer

```json
{
  "type": "transfer",
  "parameters": {
    "from_account_id": "account-002",
    "to_account_id": "account-001",
    "amount": 100000,
    "execution_date": "2026-08-22"
  }
}
```

### 규칙

- 출금계좌와 입금계좌는 달라야 한다.
- 이동금액은 0보다 커야 한다.
- 이동금액은 출금계좌의 비보호 가용잔액 이하여야 한다.
- 실행일은 해결하려는 위험일보다 늦을 수 없다.
- 총자산은 변하지 않는다.
- 사용자 승인이 필요하다.

## 16.3 reserve_funds

```json
{
  "type": "reserve_funds",
  "parameters": {
    "account_id": "account-002",
    "amount": 300000,
    "fund_type": "TAX_RESERVE"
  }
}
```

### 규칙

- 일반 가용자금을 보호자금으로 재분류한다.
- 총자산은 변하지 않는다.
- Safe-to-Spend는 감소할 수 있다.
- 사용자 승인이 필요하다.

## 16.4 adjust_discretionary_budget

```json
{
  "type": "adjust_discretionary_budget",
  "parameters": {
    "amount": 40000,
    "start_date": "2026-08-17",
    "end_date": "2026-08-31"
  }
}
```

### 규칙

- 필수지출을 감소 대상으로 포함하지 않는다.
- 선택지출 예상액을 지정 기간 동안 줄인다.
- 감소 가능 금액보다 큰 값을 적용하지 않는다.
- 사용자 승인이 필요하다.

## 16.5 pause_savings

```json
{
  "type": "pause_savings",
  "parameters": {
    "event_ids": ["event-saving-001"],
    "start_date": "2026-08-17",
    "end_date": "2026-09-17"
  }
}
```

### 규칙

- 조정 가능한 저축 이벤트만 대상으로 한다.
- 대출 상환, 보험료, 세금, 필수지출에는 적용하지 않는다.
- 사용자 승인이 필요하다.

## 16.6 shift_payment_date

```json
{
  "type": "shift_payment_date",
  "parameters": {
    "event_id": "event-card-bill-001",
    "from_date": "2026-08-25",
    "to_date": "2026-09-01"
  }
}
```

### 규칙

- 실제 변경 가능성이 있는 이벤트만 대상으로 한다.
- MVP에서는 가상 적용만 수행한다.
- 변경 후 13주 반동위험을 반드시 검사한다.
- 사용자 승인이 필요하다.
- 외부 금융기관에 실제 요청을 전송하지 않는다.

## 16.7 delay_purchase

```json
{
  "type": "delay_purchase",
  "parameters": {
    "amount": 1500000,
    "from_date": "2026-08-20",
    "to_date": "2026-10-01"
  }
}
```

### 규칙

- 아직 확정되지 않은 구매 계획에만 적용한다.
- 이미 발생한 거래에는 적용하지 않는다.
- 사용자 승인이 필요하다.

## 16.8 add_installment

```json
{
  "type": "add_installment",
  "parameters": {
    "purchase_amount": 1500000,
    "installment_months": 6,
    "first_payment_date": "2026-09-25",
    "card_id": "card-001"
  }
}
```

### 규칙

- 신규 구매 사전점검에서 사용한다.
- 월별 할부액 계산 방식은 카드 수수료 정책이 없는 경우 원금 균등 단순 분할로 처리할 수 있다.
- 수수료 포함 방식은 `TBD`다.
- 기존 카드 청구액과 중복 반영하지 않는다.
- 실제 할부 신청은 수행하지 않는다.

## 16.9 confirm_receivable

```json
{
  "type": "confirm_receivable",
  "parameters": {
    "receivable_id": "recv-001"
  }
}
```

### 규칙

- 현금흐름을 즉시 변경하지 않는다.
- 사용자 또는 거래처 확인 결과가 입력된 뒤 새로운 스냅숏에서 재분석한다.
- 확인 전에는 예정 수입을 확정 수입으로 바꾸지 않는다.

---

# 17. 대응안 평가

## 17.1 입력

```json
{
  "snapshot_id": "snapshot-001",
  "actions": [
    {
      "type": "transfer",
      "parameters": {
        "from_account_id": "account-002",
        "to_account_id": "account-001",
        "amount": 100000,
        "execution_date": "2026-08-22"
      }
    }
  ]
}
```

## 17.2 처리

1. 행동 스키마를 검증한다.
2. 현재 스냅숏의 복사본을 만든다.
3. 행동을 복사본에 가상 적용한다.
4. 13주 현금흐름을 다시 계산한다.
5. 적용 전후 위험지표를 비교한다.
6. 이후 위험 증가 여부를 확인한다.
7. 금융 안전정책을 검사한다.
8. 검증 결과를 반환한다.

## 17.3 출력

```json
{
  "valid": true,
  "before": {
    "status": "ACT_NOW",
    "risk_metrics": {}
  },
  "after": {
    "status": "STABLE",
    "risk_metrics": {}
  },
  "risk_shift": {
    "detected": false,
    "source_date": null,
    "target_date": null
  },
  "policy_violations": [],
  "requires_user_approval": true
}
```

---

# 18. 반동위험

## 18.1 정의

대응안 적용 후 다음 중 하나가 발생하면 반동위험 후보로 본다.

- 기존 위험보다 이후 날짜에 새로운 부족이 발생함
- 이후 필수결제의 부족 가능성이 증가함
- 이후 예상 부족금액이 증가함
- 이후 Safe-to-Spend가 의미 있게 감소함
- 보호자금 또는 최소 안전잔액이 침범됨

## 18.2 판정 기준

정량 임곗값은 `TBD`다.

최소 필수 규칙:

- 적용 전 위험이 없던 날짜에 적용 후 필수결제 부족이 발생하면 반동위험으로 판정한다.
- 현재 위험을 해결했지만 13주 내 전체 유동성 부족이 새로 발생하면 반동위험으로 판정한다.
- 정책 위반이 발생하면 대응안을 채택하지 않는다.

---

# 19. 금융 안전정책

## 19.1 필수 정책

- 세금 보호자금 침범 금지
- 필수생활비 감축 추천 금지
- 최소 안전잔액 침범 금지
- 기존 할부 의무 누락 금지
- 카드 청구액과 카드 거래 중복 금지
- 할부 원거래와 월별 할부금 중복 금지
- 데이터 최신성 확인
- 오래된 스냅숏 기반 행동 재검증
- 사용자 승인 필요 행동 식별
- 실제 금융거래 실행 금지

## 19.2 정책 결과

```json
{
  "valid": false,
  "violations": [
    {
      "code": "PROTECTED_FUND_VIOLATION",
      "message": "세금 보호자금을 사용할 수 없습니다.",
      "action_id": "action-001"
    }
  ],
  "requires_user_approval": true
}
```

---

# 20. 서비스 및 MCP 도구 계약

현재 MVP의 조사기는 아래 MCP 계약과 동일한 `CoreToolService` 구현을 API 프로세스
안에서 호출한다. `flowguard.mcp_server`는 같은 일곱 도구의 FastMCP 진입점을
제공하지만, 별도 MCP 프로세스와 transport 연결은 운영 확장 범위다.

## 20.1 AI 해석 서버 계약(백엔드 ↔ AI Service)

백엔드와 AI 서비스는 다음 JSON 계약을 사용한다. 계약은 `schemaVersion: "1.1"`을 기준으로 버전 관리한다.
이 계약은 **구조화 해석 전용**이며, 금융 계산·MCP 호출·후보 생성 책임을 AI 서비스에 넘기지 않는다.

### 20.1.1 백엔드 → AI 요청

```json
{
  "schemaVersion": "1.1",
  "contractVersion": "1.1",
  "promptVersion": "3",
  "requestId": "ai-request-001",
  "idempotencyKey": "analysis-001:rev-7:contract-1.1:prompt-3:ko-KR",
  "analysisId": "analysis-001",
  "snapshotId": "snapshot-007",
  "snapshotRevision": "rev-7",
  "locale": "ko-KR",
  "calculatedAt": "2026-07-26T21:30:00+09:00",
  "facts": {
    "safeToSpend": 180000,
    "nextRisk": {
      "type": "PAYMENT_ACCOUNT_SHORTAGE",
      "date": "2026-08-03",
      "shortageAmount": 240000
    },
    "cashflowSummary": {
      "lowestBalance": -240000,
      "lowestBalanceDate": "2026-08-03"
    }
  },
  "evidence": [],
  "actionCandidates": []
}
```

### 20.1.2 AI → 백엔드 응답

```json
{
  "schemaVersion": "1.1",
  "contractVersion": "1.1",
  "promptVersion": "3",
  "requestId": "ai-request-001",
  "idempotencyKey": "analysis-001:rev-7:contract-1.1:prompt-3:ko-KR",
  "analysisId": "analysis-001",
  "snapshotId": "snapshot-007",
  "snapshotRevision": "rev-7",
  "locale": "ko-KR",
  "riskExplanation": "string",
  "rankedActions": [
    {
      "actionId": "transfer-1",
      "priority": 1,
      "reason": "string"
    }
  ],
  "userMessage": "string"
}
```

### 20.1.3 계약 규칙

- AI는 기존 `actionCandidates`의 순위화와 설명 생성만 수행한다.
- AI는 새 금액, 새 날짜, 새 일수, 새 비율·퍼센트, 새 위험 판정, 새 행동 유형을 생성하지 않는다.
- AI 응답에 백엔드가 제공하지 않은 금액·날짜·일수·비율·행동이 들어오면 백엔드는 해당 응답을 신뢰하지 않는다.
- 같은 숫자라도 금액·일수·비율처럼 의미 범주가 다르면 공급된 값으로 인정하지 않는다.
- 평균·중앙·최대 지연일수는 서로 바꾸어 설명하지 않는다.
- 정시 지급률이나 데이터 신뢰도 같은 비율을 위험 확률처럼 다른 의미로 바꾸어 설명하지 않는다.
- AI 응답이 잘못된 JSON이거나 스키마를 위반하면, 백엔드는 규칙 기반 폴백 메시지로 처리한다.
- AI 서비스는 DB에 직접 접근하지 않는다.
- AI 서비스는 MVP 기준 MCP를 직접 호출하지 않는다.
- 연결 타임아웃은 3초, 응답 타임아웃은 10초, 전체 요청 한도는 15초로 한다.
- 재시도는 최대 1회만 허용한다.
- 재시도 가능 오류는 연결 실패, 일시적 타임아웃, `429`, `502`, `503`, `504`다.
- 재시도 금지 오류는 JSON 구조 오류, `analysisId`/`requestId`/`snapshotRevision` 불일치, 허용되지 않은 `actionId`, 명백한 4xx 계약 오류다.
- AI 서버 실패 시 백엔드는 계산 결과와 규칙 기반 폴백 문장을 반환한다.
- 같은 `analysisId`로 요청 전송은 여러 번 허용한다.
- 단, 같은 `idempotencyKey`를 가진 논리적 요청은 한 번만 실행한다.
- 동일 `idempotencyKey` 요청이 이미 실행 중이면 기존 실행 상태를 반환한다.
- 동일 `idempotencyKey` 요청이 이미 성공했으면 저장된 결과를 반환한다.
- 명시적 재생성 요청은 새로운 `promptVersion` 또는 `regenerationId`로 구분한다.

### 20.1.4 처리 규칙

- `requestId`는 백엔드가 생성한 AI 요청 식별자와 일치해야 한다.
- `analysisId`는 백엔드 분석 요청의 식별자와 일치해야 한다.
- `snapshotId`는 백엔드가 해석 요청에 사용한 스냅숏과 일치해야 한다.
- `snapshotRevision`은 백엔드가 해석 요청에 사용한 revision과 일치해야 한다.
- `idempotencyKey`는 백엔드가 생성한 논리적 요청 키와 일치해야 한다.
- `rankedActions`의 `actionId`는 백엔드가 제공한 후보 중 하나여야 한다.
- `rankedActions`의 `actionId`는 중복될 수 없다.
- `rankedActions`의 `priority`는 중복될 수 없다.
- `reason`은 사용자에게 노출될 수 있는 설명이므로 비속어·과장·허위 진술을 포함하지 않는다.
- `userMessage`는 1~2문장 내로 간결하게 작성한다.

### 20.1.5 라벨 분류 계약 (`POST /classify/labels`)

라벨 분류는 해석 계약과 독립된 `schemaVersion: "1.2"`,
`contractVersion: "1.2"`, `promptVersion: "classify-1"`을 사용한다. 기존
`/interpret` 계약은 `1.1`을 유지한다.

요청 예시는 다음과 같다.

```json
{
  "schemaVersion": "1.2",
  "contractVersion": "1.2",
  "promptVersion": "classify-1",
  "requestId": "ai-classify-opaque",
  "idempotencyKey": "import-opaque:label-hash:contract-1.2:classify-1:ko-KR",
  "importId": "import-opaque",
  "locale": "ko-KR",
  "labels": [
    {
      "labelId": "label-opaque-a",
      "text": "디자인컴퍼니",
      "direction": "INFLOW",
      "occurrences": 3
    }
  ]
}
```

각 `labels` 항목에 허용되는 필드는 `labelId`, `text`, `direction`, `occurrences`뿐이다.
금액, 거래일, 계좌, 잔액, 거래 ID는 포함하지 않는다. 라벨 본문에 포함된 아라비아
숫자, 한국어·영어 금액 및 날짜 표현은 `<NUM>`으로, 원천 식별자는 다른 문자열에
붙어 있어도 `<ID>`로 치환한다.

응답의 그룹 필드는 다음과 같다.

```json
{
  "schemaVersion": "1.2",
  "contractVersion": "1.2",
  "promptVersion": "classify-1",
  "requestId": "ai-classify-opaque",
  "idempotencyKey": "import-opaque:label-hash:contract-1.2:classify-1:ko-KR",
  "importId": "import-opaque",
  "locale": "ko-KR",
  "groups": [
    {
      "groupId": "group-client",
      "labelIds": ["label-opaque-a"],
      "normalizedName": "디자인컴퍼니",
      "entityKind": "CLIENT",
      "categoryHint": "RECEIVABLE",
      "essentialHint": null,
      "confidence": "HIGH",
      "reason": "동일 상호로 판단되는 표기입니다."
    }
  ],
  "ungrouped": []
}
```

`entityKind` 허용값은 다음과 같다.

```text
CLIENT
MERCHANT
PLATFORM
CARD_PAYMENT
OTHER
```

`categoryHint`는 `EventType`과 같은 다음 12개 값만 허용한다.

```text
RECEIVABLE
CARD_BILL
INSTALLMENT_PAYMENT
RENT
INSURANCE
UTILITY
LOAN_PAYMENT
TAX
SAVINGS
DISCRETIONARY_EXPENSE
OTHER_INFLOW
OTHER_OUTFLOW
```

`confidence`는 `LOW`, `MEDIUM`, `HIGH` 중 하나다. `INFLOW` 그룹은
`RECEIVABLE|OTHER_INFLOW`, `OUTFLOW` 그룹은 나머지 지출 힌트만 허용한다.
`categoryHint`는 기존 후보 유형을 바꾸지 않는다. 특히 `CARD_BILL`과
`INSTALLMENT_PAYMENT`는 실제 `card_id` 또는 `installment_plan_id` 연결 없이는
확정 이벤트의 유형으로 승격하지 않는다.

백엔드는 다음 중 하나라도 위반하면 응답 전체를 `REJECTED`하고 완전일치 그룹으로
폴백한다.

- 요청에 없는 `labelId`
- 그룹·미분류 목록 사이의 중복 `labelId`
- 어느 쪽에도 포함되지 않은 요청 라벨
- 서로 다른 방향이 섞인 그룹
- 닫힌 열거형 또는 방향과 맞지 않는 `categoryHint`
- `normalizedName`·`reason`의 숫자 표현
- 계약 식별자 에코 불일치

연결·타임아웃·HTTP 실패는 `FAILED`, 계약·검증 실패는 `REJECTED`, 검증 성공은
`SUCCEEDED`로 기록한다. 이 상태는 금융 분석 상태와 별개이며 어느 실패도 CSV
가져오기를 중단시키지 않는다.

같은 사용자·모드·라벨 집합·스키마·계약·프롬프트·locale은 안정적인 `importId`를
사용한다. 한 실행의
네트워크 재시도와 동시 요청은 같은 `idempotencyKey`를 공유하고, 성공 결과는 이후
동일 업로드에서 재사용한다. `FAILED` 또는 `REJECTED` 뒤 사용자가 다시 업로드하면 이전
`classification_id`에서 파생한 `:retry-<classification_id>` 접미사의 새 시도 키를
사용해 일시 장애가 영구 고착되지 않게 한다.

## 20.2 공통 규칙

- 모든 도구 입력에 `snapshot_id` 또는 이를 추적할 수 있는 식별자를 포함한다.
- 모든 계산 도구 결과에 `tool_version`을 포함한다.
- 에이전트는 도구 결과를 임의로 수정하지 않는다.
- 도구 실패 시 가짜 결과를 반환하지 않는다.
- 계산 도구는 구조화된 JSON을 반환한다.

---

## 20.3 get_financial_context

### 입력

```json
{
  "snapshot_id": "snapshot-001"
}
```

### 출력

```json
{
  "snapshot_id": "snapshot-001",
  "as_of": "2026-08-17T09:00:00+09:00",
  "accounts": [],
  "cards": [],
  "receivables": [],
  "scheduled_events": [],
  "installment_plans": [],
  "protected_funds": [],
  "data_quality": {}
}
```

---

## 20.4 get_counterparty_evidence

### 입력

```json
{
  "snapshot_id": "snapshot-001",
  "counterparty_id": "client-b"
}
```

### 출력

```json
{
  "counterparty_id": "client-b",
  "payment_history_count": 10,
  "on_time_rate": 0.5,
  "average_delay_days": 5.8,
  "median_delay_days": 4,
  "maximum_delay_days": 14,
  "recent_trend": "WORSENING",
  "data_confidence": 0.76
}
```

---

## 20.5 query_financial_events

### 입력

```json
{
  "snapshot_id": "snapshot-001",
  "date_from": "2026-08-17",
  "date_to": "2026-09-17",
  "directions": ["INFLOW", "OUTFLOW"],
  "event_types": ["RECEIVABLE", "CARD_BILL"]
}
```

### 출력

```json
{
  "events": [
    {
      "event_id": "event-card-bill-001",
      "event_type": "CARD_BILL",
      "direction": "OUTFLOW",
      "amount": 950000,
      "expected_date": "2026-08-25",
      "account_id": "account-001",
      "counterparty_name": null,
      "description": "생활비 카드 결제대금 950,000원이 생활비 결제계좌에서 출금될 예정입니다."
    }
  ]
}
```

`description`은 원장 필드와 연결된 계좌·카드·거래처 이름만 사용해 백엔드가
결정적으로 만드는 사용자용 설명이다.

---

## 20.6 simulate_cashflow

### 입력

```json
{
  "snapshot_id": "snapshot-001",
  "horizon_days": 91,
  "scenario": {
    "receivable_delay_mode": "EMPIRICAL"
  },
  "seed": 42
}
```

### 출력

```json
{
  "snapshot_id": "snapshot-001",
  "daily_positions": [],
  "risk_metrics": {},
  "tool_version": "cashflow-2"
}
```

---

## 20.7 calculate_safe_to_spend

### 입력

```json
{
  "snapshot_id": "snapshot-001",
  "horizon_days": 91,
  "protection_level": 0.9,
  "seed": 42
}
```

### 출력

```json
{
  "snapshot_id": "snapshot-001",
  "safe_to_spend": 170000,
  "binding_constraint": {},
  "data_confidence": 0.76,
  "tool_version": "safe-to-spend-1"
}
```

---

## 20.8 evaluate_action_plan

### 입력

```json
{
  "snapshot_id": "snapshot-001",
  "actions": [],
  "seed": 42
}
```

### 출력

```json
{
  "valid": true,
  "before": {},
  "after": {},
  "risk_shift": {},
  "policy_violations": [],
  "tool_version": "action-evaluator-1"
}
```

---

## 20.9 validate_financial_policy

### 입력

```json
{
  "snapshot_id": "snapshot-001",
  "actions": [],
  "evaluation_result": {}
}
```

### 출력

```json
{
  "valid": true,
  "violations": [],
  "requires_user_approval": true,
  "policy_version": "policy-1"
}
```

---

# 21. REST API 계약

세부 OpenAPI 스키마는 구현 시 생성한다. 다음 엔드포인트와 책임을 유지한다.

## 21.1 데이터

```text
POST  /api/v1/imports/transactions
POST  /api/v1/setup/commit
POST  /api/v1/demo/reset
GET   /api/v1/transactions
PATCH /api/v1/transactions/{transaction_id}

GET   /api/v1/scheduled-events
POST  /api/v1/scheduled-events
PATCH /api/v1/scheduled-events/{event_id}
```

`setup/commit`은 최초 설정의 환경설정과 모든 후보 결정을 한 트랜잭션으로
저장한다. `demo/reset`은 확인 문자열을 받은 뒤 현재 데모 사용자 범위의 데이터와
분석 산출물만 초기화한다.

## 21.2 분석

```text
POST /api/v1/analyses
GET  /api/v1/analyses/{analysis_id}
GET  /api/v1/analyses/{analysis_id}/events
```

## 21.3 사용자 화면

```text
GET /api/v1/dashboard
GET /api/v1/reports/latest
GET /api/v1/cashflow/timeline
GET /api/v1/risks/next
GET /api/v1/receivables
GET /api/v1/installments
```

`GET /api/v1/cashflow/timeline`은 금융 코어 결과를 화면용 읽기 모델로 변환해
반환한다.

```json
{
  "analysis_id": "analysis-001",
  "snapshot_id": "snapshot-001",
  "as_of": "2026-08-24T09:00:00+09:00",
  "analysis_horizon_days": 91,
  "horizon_days": 91,
  "balance_basis": "PAYMENT_ACCOUNT",
  "balance_basis_label": "결제계좌 안전여유",
  "requires_reanalysis": false,
  "daily_positions": [
    {
      "date": "2026-08-25",
      "available_balance": 850000,
      "liquidity_margin": 850000,
      "payment_account_margin": -120000,
      "safety_margin": -120000,
      "worst_case_safety_margin": -250000,
      "status": "ACT_NOW",
      "triggering_event_ids": ["event-card-bill-001"]
    }
  ],
  "weekly_positions": [
    {
      "week": 1,
      "start_date": "2026-08-24",
      "end_date": "2026-08-30",
      "min_available_balance": 850000,
      "min_safety_margin": -250000,
      "status": "ACT_NOW",
      "causes": ["생활비 카드 결제대금 950,000원이 생활비 결제계좌에서 출금될 예정입니다."]
    }
  ],
  "scenarios": [
    {
      "scenario": "ON_TIME",
      "scenario_label": "기준",
      "daily_positions": [],
      "shortfalls": []
    }
  ],
  "is_virtual": false
}
```

위 예시는 필드 구조를 보여주기 위해 `daily_positions`, `weekly_positions`와
`scenarios` 배열의 나머지 항목을 생략했다. 실제 응답의 지연 시나리오는 9.1의
네 가지 시나리오를 모두 포함한다.

- 최상위 위험 유형이 `PAYMENT_ACCOUNT`이면 `payment_account_margin`을 선택해
  `balance_basis="PAYMENT_ACCOUNT"`, `balance_basis_label="결제계좌 안전여유"`로
  반환한다. 그 외에는 `liquidity_margin`을 선택해
  `balance_basis="TOTAL_LIQUIDITY"`, `balance_basis_label="전체 유동성 안전여유"`로
  반환한다.
- `daily_positions[].safety_margin`은 기준 시나리오의 선택된 부호 있는 여유액이다.
- `daily_positions[].worst_case_safety_margin`은 같은 날짜의 네 가지 지연
  시나리오 중 가장 작은 선택 여유액이다.
- `weekly_positions`는 91일을 7일씩 묶은 13개 항목이다. 각 항목의
  `min_available_balance`와 `min_safety_margin`은 해당 주와 모든 시나리오의
  각 필드 최솟값이며, `status`는 `STABLE < VERIFY < PREPARE < ACT_NOW`
  순서에서 가장 높은 상태다.
- `weekly_positions[].causes`는 해당 주의 `triggering_event_ids`에 연결된
  결정적 이벤트 설명이며 원인이 없으면 빈 배열이다.
- 이전 버전의 저장 리포트처럼 부호 있는 여유액이 없으면
  `balance_basis="LEGACY_AVAILABLE_BALANCE"`,
  `balance_basis_label="전체 가용잔액 · 다시 분석 필요"`,
  `requires_reanalysis=true`를 반환한다. 이 경우 결제계좌 안전여유라고 오표시하지
  않고 `available_balance`만 호환 표시한다.
- 클라이언트는 이 읽기 모델의 안전여유, 주차 상태와 원인을 재계산하지 않는다.

## 21.4 추천안

```text
GET  /api/v1/recommendations
GET  /api/v1/recommendations/{recommendation_id}

POST /api/v1/recommendations/{recommendation_id}/approve
POST /api/v1/recommendations/{recommendation_id}/reject
POST /api/v1/recommendations/{recommendation_id}/alternatives
```

## 21.5 분석 및 해석 상태

공개 API는 금융 분석 상태와 AI 해석 상태를 분리한다.

### analysis_status

```text
QUEUED
RUNNING
SUCCEEDED
FAILED
SUPERSEDED
```

### interpretation_status

```text
NOT_REQUESTED
QUEUED
RUNNING
SUCCEEDED
FALLBACK
FAILED
STALE
```

### 내부 실행 단계

세부 진행 상태는 별도 `execution_stage` 또는 이벤트 스트림으로 유지한다.

```text
SNAPSHOT_BUILDING
BASELINE_ANALYZING
AGENT_INVESTIGATING
PLAN_EVALUATING
REPORT_BUILDING
INTERPRETATION_REQUESTING
INTERPRETATION_VALIDATING
```

AI 해석 실패는 `analysis_status=FAILED` 조건이 아니다. 금융 코어와 후보 검증이 성공하면
`analysis_status=SUCCEEDED`이며, AI 실패 시 `interpretation_status=FALLBACK` 또는
`FAILED`를 사용한다.

---

# 22. 분석 결과 저장

최소 저장 항목:

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

저장 대상:

- 입력 금융 스냅숏
- 기준 분석 결과
- 내부 위험지표
- 사용자 표시 결과
- 에이전트 가설
- MCP 호출 기록
- 후보 대응안
- 대응안 평가 결과
- 정책 검증 결과
- 사용자 승인·거절 이력
- 실패 상태와 오류 정보

최신 리포트 승격은 완료 시각이 아니라 revision 기준으로 판정한다.

```text
result.snapshotRevision < latestDataRevision
→ analysis_status = SUPERSEDED 또는 interpretation_status = STALE
→ 실행 기록은 저장
→ latest report 포인터는 갱신하지 않음
```

대시보드 및 최신 리포트 응답에는 최소한 다음 메타데이터를 포함한다.

```json
{
  "reportRevision": "rev-7",
  "latestDataRevision": "rev-8",
  "isStale": true,
  "refreshStatus": "RUNNING"
}
```

## 22.1 AI 해석 실행 저장 모델

```text
ai_interpretation_runs
```

최소 컬럼:

```text
interpretation_id
analysis_id
snapshot_id
snapshot_revision
request_id
idempotency_key
contract_version
prompt_version
model_name
status
attempt_count
fallback_used
latency_ms
response_payload
error_code
created_at
completed_at
```

`idempotency_key`에는 unique constraint를 둔다.

## 22.2 AI 라벨 분류 실행 저장 모델

```text
ai_classification_runs
```

최소 컬럼:

```text
classification_id
user_id
import_id
request_id
idempotency_key
label_set_hash
schema_version
contract_version
prompt_version
mode
status
applied
attempt_count
fallback_used
latency_ms
model_name
label_count
group_count
merged_label_count
response_payload
error_code
created_at
completed_at
```

`idempotency_key`에는 unique constraint를 둔다. 원문 라벨, 거래 금액, 날짜는 감사
테이블에 저장하지 않는다. `off` 또는 분류 클라이언트 미구성 상태에서는 행을 만들지
않으며 행 없음을 `NOT_REQUESTED`로 해석한다. `response_payload`는 검증에 성공한
응답만 저장한다. 완료 상태는 변경할 수 없고, `SUCCEEDED`에는 검증 응답과 전체 라벨을
포괄하는 그룹 수가 반드시 있어야 한다. `on`의 `applied=true` 완료와 후보·거래 저장은
같은 데이터베이스 트랜잭션에서 커밋한다.

---

# 23. 실패 처리

## 23.1 공통 원칙

- 입력 데이터가 부족하면 값을 임의로 생성하지 않는다.
- 금융 코어 계산이 실패하면 추천안을 생성하지 않는다.
- 정책 검증이 실패하면 해당 대응안을 사용자에게 최종 추천으로 노출하지 않는다.
- 분석 실패를 `STABLE`로 변환하지 않는다.
- AI 해석 실패만으로 금융 분석 전체를 `FAILED`로 처리하지 않는다.
- 오래된 스냅숏의 추천안을 승인하려 하면 최신 데이터로 재검증한다.
- 오래된 `snapshotRevision`의 분석 결과나 AI 응답은 저장할 수 있지만 `latest report`로 승격하지 않는다.
- 이전 성공 리포트와 현재 실패 상태를 구분해 저장한다.
- 사용자 화면에는 마지막 성공 분석시각과 현재 갱신 상태를 함께 표시할 수 있다.

## 23.2 오류 형식

```json
{
  "code": "INVALID_FINANCIAL_EVENT",
  "message": "예정 현금 이벤트 형식이 올바르지 않습니다.",
  "details": {},
  "retryable": false
}
```

## 23.3 주요 오류 코드

```text
INVALID_CSV_FORMAT
INVALID_FINANCIAL_EVENT
DUPLICATE_FINANCIAL_EVENT
SNAPSHOT_NOT_FOUND
SNAPSHOT_STALE
INSUFFICIENT_DATA
CASHFLOW_SIMULATION_FAILED
SAFE_TO_SPEND_CALCULATION_FAILED
AGENT_RUN_FAILED
ACTION_SCHEMA_INVALID
ACTION_NOT_FEASIBLE
POLICY_VALIDATION_FAILED
ANALYSIS_FAILED
```

---

# 24. 필수 테스트 시나리오

## Scenario 1. 정상 상태

### 조건

- 예정 수입이 정상 입금됨
- 모든 필수지출 지급 가능
- 보호자금과 최소 안전잔액 유지 가능

### 기대 결과

- 사용자 상태: `STABLE`
- Safe-to-Spend: 0보다 큼
- 추천 행동 없음
- 정책 위반 없음

---

## Scenario 2. 결제계좌 부족

### 조건

- 결제계좌 잔액이 카드대금보다 적음
- 다른 비보호 계좌에 부족분 이상의 가용자금 존재

### 기대 결과

- 부족 유형: `PAYMENT_ACCOUNT`
- 전체 유동성 부족으로 분류하지 않음
- `transfer` 행동 생성 가능
- 이체 적용 후 총자산 불변
- 이체 적용 후 결제계좌 부족 해소

---

## Scenario 3. 전체 유동성 부족

### 조건

- 모든 비보호 가용자금을 합쳐도 필수지출 충당 불가

### 기대 결과

- 부족 유형: `TOTAL_LIQUIDITY`
- 계좌이동만으로 해결된다고 판단하지 않음
- 선택지출 조정 또는 구매 연기 후보 생성 가능
- 보호자금 사용 계획은 정책 검증 실패

---

## Scenario 4. 예정 수입 지연

### 조건

- 거래처 예정 수입이 카드 결제일 전 입금 예정
- 거래처 지급 이력에 지연 사례 존재
- 7일 지연 시 카드대금과 충돌

### 기대 결과

- 지연 시나리오에서 위험 발생
- 최초 위험 날짜 탐지
- 예상 부족금액 범위 반환
- 거래처 근거 조회 기록 존재
- 사용자 상태가 `PREPARE` 또는 `ACT_NOW`로 매핑될 수 있음

---

## Scenario 5. 할부 집중

### 조건

- 여러 할부금과 카드대금, 필수지출이 같은 주에 집중

### 기대 결과

- 해당 주를 취약구간으로 탐지
- 할부 원거래와 월별 할부금 중복 없음
- 현재 카드 청구액에 포함된 할부금 중복 없음
- 중기 현금흐름 영향 표시

---

## Scenario 6. 반동위험

### 조건

- 카드 결제일을 늦추면 현재 위험은 해소
- 변경된 결제일이 다음 월세 또는 할부금과 충돌

### 기대 결과

- `risk_shift.detected=true`
- 이후 위험 날짜 반환
- 해당 대응안을 수정하거나 최종안에서 제외
- 사용자 표시: 현재 위험이 이후로 이동할 수 있음을 설명

---

# 25. 불변조건 테스트

다음 조건은 항상 유지되어야 한다.

- 내부 계좌이동 전후 총자산은 동일하다.
- 보호자금은 일반 가용자금에 포함되지 않는다.
- 카드 개별 거래와 카드 청구액은 중복 반영되지 않는다.
- 할부 원거래와 월별 할부금은 중복 반영되지 않는다.
- 현재 카드 청구액에 포함된 이번 달 할부금을 다시 추가하지 않는다.
- 동일한 스냅숏, 설정, 시드에는 동일한 결과가 나온다.
- 금융 코어 실패 시 가짜 위험지표를 반환하지 않는다.
- 정책 검증 실패 대응안은 최종 추천이 될 수 없다.
- 사용자 승인 없이 가상 적용 이상의 행동을 수행하지 않는다.
- 분석 실패를 안전 상태로 표시하지 않는다.
- AI 분류가 실패하거나 거부되면 완전일치 그룹핑 결과와 금융 데이터 revision은
  정상적으로 유지된다.
- AI 라벨 분류 요청에는 금액·날짜·계좌·거래 ID가 포함되지 않는다.

---

# 26. 완료 기준

MVP 핵심 구현은 다음 조건을 충족해야 한다.

- 합성 CSV를 표준 금융 데이터로 정규화할 수 있다.
- 선택적 AI 라벨 그룹을 검증한 뒤 기존 반복 후보 규칙에 적용하고, 실패 시
  `deterministic_grouping()`으로 폴백할 수 있다.
- 금융 스냅숏을 생성할 수 있다.
- 13주 현금흐름을 재현 가능하게 계산할 수 있다.
- 결제계좌 부족과 전체 유동성 부족을 구분할 수 있다.
- Safe-to-Spend를 계산할 수 있다.
- 예정 수입 지연 시나리오를 반영할 수 있다.
- 에이전트가 MCP 계약과 호환되는 코어 도구를 통해 근거를 조회할 수 있다.
- 후보 대응안을 정의된 행동 스키마로 생성할 수 있다.
- 대응안을 가상 적용하고 전후 결과를 비교할 수 있다.
- 반동위험을 탐지할 수 있다.
- 금융 안전정책을 검증할 수 있다.
- 내부 위험지표를 사용자 상태와 설명으로 변환할 수 있다.
- 최신 분석 결과를 저장하고 API로 조회할 수 있다.
- 필수 테스트 시나리오와 불변조건 테스트를 통과한다.
- 실패 시 가짜 분석 결과를 반환하지 않는다.

---

# 27. Open Decisions

다음 항목은 구현 전에 팀 합의가 필요하다.

1. 거래처 지급 이력이 충분하다고 보는 최소 표본 수
2. 예정 수입 지연 확률 산출 방식
3. 거래처별 분포와 전체 기본 분포의 결합 방식
4. 반동위험의 정량 임곗값
5. Safe-to-Spend 보호수준의 운영 기본값
6. 보호수준을 사용자 화면에 직접 노출할지 여부
7. 신규 할부 수수료 반영 방식
8. 선택지출 예상액 산출 방식
9. 분석 실행 제한시간과 재시도 정책
10. 알림 중복 억제 기간
11. 실제 저장소에서 사용할 ID 형식
12. CSV 표준 컬럼과 샘플 데이터 형식
13. 운영 환경의 Docker 사용 여부와 배포 방식
