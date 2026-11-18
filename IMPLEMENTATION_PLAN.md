# JARVIS — Implementation Plan

Status: **Proposed. No phase starts until the architecture review is approved.**

The plan builds the system from the inside out: durable core first, then the pipeline one stage
at a time against real APIs, then the interfaces. Every phase ends in something you can run
and verify against real data. No phase is "done" because the UI looks right.

## Phase overview

| Phase | Name | Depends on | Est. effort | External spend |
|---|---|---|---|---|
| 0 | Foundations & tooling | approval | 2–3 days | $0 |
| 1 | Durable core: DB, queue, state machines, policy, cost ledger | 0 | 4–6 days | $0 |
| 2 | Command intake + minimal operator UI shell | 1 | 3–4 days | < $1 (Claude) |
| 3 | Discovery (Google Places) | 1 | 2–3 days | $0 (free tier) |
| 4 | Website verification (probes + SerpApi) | 3 | 4–5 days | $0 (free tier) |
| 5 | Research (Firecrawl + extraction) + Qualification | 4 | 4–5 days | < $2 |
| 6 | Website builder + sandbox + QA + repair loop + preview deploy | 5 | 8–12 days | ≈ $10–15 |
| 7 | Voice (LiveKit) | 2 | 3–4 days | $0 |
| 8 | Outreach (blocked on sending domain) | 6 + domain | 4–6 days | $0–12 |
| 9 | Hardening, reporting, ops | all | 3–5 days | $0 |

Effort assumes one engineer working with Claude Code. Phases 3–5 can overlap with the Phase 2
UI work.

---

## Phase 0: Foundations

Tasks
1. Monorepo skeleton per ARCHITECTURE.md §2.2. `pyproject.toml` (uv, Python 3.12, ruff, mypy strict on `jarvis_core`), `package.json` workspace (pnpm) for the dashboard and site-scaffold.
2. Docker: `api`, `worker`, `build-sandbox` images. `docker-compose.yml` for local dev, plus a local Supabase via the CLI.
3. A `Settings` class with full validation per ENVIRONMENT_VARIABLES.md, and a test proving production refuses test doubles.
4. CI: lint, type-check, unit tests, secret scanning (gitleaks), and dependency audit.
5. `.env.example`, `.gitignore`, `README.md` quickstart.

Acceptance
- `docker compose up` starts the api and worker against local Supabase. `/health` and `/ready` return 200.
- Startup in `production` with a missing required variable fails with a clear message.
- CI is green on an empty test suite plus the settings tests.

## Phase 1: Durable core

Tasks
1. Migrations for every table in DATABASE_SCHEMA.md, with enums, indexes, triggers and RLS policies.
2. `db/queue.py`: enqueue (idempotent), claim (`SKIP LOCKED`), heartbeat, complete, fail, retry with backoff, lease-expiry recovery, cancel.
3. `domain/state_machines.py`: transition tables for campaign, prospect, site, outreach and task. `transition()` writes the audit event in the same transaction.
4. `policy/`: kill switches, campaign scope check, per-provider unit caps, budget reservation/commit/release in micro-dollars.
5. `integrations/base.py`: adapter protocol, `ProviderStatus`, retry/backoff, error mapping, usage hook, log redaction.
6. `orchestrator/`: task runner loop, handler registry, campaign planner (stage → task fan-out), pause/resume/cancel.
7. `observability/`: structlog config, `api_usage` writer, worker heartbeats.

Acceptance
- Property tests: random sequences of transitions never reach a state outside the table. Invalid transitions raise and write nothing.
- Chaos test: kill the worker mid-task. After the lease expires the task is reclaimed and finishes exactly once (idempotency key honoured).
- Budget test: concurrent reservations against a $1 cap never commit more than $1 in total.
- The global kill switch stops claims within one poll interval.
- RLS tests: an `authenticated` user can't read another owner's rows, and can't write anything.

## Phase 2: Command intake and UI shell

Tasks
1. `POST /commands`, `GET /commands/{id}`, `POST /commands/{id}/confirm`. Command agent on Haiku 5.5 with structured output → `CommandIntent`.
2. Clarification flow for missing niche, location or count. A confirmation card with the plan and cost estimate.
3. SSE endpoint `GET /campaigns/{id}/events` (with `Last-Event-ID` resume).
4. Dashboard: auth, app shell, design tokens (dark theme), JARVIS console (text), Campaigns list/detail, Activity, Settings (kill switches, budgets, provider status). Every view reads real API data. Empty states, not demo data.

Acceptance
- An eval set of 40 realistic commands, including ambiguous ones, gets ≥ 95% correct field extraction, and 100% of ambiguous cases ask for clarification instead of guessing.
- Refresh the browser mid-run and the progress stream resumes with no lost events.
- Provider panel shows `NOT_CONFIGURED` for every provider without a key.

## Phase 3: Discovery

