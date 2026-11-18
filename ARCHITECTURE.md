# JARVIS — System Architecture

Status: **Proposed, revision 3.** Awaiting architecture approval. No application code exists.
Last updated: 2026-11-18. Full change history: [CHANGELOG.md](CHANGELOG.md).

Related documents:
[docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md) ·
[docs/API_CONTRACTS.md](docs/API_CONTRACTS.md) ·
[docs/VERIFICATION_REGISTER.md](docs/VERIFICATION_REGISTER.md) ·
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) ·
[ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) ·
[docs/API_COST_MODEL.md](docs/API_COST_MODEL.md) ·
[docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) ·
[docs/SECURITY_AND_COMPLIANCE.md](docs/SECURITY_AND_COMPLIANCE.md) ·
[docs/SYSTEM_DIAGRAM.md](docs/SYSTEM_DIAGRAM.md)

### Revision 3 changes
| Area | Change |
|---|---|
| Rating filter | Optional `minimum_google_rating` (0.5 steps, no default) is sent as Text Search `minRating` and re-checked in memory, with an explicit missing-rating policy. Ratings are never persisted. See §3.6 and PR-2 |
| Website availability | New evidence-based states: `WEBSITE_LISTED`, `WEBSITE_WORKING`, `WEBSITE_BROKEN`, `NO_WEBSITE`, `WEBSITE_UNKNOWN`. A missing `websiteUri` never means `NO_WEBSITE`. See §3.5 and PR-3 |
| Compliance gate | Places-based discovery and qualification stay **disabled** until written approval from Google or legal counsel covers the stated use. The discovery provider is replaceable. SerpApi's Maps engines are no longer listed as an alternative, because they return Google Maps content. See §3.7 and PR-4 |
| Field masks and costs | Re-verified: `rating`, `userRatingCount` and `websiteUri` are Enterprise; `displayName` and `businessStatus` are Pro; `attributions` is Essentials (IDs Only). One Text Search Enterprise request returns at most 20 places, and at most 60 per query. The cost model now uses a yield-based estimate. See §5.1 |

### Revision 2 changes
| Area | Change |
|---|---|
| Google Places API | Removed `places_cache`. The only Places values we persist are Place IDs (indefinitely) and, optionally, lat/lng (deleted within 30 days). Places content is used in process memory only. Field masks were rechecked against Google's SKU tables. Discovery now uses one Text Search Enterprise call per 20 places, not one Place Details call per place. Attribution rules are documented. |
| Infrastructure | A VPS is a **required** component and a recurring cost. It hosts FastAPI, the workers, Playwright and the build sandbox. Cloudflare Pages hosts the dashboard and previews. Workers are used only for short request/response endpoints. Cloud Run is no longer an option. |
| AI routing | Haiku 5.5 for classification, extraction and routing. Sonnet 5.5 for strategy, generation, ordinary repair and outreach copy. Opus 5.5 only for escalation. Deterministic Python for scoring and policy. |
| Billing protection | Limits are checked before every billable call, using locked counters. Google Cloud quotas act as a second hard stop, and budget alerts only warn. Estimated usage and provider-reported usage are stored and shown separately. |
| Supabase | Worker activity no longer counts as protection against the inactivity pause. Added DB health monitoring, reconnection, a degraded mode and our own backups. |
| Outreach | A sending-readiness gate: legitimate postal address, authenticated domain, public unsubscribe endpoint and authorisation. Drafts work without any email configuration. |
| Testing | A feature is complete only after it passes a tightly capped live test. Diversity tests use a multi-campaign, cross-niche dataset. |

---

## 1. System Overview

JARVIS is a single-operator, cloud-hosted autonomous agent that runs a website-agency
acquisition pipeline:

```
command → discover → verify website → research → qualify → build site → QA → preview deploy → outreach → report
```

There is one conversational interface (text first, voice in Phase 7) and one orchestration core.
All work runs as **durable tasks in PostgreSQL**. Python workers on a VPS execute them, so they
don't depend on the browser or on the user's laptop.

### 1.1 Design principles

| Principle | In practice |
|---|---|
| Deterministic core, AI at the edges | State machines, scoring, budgets, policy and routing are plain Python. LLMs handle language understanding, extraction, site strategy and code, and outreach copy. They never decide what is permitted. |
| Durable by default | Every unit of work is an `agent_tasks` row with a lease, an attempt counter, an idempotency key and an event log. |
| Evidence over assertion | Every persisted fact has a source, method, timestamp and confidence. Generated websites use only facts with status `verified`. |
| Compliance by construction | Restricted Google Maps Content returned by the Places API has no column to be written into. Sending email is impossible until a readiness checklist passes. |
| Fail loudly | Integrations return a typed status: `OK`, `NOT_CONFIGURED`, `ERROR`, `QUOTA_EXCEEDED`, `RATE_LIMITED`, `BUDGET_BLOCKED`, `DISABLED`. There is no mock fallback in production. |
| Bounded autonomy | Explicit campaign authorisation sets limits on scope, counts, spend and outreach. They are checked before every billable or outward-facing action. |
| Cheapest reliable path | Free tiers first, the cheapest adequate model, and no paid call when a free signal answers the question. |

### 1.2 Key architectural decisions

1. **The VPS is mandatory.** FastAPI, the Python workers, Playwright/Chromium and the
   isolated build sandbox all run on one Linux VPS under Docker Compose. These are long-running
   workloads that need a real container runtime (Chromium, Node builds, LangGraph loops, Docker
   isolation), and Cloudflare Workers can't run them. Budget: **≤ $10/month, recurring**
   (for example a Hetzner CX23-class machine with 2 vCPU and 4 GB, ≈ €6/month including IPv4. Check the current price).
