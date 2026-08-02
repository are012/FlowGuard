# 트랙 B 구현 계획 — 위험 조사 적응

작성 기준: `main` (`600f70d`)
설계 문서: [ai-role-expansion.md](ai-role-expansion.md) 트랙 B (B1~B6)
관련 항목: [../BACKLOG.md](../BACKLOG.md) S1 · N1 · C1 · C2

이 문서는 **설계를 코드로 옮기기 위한 실행 계획**입니다. 무엇을 왜 만드는지는 설계 문서를, 어떻게 만드는지는 이 문서를 봅니다.

---

## 0. 현재 무엇이 문제인가

홈 화면 "규칙이 계산한 과정" 8단계가 전부 규칙 산출물입니다. `decision_trace.mode = DETERMINISTIC`, `model = None`.

```python
# investigator.py:542
def _hypotheses(shortfall_type, metrics):
    if shortfall_type == "PAYMENT_ACCOUNT":
        summary = "전체 자금보다 결제계좌 배치가 주요 위험 원인일 수 있습니다."
    else:
        summary = "보호자금을 제외한 전체 가용자금이 필수 유출보다 부족할 수 있습니다."
```

도구 호출도 `risk_builder.build()`의 고정 시퀀스입니다. AI는 `/interpret`에서 문장 하나를 쓸 뿐입니다.

**트랙 B는 "무엇을 의심하고 무엇을 조사할지"를 AI로 옮깁니다.** 금액·날짜·위험 판정은 그대로 규칙이 계산합니다.

## 1. 이미 있는 것 — 다시 만들지 않는다

**금융 도구는 전부 구현되어 있습니다.** 트랙 B는 도구를 만드는 작업이 아닙니다.

| 이미 있는 것 | 위치 | 상태 |
|---|---|---|
| 금융 도구 7종 | `services/tools.py` | 커버리지 **100%** |
| MCP 진입점 | `mcp_server.py` | 커버리지 **97%** |
| 도구 실행·기록 래퍼 | `investigator.py` `call()` | `tool_executions` 저장 포함 |
| 도구 호출 한도 | `MAX_TOOL_CALLS = 10` | |
| 감사 추적 저장·표시 | `agent_trace.py` · 홈 8단계 | |
| AI 계약·검증·폴백 골격 | 트랙 A (`48bfc54`) | |

도구 7종은 다음과 같습니다.

```text
get_financial_context      get_counterparty_evidence   query_financial_events
simulate_cashflow          calculate_safe_to_spend     evaluate_action_plan
validate_financial_policy
```

**빠진 것은 하나뿐입니다.**

```text
✗ 어떤 도구를 어떤 순서로 부를지 정하는 판단
```

현재는 `risk_builder.build()`가 고정 순서로 부릅니다. 어떤 사용자든 항상 같은 순서입니다. 트랙 B는 **그 판단만** AI로 옮깁니다.

따라서 이번 작업에서 새로 작성하는 코드는 전부 **AI와 주고받는 배관과 안전장치**이며, 금융 로직이나 도구 구현은 한 줄도 건드리지 않습니다.

| 새로 만드는 것 | 성격 |
|---|---|
| `/investigate/plan` · `/investigate/conclude` | AI에게 묻는 창구 |
| `investigation_validation.py` | AI 답변이 허용목록 안인지 검사 |
| `investigation_loop.py` | 2단계 왕복 진행과 한도 강제 |
| `observation_projection.py` | 도구 결과에서 AI에게 보낼 것만 추림 |
| `ai_investigation_runs` | 감사 기록 |

9일이 걸리는 이유는 도구 때문이 아니라 **검증·폴백·측정** 때문입니다. 트랙 A도 같은 구조였습니다 — 탐지 로직은 이미 있었고 AI 판단만 더했는데 5,582줄이 들었으며, 그중 대부분이 계약·검증·폴백·테스트였습니다.

## 2. 트랙 A가 확립한 패턴을 그대로 따른다

트랙 B는 새 아키텍처를 만들지 않습니다. 트랙 A(`48bfc54`)가 검증한 구조를 복제합니다.

| 역할 | 트랙 A | 트랙 B (신규) |
|---|---|---|
| 계약 모델 | `ai_contract.py` | 같은 파일에 추가 |
| 숫자 정책 | `ai_numeric_policy.py` | **재사용** |
| 응답 검증 | `services/label_group_validation.py` | `services/investigation_validation.py` |
| 오케스트레이션 | `services/import_classification.py` | `services/investigation_loop.py` |
| AI 엔드포인트 | `ai_service.py` `/classify/labels` | 같은 파일에 `/investigate/*` |
| 감사 테이블 | `ai_classification_runs` | `ai_investigation_runs` + `_turns` |
| 실행 모드 | `FLOWGUARD_AI_CLASSIFICATION` | `FLOWGUARD_AI_INVESTIGATION` |