Tasks
1. Google Places adapter: `text_search` (Pro mask), `place_details` (Enterprise mask), pagination, field-mask → SKU mapping, unit caps.
2. Query expansion: niche synonyms (e.g. "plumber", "plumbing contractor", "emergency plumber"). Geo bias with a rectangle or circle, splitting large cities into tiles.
3. Dedupe on place_id, normalised phone, and normalised name + ZIP. `places_cache` with TTL and a purge job.
4. Leads table in the dashboard, with Google attribution.

Acceptance
- A live run of "plumbing, Houston, 100" returns ≤ 100 unique, operational prospects using ≤ the planned number of calls. `api_usage` matches the Google Cloud console counts.
- With the key removed, the task goes to `blocked`, the provider reads `NOT_CONFIGURED`, and no rows are fabricated.
- The purge job deletes expired cache rows. Prospects keep `place_id` only.

## Phase 4: Website verification

Tasks
1. The `http_probe` adapter with the SSRF guard (unit tests against private IPs, redirect-to-private, DNS rebinding).
2. DNS checks, TLS, redirect chain, canonical, page title/meta extraction.
3. SerpApi adapter. Query templates. A directory/aggregator/social domain blocklist (Yelp, Angi, BBB, Facebook…) so these are never treated as "official".
4. Identity-matching scorer (name, phone, address, city). Quality signals for "outdated" (no viewport meta, no HTTPS, old copyright year, Flash/table layouts, page weight, CMS version).
5. The decision logic from ARCHITECTURE.md §3.3, producing `website_audits` with evidence and confidence.

Acceptance
- A hand-labelled set of 50 real Houston businesses (labelled by you or me, with URLs checked manually) reaches ≥ 90% agreement on status, and **0** cases of `none_found` where a functioning official site exists and is findable by the top-2 SERP queries.
- Every audit has ≥ 1 evidence item, method list and timestamp.
- The SSRF test suite passes.

## Phase 5: Research and qualification

Tasks
1. Firecrawl adapter (scrape and bounded crawl), robots.txt check, per-domain concurrency of 1.
2. Extraction with Haiku 5.5: facts with exact supporting quotes. A deterministic verifier marks a fact `verified` only if the quote appears in the fetched source text. Conflicts are detected across sources.
3. Qualification engine: a versioned rule set implementing the 35/20/15/10/10/10 model. Unknown inputs score 0 or partial per rule, never full. Each component has an explanation and evidence IDs.
4. Lead detail view: facts grouped by status, sources, score breakdown.

Acceptance
- Determinism: the same inputs and rule set always produce the same score and `inputs_hash`.
- A spot-check of 20 profiles finds zero `verified` facts without a matching source quote.
- Changing a rule weight creates a new rule-set version and old scores remain reproducible.

## Phase 6: Website builder, QA and deployment

Tasks
1. `site-scaffold/`: Astro + Tailwind starter, a component library (hero variants, service grids, area maps without Google embeds, CTA bars, forms, FAQ, footer), SEO helpers (meta, Open Graph, JSON-LD `LocalBusiness`), the noindex/preview banner, `_headers`.
2. Strategy step (Opus 5.5): audience, positioning, conversion strategy, design direction (palette, type pairing, layout archetype) → a validated `SiteSpec`. The diversity guard compares the new spec with recent sites and rejects near-duplicates.
3. Generation step: Opus 5.5 writes pages and components into allowed paths, using only `verified` facts.
4. The sandbox runner: pinned image, no network, limits (SECURITY_AND_COMPLIANCE.md §4).
5. QA in the sandbox with Playwright: build passes; desktop/mobile screenshots; nav and internal links; external links (HEAD check outside the sandbox through the SSRF guard); form behaviour; `tel:` links; console errors; missing assets; metadata/JSON-LD validity; axe-core accessibility; Lighthouse performance (optional); factual-claim check (each claim maps to a fact ID).
6. LangGraph repair loop with a Postgres checkpointer: QA failures → targeted repair prompt → rebuild. Max 3 cycles, then `requires_human_review`.
7. Deployment agent: Cloudflare Pages Direct Upload to the `jarvis-previews` project, branch = site slug. Verify the live URL (HTTP 200, title match, noindex header present). Record the deployment, plus expiry cleanup.
8. Websites view: previews, versions, QA reports with screenshots, deployment history.

Acceptance
- 5 sites for 5 real prospects in **different** niches/cities, all passing QA with zero console errors, axe "serious/critical" = 0, mobile Lighthouse performance ≥ 90 (target), and every factual claim traced to a verified fact.
- A visual and structural diversity check: no two sites share a layout archetype plus palette plus section order.
- A sandbox escape test: generated code that tries network access or writes outside `/work` fails safely.
- The preview URL is live, noindexed, carries the banner, and is deleted by the expiry job.
- Per-site cost recorded and ≤ `SITE_MAX_COST_MICROS`.

## Phase 7: Voice

Tasks
1. LiveKit Agents worker: STT → `POST /commands` (same pipeline) → TTS of the response. Barge-in (interrupt) support. Spoken confirmation for high-impact actions.
2. Dashboard: a microphone button, live transcript, interrupt/cancel.
3. Room tokens minted by the API with a short TTL.