2. **Cloudflare is used for edge and static work only:** Pages for the dashboard and previews.
   Workers (on `workers.dev`) only for short, stateless request handlers: the public unsubscribe
   endpoint and the inbound email webhook relay. These write to the API and run for milliseconds.
3. **PostgreSQL is the job queue.** It uses `SELECT … FOR UPDATE SKIP LOCKED` on `agent_tasks`.
   Cloudflare Queues isn't used in v1.
4. **LangGraph runs only the build → QA → repair loop**, with a Postgres checkpointer. The
   campaign pipeline is our own deterministic state machine.
5. **Google Places content is never persisted.** We store `place_id` and, optionally, lat/lng with
   a 30-day expiry. Everything else (name, address, phone, website, hours, **rating, review
   count**) exists only in process memory during the task that fetched it. Persistent business
   information comes only from independent sources (§3.4). **Not storing it doesn't make the use
   compliant on its own:** Places-based prospect selection is behind a compliance gate (§3.7).
6. **Model routing is by task difficulty** (§4.1). Most Claude spend goes to Sonnet 5.5, and
   Opus 5.5 is an escalation path with its own budget.
7. **Groq is deferred.** Haiku 5.5 at $0.10/$0.50 per MTok makes the saving negligible.
8. **Voice is Phase 7.** It's an input adapter on the same command pipeline.
9. **One Cloudflare Pages project for all previews, one branch per site.** Previews live at `*.pages.dev`.
10. **Generated sites are static Astro + Tailwind builds** from a constrained scaffold, built in a sandbox with no network.
11. **Outreach drafts work with no email configuration. Sending is blocked** until every
    compliance requirement in [SECURITY_AND_COMPLIANCE.md §7](docs/SECURITY_AND_COMPLIANCE.md) is met.
    No domain purchase is assumed. If you later supply or buy one, sending unlocks.

---

## 2. Component Architecture

```
┌──────────────────────────── Cloudflare (free) ─────────────────────────────┐
│ Pages: jarvis-dashboard (React SPA)   Pages: jarvis-previews (branch/site) │
│ Workers (workers.dev, short handlers only): unsubscribe · email-webhooks    │
└───────────────┬─────────────────────────────────▲──────────────▲───────────┘
                │ HTTPS + JWT                     │ Direct Upload │ HMAC-signed forward
┌───────────────▼─────────────────────────────────┴──────────────┴───────────┐
│ VPS (REQUIRED, ≈$7–10/mo) · Docker Compose · Caddy (TLS)                    │
│ ┌───────────┐ ┌──────────────────────┐ ┌────────────────────────────────┐  │
│ │ api       │ │ worker ×1–2          │ │ build-sandbox (ephemeral)      │  │
│ │ FastAPI   │ │ task runner, agents, │ │ --network none, non-root,      │  │
│ │ REST, SSE │ │ scheduler, LangGraph │ │ Node + Astro + Playwright/     │  │
│ │ auth      │ │ DB health monitor    │ │ Chromium, CPU/mem/time caps    │  │
│ └───────────┘ └──────────────────────┘ └────────────────────────────────┘  │
│ ┌───────────┐ ┌──────────────────────┐                                     │
│ │ voice (P7)│ │ backup job (pg_dump) │──► encrypted off-site copy          │
│ └───────────┘ └──────────────────────┘                                     │
└───────┬───────────────────────┬────────────────────────────────────────────┘
        │ Postgres (TLS)        │ HTTPS
┌───────▼─────────┐   ┌─────────▼──────────────────────────────────────────┐
│ Supabase Free   │   │ Google Places (New) · SerpApi · Firecrawl · Claude  │
│ Postgres, Auth, │   │ Cloudflare API · LiveKit (P7) · Email (when ready)  │
│ Storage         │   └─────────────────────────────────────────────────────┘
└─────────────────┘
```

### 2.1 Components

| Component | Tech | Responsibility | Runs on |
|---|---|---|---|
| `apps/dashboard` | React, Vite, TS, Tailwind, Framer Motion, TanStack Query, React Router, Zod | Operator UI. Talks only to the API. Live progress over SSE. Supabase Auth for login. | Cloudflare Pages |
| `apps/api` | FastAPI, Pydantic v2, SQLAlchemy 2 async, asyncpg | JWT verification, REST, SSE, command intake, user-initiated policy checks, health endpoints, receiving forwarded webhooks. | VPS |
| `apps/worker` | Python 3.12 | Claims tasks, runs agents, enforces limits, scheduler (follow-ups, preview expiry, retention purges, provider-usage sync), lease recovery, DB health monitor. | VPS |
| `build-sandbox` | Node 22, Astro, Tailwind, Playwright + Chromium | Builds and QA-tests one site in isolation. | VPS (ephemeral container) |
| `apps/voice` (Phase 7) | LiveKit Agents (Python) | STT → `/commands` → TTS, barge-in. | VPS |
| `workers/edge` | Cloudflare Worker | `GET/POST /u/:token` one-click unsubscribe (verifies signature, forwards to API, queues the event if the API is down) and `POST /webhooks/email` (verifies provider signature, forwards). | workers.dev |
| `jarvis_core` | Python package | Domain models, state machines, policy, limits, agents, adapters, scoring, cost ledger. | — |