**동일한 구조를 반복하는 것이 목표입니다.** 새 패턴을 도입하지 않습니다.

---

## 3. 선행 조건

### 3.1 필수 — N1 중복 분석 방어

**현재 실측**: 동시 요청 5건 → `201` 5개, AI 호출 5회.

트랙 B는 분석당 AI 호출이 1회 → 최대 4회로 늘어납니다. 중복 방어 없이 진행하면 **비용이 그대로 배수**가 됩니다.

```text
조치  같은 snapshotRevision 에 RUNNING 상태 분석이 있으면
      새로 만들지 말고 기존 실행을 반환한다.
      analysis.py 의 _pending 개념(545행)을 분석 실행 단위로 확장.
규모  2~3시간
```

### 3.2 강력 권장 — C1·C2 비동기 분석

현재 분석은 API 요청 안에서 동기 실행되며 AI 연결 시 약 4초입니다. 트랙 B를 얹으면 **최대 15초**가 됩니다.

동기 상태로도 동작은 하지만, 요청 하나가 15초를 점유합니다. 프런트에는 이미 진행 표시 UI가 있어 연동만 하면 됩니다.

```text
조치  분석을 백그라운드 작업으로 분리하고 상태 폴링으로 전환
규모  2~3일
```

**C1·C2를 미루고 진행할 경우**에도 `TOTAL_BUDGET`은 `PHASE_TIMEOUT × MAX_PHASES` 보다 커야 합니다.

> **실측 교훈** — 초안은 동기 지연을 억제하려 `TOTAL_BUDGET`을 8초로 낮추라고 적었으나,
> `PHASE_TIMEOUT 5초 × MAX_PHASES 2 = 10초`가 필요한 구조라 1차 단계가 제한시간을
> 소진하면 2차가 시작조차 못 했습니다. 그 결과 2차 추가 조사 발생률이 16.7%로 측정되어
> "2단계 설계 효과 미입증"이라는 **잘못된 판정**이 나왔습니다.
> 예산을 12초로 고친 뒤 재측정하니 **100%** 로 뒤집혔습니다.
> 자세한 내용은 `feature/track-b-ai-investigation` 브랜치의
> `docs/plans/track-b-shadow-results.md` 참조.

### 3.3 권장 — N5 프롬프트 용어집

`/interpret`가 `2026-08-25`, `결제 계정` 같은 비표준 표기를 씁니다. 조사 프롬프트도 같은 용어 규칙을 공유해야 하므로 함께 정리합니다. 30분.

---

## 4. 구현 전 결정 사항

코드를 쓰기 전에 다음을 조사해 결정하고 승인을 받습니다.

### D1. 관찰 투영 대상 도구 범위

설계 문서 B3은 도구 4종의 투영 규칙을 정했습니다. 그런데 현재 `CoreToolService`가 노출하는 도구는 7종입니다.

```text
결정할 것  1차에서 AI가 요청할 수 있는 도구를 어디까지 허용할지
후보       조회 3종만 (get_financial_context, get_counterparty_evidence,
                      query_financial_events)
           계산 도구(simulate_cashflow, calculate_safe_to_spend)까지
근거       계산 도구는 결과가 크고 AI 판단에 꼭 필요하지 않을 수 있음
```

**조회 3종으로 시작하는 안을 권합니다.** 투영 규칙이 단순해지고, 부족하면 나중에 넓힐 수 있습니다.

### D2. `ai_investigation_turns` 분리 여부

설계 문서 B는 실행 요약(`runs`)과 턴별 기록(`turns`)을 나눴습니다. 트랙 A는 `ai_classification_runs` 하나만 씁니다.

```text
결정할 것  턴 기록을 별도 테이블로 둘지, runs 의 JSON 컬럼에 담을지
근거       2단계 고정이므로 턴이 최대 3개. 별도 테이블이 과할 수 있음
           다만 재생 테스트와 감사에는 턴 단위 조회가 편함
```

### D3. 계약 버전

```text
/interpret         1.1 유지
/classify/labels   1.2 (트랙 A)
/investigate/*     1.2 로 할지 1.3 으로 할지
```

트랙 A가 `ClassifyEnvelope`를 별도로 만들었는지, 공통 엔벨로프를 확장했는지 확인하고 같은 방식을 따릅니다.