Acceptance
- The same 40-command eval spoken (recorded audio) gets ≥ 90% correct intents. Text and voice create identical tasks.
- Interrupting mid-response stops TTS within 500 ms.

## Phase 8: Outreach (needs a sending domain and postal address)

Tasks
1. Email adapter (Resend or SES), domain DNS checks (SPF/DKIM/DMARC), signed webhooks.
2. Outreach drafts grounded in observations (fact IDs) and a verified preview URL. Compliance footer. `List-Unsubscribe` headers. Signed one-click unsubscribe endpoint.
3. Outreach campaign authorisation with limits, send window, follow-up policy.
4. Reply ingestion and classification (Haiku 5.5). Automatic suppression on opt-out, bounce, negative reply or complaint.
5. Outreach view: drafts, approvals, sends, replies, suppression list.

Acceptance
- Sending is impossible without authorisation, domain and postal address (tested).
- An unsubscribe click suppresses immediately, and no further mail is sent to that address or domain (tested with a real inbox you control).
- Crash between provider accept and DB commit never produces a duplicate send (idempotency test with the provider's sandbox or test address).
- First live sends go only to addresses you own, before any prospect.

## Phase 9: Hardening and operations

Tasks: reporting views (funnel, cost by provider/campaign/day), cost-per-site report, backup job plus a restore drill, encryption-key rotation script, load test of the queue (1k tasks), dependency pinning, runbook (`docs/RUNBOOK.md`), an alert on worker heartbeat loss and on daily spend > 80% of cap.

Acceptance: a restore from backup into a fresh project works. All dashboard numbers reconcile with SQL counts. Runbook steps have been executed once.

---

## Test strategy

| Layer | Tool | Scope |
|---|---|---|
| Unit | pytest, hypothesis | state machines, policy, scoring, SSRF guard, identity matching, cost math |
| Integration (DB) | pytest + local Supabase | queue semantics, RLS, transactions, migrations up and down |
| Contract | pytest + recorded fixtures | each adapter's request shape and error mapping (fixtures recorded from real responses, with secrets scrubbed) |
| Live integration | manual GitHub workflow | real providers with tiny caps (1–2 calls each). Run before each phase sign-off |
| Frontend | Vitest + React Testing Library | components, Zod schemas, query hooks |
| E2E | Playwright | dashboard flows against a local stack |
| LLM evals | pytest-based eval harness | command parsing, fact extraction faithfulness, reply classification, site factuality |
| Generated sites | Playwright + axe + Lighthouse in the sandbox | per-site QA (part of the product) |

Rule: test doubles exist only under `tests/` and are only importable when `JARVIS_ENV=test`.

## Deployment strategy

- `main` is always deployable. Work happens on short-lived branches, and merges require CI green.
- Dashboard: Cloudflare Pages Git integration on `main`.
- API/worker: GitHub Actions builds images → pushes to GHCR → SSH deploy to the host (`docker compose pull && up -d`). Migrations run first (Supabase CLI), and only backwards-compatible migrations are applied ahead of code.
- The worker drains on SIGTERM (finishes or releases its lease) so deploys don't create orphaned tasks.
- Rollback: redeploy the previous image tag. Migrations are additive within a release.

## Risks and mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Google Places terms restrict this use of Places data | Medium | High | Transient cache design. Legal review. Discovery adapter is swappable for SerpApi local results |
| 2 | "No website" false positives damage credibility in outreach | Medium | High | Two independent checks, confidence scores, `unverified` state, and the Phase 4 accuracy gate |
| 3 | Generated sites look templated | Medium | High | Strategy-first spec, a diversity guard, a varied component library, and the human review of the first 10 sites |
| 4 | Generated sites contain fabricated claims | Medium | High | Verified-facts-only input, quote-verified facts, and the factual-claim QA check |
| 5 | Claude cost overrun | Low–Med | Medium | Reservations, per-site cap, Sonnet/Batch levers, prompt caching |
| 6 | Free-tier limits change | Medium | Medium | Prices in config, unit caps below the free allowance, `QUOTA_EXCEEDED` surfaced |
| 7 | Outreach blocked by no domain | High (today) | High | Decision needed now. Drafts work without it |
| 8 | Email deliverability or a spam complaint | Medium | High | Low volume, warm-up, authentication, immediate suppression, personalised content |
| 9 | Preview site viewed as impersonation | Low–Med | Medium | Banner, noindex, no copying of logos/photos, expiry |
| 10 | Sandbox escape | Low | High | No-network container, non-root, no Docker socket, static output validation. gVisor later |
| 11 | Supabase Free limits (500 MB, connection caps) | Low (v1) | Medium | Retention purges, artefact pruning, upgrade path documented |
| 12 | Single host is a single point of failure | Medium | Medium | Durable queue in Supabase means no work is lost. A host rebuild runbook. Recovery in under 1 hour |
| 13 | Prompt injection from crawled content | Medium | Medium | Data-only prompts, no dangerous tools in extraction calls, schema validation |