**API hostname without a domain:** Caddy obtains a Let's Encrypt certificate for the VPS
provider's assigned reverse-DNS hostname (or an `<ip>.sslip.io` name). When you have a domain,
this moves to `api.<domain>` with no code change (`API_PUBLIC_BASE_URL`).

### 2.2 Repository layout

```
jarvis/
├── apps/        dashboard/  api/  worker/  voice/
├── packages/jarvis_core/
│   ├── domain/          Pydantic models, enums, state machines
│   ├── policy/          authorisation, limits, budget reservations, kill switches, sending readiness
│   ├── orchestrator/    task runner, campaign planner, retries, leases
│   ├── agents/          command/ discovery/ verification/ research/ qualification/
│   │                    website_builder/ quality_assurance/ deployment/ outreach/
│   ├── integrations/    google_places/ serp_api/ firecrawl/ claude/ cloudflare/
│   │                    email/ livekit/ http_probe/ base.py
│   ├── workflows/       LangGraph build/repair graph
│   ├── db/              SQLAlchemy models, repositories, queue, health/reconnect
│   ├── security/        SSRF guard, secret encryption, signing, sandbox runner
│   └── observability/   logging, cost ledger, usage reconciliation
├── workers/edge/        Cloudflare Worker (unsubscribe + webhook relay)
├── site-scaffold/       Astro + Tailwind scaffold and component library for generated sites
├── database/migrations/ database/policies/
├── docker/              api, worker, build-sandbox, voice Dockerfiles; compose files
├── tests/               unit/ integration/ contract/ live/ e2e/ evals/ datasets/
├── docs/
└── .env.example  ARCHITECTURE.md  IMPLEMENTATION_PLAN.md  ENVIRONMENT_VARIABLES.md  README.md
```

All Python is one installable package, so `api`, `worker` and `voice` share code and import
boundaries between modules can be enforced by a lint rule.

---

## 3. Data Flow

### 3.1 Command to campaign

1. "Find 100 plumbers in Houston with at least a 3.5 Google rating and no website."
2. `POST /commands` stores the raw text. The Command agent (Haiku 5.5, structured output) returns
   validated `CampaignParameters` ([API_CONTRACTS §2](docs/API_CONTRACTS.md)):
   `minimum_google_rating=3.5`, `website_filter=[NO_WEBSITE]`. If any field is missing or invalid
   (e.g. a rating of 3.7), it asks a clarifying question instead.
3. The Policy engine (deterministic) checks the **compliance gate** for the discovery provider,
   requires a `missing_rating_policy`, and builds a **worst-case** unit and cost estimate from the
   yield model ([API_COST_MODEL §2](docs/API_COST_MODEL.md)): "≤ 80 Text Search Enterprise requests,
   ≤ 380 SERP searches (more than the free 250: blocked unless you raise the cap), ≤ 200 Firecrawl
   credits, ≤ $0.60 Claude."
4. Once you confirm, the campaign moves `draft → authorized → queued`. If the gate is closed, the
   result is `COMPLIANCE_BLOCKED` and the campaign stays `draft`.

### 3.2 Pipeline

| Stage | Agent | Persists | Billable calls |
|---|---|---|---|
| Discover + first-pass verify | Discovery → Verification (same task, per result page) | `prospects` (place_id only), `website_audits`, independent `prospect_sources` | Places Text Search (Enterprise mask, gated), SerpApi, probes (free) |
| Research | Research | `research_facts`, `prospect_contacts` (independent sources only) | SerpApi, Firecrawl, Haiku 5.5 |
| Qualify | Qualification | `lead_scores` | none |
| Build | Website Builder | `generated_sites`, `site_versions` | Sonnet 5.5 (Opus 5.5 on escalation) |
| QA / repair | QA | `site_builds`, `qa_reports` | Haiku 5.5 (fact check), Sonnet/Opus (repair) |
| Deploy | Deployment | `deployments` | Cloudflare (free) |
| Outreach | Outreach | drafts, sends, replies | Sonnet 5.5, email provider (when ready) |

### 3.3 Why discovery and first-pass verification run in the same task

Places content can't be stored, so we can't hold names, phones, websites or ratings in the DB
between tasks. Each **discovery page task** therefore does the following:
1. Checks the compliance gate. Calls Text Search once with the Enterprise mask (and `minRating`
   when set and the policy is `exclude`). Gets **up to** 20 places. Pages may hold fewer, and a query
   stops after 60 results. The Places data lives only in memory.
2. For each place: dedupes by `place_id`; skips anything not `OPERATIONAL`; applies the in-memory
   rating check and missing-rating policy (§3.6); applies the website pre-filter (e.g. for a
   `NO_WEBSITE` campaign, a listed `websiteUri` is probed first and the place is dropped if it's
   `WEBSITE_WORKING`); then runs website verification (§3.5).
