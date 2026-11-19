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
| `docs/` | Architecture, requirements, contracts, schema, security/compliance, cost model, [integrations and voice setup](docs/INTEGRATIONS_AND_VOICE.md) |

## Run locally / in GitHub Codespaces

In a Codespace, JARVIS starts automatically every time the Codespace starts. Otherwise run one command:

```bash
scripts/dev-up.sh
```

It starts PostgreSQL, applies migrations, then starts the API, worker and dashboard, checks their health and prints
the URLs. The dashboard opens directly to the Command Center; there is no sign-in during development (explicit local
access mode, refused outside `JARVIS_ENV=local`).

```bash
scripts/dev-status.sh
```

```bash
scripts/dev-down.sh
```

Prerequisites outside Codespaces: Python 3.12, Node 22 and Docker. Full guide, configuration and troubleshooting:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md). Provider and voice setup:
[docs/INTEGRATIONS_AND_VOICE.md](docs/INTEGRATIONS_AND_VOICE.md).

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
