# FlowGuard Web

프리랜서의 불규칙한 수입과 고정된 결제일을 함께 보는 Next.js App Router 웹 앱입니다. 화면에 임의의 금융 결과나 숨은 데모 값을 넣지 않으며, 모든 금액·상태·추천은 API 응답에서만 표시합니다.

## 실행

```bash
npm install
cp .env.example .env.local
npm run dev
```

기본 API 주소는 `http://localhost:8000`입니다. 다른 주소는 `.env.local`의 `NEXT_PUBLIC_API_URL`로 지정합니다.

## 검증

```bash
npm run lint
npm run typecheck
npm run build
npm audit
```

합성 샘플은 `public/samples/flowguard-synthetic-transactions.csv`에 있습니다. 온보딩 화면의 다운로드 버튼으로도 받을 수 있습니다.

실제 계좌이체, 카드 결제일 변경, 할부 신청은 수행하지 않습니다. 추천 승인과 할부 사전점검은 서버 금융 스냅숏에 대한 가상 평가만 요청합니다.
