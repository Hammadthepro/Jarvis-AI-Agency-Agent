# JARVIS — Environment Variables

Status: **Proposed.** Configuration is loaded and validated at startup by a Pydantic
`Settings` class (server) and a Zod schema (dashboard). A **required** variable that is missing
stops the process in `production`. An **optional** integration that isn't set reports
`NOT_CONFIGURED` in `/providers/health`. It never falls back silently to a mock.

Classification:
- **SECRET**: credential. Server-only. Never logged, never in the client bundle.
- **SENSITIVE**: not a credential, but not public (internal URLs, account IDs).
- **PUBLIC**: safe in the browser bundle.
- **CONFIG**: non-sensitive tuning.

Consumers: `api`, `worker`, `voice`, `dashboard`, `sandbox`, `ci`.

## 1. Core runtime

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `JARVIS_ENV` | `local` \| `test` \| `production` | Required | api, worker, voice | CONFIG | enum. In `production`, any `*_TEST_DOUBLE*` flag set aborts startup |
| `JARVIS_LOG_LEVEL` | log level | Optional (`INFO`) | api, worker | CONFIG | enum |
| `JARVIS_ENCRYPTION_KEY` | AES-256-GCM key for secrets stored in the DB | Required | api, worker | SECRET | 32 bytes, base64. Generate with `openssl rand -base64 32` |
| `JARVIS_WORKER_ID` | worker identity in heartbeats | Optional (hostname) | worker | CONFIG | slug |
| `JARVIS_WORKER_CONCURRENCY` | parallel non-build tasks | Optional (`4`) | worker | CONFIG | int 1–16 |
| `JARVIS_TASK_LEASE_SECONDS` | lease duration | Optional (`300`) | worker | CONFIG | int 60–1800 |
| `API_CORS_ORIGINS` | allowed dashboard origins | Required | api | CONFIG | comma-separated https URLs |
| `API_PUBLIC_BASE_URL` | the API's public URL (used for unsubscribe links and webhooks) | Required | api, worker | SENSITIVE | https URL |
| `JARVIS_ENABLE_TEST_DOUBLES` | enables test doubles | Optional (`false`) | test only | CONFIG | must be `false`/unset unless `JARVIS_ENV=test` |

## 2. Supabase

