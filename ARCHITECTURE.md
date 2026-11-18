# JARVIS — System Architecture

Status: **Proposed — awaiting architecture review approval.** No application code exists yet.
Last updated: 2026-11-18

Related documents:
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) ·
[ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) ·
[docs/API_COST_MODEL.md](docs/API_COST_MODEL.md) ·
[docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) ·
[docs/SECURITY_AND_COMPLIANCE.md](docs/SECURITY_AND_COMPLIANCE.md) ·
[docs/SYSTEM_DIAGRAM.md](docs/SYSTEM_DIAGRAM.md)

---

## 1. System Overview

JARVIS is a single-operator, cloud-hosted autonomous agent that runs a website-agency
acquisition pipeline:

```
command → discover → verify website → research → qualify → build site → QA → preview deploy → outreach → report
```

It has one conversational surface (text first, voice later) and one orchestration core.
All work runs as **durable tasks in PostgreSQL**, executed by a **Python worker** that does not
depend on the browser or the user's laptop.

### 1.1 Design principles

| Principle | What it means in practice |
|---|---|
| Deterministic core, AI at the edges | State machines, scoring, budgets, policies and routing are plain Python. LLMs are called for language understanding, research summarisation, site strategy/code, and outreach copy, never for deciding whether something is permitted. |
| Durable by default | Every unit of work is an `agent_tasks` row with a lease, attempt counter, idempotency key and event log. A worker crash loses at most one in-flight step, which is then retried. |
| Evidence over assertion | Every fact about a business carries source, method, timestamp and confidence. Generated websites may only use facts in `verified` status. |
| Fail loudly | Integrations return a typed status: `OK`, `NOT_CONFIGURED`, `ERROR`, `QUOTA_EXCEEDED`, `RATE_LIMITED`, `BUDGET_BLOCKED`. There is no mock fallback in production. |
| Bounded autonomy | Each campaign has explicit authorisation: max prospects, max sites, max spend, allowed actions, outreach limits. The policy engine checks these before every billable or outward-facing action. |
| Cheapest reliable path | Use free tiers first, call paid APIs only when a cheaper signal can't answer the question (e.g. no SERP call if the Maps website field already resolves to a verified site). |

### 1.2 Key architectural decisions (and departures from the brief)

These are the places where I recommend something different from, or more specific than, the
directive. Each one needs your sign-off (see §14).

1. **PostgreSQL is the job queue; Cloudflare Queues is not needed for v1.**
   A `SELECT … FOR UPDATE SKIP LOCKED` queue on `agent_tasks` gives durability, visibility,
   cancellation and transactional state changes in one place. Cloudflare Queues would add a
   second source of truth with no benefit at our volume (hundreds of tasks/day). It can be added
   later for fan-out from Workers webhooks.
2. **The Python API + worker run on one small always-on container host, not Cloudflare Workers.**
   Playwright, `npm run build` for generated sites, and LangGraph runs need a real Linux
   container with Chromium and Node. Workers cannot do that. Recommended: a single small VPS
   (≈$5–8/month) running Docker Compose. Alternatives are compared in §8.3.
3. **Cloudflare is used for what it is good at:** Pages for the dashboard and for preview sites,
   and optionally a Worker for signed inbound webhooks (email replies/bounces) that forwards to the API.
4. **LangGraph only where the flow is genuinely graph-shaped.** The campaign pipeline is a
   deterministic state machine in our own code. LangGraph (with a Postgres checkpointer) is used
   inside the **Website Builder ↔ QA repair loop**, where a strategy → generate → build → test →
   repair cycle with bounded iterations and checkpointing is exactly what it's built for.
5. **Groq is deferred.** Claude Haiku 5.5 costs $0.10 / $0.50 per million tokens, which makes
   the saving from Groq on classification negligible at our volume, while adding another key,
   another failure mode and another model to evaluate. The `LLMProvider` interface keeps Groq a
   one-adapter addition if volume ever justifies it.
6. **Voice is Phase 7.** Voice and text share one command pipeline, so voice is an input adapter
   on top of a working system. Building it first would mean demoing an interface with nothing behind it.
