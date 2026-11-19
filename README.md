# JARVIS

An autonomous AI employee for a website-development agency serving US businesses. JARVIS takes a
natural-language (text or voice) command, finds and verifies prospects, researches them from real
sources, scores them deterministically, builds personalised preview websites, tests them in a real
browser inside an isolated sandbox, deploys them to Cloudflare Pages, and prepares outreach drafts.
Sending is gated behind compliance checks and explicit authorization.

**Status:** implemented and tested locally with test doubles for every third-party provider. **Not live-verified
against any provider, not deployed, and not production-ready.** Voice, off-site backup protection, real
integrations and production deployment stay blocked until their real acceptance tests pass. See
[docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) for each criterion's implemented, test-verified,
live-verified and deployed status.

## Repository layout

| Path | What |
|---|---|
| `backend/jarvis/` | FastAPI API, durable worker, agents, provider adapters, security, site renderer |
| `backend/tests/` | pytest suite (real Postgres, real Chromium, provider test doubles), `tests/live/` capped live tests |
| `database/migrations/` | SQL migrations (schema, RLS) applied by `python -m jarvis.db.migrate` |
| `dashboard/` | React + Vite + TypeScript + Tailwind dashboard (Vitest tests, `e2e/` real-stack browser test) |
| `qa-runner/` | Playwright + axe-core browser QA runner executed in the sandbox |
| `apps/voice/` | LiveKit voice agent (same command pipeline as text) |
| `docker/`, `docker-compose*.yml` | App, QA sandbox, voice images; Caddy; production compose |
| `scripts/` | VPS setup, pinned-host-key deploy, encrypted backup, restore |
| `docs/` | Architecture, requirements, contracts, schema, security/compliance, cost model |

## Run locally

Prerequisites: Python 3.12, Node 22, Docker, PostgreSQL 15+ (or `docker compose -f docker-compose.dev.yml up -d db`).

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
export DATABASE_URL=postgresql://postgres:jarvis@localhost:5432/jarvis JARVIS_ENV=local SUPABASE_JWT_SECRET=$(openssl rand -hex 32)
```

```bash
cd backend && .venv/bin/python -m jarvis.db.migrate
```

```bash
cd backend && .venv/bin/uvicorn jarvis.api.app:get_app --factory --port 8000
```

```bash
cd backend && .venv/bin/python -m jarvis.worker.main
```

```bash
cd qa-runner && npm ci && npx playwright install chromium
```

```bash
docker build -f docker/qa-sandbox.Dockerfile -t jarvis-qa-sandbox:latest .
```

```bash
cd dashboard && npm ci && VITE_API_BASE_URL=http://localhost:8000 npm run dev
```

Without provider credentials every integration shows `NOT_CONFIGURED`, and campaigns stop with a
`blocked` state that names the missing credential. Nothing is simulated.

## Tests

```bash
cd backend && .venv/bin/pytest -q
```

```bash
cd dashboard && npm run typecheck && npm test
```

```bash
cd backend && JARVIS_LIVE_TESTS=1 .venv/bin/pytest -q tests/live -rs
```

The last command runs the capped live provider tests, and needs real credentials.

## Deploy

See [docs/OPERATIONS.md](docs/OPERATIONS.md): VPS setup, `.env`, `scripts/deploy.sh` (pinned SSH host key),
Cloudflare Pages for the dashboard, backups and restore drills.
