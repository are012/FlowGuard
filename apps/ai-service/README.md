# FlowGuard AI Classification / Interpretation Service

이 디렉터리는 FlowGuard의 별도 AI 분류·해석 프로세스가 소유하는 배포 진입점과
환경설정을 모읍니다. 현재 공모전 MVP에서 AI 서비스를 사용할 때는 API와 서로 다른
FastAPI 프로세스로 실행하지만, 계약 모델 중복을 피하기 위해 두 프로세스가 같은
`apps/api` Python distribution을 사용합니다.

## 파일과 구현 위치

- `main.py`: `make dev-ai`가 사용하는 AI 서비스 배포 진입점
- `.env.example`: AI 프로세스만 소유하는 OpenAI 환경변수 예시
- `../api/flowguard/ai_service.py`: 라벨 분류·해석 요청 검증과 OpenAI 호출 구현
- `../api/flowguard/ai_contract.py`: 백엔드와 AI 서비스가 공유하는 분류 `1.2`·해석 `1.1` 계약

즉, 현재 격리 경계는 별도 Python package나 Docker image가 아니라
**프로세스·포트·환경변수**입니다. Application API 엔트리포인트
`flowguard.main:app`은 OpenAI 클라이언트를 import하거나 `OPENAI_API_KEY`를 읽지
않습니다.

## 실행

```bash
make install-ai
cp apps/ai-service/.env.example apps/ai-service/.env
make dev-ai
```

기본 주소는 `http://localhost:8001`이며 `GET /health`, `POST /classify/labels`,
`POST /interpret`를 제공합니다. OpenAI 키는 `apps/ai-service/.env`에만 둡니다.

## 책임 경계

라벨 분류는 백엔드가 한국어·영어 금액 및 날짜 표현과 원천 식별자를 비식별화한
`labelId`, `text`, `direction`, `occurrences`만 사용합니다. 금액·날짜·계좌·거래 ID는
요청하지 않습니다. 해석은 검증
완료 `facts`, `evidence`, `actionCandidates`만 사용해 후보 순위와 사용자 설명을
생성합니다. 두 경로 모두 데이터베이스와 MCP 도구에 접근하지 않고
금액·날짜·일수·비율·위험 판정·행동 후보를 만들지 않습니다.

분류 활성화는 API 프로세스의 `FLOWGUARD_AI_CLASSIFICATION=off|shadow|on`으로
제어합니다. 기본값 `off`는 이 엔드포인트를 호출하지 않으며, `shadow`는 기록만,
`on`은 백엔드 계약 검증을 통과한 그룹만 후보 탐지에 적용합니다.

한 분류 시도 안의 재시도와 동시 요청은 같은 멱등 키를 사용합니다. 백엔드는 성공
결과를 재사용하고, 실패·거부 뒤의 새 업로드에는 이전 감사 ID에서 파생한 새 시도 키를
사용합니다.

현재 MVP는 프로세스 로컬 멱등성 캐시를 사용하므로 AI 서비스는 Uvicorn worker
하나로 실행해야 합니다. 다중 worker, 재시작 후 멱등성, 중단된 실행의 lease 복구는
공유 저장소와 durable queue를 도입하는 확장 단계의 책임입니다.

## 검증 범위

자동화 테스트는 Fake OpenAI client와 MockTransport로 두 엔드포인트의 구조화 응답,
계약 위반, 재시도와 폴백을 검증합니다. 실제 OpenAI 키·모델을 사용하는 smoke test는
자동 검증 범위에 포함하지 않습니다.
