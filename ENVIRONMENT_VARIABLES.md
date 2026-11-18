# JARVIS — Environment Variables

Status: **Proposed, revision 3.** Server configuration is validated at startup by a Pydantic
`Settings` class. Dashboard configuration is validated by a Zod schema.
- A missing **required** variable stops the process in `production`.
- An **optional** integration that isn't set reports `NOT_CONFIGURED`, and never falls back silently to a mock.

Classification: **SECRET** (credential, server-only, never logged), **SENSITIVE** (not public),
**PUBLIC** (safe in the browser bundle), **CONFIG** (tuning).
Consumers: `api`, `worker`, `voice`, `dashboard`, `edge` (Cloudflare Worker), `backup`, `ci`.

Revision 3: Places API (New) naming; `PLACES_API_BASE_URL`; the listing-panel cap is now Details Enterprise; rating, compliance-gate and website-verification variables. The compliance gate itself lives in the DB (`compliance_approvals`), not in env vars.

Revision 2: added VPS/hostname, model-routing, usage-sync, coordinate-storage, sending-readiness,
backup and edge-Worker variables. Budget defaults now match the cost model §6.4.

## 1. Core runtime (VPS)

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `JARVIS_ENV` | `local` \| `test` \| `live-test` \| `production` | Required | api, worker, voice | CONFIG | enum. Startup aborts if `JARVIS_ENABLE_TEST_DOUBLES=true` outside `test` |
| `JARVIS_ENABLE_TEST_DOUBLES` | enables test doubles | Optional (false) | test only | CONFIG | allowed only when `JARVIS_ENV=test` |
| `JARVIS_LOG_LEVEL` | log level | Optional (`INFO`) | all | CONFIG | enum |
| `JARVIS_ENCRYPTION_KEY` | AES-256-GCM key for secrets stored in the DB | Required | api, worker | **SECRET** | 32 bytes base64 (`openssl rand -base64 32`) |
| `JARVIS_WORKER_ID` | worker identity | Optional (hostname) | worker | CONFIG | slug |
| `JARVIS_WORKER_CONCURRENCY` | parallel non-build tasks | Optional (`3`) | worker | CONFIG | 1–8 (sized for 2 vCPU / 4 GB) |
| `JARVIS_TASK_LEASE_SECONDS` | lease length | Optional (`300`) | worker | CONFIG | 60–1800 |
| `API_PUBLIC_BASE_URL` | public API URL (provider hostname or `<ip>.sslip.io` until a domain exists) | Required | api, worker, edge | SENSITIVE | https URL |
| `API_CORS_ORIGINS` | dashboard origin(s) | Required | api | CONFIG | https URLs |
| `CADDY_ACME_EMAIL` | Let's Encrypt registration email | Required | caddy | SENSITIVE | email |
| `SANDBOX_IMAGE` | pinned build-sandbox image digest | Required | worker | CONFIG | `name@sha256:…` |
| `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_TIMEOUT_S` | sandbox limits | Optional (`1` / `1g` / `180`) | worker | CONFIG | numeric |

## 2. Supabase and database

Get these from Supabase Dashboard → Project Settings → API / Database.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `SUPABASE_URL` | project URL | Required | api, worker | SENSITIVE | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | server access (bypasses RLS) | Required | api, worker | **SECRET** | never in the dashboard |
| `SUPABASE_JWT_ISSUER` / `SUPABASE_JWKS_URL` | user token verification | Required | api | CONFIG | URLs |
| `DATABASE_URL` | Postgres (session pooler) | Required | api, worker, backup | **SECRET** | `postgresql+asyncpg://…`, TLS required |
| `DATABASE_POOL_SIZE` | pool per process | Optional (`5`) | api, worker | CONFIG | keep the total under Free plan limits |
| `DB_HEALTH_INTERVAL_S` | health probe interval | Optional (`30`) | api, worker | CONFIG | 5–300 |
| `DB_RECONNECT_MAX_BACKOFF_S` | reconnect backoff ceiling | Optional (`60`) | api, worker | CONFIG | 5–600 |
| `SUPABASE_STORAGE_BUCKET_SITES` | private bucket | Optional (`sites`) | worker | CONFIG | |
| `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` | browser auth | Required | dashboard | PUBLIC | RLS protected |
| `VITE_API_BASE_URL` | API URL | Required | dashboard | PUBLIC | https URL |

