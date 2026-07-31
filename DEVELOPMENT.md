# FlowGuard MVP Development

This repository implements the contest MVP described in `SPECIFICATION.md`. The
financial calculations are deterministic; the investigator selects evidence and
predefined actions but never invents balances, gaps, probabilities, or policy
results.

## Local topology

- `apps/api`: FastAPI, the deterministic financial core, the investigator, REST
  API, MCP tools, and persistence.
- `apps/web`: Next.js user interface.
- Local development uses SQLite. Set `DATABASE_URL` to a PostgreSQL SQLAlchemy URL
  for PostgreSQL.
- Analysis runs synchronously in the MVP while preserving the documented run
  states and audit events. A durable Redis worker is an infrastructure extension,
  not a hidden in-process retry.
- Authentication and multi-tenant authorization are not implemented in this
  contest MVP. `X-User-ID` only selects an isolated local demo namespace; do not
  expose the API to an untrusted network without a real identity boundary.

No endpoint performs a real transfer, changes a real card payment date, or applies
for a financial product. Approving a recommendation creates and analyzes a
virtual snapshot only.

## Optional Luna investigator

The default `FLOWGUARD_AGENT_MODE=auto` uses OpenAI `gpt-5.6-luna` through the
Responses API when `OPENAI_API_KEY` is present. Without a key, the API remains
fully usable and records `DETERMINISTIC_FALLBACK` with
`OPENAI_API_KEY_NOT_CONFIGURED` in the public decision trace.

Copy `apps/api/.env.example` to `apps/api/.env` and add only the local API key:

```text
OPENAI_API_KEY=...
```

The Luna layer chooses which read-only evidence tools and backend-generated
action candidates to inspect. All balances, dates, simulations, rebound-risk
checks, and policy decisions remain authoritative outputs of the deterministic
financial core. The UI exposes a public audit trace of hypotheses, tool calls,
candidate evaluations, and the selection rationale. It never exposes hidden
model chain-of-thought.

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
| Persistence | SQLite locally and PostgreSQL via `DATABASE_URL`; no Docker resources are created |
| Retries and alerts | No silent retry or notification suppression default is invented for the MVP |

The exact constants and version identifiers live in
`apps/api/flowguard/config.py` and are covered by tests.

## Run locally

```bash
make install
make dev-api
```

In a second terminal:

```bash
make dev-web
```

Open `http://localhost:3000`, download the explicit sample CSV from the setup
screen, and upload it. Until data is imported, the UI shows a real empty state;
it does not substitute demo analysis results.

## Validate

```bash
make check
```

The API uses `http://localhost:8000` by default. Override the web client URL with
`NEXT_PUBLIC_API_URL`; see each app's `.env.example`.