7. **One Cloudflare Pages project for all previews, one branch per site.** Each site deploys to
   `https://<site-slug>.<project>.pages.dev` (branch alias). This avoids per-account project limits
   and makes cleanup a single API call per deployment.
8. **Generated sites are static Astro + Tailwind builds** from a constrained scaffold. Claude
   writes a structured site spec and then the page/component source within that scaffold. Static
   output is fastest, most SEO-friendly, cheapest to host and safest to build.
9. **Outreach cannot be compliant without a sending domain.** The brief forbids buying a domain
   *and* forbids using pages.dev for email. Outreach therefore needs either an existing agency
   domain you control, or approval to buy one (~$10–12/yr). Until then, the Outreach agent can
   draft but not send. This is the most important open decision.
10. **Google Places data is treated as a short-lived, non-owned cache.** We keep `place_id`
    permanently and refresh Places fields on demand. Facts we independently verify (from the
    business's own website or other sources) are stored separately with their own provenance.
    See [SECURITY_AND_COMPLIANCE.md §1](docs/SECURITY_AND_COMPLIANCE.md).

---

## 2. Component Architecture

```
┌─────────────────────────── Cloudflare ───────────────────────────┐
│  Pages: dashboard (React SPA)        Pages: jarvis-previews       │
│  Worker (optional): webhook-relay    (one branch per generated    │
│                                       site → *.pages.dev)         │
└──────────────┬───────────────────────────────▲───────────────────┘
               │ HTTPS (JWT)                   │ Direct Upload API
┌──────────────▼───────────────────────────────┴───────────────────┐
│  Container host (Docker Compose)                                   │
│  ┌──────────────┐  ┌────────────────────┐  ┌───────────────────┐  │
│  │ api          │  │ worker (N=1..2)    │  │ build-sandbox     │  │
│  │ FastAPI      │  │ task runner,       │  │ ephemeral, no-net │  │
│  │ REST + SSE   │  │ agents, scheduler  │  │ Node + Astro +    │  │
│  │ auth, policy │  │ LangGraph          │  │ Playwright        │  │
│  └──────┬───────┘  └─────────┬──────────┘  └─────────▲─────────┘  │
│         │                    │  spawns (docker run)  │            │
│         │                    └───────────────────────┘            │
│  ┌──────────────┐  (Phase 7)                                       │
│  │ voice agent  │  LiveKit Agents worker                           │
│  └──────────────┘                                                  │
└─────────┬──────────────────────┬───────────────────────────────────┘
          │ SQL (pooled)         │ HTTPS
┌─────────▼────────┐   ┌─────────▼──────────────────────────────────┐
│ Supabase         │   │ External APIs                               │
│ Postgres + Auth  │   │ Google Places (New) · SerpApi · Firecrawl   │
│ + Storage + RLS  │   │ Claude API · LiveKit · Cloudflare API       │
└──────────────────┘   │ Email provider (Resend/SES, needs domain)   │
                       └─────────────────────────────────────────────┘
```

### 2.1 Components

| Component | Tech | Responsibility | Deploys to |
|---|---|---|---|
| `apps/dashboard` | React, Vite, TS, Tailwind, Framer Motion, TanStack Query, React Router, Zod | Operator UI. Reads/writes only through the API. Live progress via SSE. Uses Supabase Auth for login only. | Cloudflare Pages (`jarvis-dashboard.pages.dev`) |
| `apps/api` | FastAPI, Pydantic v2, SQLAlchemy 2 (async), asyncpg | Auth verification (Supabase JWT), REST endpoints, SSE stream of `task_events`, command intake, policy checks for user-initiated actions, webhook receivers. Stateless. | Container host |
| `apps/worker` | Python 3.12, same core package | Claims tasks from `agent_tasks`, runs agent handlers, writes events, enforces budgets, runs the scheduler (follow-ups, preview expiry, retention purges), lease recovery. | Container host |
| `build-sandbox` image | Node 22, Astro, Tailwind, Playwright + Chromium | Runs `npm run build` and Playwright QA for one site, with no network, as non-root, with CPU/memory/time caps. Started per job by the worker. | Container host (ephemeral) |
| `apps/voice` (Phase 7) | LiveKit Agents (Python) | Joins a LiveKit room, does STT → posts text to the same `/commands` endpoint → speaks responses via TTS. Supports barge-in. | Container host |
| `workers/webhook-relay` (optional) | Cloudflare Worker | Public endpoint for email-provider webhooks. Verifies signature, forwards to API. Lets the API stay off the public internet if desired. | workers.dev |
| `jarvis_core` (Python package) | Shared library | Domain models, state machines, policy engine, agents, integration adapters, scoring, cost tracking. Imported by api, worker, voice. | — |

### 2.2 Proposed repository layout

Improved from the brief: all Python lives in **one installable package** (`packages/jarvis_core`)
so `api`, `worker` and `voice` share code without path hacks, and so the agent/integration
boundaries are enforced by import rules rather than directory conventions.

```
jarvis/
├── apps/
│   ├── dashboard/            React SPA
│   ├── api/                  FastAPI app (thin: routers, auth, SSE)
│   ├── worker/               worker entrypoint, scheduler
│   └── voice/                LiveKit agent (Phase 7)
├── packages/
│   └── jarvis_core/
│       ├── domain/           Pydantic models, enums, state machines
│       ├── policy/           authorisation, budgets, kill switches, limits
│       ├── orchestrator/     task runner, campaign planner, retries, leases
│       ├── agents/
│       │   ├── command/  discovery/  verification/  research/
│       │   ├── qualification/  website_builder/  quality_assurance/
│       │   └── deployment/  outreach/
│       ├── integrations/
│       │   ├── google_places/  serp_api/  firecrawl/  claude/
│       │   ├── livekit/  cloudflare/  email/  http_probe/
│       │   └── base.py       Adapter protocol, ProviderStatus, errors, usage hooks
│       ├── workflows/        LangGraph graphs (site build/repair)
│       ├── db/               SQLAlchemy models, repositories, queue
│       ├── security/         SSRF guard, secrets, signing, sandbox runner
│       └── observability/    structured logging, cost ledger, metrics
├── site-scaffold/            Astro + Tailwind starter + component library for generated sites
├── database/
│   ├── migrations/           Supabase CLI SQL migrations (source of truth)
│   └── policies/             RLS policies (included by migrations)
├── docker/                   Dockerfiles: api, worker, build-sandbox, voice
├── tests/                    unit / integration / e2e (pytest, vitest, playwright)
├── docs/
├── .env.example
├── ARCHITECTURE.md  IMPLEMENTATION_PLAN.md  ENVIRONMENT_VARIABLES.md  README.md
```

---

## 3. Data Flow

### 3.1 Command to campaign

1. User types (or speaks) "Find 100 plumbing businesses in Houston that don't have websites."
2. `POST /commands` stores the raw command, then the **Command agent** (Claude Haiku 5.5,
   structured output) returns a validated `CommandIntent`:
   `{intent: "create_campaign", niche: "plumbing", location: {city: "Houston", state: "TX"}, target_count: 100, filters: {website_status: ["none_found","broken"]}}`.
3. Missing required fields produce a clarification question, not a guess.
4. The **Policy agent** (deterministic) computes an execution plan and a cost estimate from
   [API_COST_MODEL.md](docs/API_COST_MODEL.md) unit costs, and returns a confirmation card:
   "This will make ~10 Places searches and ~100 Place Details calls (within free tier), up to 250
   SERP searches, est. $0.00–$6.50. Authorise?"
5. On confirmation, the campaign moves `draft → authorized → queued` and a root task is enqueued.

### 3.2 Pipeline (per campaign)

| Stage | Agent | Input | Output | Paid calls |
|---|---|---|---|---|
| Discover | Discovery | niche, geo, count | `prospects` (place_id + transient Places cache) | Places Text Search (Pro), Place Details (Enterprise) |
| Verify | Verification | prospect | `website_audits` with status + evidence + confidence | SerpApi (only when needed), HTTP/DNS probes (free) |
| Research | Research | top-N prospects | `research_facts` (verified / inferred / unknown / conflicting) | SerpApi, Firecrawl, Claude Haiku 5.5 for extraction |
| Qualify | Qualification | facts + audits | `lead_scores` with per-rule explanation | none (deterministic) |
| Build | Website Builder | verified facts, brand cues | `generated_sites`, `site_versions` (source in Supabase Storage) | Claude Opus 5.5 |
| QA | Quality Assurance | site version | `site_builds`, `qa_reports` | none (sandbox), Claude for repair |
| Deploy | Deployment | passed build | `deployments` with pages.dev URL, verified live | Cloudflare (free) |
| Outreach | Outreach | prospect, site, observations | `outreach_messages` drafts → sends | Claude for copy, email provider |

Each stage is a separate task type, so a campaign can be paused between any two stages, and
the user can run "Research the top 20 prospects" as a standalone command on an existing campaign.

### 3.3 Website verification decision logic

```
Maps websiteUri present? ──yes──▶ probe URL (DNS, HTTP, redirects, TLS, canonical)
        │                              │
        no                       reachable & identity match ≥ threshold?
        │                         yes ─▶ FUNCTIONING (then quality audit → OUTDATED?)
        ▼                         no  ─▶ BROKEN (if DNS/HTTP fail) or go to SERP
SERP: "<name>" "<city>" + "<name>" <phone>  (max 2 queries)
        │
  candidate domains (filtered: no directories/social/aggregators)
        │
  probe + identity match (name, phone, address on page; Firecrawl single-page scrape if needed)
        │
  match ─▶ FUNCTIONING / OUTDATED / BROKEN
  none  ─▶ NONE_FOUND (confidence from number of checks done)
  ambiguous ─▶ UNVERIFIED (flagged for human review)
```

Identity matching is deterministic: normalised business name similarity, phone number match
(E.164), street/ZIP match, and city match each contribute to a score. "No website" is only
asserted after both the Maps field and SERP checks come back empty, and it carries a confidence
value, never a bare boolean.

---

## 4. Agent Architecture

All agents implement one interface:

```python
class Agent(Protocol):
    role: AgentRole
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_tools: frozenset[ToolName]       # enforced by the ToolGateway
    max_attempts: int
    timeout_s: int
    async def run(self, ctx: TaskContext, inp: BaseModel) -> AgentResult: ...
```

`TaskContext` exposes only the integrations listed in `allowed_tools`, each wrapped by the
**ToolGateway**, which: checks kill switches → checks campaign authorisation → reserves budget
→ calls the adapter → records `api_usage` and the actual cost → releases or commits the budget
reservation → emits a `task_event`. Agents cannot call providers directly.

| # | Agent | Inputs | Outputs | Allowed tools | Retries | Key boundary |
|---|---|---|---|---|---|---|
| 1 | Command | raw text, conversation context | `CommandIntent` or `ClarificationNeeded` | Claude (Haiku 5.5) | 2 | Produces intents only, never executes. |
| 2 | Orchestrator & Policy | intents, task results | task graph, state transitions, approvals | DB only | n/a | Only component that changes campaign state. Deterministic. |
| 3 | Discovery | niche, geo, limits | prospects (deduped by place_id + phone + normalised name/address) | Google Places | 3 (backoff) | Max pages/queries per campaign; never scrapes Maps. |
| 4 | Verification | prospect | `website_audits` | SerpApi, HTTP probe, DNS, Firecrawl (1 page) | 3 | SSRF guard on every probe; max 2 SERP queries/prospect by default. |
| 5 | Research | prospect + audit | `research_facts`, `prospect_contacts` | SerpApi, Firecrawl, Claude (Haiku 5.5) | 2 | Crawl budget per prospect (default 5 pages); LLM extracts with citations to source text; unsupported claims dropped. |
| 6 | Qualification | facts, audits | `lead_scores` | none | 0 | Pure function, versioned rule set. |
| 7 | Website Builder | verified facts, brand cues, scaffold | site spec + source | Claude (Opus 5.5), Storage | 2 per stage | Can only use `verified` facts; outputs into scaffold paths only. |
| 8 | QA | site version | `site_builds`, `qa_reports`, repair requests | Build sandbox, Playwright | max 3 repair cycles | Build runs in no-network sandbox; after 3 failed repairs → `requires_human_review`. |
| 9 | Deployment | passed build artefact | `deployments` | Cloudflare Pages API, HTTP probe | 3 | Preview project only; live-domain deploy is not a capability of this agent. |
| 10 | Outreach | prospect, site, observations, outreach campaign | drafts, sends, reply classifications | Claude (Sonnet/Opus), Email provider | 2 | Send only inside an authorised outreach campaign; suppression check in the same transaction as the send. |

Supporting infrastructure, not agents: cost ledger, logging, scheduler, retention purger, health checks.

### 4.1 Model routing

| Use | Model | Why |
|---|---|---|
| Command parsing, reply classification, fact extraction | `claude-haiku-5-5` | Structured output, very cheap ($0.10/$0.50 per MTok). |
| Website strategy, site code generation, repair | `claude-opus-5-5` | Highest-quality code and design reasoning; this is the product's core value. Effort `high` for generation, `medium` for repair. |
| Outreach copy | `claude-opus-5-5` (default) — `claude-sonnet-5-5` is the cost lever | Short outputs, quality matters for reply rate. |

Cost levers you can approve: Sonnet 5.5 for generation (~50% cheaper), Message Batches for
non-urgent generation (50% off), prompt caching for the scaffold/system prompt (cache reads at
$0.20/MTok on Opus 5.5). All Claude calls set `max_tokens` and a per-task token budget; the
policy engine rejects a call whose worst-case cost would exceed the remaining campaign budget.

---

## 5. API Integrations

Every integration lives in `integrations/<name>/` and implements:

```python
class ProviderStatus(StrEnum): OK, NOT_CONFIGURED, ERROR, QUOTA_EXCEEDED, RATE_LIMITED, BUDGET_BLOCKED, DISABLED

class Adapter(Protocol):
    name: str
    def configured(self) -> bool                    # config validated at startup
    async def health(self) -> ProviderHealth        # cheap, cached 5 min
    # typed methods, e.g. GooglePlaces.text_search(req) -> TextSearchResult
```

Common behaviour (in `integrations/base.py`): `httpx.AsyncClient` with explicit timeouts,
retry with exponential backoff + jitter on 429/5xx/network (max 3, respects `Retry-After`),
error mapping to typed exceptions, request/response metadata logging (never secrets or full
bodies containing personal data), and a usage hook that writes `api_usage`.

| Integration | Endpoints used | Notes |
|---|---|---|
| Google Places (New) | `POST places:searchText`, `GET places/{id}` | Always send `X-Goog-FieldMask`; the most expensive requested field sets the SKU. Discovery uses a Pro-tier mask; Details uses an explicit Enterprise mask only for prospects that survive dedupe. |
| SerpApi | `GET /search?engine=google` | `location`, `gl=us`, `hl=en`, `num=10`. Store query + params + top results. `no_cache=false` (cached repeats are free on SerpApi). |
| Firecrawl | `POST /v2/scrape`, `POST /v2/crawl` (with `limit`), `GET /v2/crawl/{id}` | Scrape for single pages; crawl only for verified official sites, `limit` ≤ budget. Our own robots.txt check before crawling. |
| Claude | `POST /v1/messages` (streaming), `/v1/messages/batches` | Official `anthropic` Python SDK. Structured outputs for intents/facts. Usage → cost ledger. |
| Cloudflare | Pages Direct Upload (via `wrangler pages deploy` in sandbox or the API), deployments list/delete | Scoped API token: Pages edit on one account only. |
| Email (blocked on domain) | Resend or Amazon SES | Signed webhooks for bounces/complaints/replies. |
| LiveKit (Phase 7) | Server API for room tokens; Agents SDK | Tokens minted by API, short TTL. |

---

## 6. Database Design (summary)

Full detail in [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md). Highlights:

- ~30 tables, UUID PKs, `created_at`/`updated_at` everywhere, soft delete only on user-facing
  entities (campaigns, prospects, generated_sites).
- `agent_tasks` is both the queue and the execution record; `task_events` is append-only.
- Provenance: `prospect_sources` + `research_facts.source_*` columns + `retention_policy` enum on
  every externally sourced field. Google Places content lives in `places_cache` with an
  `expires_at`, separate from independently verified data.
- Money: `budget_ledger` uses integer **micro-dollars**; reservations and commits are separate
  rows so spend is never double-counted or lost on crash.
- RLS enabled on every table; the dashboard never queries tables directly in v1, but RLS is
  defence in depth.

---

## 7. State Machines

Implemented in `domain/state_machines.py` as explicit transition tables. `transition(entity,
to_state, actor, reason)` validates against the table, writes the new state and an
`audit_events` row in the same transaction, and raises `InvalidTransition` otherwise.

### 7.1 Campaign

```
draft → authorized → queued → discovering → verifying → researching → qualifying
      → building → qa → preview_deployed → outreach_authorized → completed
```
From any active state: `paused` (resume returns to the stored `resume_state`), `cancelled`
(terminal), `failed` (terminal unless retried by user), `requires_human_review` (resumes to the
stored state after the user acts). Stages can be skipped if the campaign scope excludes them
(e.g. a discovery-only campaign goes `qualifying → completed`).

### 7.2 Prospect
```
discovered → identity_verified → website_verified → researched → qualified
                     └──────────────┴─────────────────┴──────────→ rejected
```

### 7.3 Website
```
queued → strategy → generating → building → qa → passed → deployed
                                    ▲         │
                                    └─ repair ┘   (max 3 cycles → requires_human_review)
```
plus `failed`, `expired` (preview cleaned up), `archived`.

### 7.4 Outreach message
```
draft → authorized → queued → sent → replied → follow_up → closed
terminal side states: suppressed, bounced, opted_out, failed
```

### 7.5 Task (internal)
```
pending → leased → running → succeeded
                    ├→ retry_wait → pending      (attempts < max_attempts)
                    ├→ failed                    (attempts exhausted / non-retryable)
                    ├→ cancelled                 (user / kill switch)
                    └→ blocked                   (budget, quota, NOT_CONFIGURED, needs approval)
```

---

## 8. Deployment Topology

### 8.1 Environments

| Env | Purpose | Data | External APIs |
|---|---|---|---|
| `local` | development | local Supabase (CLI, Docker) | real keys with low caps, or test doubles **only** when `JARVIS_ENV=test` |
| `test` | CI | ephemeral Postgres | test doubles (explicit, never in prod build) + contract tests against recorded fixtures |
| `production` | live | Supabase Free project | real |

The application refuses to start in `production` if any test-double flag is set.

### 8.2 Production

- **Dashboard:** Cloudflare Pages (free), built from GitHub on push to `main`.
- **API + worker (+ voice later):** Docker Compose on one container host, behind Caddy for TLS.
  Public hostname: a free subdomain from the host, or a Cloudflare Tunnel (`*.trycloudflare.com`
  is not stable; a named tunnel requires a domain on Cloudflare — another reason a domain helps).
- **DB/Auth/Storage:** Supabase Free.
- **Previews:** Cloudflare Pages project `jarvis-previews`, Direct Upload, branch per site.
- **CI:** GitHub Actions (free for public repos; 2,000 min/month on private).

### 8.3 Container host options

| Option | Cost | Pros | Cons | Verdict |
|---|---|---|---|---|
| Small VPS (e.g. Hetzner, 2 vCPU/4 GB) | ≈$5–8/mo | Always on, Docker-in-Docker sandbox possible, predictable | You own patching | **Recommended** |
| Google Cloud Run (service + jobs) | Free tier, then usage | Scales to zero, managed | Background worker needs min-instances (cost) or jobs; nested sandbox harder | Good alternative |
| Render / Railway | $5–7+/mo | Simple | Free tiers sleep; sandboxing limited | Not preferred |
| Cloudflare Workers / Containers | varies | Same vendor | Python + Chromium + Node builds are a poor fit for Workers | Not for the worker |

---

## 9. Security Boundaries

Full detail in [docs/SECURITY_AND_COMPLIANCE.md](docs/SECURITY_AND_COMPLIANCE.md).

| Boundary | Control |
|---|---|
| Browser ↔ API | Supabase JWT (verified against project JWKS), CORS locked to dashboard origin, rate limiting, Zod (client) + Pydantic (server) validation. No secrets in the SPA bundle. |
| API/worker ↔ providers | Keys only in server env (Docker secrets / env file with 600 perms), never logged, scoped tokens (Cloudflare token limited to Pages on one account). |
| Worker ↔ internet (probes) | SSRF guard: resolve DNS, reject private/loopback/link-local/metadata IPs, re-check after redirects, http/https only, ports 80/443, size and time limits. |
| Worker ↔ generated code | Code never runs in the worker. Build/QA in a throwaway container: `--network none`, non-root, read-only root FS, tmpfs workdir, CPU/mem/pids limits, hard timeout, pre-installed dependencies (no `npm install` of model-chosen packages). |
| Agent ↔ tools | ToolGateway allow-list per agent role. |
| System ↔ outside world | Policy engine: kill switches, authorisation scope, budgets, daily limits, suppression list, human-approval gates. |

---

## 10. Failure Recovery

| Failure | Behaviour |
|---|---|
| Worker crash mid-task | Lease expires (default 5 min, renewed by heartbeat every 60 s); task returns to `pending`, attempt counter incremented. Handlers are idempotent per step via `idempotency_key`. |
| Provider 429 / 5xx | Retry with backoff up to adapter limit, then task `retry_wait` with `run_after`; after `max_attempts` → `failed` with mapped error; campaign continues for other prospects. |
| Quota exhausted | Adapter returns `QUOTA_EXCEEDED`; task → `blocked`; campaign shows the blocking provider; no further calls to that provider until reset or user action. |
| Budget exceeded | Reservation fails before the call; task `blocked (BUDGET_BLOCKED)`; campaign → `requires_human_review`. |
| Claude refusal / truncated output | `stop_reason` checked; refusal → logged, task failed with reason (fallback model configured where supported); `max_tokens` → one retry with continuation, then fail. |
| Build/QA keeps failing | 3 repair cycles max → `requires_human_review` with the last QA report attached. |
| Deployment URL not live | Verify up to 5 times over 2 min; then `failed`, no outreach can reference it. |
| DB unavailable | API returns 503; worker pauses claiming with backoff. Supabase daily backups (Free = no PITR) + our weekly `pg_dump` to Storage/off-site. |
| Kill switch flipped | Worker stops claiming immediately; running tasks are cancelled at their next checkpoint (ToolGateway refuses all calls). |

---

## 11. Observability

- Structured JSON logs (`structlog`) with `task_id`, `campaign_id`, `agent_role`, `provider`,
  `model`, `duration_ms`, `tokens_in/out`, `cost_usd_micros`, `attempt`, `result`.
- `api_usage` + `budget_ledger` power the cost dashboard; `task_events` powers live progress.
- `/health` (liveness), `/ready` (DB + queue), `/providers/health` (per-adapter status).
- Worker heartbeat table → dashboard "worker health"; queue depth = `count(*) where status in ('pending','retry_wait')`.
- Optional later: OpenTelemetry export, Sentry free tier for exceptions.

---

## 12. Reporting

All dashboard numbers are SQL views over real rows (`v_campaign_funnel`, `v_cost_by_provider`,
`v_daily_usage`). There is no client-side aggregation of invented data and no demo dataset in
production builds.

---

## 13. Scalability Path

v1 targets one operator, ~1–3k prospects and ~100 sites/month. Beyond that: multiple worker
replicas (queue already supports it), Message Batches for generation, a separate build-runner
host, Cloudflare Queues for webhook fan-in, Supabase Pro for PITR and capacity.

---

## 14. Decisions Requiring Approval

1. Container host: small VPS (~$5–8/mo) vs Cloud Run.
2. Sending domain for outreach: use an existing domain you control, or approve buying one.
3. Defer Groq (use Claude Haiku 5.5 for cheap tasks).
4. Default generation model: Opus 5.5 (quality) vs Sonnet 5.5 (≈half the cost).
5. Voice in Phase 7, after the text pipeline works end to end.
6. Google Places data handling approach (transient cache + place_id), and acceptance that this
   needs your own reading of Google's terms (I am not a lawyer).
7. Preview-site policy: `noindex`, "concept preview, not affiliated" banner, no copying of the
   prospect's photos/logo without permission, 30-day expiry.