### D4. 실패 시 관찰 보존 범위

설계 문서 B4는 "이미 수집한 관찰은 유지"로 정했습니다. 구현상 확인할 점이 있습니다.

```text
결정할 것  2차에서 거부되었을 때 1차 관찰을 후보 생성 입력으로 쓸지,
           아니면 결정론적 경로가 자체 수집한 근거만 쓸지
근거       두 경로의 근거가 섞이면 감사 추적에서 출처 표시가 복잡해짐
```

### D5. 프런트 표시 범위

`decision_trace`에 `source`와 `reason`이 추가됩니다.

```text
결정할 것  홈 화면 8단계에 "AI가 이 조회를 고른 이유"를 어떻게 넣을지
           단계 카드 안에 접기로 둘지, 항상 노출할지
근거       현재 카드가 이미 제목+요약 2줄. 이유까지 넣으면 3줄이 됨
```

---

## 5. 작업 단계

각 단계는 **AI 미설정 환경에서 기존 276개 테스트가 통과하는 상태**로 끝나야 합니다.

### 단계 0 — 선행 조건 (2~3시간 + 선택 2~3일)

N1 중복 분석 방어. C1·C2는 여기서 할지 미룰지 결정.

### 단계 1 — 계약 모델 (0.5일)

`ai_contract.py`에 추가.

```python
class InvestigationTarget(AIContractModel):
    counterpartyIds: list[AIIdentifier]
    eventWindow: EventWindow
    actionTypes: list[AIIdentifier]

class InvestigationRequestBase(...):   # 계약 식별자 9종 에코
class InvestigationPlanRequest(...):   # 1차: baseline + targets
class InvestigationPlanResponse(...):  # investigations[]
class InvestigationConcludeRequest(...):  # + observations[]
class InvestigationConcludeResponse(...): # additionalInvestigations[] | conclusion
```

`params`에 금액 필드를 두지 않습니다. 자유 텍스트(`reason`, `summary`, `unresolved`)에는 `contains_numeric_expression` 검사를 적용합니다.

**산출물**: 계약 스키마 테스트 (버전 거부, 필드 누락 거부, 숫자 포함 거부)

### 단계 2 — 검증기 (1일)

`services/investigation_validation.py`. `label_group_validation.py`와 같은 형태로 작성합니다.

| 규칙 | 위반 시 |
|---|---|
| 계약 식별자 9종 불일치 | 턴 거부 |
| `investigations`와 `conclusion` 동시/부재 | 턴 거부 |
| `tool`이 허용목록 밖 | 턴 거부 |
| `counterpartyId`가 요청에 없던 값 | 턴 거부 |
| 날짜가 `eventWindow` 밖 | 턴 거부 |
| 동일 `(tool, params)` 재요청 | 턴 거부 |
| 자유 텍스트에 미공급 숫자 | 턴 거부 |
| `conclusion.hypotheses` 3개 초과 | 턴 거부 |

**산출물**: 규칙별 거부 테스트 8종 이상

### 단계 3 — 관찰 투영 (0.5일)

`services/observation_projection.py`. 도구별 화이트리스트, **미정의 도구는 기본 거부**.

**산출물**: 도구별 제외 필드 확인 테스트, 미정의 도구 거부 테스트

### 단계 4 — AI 서비스 엔드포인트 (1일)

`ai_service.py`에 `/investigate/plan`과 `/investigate/conclude` 추가. 프롬프트 `invest-1`.

멱등성 캐시는 트랙 A/`/interpret`와 동일한 방식을 씁니다.

**산출물**: 가짜 OpenAI 클라이언트로 두 엔드포인트 테스트

### 단계 5 — 백엔드 클라이언트와 루프 (1.5일)

`services/investigation_loop.py`. `import_classification.py`와 같은 형태.

```python
MAX_PHASES = 2
MAX_TOOL_CALLS = 6
PHASE_TIMEOUT = 5.0
TOOL_EXECUTION_ALLOWANCE = 2.0
# 단계마다 제한시간을 소진할 수 있으므로 총예산은 그 합보다 커야 한다.
TOTAL_BUDGET = PHASE_TIMEOUT * MAX_PHASES + TOOL_EXECUTION_ALLOWANCE  # 12.0
```

백엔드가 한도를 강제합니다. AI의 자기 신고를 믿지 않습니다.