3. Persists only the `place_id`, the independently obtained evidence (probe results, SerpApi
   results, pages fetched from the business's own site) and the audit outcome.
4. Discards the Places data when the task ends. The task payload and result contain no Places content.

If the task crashes, the retry re-issues the same Text Search call (one extra Enterprise
request). We accept that cost in exchange for compliance.

### 3.4 Independently sourced business information

Persisted business details must come from a non-Google-Maps source:

| Field | Acceptable independent sources | `verified` when |
|---|---|---|
| Business name | Official website, SerpApi result page fetched by Firecrawl, state licence register, operator confirmation | the same name, normalised, appears in ≥ 1 fetched non-Maps page (with a quote), or the operator confirms it |
| Phone | Official website, directory or social page fetched, operator | the exact E.164 match appears in a fetched non-Maps page |
| Website URL | Our HTTP probe of the site plus identity match on the page content | the probe succeeds and the identity score ≥ threshold |
| Services, service area, brand | Official website, social pages | quoted from the fetched text |

We can use the Places name and phone in memory to *search* for independent sources, but they are
never written to the DB. A prospect with no independently confirmed name and contact detail can't
reach `qualified`. It goes to `requires_human_review`, where the operator can confirm it
(`source_type = manual`). This is the main product consequence of the Places terms. It costs
roughly 1–2 extra SERP searches and Firecrawl pages per no-website prospect, and both are budgeted.

### 3.5 Website availability: evidence-based states

States (defined in PR-3): `WEBSITE_LISTED`, `WEBSITE_WORKING`, `WEBSITE_BROKEN`, `NO_WEBSITE`,
`WEBSITE_UNKNOWN`. Quality (`modern`, `outdated`, `unassessed`) is a separate attribute of
`WEBSITE_WORKING`.

```
Listing sources: Places websiteUri (in memory only) · directory/social profile links · SERP results
   any listed URL → state WEBSITE_LISTED (persisted only if an independent source lists it)
      → probe (DNS, HTTP, redirects, TLS, parked-page detection, canonical) → identity match
         responds + identity match   → WEBSITE_WORKING (+ quality audit)
         NXDOMAIN / connect / TLS / 4xx-5xx at root / parked / expired
                                     → second probe ≥ 10 min later → still failing → WEBSITE_BROKEN(reason)
         responds, identity mismatch → listed URL not this business → continue independent search
   no listed URL anywhere (a missing Places websiteUri is a hint only, never a conclusion):
      required independent checks (PR-3.3):
         1. SERP "<name>" <city> <state>       2. SERP "<phone>"
         3. up to 3 independent profiles checked for a website link
         4. DNS of up to 3 candidate domains derived from the name
      all completed, no site found, confidence ≥ NO_WEBSITE_MIN_CONFIDENCE → NO_WEBSITE
      any check failed / skipped by quota or error                        → WEBSITE_UNKNOWN(verification_incomplete)
      completed but weak or conflicting                                    → WEBSITE_UNKNOWN(insufficient_evidence | conflicting_evidence)
```

Identity matching is deterministic: name similarity, phone match, ZIP/street match and city
match (service-area businesses have no address, so they rely on name and phone). Availability is
never a bare boolean. It's a state with checks, evidence and confidence. `WEBSITE_UNKNOWN` never
satisfies a campaign's website filter, and outreach can't claim "no website" for it.

### 3.6 Dynamic Google rating filter

| Step | Behaviour |
|---|---|
| Parameter | `minimum_google_rating`: optional, **no default**, 0.5–5.0 in 0.5 steps, validated without rounding |
| Request | Policy `exclude`: send `minRating = minimum`. Policy `include_for_review`: omit `minRating` (Google's handling of unrated places is undocumented) and filter in memory. The mask always includes `places.rating` when a minimum is set (no SKU change: the mask is already Enterprise) |
| In-memory check | Every returned place: `rating ≥ minimum` → pass. `rating` absent → apply the policy (`exclude` drops it; `include_for_review` keeps it, flagged `rating_unknown` for review). Below the minimum → drop |
| Persistence | **None.** No rating, review count, pass/fail flag, rating band or derived score is written to any table, log, task payload or metric label. Campaign membership is the only trace, and the parameter is the operator's own input |
| Scoring | Lead scoring doesn't use Google ratings or reviews |
| Display | Only in the live `<GooglePlacesListingPanel>` (Place Details Enterprise call, attributed, `no-store`) |
| No provider | If the discovery provider can't supply Google ratings, or the gate is closed, a campaign with a minimum is rejected with `COMPLIANCE_BLOCKED` / `VALIDATION_ERROR` |

### 3.7 Google Places compliance gate

- **Intended use (PR-4.1):** use Places Text Search, with `minRating` and website-listing data, to find and filter US businesses by Google rating and website availability, in order to select potential website-development clients for commercial email outreach.
- **Not storing the data doesn't settle compliance.** The use itself needs confirmation from Google, or a written opinion from qualified legal counsel covering this exact use.
- Gate scopes, stored in `compliance_approvals` and checked by the ToolGateway before any Places call:

| Scope | Unlocks | Default |
|---|---|---|
| `google_places_prospect_selection` | discovery, rating filter, qualification from Places results | **closed** |
| `google_places_listing_display` | live Google Places listing panel in lead detail | **closed** |
| `google_places_technical_testing` | capped adapter tests in `live-test` only, with no prospect records created | closed (operator decision) |

- While closed: those calls return `COMPLIANCE_BLOCKED` with no request sent. The dashboard shows a gate banner.
- **Provider replaceability:** `DiscoveryProvider` interface with `google_places` (gated), `manual_import` (operator-supplied list with an attested right to use it), and `licensed_business_data` (future, TBD). Verification, research, building and outreach work the same for every provider. Note: SerpApi's Google Maps or local engines return Google Maps content and fall under the same open question. They aren't a workaround.
- **Development impact:** Phases 4–6 are built and live-tested using `manual_import` prospects and the diversity dataset, so the gate doesn't block development.

---

## 4. Agent Architecture

All agents implement:

```python
class Agent(Protocol):
    role: AgentRole
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_tools: frozenset[ToolName]   # enforced by ToolGateway
    max_attempts: int
    timeout_s: int
    async def run(self, ctx: TaskContext, inp: BaseModel) -> AgentResult: ...
```

Agents reach providers only through the **ToolGateway**, which on every call:
1. checks the global, provider and campaign kill switches;
2. checks the campaign's authorisation scope;
3. **reserves** the call's worst-case units and cost against every applicable limit (campaign,
   daily, monthly, provider unit cap), with a row lock. If any limit would be exceeded it returns
   `BUDGET_BLOCKED` and **no request is sent**;
4. calls the adapter;
5. records actual units and cost in `api_usage` and commits the reservation (or releases it);
6. emits a `task_event`.

| # | Agent | Inputs → Outputs | Allowed tools | Retries | Key boundary |
|---|---|---|---|---|---|
| 1 | Command | text → `CommandIntent` / clarification | Haiku 5.5 | 2 | Produces intents. Never executes. |
| 2 | Orchestrator & Policy | intents, results → task graph, transitions | DB | n/a | Deterministic. The only component that changes campaign state. |
| 3 | Discovery | `CampaignParameters` → place_ids + in-memory candidates | `DiscoveryProvider` (Places gated; manual import) | 3 | Compliance gate (§3.7). Field masks fixed (§5.1). In-memory rating filter and missing-rating policy (§3.6). No Places content persisted. |
| 4 | Verification | in-memory candidate → `website_audits` (evidence-based state, §3.5) | SerpApi, HTTP probe, DNS, Firecrawl (≤ 3 profile pages) | 3 | SSRF guard. ≤ 2 SERP queries per prospect. `NO_WEBSITE` only after every required check completes. |
| 5 | Research | prospect → `research_facts`, contacts | SerpApi, Firecrawl, Haiku 5.5 | 2 | Crawl budget per prospect. Independent sources only. Quote-verified facts. |
| 6 | Qualification | facts, audits → `lead_scores` | none | 0 | Pure function with a versioned rule set. No Places-derived inputs (no rating, no review count). `WEBSITE_UNKNOWN` scores 0 for website opportunity. |
| 7 | Website Builder | verified facts → spec + source | Sonnet 5.5, Opus 5.5 (escalation), Storage | 2 per stage | Verified facts only. Writes only to scaffold paths. |
| 8 | QA | site version → builds, reports | Sandbox, Playwright, Haiku 5.5 | ≤ 3 repair cycles | Then `requires_human_review`. |
| 9 | Deployment | passed build → `deployments` | Cloudflare Pages API, HTTP probe | 3 | Preview project only. |
| 10 | Outreach | prospect, site → drafts, sends, reply classes | Sonnet 5.5, Haiku 5.5, Email | 2 | Drafts always allowed. Sends only when sending readiness passes and the campaign is authorised. |

### 4.1 Model routing

| Task | Model | Rationale |
|---|---|---|
| Command parsing and routing, reply classification, fact extraction, factual-claim checks, diversity-guard text comparison | `claude-haiku-5-5` | Structured, short, high volume. $0.10 / $0.50 per MTok |
| Website strategy (`SiteSpec`), page and component generation, ordinary repair (cycles 1–2), outreach copy | `claude-sonnet-5-5` | Strong code and design quality at $2 / $10 per MTok |
| Escalation only: repair cycle 3 after two failed Sonnet repairs; resolving conflicting facts that the deterministic rules can't resolve; re-planning a strategy rejected twice by the diversity guard | `claude-opus-5-5` | $4 / $20 per MTok. Its own per-site sub-cap |
| Scoring, policy, limits, state transitions, identity matching, dedupe | Python | Deterministic and reproducible |

Controls: every call sets `max_tokens`, which bounds worst-case cost for the reservation.
Prompt caching covers the scaffold and system prompts. Message Batches (50% off) are available
for non-urgent builds. Opus escalations are counted separately and capped at
`OPUS_MAX_ESCALATIONS_PER_DAY`.

---

## 5. API Integrations

Every integration lives in `integrations/<name>/` with a typed adapter, config validation, auth,
explicit timeouts, retries with backoff and jitter (max 3, honouring `Retry-After`), error mapping,
a usage hook, and contract and live tests.

### 5.0 Google API scope and terminology

| Term | Meaning in JARVIS documents |
|---|---|
| **Google Maps Platform** | The overall Google platform: its Terms of Service, Service Specific Terms, billing, quotas, attribution policy and the Google Cloud project. Used only when talking about terms, billing or quotas |
| **Google Places API (New)** | The **only** Google Maps Platform API JARVIS integrates. Provider ID `google_places`, adapter `integrations/google_places/`, key `GOOGLE_PLACES_API_KEY` |
| **Google Maps Content** | Google's contractual term for data returned by Maps Platform APIs, including Places API responses. Used when describing storage and display restrictions |

**Endpoints used (Places API New only):**

| Endpoint | Purpose | Notes |
|---|---|---|
| `POST https://places.googleapis.com/v1/places:searchText` (Text Search) | **Primary.** Business discovery by category and location; dynamic `minRating` filter; retrieves `rating`, `userRatingCount`, `websiteUri` (whether a website is listed) and `businessStatus` in the same response | All discovery and filtering |
| `GET https://places.googleapis.com/v1/places/{place_id}` (Place Details) | Secondary, optional: the on-demand Google Places listing panel only | Gated separately (`google_places_listing_display`) |

**Explicitly not integrated:** Maps JavaScript API, Maps Embed API, Maps Static API, Routes API
(and the legacy Directions/Distance Matrix), Geocoding API, Places Autocomplete, Places photos,
and Nearby Search (New). Text Search covers category + location queries, and the `minRating`
parameter is documented for Text Search. There are no interactive maps, directions or navigation.
The Google Cloud project enables only Places API (New), and the API key is restricted to it.

**Other providers, unchanged:** SerpApi (Google Search engine) for search research and
independent website verification. Firecrawl for website crawling and extraction.

### 5.1 Google Places API (New): field masks and SKUs

**Re-verified 2026-11-18** against Google's Text Search (New) page and its Place Data Fields
(New) table **[confirmed]**. The **highest-tier field in the mask sets the SKU for the whole
request.** Masks are code constants, and a unit test asserts each mask maps to its expected SKU.

Per-field SKUs **[confirmed]**:

| Field | Text Search SKU | Place Details SKU |
|---|---|---|
| `rating` | Enterprise | Enterprise |
| `userRatingCount` | Enterprise | Enterprise |
| `websiteUri` | Enterprise | Enterprise |
| `nationalPhoneNumber` | Enterprise | Enterprise |
| `displayName` | Pro | Pro |
| `businessStatus` | Pro | Pro |
| `googleMapsUri` | Pro | Pro |
| `primaryType` | Pro | Pro |
| `formattedAddress` | Pro | Essentials |
| `types` | Pro | Essentials |
| `attributions` | Essentials (IDs Only) | Essentials (IDs Only) |
| `id` | Essentials (IDs Only) | Essentials (IDs Only) |
| `nextPageToken` | Essentials (IDs Only) | n/a |

Masks:

| Mask | Endpoint | Fields | SKU | Used for | Gate scope |
|---|---|---|---|---|---|
| `DISCOVERY_ENTERPRISE` | Text Search | `places.id, places.attributions, places.displayName, places.formattedAddress, places.businessStatus, places.primaryType, places.websiteUri, places.nationalPhoneNumber, nextPageToken` (+ `places.rating` only when a minimum rating is set) | **Text Search Enterprise**. Adding `rating` doesn't change the SKU | discovery, rating filter, website pre-filter | prospect_selection |
| `DISCOVERY_PRO` (fallback) | Text Search | as above, minus `websiteUri`, `nationalPhoneNumber`, `rating` | Text Search Pro | only when no rating filter is set and the Enterprise cap is reached. Website checks then rely on independent sources only | prospect_selection |
| `IDS_ONLY` | Text Search | `places.id, places.attributions, nextPageToken` | Essentials (IDs Only) | checking whether a place is still listed | prospect_selection |
| `LISTING_DISPLAY` | Place Details | `id, attributions, displayName, formattedAddress, businessStatus, googleMapsUri, rating, userRatingCount` | **Place Details Enterprise** (rating, userRatingCount) | live Google Places listing panel | listing_display |

`minRating` is a request **parameter**, not a field. It has no SKU of its own and filters
server-side within the same Text Search request **[confirmed: 0.0–5.0, 0.5 increments, rounded
up]**. So the rating filter needs **no separate Place Details call per result**. The fields it
needs (`rating`, plus `websiteUri` for the website pre-filter) all come back in the same Text
Search Enterprise response.

Never requested: `regularOpeningHours`, `reviews`, `photos`, `priceLevel`.

**Pagination realities [confirmed]:** `pageSize` 1–20 (default 20). A query returns at most 60
results across pages. Pages may hold fewer than `pageSize`. Results for identical requests
aren't guaranteed to be consistent. Follow-up pages must repeat every parameter except
`pageSize`/`pageToken`. `locationRestriction` (rectangle, categorical queries only) and
`locationBias` (circle ≤ 50 km or rectangle) are mutually exclusive.
`includePureServiceAreaBusinesses=true` returns businesses with no storefront, and those omit
`formattedAddress`.
**Unconfirmed:** whether a page that returns zero results is billed (we assume it is), and how
`minRating` treats unrated places (we don't depend on it: §3.6).

Reaching the target count: large cities are split into tiles of ≤ 60 results each, crossed with
niche synonyms. Requests per campaign are planned with the yield model in
[API_COST_MODEL §2](docs/API_COST_MODEL.md), not by assuming 20 useful results per request.

### 5.2 Other providers

| Integration | Endpoints | Notes |
|---|---|---|
| SerpApi | `GET /search?engine=google`; `GET /account` (usage sync) | `location`, `gl=us`, `hl=en`. The API key is redacted from logs. Cached repeats are free. |
| Firecrawl | `/v2/scrape`, `/v2/crawl` (with `limit`), credit-usage endpoint (usage sync) | Our own robots.txt check first. Plain markdown scrapes, extraction done by Haiku. |
| Claude | `/v1/messages` (streaming), `/v1/messages/batches` | Official `anthropic` SDK. The `usage` in each response gives exact token counts. |
| Cloudflare | Pages Direct Upload, deployments list/delete | Token scoped to Pages:Edit on one account. |
| Email | Resend or SES (when ready) | Signed webhooks through the edge Worker. |
| LiveKit (Phase 7) | Server API (tokens), Agents SDK | Short-lived tokens. |

---

## 6. Billing Protection and Usage Visibility

**Google Cloud budget alerts don't cap spending.** They only send notifications. JARVIS
therefore enforces its own limits, with a Google-side hard stop as a backstop.

| Layer | Mechanism | Hard stop? |
|---|---|---|
| 1. Application limits (primary) | Locked counters in `usage_counters` per provider/SKU/day/month, plus `budget_ledger` reservations. The worst-case cost and units are reserved **before** the request. A call that would exceed any cap is not sent. | Yes, before the request |
| 2. Provider-side quotas | Google Cloud Console → Places API (New) → Quotas: a per-day request cap on `SearchText` and `GetPlace` set below our app limits, with a margin for enforcement lag. SerpApi and Firecrawl stop at plan limits (no overage while auto-recharge is off). Claude: a workspace spend limit in the Console. | Yes, with Google's enforcement lag |
| 3. Alerts (warning only) | A Google Cloud budget alert at $1 and $5. Daily email or dashboard alert at 80% of any JARVIS cap. | No |

Default caps sit **below** each free allowance (for example 900 of 1,000 Text Search Enterprise
calls/month), so normal operation never reaches paid usage. Raising a cap above a free allowance
is a user action, it is audited, and the UI shows the price.

**Estimated vs actual usage** are stored separately and never blended:
- **Estimated** (`api_usage.estimated_cost_micros`): our ledger, computed at call time from
  configured unit prices.
- **Provider-reported** (`provider_usage_reports`): pulled on a schedule where an API exists
  (SerpApi account usage, Firecrawl credit usage, Claude response `usage` per call plus the Usage
  and Cost Admin API if an admin key is configured). Entered or imported manually for Google
  (Cloud Billing reports), and labelled as such.
- The Activity dashboard shows both columns, the variance and the last sync time. A missing
  provider report shows "not available", never a copied estimate.

---

## 7. Database Design (summary)

Full detail in [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md).
- About 30 tables. `agent_tasks` is both the queue and the execution record.
- **No table has a column for Google Places content, including rating and review count.** Places-derived data is limited to
  `place_refs.place_id` and an optional `place_refs.lat/lng` with `coords_expires_at` ≤ 30 days.
  A CI schema test fails if new columns match a denylist (`*_name` on `place_refs`,
  `formatted_address`, `rating`, …) on Places-linked tables.
- Provenance on every independently sourced value: `source_type`, `source_ref`, `retrieved_at`, `retention_policy`.
- Money is stored in integer micro-dollars. Usage counters and ledger rows are locked for each reservation.
- RLS is enabled on every table.

---

## 8. State Machines

Implemented as explicit transition tables. `transition()` writes the state change and an
`audit_events` row in one transaction and rejects any invalid transition.

**Campaign:** `draft → authorized → queued → discovering → verifying → researching → qualifying → building → qa → preview_deployed → outreach_authorized → completed`; plus `paused`, `cancelled`, `failed`, `requires_human_review` (both resume to a stored state). Stages outside the campaign's scope are skipped. In the combined discovery task, `discovering` and `verifying` overlap per page, and the campaign moves to `verifying` once every page task is done.

**Prospect:** `discovered → identity_verified → website_verified → researched → qualified`, with `rejected` reachable from any non-terminal state and `requires_human_review` for unconfirmed identity or `rating_unknown` (missing-rating policy `include_for_review`). `website_verified` means a persisted availability state other than `WEBSITE_UNKNOWN`.

**Website availability (per prospect):** `WEBSITE_LISTED → WEBSITE_WORKING | WEBSITE_BROKEN | WEBSITE_UNKNOWN`, or (no listing) `→ NO_WEBSITE | WEBSITE_UNKNOWN`. `WEBSITE_UNKNOWN → re-verify → any state`. A `WEBSITE_LISTED` state whose only source is Google is resolved within the task and never persisted.

**Website:** `queued → strategy → generating → building → qa → (repair → building)* → passed → deployed`; plus `failed`, `requires_human_review`, `expired`, `archived`.

**Outreach message:** `draft → authorized → queued → sent → replied → follow_up → closed`; terminal side states `suppressed`, `bounced`, `opted_out`, `failed`. `draft` needs no email configuration. `draft → authorized` requires sending readiness.

**Task:** `pending → leased → running → succeeded | retry_wait → pending | failed | cancelled | blocked`.

---

## 9. Deployment Topology

| Env | DB | External APIs |
|---|---|---|
| `local` | local Supabase (CLI) | real keys with very low caps, or test doubles only when `JARVIS_ENV=test` |
| `test` (CI) | ephemeral Postgres | test doubles plus recorded contract fixtures |
| `live-test` (manual workflow) | dedicated Supabase schema or project | **real providers, tightly capped** (§10 of the plan) |
| `production` | Supabase Free | real |

Production:
- **Dashboard:** Cloudflare Pages from `main`.
- **Previews:** Pages project `jarvis-previews`, Direct Upload, branch per site, `*.pages.dev`.
- **Edge Worker:** `jarvis-edge.<account>.workers.dev` (unsubscribe, webhooks).
- **VPS (required):** Docker Compose running `caddy`, `api`, `worker`, `backup`, and later
  `voice`, with sandbox containers launched by a restricted runner. Unattended security upgrades,
  SSH keys only, a firewall allowing only 80/443 and SSH from your IP.
- **Supabase Free:** Postgres, Auth, Storage.
- **CI:** GitHub Actions.

---

## 10. Security Boundaries

Detail in [docs/SECURITY_AND_COMPLIANCE.md](docs/SECURITY_AND_COMPLIANCE.md).

| Boundary | Control |
|---|---|
| Browser ↔ API | Supabase JWT checked against JWKS, CORS restricted to the dashboard origin, rate limits, Zod and Pydantic validation, no secrets in the bundle |
| Server ↔ providers | Server-only keys, scoped tokens, log redaction |
| Worker ↔ internet | SSRF guard on every direct fetch |
| Worker ↔ generated code | Sandbox with no network, non-root user, read-only root, resource caps, static-only output |
| Agent ↔ tools | ToolGateway allow-list and limits |
| System ↔ outside world | Kill switches, authorisation, budgets, suppression, sending readiness, human-approval gates |

---

## 11. Failure Recovery

| Failure | Behaviour |
|---|---|
| Worker crash | Lease expiry (5 min, heartbeat every 60 s) → task reclaimed. Step idempotency keys stop duplicate side effects. |
| Provider 429/5xx | Backoff retries → `retry_wait` → `failed` after `max_attempts`. The campaign continues with other prospects. |
| Quota or cap reached | `QUOTA_EXCEEDED` / `BUDGET_BLOCKED` → task `blocked`, shown on the campaign. No further calls to that provider. |
| **Database unreachable** (network, restart, Supabase pause) | The DB health monitor detects it (`SELECT 1` every 30 s, connection errors). API → `/ready` 503, writes rejected with "database unavailable". Worker → stops claiming, finishes nothing that needs a commit, and holds no new external calls (nothing billable runs without a ledger write). Reconnects with exponential backoff (1 s → 60 s cap). An alert goes out through the edge Worker or email. On reconnect: lease recovery, then normal operation. |
| **Supabase project paused for inactivity** | Detected as a persistent connection failure with a paused-project error. Shown in the dashboard as "Database paused: resume it in the Supabase dashboard" (a manual action). After resume, the worker reconnects automatically. Inactivity pausing is **not** assumed to be prevented by worker traffic (§12). |
| Data loss / corruption | Restore from our own daily `pg_dump` (Supabase Free has no managed backups or PITR). Runbook in Phase 9. |
| Claude refusal / truncation | `stop_reason` checked. A refusal is logged and the task fails with its reason (a server-side fallback is configured where supported). On `max_tokens`: one continuation, then fail. |
| Build/QA keeps failing | 2 Sonnet repairs → 1 Opus repair → `requires_human_review`. |
| Preview not live | 5 checks over 2 min, then `failed`. Outreach can't reference it. |
| Edge Worker can't reach the API | The unsubscribe is still recorded: the Worker stores the token in Workers KV (free) and the API drains KV on recovery. The user sees a confirmation either way. |
| Kill switch | Claims stop immediately. The ToolGateway refuses all calls, and running tasks are cancelled at their next checkpoint. |

---

## 12. Supabase Free Tier: Limits and Mitigations

Figures from secondary sources. **Check them on supabase.com/pricing before Phase 1.**

| Limitation | Impact | Mitigation |
|---|---|---|
| 500 MB database | Grows with evidence and events | Retention purges (task events 90 d, SERP 180 d), raw pages kept in Storage not rows, storage monitored at 70% |
| 1 GB file storage | Site sources and artefacts | Content-addressed artefacts, keep only the last 2 builds per site, purge on preview expiry |
| 5 GB egress | Dashboard and Storage downloads | Artefacts go straight to Cloudflare, and the dashboard shows thumbnails |
| **Inactivity pause** (≈ 7 days of low activity) | Production outage until resumed manually | Not guaranteed to be prevented by worker traffic. Detected and alerted. Manual resume runbook. Upgrade to Pro ($25/mo, optional) removes it |
| **No managed backups or PITR on Free** | Data loss risk | Our daily `pg_dump` (schema + data) to the VPS plus an encrypted off-site copy (Cloudflare R2 free tier or similar). Retention: 7 daily + 4 weekly. A tested restore |
| Connection limits | Pool exhaustion | Small pools (api 5, worker 5) through the session pooler |
| 2 projects | live-test can't have its own project if two already exist | Use a separate schema for live tests, or reuse the second free project |

---

## 13. Observability

- Structured JSON logs carrying `task_id`, `campaign_id`, `agent_role`, `provider`, `model`, `sku`, `duration_ms`, `tokens_in/out`, `estimated_cost_micros`, `attempt` and `result`.
- `/health` (process), `/ready` (DB and queue), `/providers/health` (per-adapter status, including `NOT_CONFIGURED`).
- A worker heartbeat table, queue depth, DB health state and the last backup time are shown on the dashboard.
- The cost dashboard shows estimated vs provider-reported usage, by provider, campaign and day.

## 14. Reporting

All dashboard figures are SQL views over real rows. Production builds have no demo data and no client-side fabrication.

## 15. Scalability Path

More worker replicas, Batches for generation, a second VPS for builds, Supabase Pro, Cloudflare Queues for webhook fan-in.

## 16. Decisions Requiring Approval

1. VPS provider and size (recommended: CX23-class, 2 vCPU / 4 GB, ≤ $10/month).
2. The model routing in §4.1 (Sonnet 5.5 as the main generation model, Opus 5.5 for escalation only).
3. The Places data design (§3.3–3.6, §5.1), including that no-website prospects need an independent or operator-confirmed name and contact before they qualify.
3a. **Compliance gate (blocker):** obtain written confirmation from Google, or a written opinion from qualified counsel, for the exact use in §3.7. Until then Places discovery, the rating filter and the listing panel stay disabled.
3b. Whether to open `google_places_technical_testing` (capped adapter tests, no prospect records) before 3a is resolved.
3c. The default `NO_WEBSITE_MIN_CONFIDENCE` (proposed 0.8) and the missing-rating policy pre-selection (proposed `exclude`).
4. Deferring Groq.
5. Voice in Phase 7.
6. The preview-site policy: noindex, a non-affiliation banner, no logos or photos copied, 30-day expiry.
7. Outreach sending stays blocked until you provide a sending domain and a legitimate postal address (no purchase assumed).
8. An off-site backup target (recommended: Cloudflare R2 free tier).
