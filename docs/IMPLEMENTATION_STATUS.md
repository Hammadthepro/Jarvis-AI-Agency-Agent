# JARVIS — Implementation Status

As of 2026-11-18. Branch `claude/sleepy-shannon-4lorvo`.
Every claim below is backed by an automated test or a run recorded in this document. "Live-verified" means
exercised against the real third-party provider. **No provider credentials were available in the build
environment, so no third-party integration has been live-verified yet.**

## 1. Test results (latest run)

| Suite | Result | What it exercises |
|---|---|---|
| Backend `pytest` | **77 passed, 4 skipped** | Real PostgreSQL 16 with real migrations and RLS; real Chromium via Playwright/axe; real Docker sandbox; provider HTTP served by test doubles (`tests/doubles.py`) |
| Live provider tests (`tests/live`) | **4 skipped**: credentials not set | Claude, SerpApi, Firecrawl, Cloudflare. One capped call each |
| Dashboard Vitest | **7 passed** | Validation, honest error states, command and clarification workflow, API-unreachable state |
| Dashboard typecheck and build | **pass** | `tsc -b`, `vite build` |
| Real-stack E2E (`dashboard/e2e/stack.e2e.mjs`) | **pass** | Real API, worker, Postgres and built dashboard in Chromium, with no credentials. Covers: the Places compliance block, an import campaign blocked with `NOT_CONFIGURED: … SERPAPI_API_KEY`, Settings showing all 7 providers `NOT_CONFIGURED`, and no page errors |
| Docker QA sandbox | **pass** | `--network none`, read-only root, uid 10001, all capabilities dropped. Outbound fetch fails (`EAI_AGAIN`) and root-filesystem writes fail (`EROFS`) |
| Backup and restore drill | **pass** | `scripts/backup.sh` (pg_dump + age) then `scripts/restore.sh` into a scratch DB. Recorded as `BACKUP_NOT_PROTECTED` (no off-site target) |
| Lint | `ruff` clean | |

## 2. Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | User can authenticate | ✅ Supabase JWT verification (JWKS or HS256). Tested with locally minted tokens. A real Supabase project is **NOT_CONFIGURED** |
| 2 | Natural-language text command | ✅ tested (API, UI and E2E) |
| 3 | Parsed into structured intent | ✅ The deterministic parser is tested for all rating/phrasing cases. The Claude Haiku parser is tested with a double; **not live-verified** |
| 4 | Persistent task | ✅ Postgres queue |
| 5 | Survives restarts | ✅ test: a lease-expired task is reclaimed and completes (at-least-once) |
| 6 | Discovery via an approved, configured provider | ⛔ **Google Places: BLOCKED_PENDING_APPROVAL** (compliance gate) and NOT_CONFIGURED. `manual_import` works end to end |
| 7 | Provenance retained | ✅ evidence and facts carry source, quote and timestamp. Test T14 proves no Places content is persisted |
| 8 | Multi-source website verification | ✅ listed-site probe, 2 SERP queries, profile pages, candidate-domain DNS, second probe for broken sites. Tested with doubles; **not live-verified** |
| 9 | Uncertain findings marked | ✅ `WEBSITE_UNCERTAIN`, `CONFLICTING_EVIDENCE`, `VERIFICATION_FAILED`. Missing credentials **block** the campaign instead of completing it |
| 10 | Research via real search/extraction | ⚠️ implemented (SerpApi + Firecrawl + Haiku, quote-verified). **Not live-verified** |
| 11 | Deterministic, explainable scoring | ✅ tested |
| 12 | Unique websites | ✅ 12-site, 4-campaign, 9-niche, 4-metro diversity test |
| 13 | Verified facts only | ✅ factual guard + QA fact check. The test confirms an invented fact is dropped |
| 14 | Isolated builds | ✅ the model never writes code (structured SiteSpec → owned renderer). QA runs in the no-network Docker sandbox |
| 15 | Real browser QA | ✅ Playwright + axe on desktop and mobile |
| 16 | QA findings stored | ✅ `qa_runs`, `qa_findings`, screenshots |
| 17 | Bounded repairs | ⚠️ implemented (max 3: Sonnet ×2, then Opus; contrast and copy repair). Loop termination is enforced in code but **not covered by a dedicated test** |
| 18 | Cloudflare real API | ⚠️ implemented (wrangler Direct Upload + REST confirmation and cleanup). **Not live-verified**: no token |
| 19 | Deployment verified before success | ✅ API confirmation + live URL, title and noindex check. Tested with doubles |
| 20 | Preview URLs in dashboard | ✅ |
| 21 | Costs and usage recorded | ✅ reservations, ledger, usage events, per campaign and site, estimated vs reported |
| 22 | Honest error states | ✅ E2E-verified |
| 23 | Authorization and isolation tested | ✅ API cross-workspace, forged header, viewer role, RLS |
| 24 | Safe job recovery | ✅ leases, heartbeats, dead letters, backoff, per-workspace kill switch |
| 25 | Drafts from verified evidence | ✅ tested |
| 26 | Sending protected by gates | ✅ tested: readiness, authorization, limits, suppression re-check, harvesting flag, ambiguous result never resent, fail-closed unsubscribe |
| 27 | Voice implemented and testable | ⚠️ LiveKit agent (`apps/voice`, livekit-agents 1.8.12) + token endpoint + internal pipeline endpoint (tested). **Not live-verified**: no LiveKit project |
| 28 | Monitoring, logging, backups, docs | ✅ (off-site backup target not configured → `BACKUP_NOT_PROTECTED`) |
| 29 | Automated tests pass | ✅ |
| 30 | Full workflow on real integrations | ❌ **Not achieved**: needs credentials and the Places approval (or an imported list). The full workflow passes end to end with provider doubles |

