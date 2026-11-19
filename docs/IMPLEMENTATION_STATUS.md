# JARVIS — Implementation Status

As of 2026-11-19 (revision 6: consistency and data-integrity audit). Branch `claude/sleepy-shannon-4lorvo`.

**Overall: NOT production-ready and NOT deployed.** The platform is implemented and passes its automated tests
against real local infrastructure (PostgreSQL, Chromium, Docker) with **test doubles for every third-party provider**.
No third-party integration has been exercised against the real provider, because no credentials were available.
No VPS has been purchased, so nothing runs in production.

Four separate levels are tracked below. A higher level is never implied by a lower one:
- **Implemented:** code exists in the repository.
- **Test-verified:** an automated test exercises it (real Postgres, Chromium and Docker; providers via doubles unless stated).
- **Live-verified:** exercised against the real third-party provider with real credentials. **None so far.**
- **Deployed:** running on production infrastructure. **None so far.**

## 1. Test results (latest run, 2026-11-19)

| Suite | Where | Result | What it exercises |
|---|---|---|---|
| Backend `pytest` | this environment | **122 passed, 4 skipped** | Real PostgreSQL 16 (migrations 0001–0006, RLS); real Chromium (Playwright/axe); real Docker sandbox and sandbox runner; S3-protocol server (moto, loopback) for backups; provider HTTP via test doubles |
| Backend live tests (`tests/live`) | this environment | **4 skipped**: no credentials | Claude, SerpApi, Firecrawl, Cloudflare. One capped call each |
| Voice agent (`apps/voice/tests`) | this environment | **5 passed, 2 skipped** | Offline turn handling, identity binding, real-SDK object construction. Live LiveKit test skipped: no credentials |
| Dashboard Vitest / typecheck / build | this environment | **8 passed / clean / pass** | |
| Real-stack E2E | this environment | **pass** (2 consecutive runs) | Real API, worker, Postgres and built dashboard in Chromium |
| Lint | this environment | `ruff` clean | |
| GitHub Actions CI | GitHub, last observed run (commit 9e67f88) | **failed overall.** `backend`, `dashboard`, `voice` and `images` (QA sandbox image, no-network check, app image) **passed**. `secrets-scan` **failed** (see §5) | The revision 6 commits had not been run by CI when this was written |

Regression tests added in revision 6:
- `test_architecture_consistency.py` (4): only `sandbox-runner` mounts the Docker socket; no document, script, CI file or config describes the removed socket proxy as current; the docs name the runner as the sole socket holder; CI and deploy pin the sandbox image.
- `test_evidence_integrity.py` (5): purged evidence becomes a content-free tombstone; no dangling references; verification summaries unchanged; legal holds block deletion and tombstoning; ad hoc and cascading deletes; backfill of earlier dangling references; API shows `expired` references.
- `test_retention_usage_scheduler.py` (+2): Anthropic local cost is never reported as provider billing; the database refuses mislabelled costs.
- `test_backup.py` (+3): a missing checksum fails the drill; inconsistent restored row counts fail the drill; a bucket on this server's own public IP is not independent.
- `test_api.py` (+1): concurrent first requests create exactly one workspace (bug found by the E2E and fixed).
- Dashboard (+1): usage labels show local estimates and never an unconfirmed invoice.

## 2. Acceptance criteria

✅ yes · ⚠️ partly (see notes) · ❌ no · — not applicable (no third-party provider involved)