## 3. Backups

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `BACKUP_SCHEDULE_CRON` | daily `pg_dump` schedule | Optional (`0 4 * * *`) | backup | CONFIG | cron |
| `BACKUP_AGE_RECIPIENT` | age public key for encryption | Required | backup | SENSITIVE | `age1…` (keep the private key offline) |
| `BACKUP_R2_ACCOUNT_ID` / `BACKUP_R2_BUCKET` | off-site target | Optional (recommended) | backup | SENSITIVE / CONFIG | |
| `BACKUP_R2_ACCESS_KEY_ID` / `BACKUP_R2_SECRET_ACCESS_KEY` | R2 token scoped to the backup bucket | Optional | backup | **SECRET** | |

## 4. Google Places API (New)

Google Maps Platform setup: in a Google Cloud project, enable **only Places API (New)** (not
Maps JavaScript, Embed, Static, Routes, Geocoding or Autocomplete) → Credentials → API key.
Restrict the key to Places API (New) and the VPS IP. Set per-day quotas and budget alerts (alerts
are warnings only). **Having a key doesn't enable Places usage.** The compliance gate in
`compliance_approvals` must also be approved for the relevant scope.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `GOOGLE_PLACES_API_KEY` | Places API (New) calls (Text Search; Place Details for the listing panel) | Optional (shows NOT_CONFIGURED without it; COMPLIANCE_BLOCKED while the gate is closed) | worker, api (listing panel) | **SECRET** | `AIza…`, 39 chars |
| `PLACES_API_BASE_URL` | Places API (New) host | Optional (`https://places.googleapis.com/v1`) | worker, api | CONFIG | must be exactly this host in production (no other Google API hosts are allowed) |
| `PLACES_CAP_TEXT_SEARCH_ENTERPRISE_MONTHLY` / `_DAILY` | app hard cap | Optional (`900` / `60`) | worker | CONFIG | ≤ free allowance unless `allow_paid` |
| `PLACES_CAP_TEXT_SEARCH_PRO_MONTHLY` | fallback mask cap | Optional (`4500`) | worker | CONFIG | |
| `PLACES_CAP_DETAILS_ENTERPRISE_MONTHLY` | listing-panel cap (`LISTING_DISPLAY` is Enterprise because it includes the rating) | Optional (`100`) | api | CONFIG | ≤ free allowance unless `allow_paid` |
| `PLACES_INCLUDE_SERVICE_AREA_DEFAULT` | default for `includePureServiceAreaBusinesses` | Optional (`true`) | worker | CONFIG | bool |
| `DEFAULT_MISSING_RATING_POLICY_PRESELECT` | which option the confirmation card pre-selects (the operator must still confirm) | Optional (`exclude`) | api | CONFIG | `exclude` \| `include_for_review` |
| `PLACES_STORE_COORDINATES` | persist lat/lng (≤ 30 days) | Optional (`false`) | worker | CONFIG | bool |
| `PLACES_COORDINATES_TTL_DAYS` | coordinate expiry | Optional (`30`) | worker | CONFIG | 1–30 (hard upper bound) |

There is **no** cache-TTL variable for other Places content, because it is never stored. There is **no** default minimum-rating variable: `minimum_google_rating` comes only from the operator's command.

### 4a. Website verification

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `NO_WEBSITE_MIN_CONFIDENCE` | threshold for `NO_WEBSITE` (otherwise `WEBSITE_UNKNOWN`) | Optional (`0.8`) | worker | CONFIG | 0.5–1.0 |
| `BROKEN_SECOND_PROBE_MINUTES` | delay before the confirming probe | Optional (`10`) | worker | CONFIG | ≥ 10 |
| `MAX_CANDIDATE_DOMAINS` / `MAX_PROFILE_CHECKS` | independent-check bounds | Optional (`3` / `3`) | worker | CONFIG | 1–5 |

## 5. SerpApi

