# JARVIS — Implementation Status

As of 2026-11-19 (revision 5 hardening). Branch `claude/sleepy-shannon-4lorvo`.
Every claim below is backed by an automated test or a run recorded in this document. "Live-verified" means
exercised against the real third-party provider. **No provider credentials were available in the build
environment, so no third-party integration has been live-verified yet.**

## 1. Test results (latest run, 2026-11-19)

| Suite | Result | What it exercises |
|---|---|---|
| Backend `pytest` | **107 passed, 4 skipped** | Real PostgreSQL 16 (migrations 0001–0004, RLS); real Chromium (Playwright/axe); real Docker sandbox and sandbox runner; S3-protocol server (moto) for backups; provider HTTP via test doubles |
| Backend live tests (`tests/live`) | **4 skipped**: no credentials | Claude, SerpApi, Firecrawl, Cloudflare. One capped call each |
| Voice agent (`apps/voice/tests`) | **5 passed, 2 skipped** | Offline turn handling, identity binding, real-SDK object construction. The live LiveKit test skipped: credentials not set |
| Dashboard Vitest / typecheck / build | **7 passed / clean / pass** | |
| Real-stack E2E | **pass** | Real API, worker, Postgres and built dashboard in Chromium |
| Lint | `ruff` clean | |

New regression tests this revision:
- `test_ratelimit.py` (4): concurrency across 3 pools; refill; 2 API instances sharing a bucket; provider limit across worker pools before any reservation.
- `test_sandbox_runner.py` (5): fixed hardened flags; auth; path confinement and symlink rejection; no pull for a missing image; a real run through runner mode; an in-sandbox probe (no socket, no host filesystem, no secrets, uid 10001, all capabilities zero, no network).
- `test_retention_usage_scheduler.py` (5): every retention rule including 180-day SERP cleanup; protected record types; legal hold and release; usage sync never writes zero for unknown usage; the `/usage` figures; scheduler locking and once-per-interval.
- `test_repair_loop.py` (8): Sonnet repairs 1 and 2, Opus repair 3, success at each stage, failure after the limit, unrepairable failure, contrast repair without a model call, no model call after the limit even on resume.
- `test_backup.py` (4): off-site upload with checksum; restore drill from the off-site copy (row counts match); `PROTECTED` only for an independent destination with a drill; tampered copy rejected; independence check.
- `test_production_config.py` (4): production settings refuse test doubles, subprocess or docker QA, a missing runner, or a missing JWKS; no test-double imports in the package; compose hardening invariants; deploy retry after a crash never re-uploads.

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
| 17 | Bounded repairs | ✅ max 3 (Sonnet, Sonnet, Opus). 8 dedicated tests, including the hard stop and no model call after the limit |
| 18 | Cloudflare real API | ⚠️ implemented (wrangler Direct Upload + REST confirmation and cleanup). **Not live-verified**: no token |
| 19 | Deployment verified before success | ✅ API confirmation + live URL, title and noindex check. Tested with doubles |
| 20 | Preview URLs in dashboard | ✅ |
| 21 | Costs and usage recorded | ✅ reservations, ledger, usage events, per campaign and site, estimated vs reported |
| 22 | Honest error states | ✅ E2E-verified |
| 23 | Authorization and isolation tested | ✅ API cross-workspace, forged header, viewer role, RLS |
| 24 | Safe job recovery | ✅ leases, heartbeats, dead letters, backoff, per-workspace kill switch |
| 25 | Drafts from verified evidence | ✅ tested |
| 26 | Sending protected by gates | ✅ tested: readiness, authorization, limits, suppression re-check, harvesting flag, ambiguous result never resent, fail-closed unsubscribe |
| 27 | Voice implemented and testable | ⚠️ Audited and hardened. Offline tests pass. A credentialed live test (`apps/voice/tests/test_voice_live.py`) covers auth, audio in, transcription, command execution, spoken reply and reconnect, but **has not run: no LiveKit project**. Voice is **not** production-ready until it passes |
| 28 | Monitoring, logging, backups, docs | ✅ Off-site backup and restore drill implemented and tested against an S3-protocol server on loopback. **`BACKUP_NOT_PROTECTED` remains** until a real independent bucket (for example R2) is configured and a drill from it succeeds |
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
- **App image build not verified in this environment.** The Docker build cannot reach apt/npm registries through this container's proxy (TLS interception). CI builds both images (`images` job). The QA sandbox image was verified here from lockfile-identical modules.
- **Google usage is not reported automatically.** No usage API is integrated; reports are `unavailable` until imported or entered manually.
- **Claude reported usage is per-response token counts priced locally.** The organisation billing API is not integrated.
- **Dangling evidence references after SERP cleanup.** Retention removes uncited SERP evidence after 180 days. Verification records keep their check summaries, but their `evidence_ids` may then point to purged rows.
