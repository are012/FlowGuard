# FlowGuard MVP Development

This repository implements the contest MVP described in `SPECIFICATION.md`. The
financial calculations and recommendation candidates are deterministic. The
backend investigator selects evidence, builds predefined actions, and validates
them without inventing balances, gaps, probabilities, or policy results. A
separate AI service may rank validated candidates and explain them to the user.

## Local topology

- `apps/api`: FastAPI, the deterministic financial core, the investigator, REST
  API, MCP tools, and persistence.
- `apps/ai-service/main.py`: the deployment entrypoint for an independently
  started FastAPI process that calls OpenAI for candidate ranking and
  user-facing explanations only. Its implementation comes from the same
  `apps/api` Python distribution; see `apps/ai-service/README.md`.
- `apps/web`: Next.js user interface.
- The investigator invokes the MCP-compatible `CoreToolService` in process. A
  FastMCP entrypoint exists, but no separate MCP process or transport is used in
  the default analysis path.
- SQLite is the exercised contest-MVP store. `DATABASE_URL` accepts a PostgreSQL
  SQLAlchemy URL, but a shared PostgreSQL deployment and migrations are part of
  the expansion profile.
- Analysis runs synchronously in the MVP while preserving separate financial
  analysis and AI interpretation states. A durable Redis worker is an
  infrastructure extension, not a hidden in-process retry.
- No Redis queue, scheduler, Dockerfile, or Compose stack is part of the current
  contest MVP.
- Authentication and multi-tenant authorization are not implemented in this
  contest MVP. `X-User-ID` only selects an isolated local demo namespace; do not
  expose the API to an untrusted network without a real identity boundary.

No endpoint performs a real transfer, changes a real card payment date, or applies
for a financial product. Approving a recommendation creates and analyzes a
virtual snapshot only.

## Separate AI interpretation service

The backend always owns snapshot creation, financial calculations, evidence and
MCP tool selection, candidate generation, simulation, and policy validation. It
never imports an OpenAI client or reads `OPENAI_API_KEY`. The isolated AI service
receives only validated `facts`, `evidence`, and `actionCandidates`; it has no
database or MCP access and cannot create a new financial action.

The backend and AI service communicate through contract `1.1`. Each request and
response carries the analysis, snapshot, revision, request, idempotency, prompt,
and locale identifiers needed for strict correlation. Unknown actions, duplicate
rankings, malformed responses, and mismatched identifiers are rejected.

Copy the environment examples into separate files:

```bash
cp apps/api/.env.example apps/api/.env
cp apps/ai-service/.env.example apps/ai-service/.env
```

Put `OPENAI_API_KEY` only in `apps/ai-service/.env`. `OPENAI_MODEL` selects the
model in that process. `FLOWGUARD_AI_MODEL_NAME` in `apps/api/.env` is audit
metadata only and must match the deployed AI service configuration.

The backend uses a 3-second connection timeout, 10-second response timeout, and
15-second total budget. It retries at most once, and only for connection failures,
temporary timeouts, `429`, `502`, `503`, and `504`. Retries reuse the same request
and idempotency identifiers. If the service is unavailable or returns an invalid
response, the deterministic financial result remains successful and FlowGuard
returns a rule-based explanation.

Financial analysis status and AI interpretation status are independent. A valid
combination is `analysis_status=SUCCEEDED` with
`interpretation_status=FALLBACK`. When data changes during an older run, that
run and its AI response remain available for audit, but their stale snapshot
revision is never promoted as the latest report.

The contest MVP runs one AI-service process with one Uvicorn worker because its
in-flight and successful-response idempotency cache is process-local. A
multi-worker deployment must add a shared idempotency store before scaling the AI
service; otherwise a timeout retry routed to another worker could execute the
same logical OpenAI request twice.

Durable recovery of an interpretation left `RUNNING` by a terminated synchronous
API process also requires a shared lease/worker queue and is outside the contest
MVP. A new analysis remains safe because financial reports and revision promotion
do not depend on recovering that abandoned AI call.

## Versioned MVP decisions

The specification marks several rules as `TBD`. The implementation keeps these
decisions explicit and versioned:

| Decision | MVP rule |
|---|---|
| Receivable evidence | Fewer than 3 payment observations is low confidence |
| Delay distribution | Four deterministic scenarios: on time, +3, +7, and +14 days |
| Protection level | 0.90 by default; the UI explains the safety buffer without presenting it as a guarantee |
| Risk presentation | Rule-based thresholds combining probability, urgency, shortfall type, and data quality |
| Rebound risk | Any newly created essential-payment shortfall or total-liquidity shortfall is a rebound |
| Installment fees | Principal-only equal installments; fees are explicitly excluded |
| Discretionary capacity | Only scheduled events explicitly marked adjustable can be reduced |
| Long horizon | Weeks 5–13 are grouped by week; weeks 1–4 retain daily detail |
| IDs | Generated workflow IDs are UUID-backed strings; imported and API entity IDs remain opaque strings |
| CSV | UTF-8 wide CSV with `record_type`; legacy transaction-only rows remain valid |
| Persistence | SQLite is tested; a PostgreSQL URL/driver path exists but production PostgreSQL and migrations are expansion work; no Docker resources are created |
| AI interpretation retry | One retry only for documented transient transport and HTTP failures, within a 15-second total budget |
| Latest report promotion | Only a result matching the latest data revision can become the latest report |

The exact constants and version identifiers live in
`apps/api/flowguard/config.py` and are covered by tests.

## Run locally

```bash
make install
make dev-ai
```

In a second terminal:

```bash
make dev-api
```

In a third terminal:

```bash
make dev-web
```

The AI service listens on `http://localhost:8001` and the application API on
`http://localhost:8000` by default. Without an AI service key, financial analysis
still completes and exposes the deterministic fallback interpretation.

Open `http://localhost:3000`, download the explicit sample CSV from the setup
screen, and upload it. Until data is imported, the UI shows a real empty state;
it does not substitute demo analysis results. The bundled sample is analyzed at
the fixed scenario time `2026-07-24T09:00:00+09:00`, so the contest walkthrough
remains reproducible. The setup screen can reset only the current demo user's
local data and analysis artifacts.

## Validate

```bash
make check
```

The browser walkthrough can be verified separately after activating the Python
virtual environment:

```bash
npm --prefix apps/web run test:e2e
```

The automated suite uses SQLite, Fake OpenAI clients, and MockTransport. The E2E
walkthrough deliberately points the API at an unavailable AI port and verifies
`analysis_status=SUCCEEDED`, `interpretation_status=FALLBACK`, and the
deterministic fallback source. It does not make a live OpenAI request.

After `make dev-ai`, the deployment entrypoint can be checked manually without a
live OpenAI request:

```bash
curl --fail http://127.0.0.1:8001/health
```

The following remain outside the exercised MVP validation boundary:

- a live OpenAI key/model smoke test;
- an automated API-to-AI process success-path integration test;
- PostgreSQL migrations and integration tests;
- Redis, durable worker recovery, and multi-worker AI idempotency;
- Docker/Compose deployment; and
- a process-to-process MCP transport path.

Override the web client URL with `NEXT_PUBLIC_API_URL`; see each app's
`.env.example`.