Get it from serpapi.com → Dashboard → API Key.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `SERPAPI_API_KEY` | search | Optional | worker | **SECRET** | 64 hex chars. Redacted in logs |
| `SERPAPI_CAP_MONTHLY` / `SERPAPI_CAP_HOURLY` | app hard caps | Optional (`240` / `45`) | worker | CONFIG | below plan limits |
| `SERPAPI_USAGE_SYNC_MINUTES` | provider-reported usage sync | Optional (`60`) | worker | CONFIG | |

## 6. Firecrawl

Get it from firecrawl.dev → Dashboard → API Keys. Keep auto-recharge **off**.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `FIRECRAWL_API_KEY` | scrape/crawl | Optional | worker | **SECRET** | `fc-…` |
| `FIRECRAWL_CAP_CREDITS_MONTHLY` | app hard cap | Optional (`950`) | worker | CONFIG | |
| `FIRECRAWL_MAX_PAGES_PER_PROSPECT` | crawl limit | Optional (`5`) | worker | CONFIG | 1–20 |
| `FIRECRAWL_USAGE_SYNC_MINUTES` | usage sync | Optional (`60`) | worker | CONFIG | |

## 7. Claude API

Get it from platform.claude.com → API Keys. Use a dedicated workspace with a **spend limit of $35/month**.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | Claude calls | Required for commands, research, building, drafts | api, worker, voice | **SECRET** | `sk-ant-…` |
| `ANTHROPIC_ADMIN_KEY` | Usage and Cost API (provider-reported usage) | Optional | worker | **SECRET** | `sk-ant-admin…`. Sync shows "not available" if unset |
| `CLAUDE_MODEL_FAST` | classification, extraction, routing | Optional (`claude-haiku-5-5`) | api, worker | CONFIG | allowed list |
| `CLAUDE_MODEL_BUILD` | strategy, generation, ordinary repair, outreach copy | Optional (`claude-sonnet-5-5`) | worker | CONFIG | allowed list |
| `CLAUDE_MODEL_ESCALATION` | difficult reasoning, repair escalation | Optional (`claude-opus-5-5`) | worker | CONFIG | allowed list |
| `OPUS_MAX_ESCALATIONS_PER_SITE` / `_PER_DAY` | escalation caps | Optional (`2` / `5`) | worker | CONFIG | int |
| `SITE_MAX_COST_MICROS` | per-site hard cap | Optional (`2000000`) | worker | CONFIG | int |
| `CLAUDE_USE_BATCHES` | batch non-urgent builds | Optional (`false`) | worker | CONFIG | bool |

## 8. Groq (deferred, not used in v1)
`GROQ_API_KEY`: reserved, optional, **SECRET**. Ignored in v1.

## 9. Cloudflare

Get a custom API token from Cloudflare Dashboard → My Profile → API Tokens, with Account →
Cloudflare Pages → Edit and Workers KV → Edit, on one account only.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Pages deployments and cleanup | Optional (Deployment shows NOT_CONFIGURED without it) | worker, ci | **SECRET** | verified via `/user/tokens/verify` |
| `CLOUDFLARE_ACCOUNT_ID` | account | Required with token | worker, ci | SENSITIVE | 32 hex |
| `CLOUDFLARE_PAGES_PREVIEW_PROJECT` | preview project | Optional (`jarvis-previews`) | worker | CONFIG | slug |
| `PREVIEW_TTL_DAYS` | expiry | Optional (`30`) | worker | CONFIG | 1–90 |
| `EDGE_TO_API_HMAC_SECRET` | signs edge → API forwards | Required if the edge Worker is deployed | api, edge | **SECRET** | ≥ 32 bytes |
| `EDGE_PUBLIC_BASE_URL` | `https://jarvis-edge.<acct>.workers.dev` | Required for unsubscribe links | api, worker | CONFIG | https URL |

## 10. Outreach and email

