# JARVIS — Implementation Status

As of 2026-11-19 (revision 7: Integrations Center, voice agent and unified conversation router). Branch `main`.

**Overall: NOT production-ready and NOT deployed.** The platform is implemented and passes its automated tests
against real local infrastructure (PostgreSQL, Chromium, Docker) with **test doubles for every third-party provider**.
No provider has been validated as CONNECTED with real credentials, because none were available. What has been
checked live is narrower: the five providers below were called for real with deliberately invalid keys, and each
rejection was reported correctly as INVALID_CREDENTIALS (section 1a).
No VPS has been purchased, so nothing runs in production.

Four separate levels are tracked below. A higher level is never implied by a lower one:
- **Implemented:** code exists in the repository.
- **Test-verified:** an automated test exercises it (real Postgres, Chromium and Docker; providers via doubles unless stated).
- **Live-verified:** exercised against the real third-party provider. **No successful connection yet** (no real credentials);
  only invalid-credential detection has been live-verified (section 1a).
- **Deployed:** running on production infrastructure. **None so far.**

## 1. Test results (latest run, 2026-11-19)

| Suite | Where | Result | What it exercises |
|---|---|---|---|
| Backend `pytest` | this environment | **151 passed, 4 skipped** | Real PostgreSQL 16 (migrations 0001–0008, RLS); real Chromium (Playwright/axe); real Docker sandbox and sandbox runner; S3-protocol server (moto) for backups; provider HTTP via test doubles or httpx MockTransport |
| Backend live tests (`tests/live`) | this environment | **4 skipped**: no credentials | Claude, SerpApi, Firecrawl, Cloudflare. One capped call each |
| Voice agent (`apps/voice/tests`) | this environment | **10 passed, 2 skipped** | Turn handling, progress narration, control-message ownership, credential sourcing, heartbeat. Live LiveKit test skipped: no credentials |
| Dashboard Vitest / typecheck / build | this environment | **14 passed / clean / pass** | Integrations Center, voice states, usage labels, command flow |
| Real-stack E2E | this environment | **pass** | Real API, worker, Postgres and built dashboard in Chromium, including the Integrations Center, Voice page and the live invalid-key checks below |
| Lint | this environment | `ruff` clean | |
| GitHub Actions CI | GitHub, `main` at 9d768a7 | **all jobs passed** | backend, dashboard, voice, images, secrets-scan. Revision 7 commits not yet observed in CI when this was written |

### 1a. Live provider checks (real network calls, deliberately invalid keys)
Through the real dashboard and API, an administrator saved format-valid but invalid keys and ran Test connection:

| Provider | Real response | JARVIS state |
|---|---|---|
| SerpApi | HTTP 401 "Invalid API key" | INVALID_CREDENTIALS |
| Anthropic | HTTP 401 | INVALID_CREDENTIALS |
| Firecrawl | HTTP 401 "Unauthorized: Invalid token" | INVALID_CREDENTIALS |
| Cloudflare | HTTP 401 "Invalid API Token" | INVALID_CREDENTIALS |
| Resend | HTTP **400** "API key is invalid" | INVALID_CREDENTIALS (this check found a mapping bug: Resend's 400 was shown as a generic error; fixed) |

This proves the Test connection path reaches the real providers and classifies rejected keys correctly. It does **not**
prove any provider works with valid credentials. LiveKit and Google Places were not called (no URL / restricted).

Regression tests added in revision 7:
- `test_integrations_center.py` (20): AES-GCM vault (binding, wrong key, tamper, rotation); the database refuses plaintext; admin-only management; fail-closed without a key; secrets never in responses, storage or audit logs; invalid input never echoed; every connection state (CONNECTED, INVALID_CREDENTIALS incl. HTTP 200/400 bodies, RATE_LIMITED, QUOTA_EXCEEDED, CONNECTION_FAILED, PERMISSION_DENIED); disable blocks the gateway; removal falls back to the environment; worker and other API instances pick up changes.
- `test_voice_backend.py` (8): token scope and 10-minute expiry; voice never OPERATIONAL on configuration alone; agent heartbeat and config auth; ending only your own session; multi-turn clarify, status and cancel; the example agency command; viewers cannot start work.
- `test_rating_and_command.py` (+1): "prepare two redesign previews" and similar phrasings enable the build stages.
- Voice agent (+5), dashboard (+6).

Revision 6 tests (architecture consistency, evidence integrity, usage labels, backup drill, workspace race) remain in the suite.

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
| 27 | Voice implemented and testable | ✅ | ✅ | ❌ | ❌ | **BLOCKED (live).** Shared conversation router, scoped tokens, agent heartbeat, progress narration, interrupt and end controls, and a full dashboard experience are implemented and tested offline. The credentialed live test has never run (no LiveKit project). Not production-ready until it passes |
| 28 | Monitoring, logging, backups, docs | ✅ | ⚠️ | ❌ | ❌ | Backup logic tested against moto on loopback only. **R2 or any real bucket is unverified. Status stays `BACKUP_NOT_PROTECTED`** until a real independent destination passes upload, checksum and restore drill |
| 29 | Automated tests pass | ✅ | ✅ | — | — | Locally: all pass. CI: functional jobs pass; `secrets-scan` fails (§5) |
| 30 | Full workflow on real integrations | ✅ | ✅ | ❌ | ❌ | **Not achieved.** Passes end to end with provider doubles only. Needs credentials and the Places approval (or an imported list) |
| 31 | Integrations Center: configure, test, disable and remove providers from the dashboard | ✅ | ✅ | ⚠️ | ❌ | Live: invalid-key detection verified for 5 providers (§1a). No provider validated as CONNECTED (no real keys) |
| 32 | Credentials encrypted, never returned or logged, admin-only | ✅ | ✅ | — | ❌ | AES-256-GCM; fails closed without `JARVIS_ENCRYPTION_KEY` |
| 33 | Voice and text share one command system | ✅ | ✅ | ❌ | ❌ | `/conversation/turn` and `/internal/voice/command` route to the same handlers |
| 34 | Voice reports OPERATIONAL only after real validation and a live agent | ✅ | ✅ | ❌ | ❌ | |
| 35 | Development startup: one command / automatic in Codespaces, migrations gate readiness, no duplicate processes | ✅ | ✅ | — | — | `scripts/tests/dev-startup-test.sh` (real Docker Postgres) + `test_dev_environment.py`. The Codespaces hooks themselves were not run here (no Codespace available to this session) |
| 36 | No sign-in during development; local access refused outside `JARVIS_ENV=local` | ✅ | ✅ | — | — | Production sign-in is deferred; Supabase JWT verification remains for non-local environments |
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