| # | Criterion | Implemented | Test-verified | Live-verified | Deployed | Notes |
|---|---|---|---|---|---|---|
| 1 | User can authenticate | ✅ | ✅ | ❌ | ❌ | JWT verification (JWKS or HS256) tested with locally minted tokens. No real Supabase project |
| 2 | Natural-language text command | ✅ | ✅ | — | ❌ | API, UI and E2E |
| 3 | Parsed into structured intent | ✅ | ✅ | ❌ | ❌ | Deterministic parser tested. The Claude Haiku fallback is tested with a double only |
| 4 | Persistent task | ✅ | ✅ | — | ❌ | Postgres queue |
| 5 | Survives restarts | ✅ | ✅ | — | ❌ | A lease-expired task is reclaimed and completes (at-least-once) |
| 6 | Discovery via an approved, configured provider | ✅ | ✅ | ❌ | ❌ | **Google Places: BLOCKED_PENDING_APPROVAL** (compliance gate) and NOT_CONFIGURED. Adapter tested with doubles. `manual_import` works end to end |
| 7 | Provenance retained | ✅ | ✅ | — | ❌ | Evidence and facts carry source, quote and timestamp. Purged evidence leaves a content-free tombstone. T14 proves no Places content is persisted |
| 8 | Multi-source website verification | ✅ | ✅ | ❌ | ❌ | Doubles only. Real SerpApi results and real sites not yet exercised |
| 9 | Uncertain findings marked | ✅ | ✅ | ❌ | ❌ | `WEBSITE_UNCERTAIN`, `CONFLICTING_EVIDENCE`, `VERIFICATION_FAILED`. Missing credentials block the campaign |
| 10 | Research via real search/extraction | ✅ | ✅ | ❌ | ❌ | SerpApi + Firecrawl + Haiku with quote verification; doubles only |
| 11 | Deterministic, explainable scoring | ✅ | ✅ | — | ❌ | |
| 12 | Unique websites | ✅ | ✅ | ❌ | ❌ | 12-site diversity test; site copy from a Claude double. Real model copy not yet exercised |
| 13 | Verified facts only | ✅ | ✅ | ❌ | ❌ | Factual guard + QA fact check drop an invented fact (double). Real model behaviour unverified |
| 14 | Isolated builds | ✅ | ✅ | — | ❌ | Model never writes code. QA runs in the no-network Docker sandbox via the sandbox runner. Images built in CI |
| 15 | Real browser QA | ✅ | ✅ | — | ❌ | Playwright + axe, desktop and mobile |
| 16 | QA findings stored | ✅ | ✅ | — | ❌ | |
| 17 | Bounded repairs | ✅ | ✅ | ❌ | ❌ | Max 3 (Sonnet, Sonnet, Opus); 8 tests with doubles |
| 18 | Cloudflare real API | ✅ | ✅ | ❌ | ❌ | wrangler Direct Upload + REST confirmation; doubles only. No token |
| 19 | Deployment verified before success | ✅ | ✅ | ❌ | ❌ | API confirmation + live URL, title and noindex check; doubles only. Previously shown as fully green; corrected |
| 20 | Preview URLs in dashboard | ✅ | ✅ | ❌ | ❌ | No real `pages.dev` URL produced yet |
| 21 | Costs and usage recorded | ✅ | ✅ | ❌ | ❌ | Reservations, ledger and usage events. **All JARVIS money figures are local estimates.** Provider-reported usage: SerpApi/Firecrawl tested with doubles; Anthropic, Google, Cloudflare, Resend, LiveKit account usage **UNAVAILABLE**; no invoice integrated |
| 22 | Honest error states | ✅ | ✅ | — | ❌ | E2E |
| 23 | Authorization and isolation tested | ✅ | ✅ | ❌ | ❌ | API cross-workspace, forged header, viewer role, RLS on local Postgres. RLS on a real Supabase project not verified |
| 24 | Safe job recovery | ✅ | ✅ | — | ❌ | Leases, heartbeats, dead letters, backoff, per-workspace kill switch |
| 25 | Drafts from verified evidence | ✅ | ✅ | ❌ | ❌ | Claude double |
| 26 | Sending protected by gates | ✅ | ✅ | ❌ | ❌ | Gates tested. Sending is not configured (no domain, no Resend key) |
| 27 | Voice implemented and testable | ✅ | ⚠️ | ❌ | ❌ | **BLOCKED.** Offline tests only. The credentialed live test has never run (no LiveKit project). Not production-ready until it passes |
| 28 | Monitoring, logging, backups, docs | ✅ | ⚠️ | ❌ | ❌ | Backup logic tested against moto on loopback only. **R2 or any real bucket is unverified. Status stays `BACKUP_NOT_PROTECTED`** until a real independent destination passes upload, checksum and restore drill |
| 29 | Automated tests pass | ✅ | ✅ | — | — | Locally: all pass. CI: functional jobs pass; `secrets-scan` fails (§5) |
| 30 | Full workflow on real integrations | ✅ | ✅ | ❌ | ❌ | **Not achieved.** Passes end to end with provider doubles only. Needs credentials and the Places approval (or an imported list) |
| — | Production deployment | ⚠️ | ⚠️ | — | ❌ | Compose, deploy script and hardening are implemented and checked statically; images build in CI. **No VPS: nothing deployed** |

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
- **Docker access:** only the `sandbox-runner` service mounts the Docker socket. The worker has no socket and reaches the runner over the internal network (`POST /qa`, bearer token), which starts the QA container with fixed hardened flags. Sandboxes never see the socket. (Revision 4 used a generic socket proxy; it was removed in revision 5.)

## 5. Known limitations
- **CI `secrets-scan` fails.** gitleaks reports one `generic-api-key` finding at `env/app.env.example` line 64 (commit 0d15153). Line 64 is an empty `SERPAPI_API_KEY=` placeholder and the match continues onto the next line (`SERPAPI_CAP_MONTHLY=240`), so this appears to be a false positive and no secret is present. It was **not** suppressed: adding an allowlist or `.gitleaksignore` entry is an owner decision.
- **App image build not verified in this environment.** This container's proxy blocks the registries the Docker build needs. The CI `images` job built both images successfully at commit 9e67f88.
- **Google usage is not reported automatically.** No usage API is integrated; reports are `unavailable` until imported or entered manually.
- **Claude account-level usage and billing are UNAVAILABLE.** No Usage and Cost Admin API is integrated. JARVIS records the token counts returned on its own responses and prices them locally; these are labelled `local_estimate` and never shown as reconciled or invoiced (tested).
- **Expired evidence references.** Retention removes uncited SERP evidence after 180 days and raw pages after 90 days. Since migration 0005 each purge leaves a content-free tombstone, so verification references resolve as `expired` rather than dangling (tested). The UI does not yet render expired references distinctly; the API returns them as `evidence_refs`.