Drafting needs **none** of these. Sending is blocked until the sending-readiness gate passes (SECURITY_AND_COMPLIANCE §7.3).

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `EMAIL_PROVIDER` | `resend` \| `ses` | Required to send | worker, api | CONFIG | enum |
| `RESEND_API_KEY` | send | Required to send (Resend) | worker | **SECRET** | `re_…` |
| `RESEND_WEBHOOK_SECRET` | signature check (at the edge) | Required to send (Resend) | edge | **SECRET** | `whsec_…` |
| `AWS_SES_ACCESS_KEY_ID` / `AWS_SES_SECRET_ACCESS_KEY` / `AWS_SES_REGION` | SES alternative | Required to send (SES) | worker | **SECRET** / **SECRET** / CONFIG | IAM limited to `ses:SendEmail` |
| `EMAIL_SENDING_DOMAIN` | authenticated domain | Required to send | worker | SENSITIVE | SPF/DKIM/DMARC checked. `pages.dev`, `workers.dev` and free-mail rejected |
| `EMAIL_FROM_ADDRESS` / `EMAIL_REPLY_TO` | sender / monitored inbox | Required to send | worker | SENSITIVE | must be on the sending domain |
| `UNSUBSCRIBE_SIGNING_KEY` | signs unsubscribe tokens (with `kid`) | Required to send | api, worker, edge | **SECRET** | ≥ 32 bytes |

The postal address isn't an environment variable. It's entered, typed and attested in Settings
(`sending_readiness`), so the attestation is audited.

## 11. LiveKit (Phase 7)

Get these from LiveKit Cloud → Project → Settings → Keys.

| Variable | Purpose | Req | Consumers | Class |
|---|---|---|---|---|
| `LIVEKIT_URL` | WebSocket URL | Optional | api, voice | SENSITIVE |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | token minting | Optional | api, voice | **SECRET** |
| `VOICE_STT_PROVIDER` / `VOICE_TTS_PROVIDER` | provider choice | Optional | voice | CONFIG |

## 12. Budgets (defaults; can be overridden in `system_settings` / `usage_limits`)

| Variable | Default | Purpose |
|---|---|---|
| `BUDGET_DAILY_CAP_USD` | `5` | global metered spend per day |
| `BUDGET_MONTHLY_CAP_USD` | `35` | global metered spend per month (matches the Claude Console limit) |
| `BUDGET_CAMPAIGN_DEFAULT_USD` | `5` | per-campaign cap |
| `ALLOW_PAID_DEFAULT` | `false` | caps can't exceed free allowances unless changed per limit by the owner |
| `MAX_CONCURRENT_BUILDS` | `1` | sandbox concurrency |
| `OUTREACH_DAILY_LIMIT_DEFAULT` | `20` | per outreach campaign |
| `PRICING_OVERRIDES_JSON` | unset | unit-price corrections (versioned) |

## 13. Live-test caps (`JARVIS_ENV=live-test`, manual CI workflow only)

| Variable | Default | Purpose |
|---|---|---|
| `LIVE_TEST_MAX_PLACES_REQUESTS` | `3` | per run. Also needs the `google_places_technical_testing` approval |
| `LIVE_TEST_MAX_SERP_SEARCHES` | `5` | per run |
| `LIVE_TEST_MAX_FIRECRAWL_CREDITS` | `5` | per run |
| `LIVE_TEST_MAX_CLAUDE_USD` | `0.50` | per run (site-build live test: `2.00`) |
| `LIVE_TEST_MAX_CF_DEPLOYS` | `1` | per run (deleted at teardown) |

## 14. CI secrets (GitHub Actions)
`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `SUPABASE_ACCESS_TOKEN`, `SUPABASE_DB_PASSWORD`,
`VPS_SSH_KEY` (deploy), and live-test provider keys in a protected `live-test` environment that
needs manual approval. All **SECRET**.

## 15. Rotation
| Secret | When | How |
|---|---|---|
| Provider keys | suspected leak, every 6 months | create new → update → restart → revoke old |
| Supabase service key / DB password | suspected leak | roll in Supabase → update → restart |
| `JARVIS_ENCRYPTION_KEY` | yearly | dual-key re-encryption script (Phase 9) |
| Signing keys (`UNSUBSCRIBE_SIGNING_KEY`, `EDGE_TO_API_HMAC_SECRET`) | suspected leak | rotate with a `kid`. Old links stay valid until expiry |
| Backup age key | if compromised | new key pair. Re-encrypt retained backups |