Where to get them: Supabase Dashboard → Project Settings → API / Database.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `SUPABASE_URL` | project URL | Required | api, worker | SENSITIVE | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | server access that bypasses RLS (Storage, admin) | Required | api, worker | **SECRET** | JWT/`sb_secret_…` format. **Never in the dashboard** |
| `SUPABASE_JWT_ISSUER` | expected `iss` for user JWTs | Required | api | CONFIG | URL |
| `SUPABASE_JWKS_URL` | JWKS used to verify user tokens | Required | api | CONFIG | URL |
| `DATABASE_URL` | Postgres connection (session pooler, port 5432, for the worker's `SKIP LOCKED` queue) | Required | api, worker | **SECRET** | `postgresql+asyncpg://…`, `sslmode=require` |
| `DATABASE_POOL_SIZE` | pool size | Optional (`5`) | api, worker | CONFIG | int. Keep the total under the Free plan's connection limit |
| `SUPABASE_STORAGE_BUCKET_SITES` | bucket for site sources and artefacts | Optional (`sites`) | worker | CONFIG | private bucket |
| `VITE_SUPABASE_URL` | auth in the browser | Required | dashboard | PUBLIC | URL |
| `VITE_SUPABASE_ANON_KEY` | anon/publishable key (RLS protected) | Required | dashboard | PUBLIC | key format |
| `VITE_API_BASE_URL` | API base URL | Required | dashboard | PUBLIC | https URL |

## 3. Google Places API (New)

Where to get it: Google Cloud Console → APIs & Services → enable "Places API (New)" → Credentials → API key. Restrict it to Places API (New) and the host's IP. Set a billing budget alert.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `GOOGLE_PLACES_API_KEY` | Places calls | Optional (Discovery shows NOT_CONFIGURED without it) | worker | **SECRET** | starts with `AIza`, 39 chars |
| `PLACES_MAX_TEXT_SEARCH_PRO_MONTHLY` | unit cap | Optional (`4500`) | worker | CONFIG | ≤ free allowance by default |
| `PLACES_MAX_ENTERPRISE_CALLS_MONTHLY` | unit cap (Details/Text Search Enterprise) | Optional (`950`) | worker | CONFIG | int |
| `PLACES_CACHE_TTL_HOURS` | `places_cache` expiry | Optional (`720`) | worker | CONFIG | int ≤ 720. Set per the current terms |

## 4. SerpApi

Where to get it: serpapi.com → Dashboard → API Key.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `SERPAPI_API_KEY` | Google Search via SerpApi | Optional | worker | **SECRET** | 64 hex chars. Redacted from URLs in logs |
| `SERPAPI_MAX_SEARCHES_MONTHLY` | unit cap | Optional (`240`) | worker | CONFIG | int |
| `SERPAPI_MAX_PER_HOUR` | throughput cap | Optional (`45`) | worker | CONFIG | int below plan limit |

## 5. Firecrawl

Where to get it: firecrawl.dev → Dashboard → API Keys.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `FIRECRAWL_API_KEY` | scrape/crawl | Optional | worker | **SECRET** | starts with `fc-` |
| `FIRECRAWL_MAX_CREDITS_MONTHLY` | unit cap | Optional (`950`) | worker | CONFIG | int |
| `FIRECRAWL_MAX_PAGES_PER_PROSPECT` | default crawl limit | Optional (`5`) | worker | CONFIG | int 1–20 |

## 6. Claude API

Where to get it: platform.claude.com → API Keys. Use a dedicated workspace and set a workspace spend limit.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `ANTHROPIC_API_KEY` | Claude calls | Required for command parsing, building and outreach | api, worker, voice | **SECRET** | starts with `sk-ant-` |
| `CLAUDE_MODEL_REASONING` | generation/strategy model | Optional (`claude-opus-5-5`) | worker | CONFIG | must be in the allowed list |
| `CLAUDE_MODEL_FAST` | parsing/extraction/classification | Optional (`claude-haiku-5-5`) | api, worker | CONFIG | allowed list |
| `CLAUDE_MODEL_OUTREACH` | outreach copy | Optional (`claude-opus-5-5`) | worker | CONFIG | allowed list |
| `CLAUDE_MAX_TOKENS_GENERATION` | per-call output cap | Optional (`64000`) | worker | CONFIG | int ≤ 128000 |
| `SITE_MAX_COST_MICROS` | per-site spend cap | Optional (`4000000`) | worker | CONFIG | int |

## 7. Groq (deferred, not used in v1)

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `GROQ_API_KEY` | reserved for a future cheap-classification adapter | Optional | worker | **SECRET** | starts with `gsk_` |

## 8. Cloudflare

Where to get them: Cloudflare Dashboard → My Profile → API Tokens → Create Token. Use a custom token with **Account → Cloudflare Pages → Edit**, limited to one account. The account ID is on the account overview page.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | Pages deployments and cleanup | Optional (Deployment shows NOT_CONFIGURED without it) | worker, ci | **SECRET** | verified at startup via `/user/tokens/verify` |
| `CLOUDFLARE_ACCOUNT_ID` | account | Required with token | worker, ci | SENSITIVE | 32 hex |
| `CLOUDFLARE_PAGES_PREVIEW_PROJECT` | preview project name | Optional (`jarvis-previews`) | worker | CONFIG | slug |
| `PREVIEW_TTL_DAYS` | preview expiry | Optional (`30`) | worker | CONFIG | int 1–90 |
| `WEBHOOK_RELAY_SHARED_SECRET` | HMAC between the Worker relay and the API (if used) | Optional | api, webhook-relay | **SECRET** | ≥ 32 bytes |

## 9. Email (blocked until a sending domain is approved)

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `EMAIL_PROVIDER` | `resend` \| `ses` | Optional | worker, api | CONFIG | enum |
| `RESEND_API_KEY` | send API | Optional | worker | **SECRET** | starts with `re_` |
| `RESEND_WEBHOOK_SECRET` | Svix signature verification | Optional | api | **SECRET** | starts with `whsec_` |
| `AWS_SES_ACCESS_KEY_ID` / `AWS_SES_SECRET_ACCESS_KEY` / `AWS_SES_REGION` | SES alternative | Optional | worker | **SECRET** / SECRET / CONFIG | IAM user limited to `ses:SendEmail` |
| `EMAIL_FROM_ADDRESS` | sender | Required to send | worker | SENSITIVE | must be on `EMAIL_SENDING_DOMAIN`. Rejected if the domain is `pages.dev`, `workers.dev` or free mail |
| `EMAIL_SENDING_DOMAIN` | verified domain | Required to send | worker | SENSITIVE | SPF/DKIM/DMARC checked via DNS at startup |
| `EMAIL_REPLY_TO` | monitored inbox | Required to send | worker | SENSITIVE | email |
| `UNSUBSCRIBE_SIGNING_KEY` | signs unsubscribe tokens | Required to send | api, worker | **SECRET** | ≥ 32 bytes |

## 10. LiveKit (Phase 7)

Where to get them: LiveKit Cloud → Project → Settings → Keys.

| Variable | Purpose | Req | Consumers | Class | Validation |
|---|---|---|---|---|---|
| `LIVEKIT_URL` | project WebSocket URL | Optional | api, voice | SENSITIVE | `wss://…livekit.cloud` |
| `LIVEKIT_API_KEY` | token minting | Optional | api, voice | **SECRET** | starts with `API` |
| `LIVEKIT_API_SECRET` | token minting | Optional | api, voice | **SECRET** | non-empty |
| `VOICE_STT_PROVIDER` / `VOICE_TTS_PROVIDER` | STT/TTS choice (LiveKit Inference by default) | Optional | voice | CONFIG | enum |

## 11. Budgets & limits (global defaults; can be overridden in `system_settings`)

| Variable | Default | Purpose |
|---|---|---|
| `BUDGET_DAILY_CAP_USD` | `5` | global daily spend cap |
| `BUDGET_MONTHLY_CAP_USD` | `50` | global monthly spend cap |
| `BUDGET_CAMPAIGN_DEFAULT_USD` | `10` | default per-campaign cap |
| `MAX_CONCURRENT_BUILDS` | `1` | sandbox concurrency |
| `OUTREACH_DAILY_LIMIT_DEFAULT` | `20` | per outreach campaign |
| `PRICING_OVERRIDES_JSON` | unset | optional unit-price overrides for the cost model |

## 12. CI only (GitHub Actions secrets)

`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (dashboard deploy), `SUPABASE_ACCESS_TOKEN` and
`SUPABASE_DB_PASSWORD` (migrations, **SECRET**). Integration tests against real providers run
only through a manual workflow with low caps, never on every push.

## 13. Rotation

| Secret | Rotate when | How |
|---|---|---|
| Provider API keys | suspected leak, staff change, every 6 months | create the new key → update env → restart → revoke the old one |
| `SUPABASE_SERVICE_ROLE_KEY` / DB password | suspected leak | Supabase dashboard → roll the key |
| `JARVIS_ENCRYPTION_KEY` | yearly | dual-key period: decrypt with old, re-encrypt with new (script in Phase 9) |
| Signing secrets | suspected leak | rotate. Outstanding unsubscribe links stay valid through the key-ID (`kid`) mechanism |
