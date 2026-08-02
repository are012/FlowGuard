# FlowGuard AI Interpretation Service

이 디렉터리는 FlowGuard의 별도 AI 해석 프로세스가 소유하는 배포 진입점과
환경설정을 모읍니다. 현재 공모전 MVP에서 AI 서비스를 사용할 때는 API와 서로 다른
FastAPI 프로세스로 실행하지만, 계약 모델 중복을 피하기 위해 두 프로세스가 같은
`apps/api` Python distribution을 사용합니다.

## 파일과 구현 위치

- `main.py`: `make dev-ai`가 사용하는 AI 서비스 배포 진입점
- `.env.example`: AI 프로세스만 소유하는 OpenAI 환경변수 예시
- `../api/flowguard/ai_service.py`: 해석 요청 검증과 OpenAI 호출 구현
- `../api/flowguard/ai_contract.py`: 백엔드와 AI 서비스가 공유하는 계약 `1.1`

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

기본 주소는 `http://localhost:8001`이며 `GET /health`와 `POST /interpret`를
제공합니다. OpenAI 키는 `apps/ai-service/.env`에만 둡니다.

## 책임 경계

AI 서비스는 백엔드가 전달한 검증 완료 `facts`, `evidence`,
`actionCandidates`만 사용해 후보 순위와 사용자 설명을 생성합니다. 데이터베이스와
MCP 도구에 접근하지 않고 금액·날짜·위험 판정·행동 후보를 만들지 않습니다.

현재 MVP는 프로세스 로컬 멱등성 캐시를 사용하므로 AI 서비스는 Uvicorn worker
하나로 실행해야 합니다. 다중 worker, 재시작 후 멱등성, 중단된 실행의 lease 복구는
공유 저장소와 durable queue를 도입하는 확장 단계의 책임입니다.

## 검증 범위

자동화 테스트는 Fake OpenAI client와 MockTransport로 구조화 응답, 계약 위반,
재시도와 폴백을 검증합니다. E2E는 AI 서비스를 사용할 수 없는 연결 실패 경로를
검증합니다. API와 AI 프로세스 사이의 성공 경로 및 실제 OpenAI 키·모델을 사용하는
smoke test는 자동 검증 범위에 포함하지 않습니다.