**산출물**: 루프 시나리오 테스트 6종
- 1차 후 `conclusion` → `SUCCEEDED`
- 1차 후 추가 조사 → 강제 종료 → `SUCCEEDED`
- 2차 거부 → 관찰 유지 → `PARTIAL`
- 첫 턴 연결 실패 → `FAILED`, 현재와 동일 결과
- 동일 도구 반복 → 거부
- 한도 초과 → `PARTIAL`

### 단계 6 — 저장 모델 (0.5일)

`ai_investigation_runs` (+ D2 결정에 따라 `_turns`). Alembic `20260802_0003`.

`schema_parity` 통과 확인.

### 단계 7 — investigator 통합 (1.5일)

```text
현재
  risk_builder.build(snapshot_id, baseline, call)
  _hypotheses(shortfall_type, metrics)
  action_service.create(context, ...)

변경 후
  outcome = investigation_loop.run(baseline, targets, call)
  if outcome.status in {NOT_REQUESTED, FAILED}:
      hypotheses = deterministic_hypotheses(...)   # 기존 로직을 함수로 분리
  else:
      hypotheses = outcome.hypotheses
  action_service.create(context, ..., priorities=outcome.priorities)
```

**기존 `call()` 래퍼를 그대로 재사용합니다.** 도구 실행과 `tool_executions` 기록은 이미 그 안에 있습니다. 루프는 "무엇을 부를지"만 정합니다.

**기존 결정론 경로는 삭제하지 않고 `deterministic_hypotheses()`로 분리해 보존합니다.**

### 단계 8 — 감사 추적과 화면 (1일)

`decision_trace.mode`에 `AI_INVESTIGATED` · `AI_PARTIAL` 추가. 단계에 `source`와 `reason` 부여.

홈 화면은 D5 결정에 따라 표시합니다. 현재 문구도 함께 갱신합니다.

```text
현재   섹션 제목 "규칙이 계산한 과정"
       배지 "같은 정보 · 같은 결과"
       "AI는 그 결과를 이 문장으로 옮겨 적기만 합니다"

이후   AI 조사 성공 시 위 문구가 사실과 달라지므로 모드별로 분기
```

E2E 단언도 함께 갱신합니다.

### 단계 9 — shadow 측정 (1일)

`FLOWGUARD_AI_INVESTIGATION=shadow`로 결과를 기록만 하고 사용하지 않습니다. 지표를 모아 `on` 전환 여부를 판단합니다.

---

## 6. 합계와 순서

| 단계 | 규모 |
|---|---|
| 0 선행 조건 (N1만) | 0.5일 |
| 1 계약 모델 | 0.5일 |
| 2 검증기 | 1일 |
| 3 관찰 투영 | 0.5일 |
| 4 AI 엔드포인트 | 1일 |
| 5 루프 | 1.5일 |
| 6 저장 모델 | 0.5일 |
| 7 investigator 통합 | 1.5일 |
| 8 감사 추적·화면 | 1일 |
| 9 shadow 측정 | 1일 |
| **합계** | **9일** |

C1·C2를 포함하면 12일입니다. 본선(9월 2일)까지 3주이므로 여유가 있습니다.

---

## 7. 완료 판정

| 지표 | 목표 | 측정 |
|---|---|---|
| 조사 완주율 | 80% 이상 `SUCCEEDED` | `ai_investigation_runs.status` |
| **2차 추가 조사 발생률** | **30% 이상** | 관찰 기반 적응이 실제로 일어나는지 |
| 턴 거부율 | 20% 미만 | 턴 기록 |
| 조사 다양성 | 서로 다른 입력에서 도구 조합 3종 이상 | 턴 기록 집계 |
| 결론 재현성 | 재생 시 최종 수치 100% 일치 | 재생 테스트 |
| 회귀 | AI 미설정 시 기존 276개 전부 통과 | CI |
| 분석 소요 | 동기 유지 시 15초 이내 · 비동기 전환 시 응답 1초 이내 | 실측 |

**2차 추가 조사 발생률이 핵심입니다.** 이 값이 0에 가까우면 AI가 관찰에 반응하지 않는다는 뜻이고, 그러면 2단계 설계를 택한 이유가 사라집니다. 그 경우 단발성 계획으로 축소하는 것이 옳은 결론입니다.

## 8. 하지 않을 것

- 6턴 가변 루프 — 단계 9 측정 후 필요성이 확인되면 그때 검토
- AI가 도구를 직접 호출 — 실행 주체는 언제나 백엔드
- AI가 금액·날짜·위험 등급을 산출 — 계약에 필드를 두지 않음
- 기존 결정론 경로 삭제 — 폴백이자 기본 경로로 보존
- 트랙 A 코드 수정 — 독립 유지