## 3. What you must provide

| Item | Unblocks | Cost |
|---|---|---|
| `ANTHROPIC_API_KEY` (Claude Console) | parsing fallback, research extraction, site copy, outreach drafts | metered |
| `SERPAPI_API_KEY` | website verification, research | free 250/month |
| `FIRECRAWL_API_KEY` | research extraction | free tier |
| `CLOUDFLARE_API_TOKEN` (Pages:Edit) + `CLOUDFLARE_ACCOUNT_ID` | preview deployment | free |
| Supabase project: `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_JWKS_URL`, dashboard `VITE_SUPABASE_*` | production DB and auth | free tier |
| VPS (2 vCPU / 4 GB) | 24/7 cloud operation | **purchase: needs your authorisation** (≈ $7–10/month) |
| Written approval from Google or counsel (recorded via `POST /compliance/approvals`) + `GOOGLE_PLACES_API_KEY` | Google Places discovery and rating filter | legal / free tier |
| `LIVEKIT_*` + `VOICE_AGENT_SHARED_SECRET` | voice | free tier |
| Sending domain, postal address, Terms/Privacy URLs, `RESEND_API_KEY`, `UNSUBSCRIBE_SIGNING_KEY` | outreach sending | domain is a purchase if you have none |
| Off-site backup bucket (`BACKUP_S3_*`) + `BACKUP_AGE_RECIPIENT` | `BACKUP_NOT_PROTECTED` → `PROTECTED` | free tier (R2) |

## 4. Implementation decisions differing from earlier documents
- **Website generation:** Claude produces a structured SiteSpec (copy and strategy only). A deterministic, owned renderer (6 archetypes, 12 palettes with WCAG-enforced contrast, 8 type pairings, motifs, diversity guard) produces static HTML/CSS. The model never emits executable code. This removes the need for an npm build of model-written code.
- **Orchestration:** a Postgres-checkpointed workflow engine (`jarvis/workflow/engine.py`) instead of LangGraph, as the brief allowed ("or an equivalent").
- **Verification statuses** follow the build brief: `VERIFIED_HAS_WEBSITE` (+ `site_health` working, broken or outdated), `VERIFIED_NO_WEBSITE`, `WEBSITE_UNCERTAIN`, `CONFLICTING_EVIDENCE`, `VERIFICATION_FAILED`.
- **Unsubscribe and webhooks** are served by the API over HTTPS (Caddy) and backed by the authoritative DB (fail-closed), not by a Cloudflare Worker + KV.
- **Deployment upload** uses the official `wrangler pages deploy` (pinned `wrangler@4.160.1`), with REST confirmation and cleanup.
- **Docker access:** the worker reaches Docker only through a restricted socket proxy (container lifecycle only). Sandboxes never see the socket.

## 5. Known limitations
- Provider-reported usage sync is on demand (Settings → Test) rather than scheduled. Google usage is entered manually.
- Retention purge covers task events (90 days) and raw scraped pages (90 days). Purging SERP results after 180 days is not yet scheduled.
- The rate limiter is in-process, so it only covers a single API instance.
- The Docker sandbox image build needs registry access during `npm ci`. In this build environment it was verified with pre-installed, lockfile-identical modules.
