# JARVIS — Implementation Plan

Status: **Proposed, revision 3. No phase starts until the architecture review is approved.**

Revision 3: rating-filter tasks and tests, evidence-based website states, the Places compliance gate (Phase 3 production discovery stays off until approval; Phases 4–6 use `manual_import`), Places API (New) naming, and a required-test matrix (§Required tests).

Revision 2:
- Added a Definition of Done that requires a capped live test.
- The cross-niche diversity dataset is now a multi-campaign dataset.
- Places compliance tasks and tests added.
- Billing hard-stop tests added.
- DB recovery and backup tasks added.
- Sending-readiness gate added.
- VPS provisioning moved to Phase 0.
- Phase costs now match the cost model.

## Definition of Done (applies to every feature)

A feature is **complete** only when all of these are true:
1. Unit, integration and contract tests pass in CI.
2. **A tightly capped live test against the real provider, or the real DB/sandbox/Cloudflare, has passed.** Its run ID, provider usage and cost are recorded in `docs/test-runs/<phase>.md`.
3. Its acceptance criteria below are demonstrated with real data, and the dashboard shows that data from the DB.
4. Errors surface correctly: `NOT_CONFIGURED`, `ERROR`, `QUOTA_EXCEEDED` and `BUDGET_BLOCKED` are each exercised at least once.

**Mocked results never satisfy the Definition of Done.** Test doubles and recorded fixtures are
allowed for fast CI feedback. A feature verified only against them is reported as "implemented,
not verified".

## Phase overview

| Phase | Name | Depends on | Effort | Metered spend (est.) |
|---|---|---|---|---|
| 0 | Foundations, VPS provisioning, tooling | approval | 2–3 d | VPS $10/month starts |
| 1 | Durable core: schema, queue, state machines, limits, ledger, DB resilience, backups | 0 | 5–7 d | $0 |
| 2 | Command intake + dashboard shell | 1 | 3–4 d | ≈ $0.10 |
| 3 | Discovery: `DiscoveryProvider`, manual import, Google Places API (New) adapter behind the compliance gate, rating filter | 1 | 4 d | $0 (gate closed; optional ≤ 3 technical requests) |
| 4 | Website verification | 3 | 4–5 d | $0 (free tier) |
| 5 | Research + independent sourcing + qualification | 4 | 4–5 d | ≈ $0.50 |
| 6 | Builder, sandbox, QA, repair, preview deploy, diversity | 5 | 8–12 d | ≈ $26 |
| 7 | Voice (LiveKit) | 2 | 3–4 d | $0 |
| 8 | Outreach (drafts always; sending gated) | 6 | 4–6 d | ≈ $0.20 |
| 9 | Hardening, reporting, ops | all | 3–5 d | ≈ $0.50 |

Total development metered spend ≈ $29 (API_COST_MODEL §4). Plus the VPS.

---

## Phase 0: Foundations

Tasks
1. Monorepo per ARCHITECTURE §2.2. uv + Python 3.12, ruff, mypy strict. pnpm workspace.
2. Docker images (`api`, `worker`, `build-sandbox`, `backup`), compose files, local Supabase via the CLI.
3. `Settings` validation per ENVIRONMENT_VARIABLES.md. Production refuses test doubles.
4. **VPS provisioning (required component):** provision the approved instance; harden it (SSH keys, firewall, unattended upgrades); install Docker; set up Caddy with TLS on the provider hostname or `sslip.io`; add a deploy user and the GitHub Actions deploy key.
5. CI: lint, types, unit tests, gitleaks, dependency audit, and the Places schema-denylist test (initially vacuous).
6. A manual `live-test` workflow with a protected environment and the per-run caps from ENVIRONMENT_VARIABLES §13.

Acceptance
- `docker compose up` locally gives `/health` 200. On the VPS, `https://<host>/health` returns 200 over a valid certificate.
- Startup in production with a missing required variable fails with a clear message.
- Live: a VPS deploy from CI succeeds and rolls back to the previous image tag.

## Phase 1: Durable core

Tasks
1. Migrations for all tables in DATABASE_SCHEMA.md, including the `place_refs` constraints, the `source_type` check (no `google_places`), RLS, and triggers.
2. Queue: idempotent enqueue, `SKIP LOCKED` claim, heartbeat, retry/backoff, lease recovery, cancel.
3. State machines and audited `transition()`.
4. **Limits and ledger:** `usage_limits`, `usage_counters`, reservation/commit/release, and the sweeper. The ToolGateway refuses to call when a reservation fails.
5. Adapter base: `ProviderStatus`, retries, error mapping, redaction (keys, and Places bodies).
6. **DB resilience:** health monitor, `/ready`, reconnection with exponential backoff, a degraded mode (API 503 on writes; the worker stops claiming and makes no external calls), `db_health_events`, an alert hook, and detection of a paused project.
7. **Backups:** daily encrypted `pg_dump` → VPS + off-site (R2 if approved), `backup_runs`, and a restore script.
8. Orchestrator: task runner, planner, pause/resume/cancel, kill switches.

Acceptance
- Property tests: no state outside the transition tables is reachable.
- Chaos test: kill the worker mid-task. The task is reclaimed and finishes exactly once.
- **Cap test:** 50 concurrent reservations against a cap of 10 → exactly 10 succeed, 40 return `BUDGET_BLOCKED`, and **0 adapter requests are made for the blocked ones** (verified by the adapter call counter).
- **DB outage test:** stop Postgres during a run. The API reports 503 and the worker pauses claiming and makes no provider calls. Restart Postgres → automatic reconnection and lease recovery, with no lost or duplicated tasks.
- **Live:** run a backup against the real Supabase project and restore it into a scratch DB, with row counts matching.
- RLS tests: no cross-owner reads, and no direct writes by `authenticated`.

## Phase 2: Command intake and dashboard shell

Tasks
1. `POST /commands`, confirm/clarify flow, worst-case plan and cost estimate.
2. SSE progress with `Last-Event-ID` resume.
3. Dashboard: auth, shell, dark design system, JARVIS console (text), Campaigns, Activity (with **Estimated vs Provider-reported** cost columns, which read "not available" until a sync exists), Settings (kill switches, caps, provider status, DB health, last backup).
4. Real data only, with empty states.

Acceptance
- Command eval (40 commands, including ambiguous ones, **including the rating fixtures in API_CONTRACTS §2**: 3.5, 4.0, 4.5, "4+ stars", 3.7, 6, -1, no rating): ≥ 95% field accuracy; 100% of ambiguous or invalid cases ask or reject instead of guessing; **0 cases where a rating is invented when none was stated**.
- **Live:** the eval runs on Haiku 5.5 (capped at $0.50) and its cost appears in the ledger. The provider-reported Claude usage (from response `usage`) matches the token counts.
- A browser refresh mid-run loses no events.

## Phase 3: Discovery (Google Places API (New), gated)

Tasks
1. `DiscoveryProvider` interface. `manual_import` provider (CSV upload with operator attestation of the right to use it).
2. `google_places` provider: a Places API (New) adapter (`integrations/google_places/`) for **Text Search** (`places:searchText`) and, for the listing panel only, Place Details. Fixed mask constants (`DISCOVERY_ENTERPRISE`, `DISCOVERY_PRO`, `IDS_ONLY`, `LISTING_DISPLAY`) with a unit test of mask → SKU for each. No other Google Maps Platform API is integrated.
3. **Compliance gate:** the `compliance_approvals` table, gate checks in the ToolGateway (`COMPLIANCE_BLOCKED` before any request), and the dashboard gate banner. Scopes: `google_places_prospect_selection`, `google_places_listing_display`, `google_places_technical_testing`.
4. **Rating filter:** `minimum_google_rating` validation (0.5 steps, no default, no rounding), mapping to `minRating`, an in-memory re-check, the missing-rating policy (`exclude` / `include_for_review`; the latter queries without `minRating`), and rating values never leaving memory.
5. Discovery page task: Text Search, in memory only, dedupe by place_id, `OPERATIONAL` filter, rating filter, website pre-filter, and only `place_refs`/`prospects` persisted.
6. Geo tiling plus niche synonyms. Pagination honouring ≤ 20 per page, ≤ 60 per query, short pages and inconsistent results. `includePureServiceAreaBusinesses`.
7. `PlaceInMemory` model (frozen, redacting `__repr__`), payload models with no Places fields, log redaction, CI denylist test (includes `rating`, `user_rating_count`).
8. `<GooglePlacesListingPanel>` (live `LISTING_DISPLAY` call, `no-store`) with `<GoogleMapsAttribution>` meeting the logo/text rules, in a visually separated container. The only view that shows a Google rating.
9. Google Cloud: only Places API (New) enabled; key restricted to it and to the VPS IP; per-day quotas below app caps (documented runbook step).
10. Yield measurement: record per-request returned-result counts and filter pass rates (counts only, no content) to replace the yield-model assumptions.

Acceptance
- Gate closed (default): every Places operation returns `COMPLIANCE_BLOCKED`, **with 0 HTTP requests made** (adapter call counter). A campaign with `minimum_google_rating` is rejected at confirmation.
- **Live, technical scope only (if you open `google_places_technical_testing`; capped at 3 requests):** a Text Search with `minRating=4.0` returns places, and an in-memory check confirms each returned `rating ≥ 4.0`. Returned-result counts are recorded. The DB contains only place_ids (verified by a query across every table for returned names, phones and rating values, which must find nothing). `api_usage` shows Enterprise units. No prospects are created.
- **Live cap test (same scope):** daily cap 2, request 3 → the third isn't sent.
- Without the key → `NOT_CONFIGURED`. With the gate closed → `COMPLIANCE_BLOCKED`. Neither creates rows.
- `manual_import`: a 20-row CSV creates prospects and flows into Phase 4 verification.
- Production Places discovery is **not** enabled by this phase. It needs an approved `google_places_prospect_selection` record.

## Phase 4: Website verification

Tasks
1. HTTP/DNS probe with the SSRF guard (tests: private IPs, redirect-to-private, rebinding), plus parked-page detection.
2. SerpApi adapter (query templates, hash-only storage of Places-derived queries, usage sync).
3. Directory/aggregator/social blocklist for "official site" detection, and the independent profile-link checker (≤ 3 profiles).
4. Candidate-domain DNS check (≤ 3 domains derived from the name).
5. Identity matcher (name, phone, address; name and phone only for service-area businesses) and quality signals (`modern`/`outdated`).
6. Availability state machine (ARCHITECTURE §3.5): `WEBSITE_LISTED` → `WEBSITE_WORKING` / `WEBSITE_BROKEN` (second probe ≥ 10 min) / `WEBSITE_UNKNOWN`; no listing → `NO_WEBSITE` only after all required checks complete, otherwise `WEBSITE_UNKNOWN(reason)`. `NO_WEBSITE_MIN_CONFIDENCE`.

Acceptance
- **Labelled multi-city dataset:** 60 real businesses across 3 metros and 4 niches (supplied through `manual_import`), labelled by hand with availability state and official URL. ≥ 90% state agreement; **0** `NO_WEBSITE` where a working official site is findable through the required checks; `WEBSITE_UNKNOWN` ≤ 15%.
- **Live (capped: SerpApi 5 searches, Firecrawl 3 credits):** manual-import prospects get assessments with evidence and checks. SerpApi provider-reported usage matches our ledger.
- SSRF suite passes.

## Phase 5: Research, independent sourcing, qualification

Tasks
1. Firecrawl adapter: robots.txt check, bounded crawl, credit caps, usage sync.
2. Independent identity sourcing for no-website prospects (directory, social and licence pages through SerpApi + single-page Firecrawl). Operator confirmation flow (`source_type = manual`).
3. Haiku extraction with exact quotes. A deterministic quote verifier marks facts `verified`. Conflict detection, with Opus escalation only when the rules can't resolve a conflict.
4. Qualification engine (35/20/15/10/10/10), versioned rules. Unknown inputs never get full points. No Places-derived inputs.
5. Lead detail: facts by status, sources, score breakdown, kept visually separate from the Google Places listing panel.

Acceptance
- **Live (capped: SerpApi 5, Firecrawl 5, Claude $0.50):** 3 real prospects get facts. Every `verified` fact's quote appears in its stored source text.
- Determinism: same inputs + rules → same score and hash.
- A prospect without an independently confirmed name and contact can't reach `qualified`.

## Phase 6: Website builder, QA, deployment

Tasks
1. `site-scaffold/`: Astro + Tailwind, a varied component library (several layout archetypes, hero, service and CTA variants), SEO helpers and JSON-LD `LocalBusiness` (from verified facts only), the noindex banner and `_headers`.
2. Strategy → `SiteSpec` (Sonnet 5.5). Diversity guard (Haiku 5.5 text check + deterministic structural checks). Opus re-plan after 2 rejections.
3. Generation (Sonnet 5.5) into allowed paths, with verified facts only.
4. Sandbox runner (SECURITY_AND_COMPLIANCE §4).
5. QA in the sandbox: build, desktop and mobile screenshots, nav and links, forms, `tel:`, console errors, assets, metadata and JSON-LD, axe-core, optional Lighthouse, factual-claim check (Haiku).
6. LangGraph repair loop: Sonnet repairs 1–2 → Opus repair 3 → `requires_human_review`. Per-site cost cap.
7. Deployment: Pages Direct Upload, branch per site, live verification (200, title, noindex header), expiry cleanup.
8. Websites view.

**Cross-niche diversity test dataset** (`tests/datasets/diversity/`):
- **4 campaigns × 3 prospects = 12 research profiles**, covering at least **6 niches** and **3 metros**:

| Campaign | Metro | Niches (one prospect each, plus one same-niche pair) |
|---|---|---|
| D1 | Houston, TX | plumbing ×2 (same-niche, same-city pair), roofing |
| D2 | Phoenix, AZ | HVAC, landscaping, dental clinic |
| D3 | Atlanta, GA | auto repair, hair salon, bakery |
| D4 | Denver, CO | electrician, pest control, plumbing (cross-city same-niche) |

- Profiles are frozen snapshots of **independently sourced facts only** (no Places content), produced by real Phase 5 research runs and reviewed by you. They're versioned so generation tests are repeatable.
- Metrics per site pair: layout archetype, section order (edit distance), palette distance (CIEDE2000), type pairing, DOM structure similarity, copy similarity (shingled Jaccard on visible text).
- Pass thresholds:
  - No two sites share archetype + palette + section order.
  - The same-niche pairs (Houston pair, Houston/Denver plumbers) still differ in at least 3 of the 5 structural and design dimensions.
  - Copy Jaccard < 0.25 for every pair.
  - Every site passes QA.
- Full run ≈ $10 (12 sites). A smaller 4-site subset runs at each builder change.

Acceptance
- **Live diversity run:** all 12 dataset sites generated, built, QA-passed and deployed to pages.dev, meeting the thresholds above. Costs recorded per site, each ≤ $2.00 and averaging ≈ $0.80. Previews deleted after review.
- **Live pilot:** 5 sites for 5 real qualified prospects from a real campaign (discovered through `manual_import` while the Places gate is closed), with zero console errors, zero serious or critical axe violations, mobile Lighthouse performance ≥ 90 (target), and every claim traced to a verified fact.
- Sandbox escape tests (network access, writes outside `/work`, fork bomb) fail safely.
- The expiry job deletes a real deployment (checked in the Cloudflare dashboard).

## Phase 7: Voice
Tasks: LiveKit agent (STT → `/commands` → TTS), barge-in, spoken confirmations, mic UI, short-lived tokens.
Acceptance:
- **Live:** the 40-command eval as recorded audio gets ≥ 90% intent accuracy, and voice creates tasks identical to text.
- Interruption stops TTS within 500 ms.
- LiveKit minutes appear in provider-reported usage.

## Phase 8: Outreach

Tasks
1. Draft generation (Sonnet 5.5), grounded in observations (fact IDs) and a verified preview URL. Drafts work with **no** email configuration.
2. The `sending_readiness` checks: postal address entry, type and attestation; DNS SPF/DKIM/DMARC checks; provider verification; Reply-To confirmation; unsubscribe round-trip.
3. Edge Worker: signed unsubscribe (with a KV buffer for when the API is down) and webhook relay.
4. Email adapter (Resend or SES), idempotent sends, `List-Unsubscribe` headers, compliance footer.
5. Reply classification (Haiku), automatic suppression, follow-up scheduler.
6. Outreach UI: drafts, checklist, authorisation, sends, replies, suppression.

Acceptance
- With no email configuration: drafts are created and editable, and the send action reports `NOT_CONFIGURED` with the failing checklist items.
- Each readiness item, removed one at a time, blocks sending (tested).
- **Live (only after the gate passes with your domain):** sends go only to inboxes you own. The one-click unsubscribe suppresses immediately. A forced crash between provider accept and commit produces no duplicate.
- With the API offline, an unsubscribe is still recorded through KV and drained on recovery.

## Phase 9: Hardening and operations
Tasks:
- Reporting views, with reconciliation of estimated vs reported usage.
- Restore drill.
- Encryption-key rotation script.
- Queue load test (1k tasks).
- Runbook covering: Supabase paused, DB restore, VPS rebuild, key leak, cap reached.
- Alerts: heartbeat loss, 80% of cap, backup failure, DB unhealthy.
- gVisor evaluation for the sandbox.

Acceptance:
- **Live:** a full restore into a fresh project.
- VPS rebuilt from scratch in under 1 hour following the runbook.
- All dashboard figures reconcile with SQL.

---

## Required tests (rating, website availability, compliance)

| # | Test | Type | Expected result |
|---|---|---|---|
| T1 | Minimum rating 3.5 | unit (parser + validator) + live technical (if scope opened) | parses to 3.5; request carries `minRating: 3.5`; every retained place has `rating ≥ 3.5` in memory |
| T2 | Minimum rating 4.0 | unit + live technical | as T1 with 4.0. "Dentists in Dallas with a minimum Google rating of 4.0" also asks for the missing target count |
| T3 | Missing ratings, policy `exclude` | unit with a fixture response containing unrated places | unrated places dropped and counted (count only). None reach `prospects` |
| T4 | Missing ratings, policy `include_for_review` | unit | request sent **without** `minRating`; unrated places kept with `review_reason = discovery_filter_unconfirmed`; can't reach `qualified` without review; no rating value stored |
| T5 | Missing policy not chosen | unit/API | confirm with a minimum and no policy → `VALIDATION_ERROR` |
| T6 | Invalid thresholds | unit/API | 3.7, 4.25, 0, 0.0, -1, 5.5, 6, "four-ish", "abc" → clarification or `VALIDATION_ERROR`; never rounded silently; never sent to Google |
| T7 | No default rating | unit | a command without a rating → `minimum_google_rating = null`, no `minRating` sent, `places.rating` not in the mask |
| T8 | No website listed, verification incomplete | unit + live (SerpApi quota forced to 0 mid-check) | state `WEBSITE_UNKNOWN(verification_incomplete)`, **not** `NO_WEBSITE`; doesn't satisfy a `NO_WEBSITE` filter |
| T9 | Listed website broken | integration (local test server returning 503 / NXDOMAIN domain / parked page) + live (known-dead domain) | first failure → second probe ≥ 10 min later → `WEBSITE_BROKEN(reason)`; a site that recovers on the second probe → `WEBSITE_WORKING` |
| T10 | Independently verified no-website | integration with recorded SERP and profile fixtures + live on a labelled case | all PR-3.3 checks `completed`, no site found, confidence ≥ threshold → `NO_WEBSITE` with evidence |
| T11 | Missing `websiteUri` alone | unit | a Places result without `websiteUri` and no further checks → never `NO_WEBSITE` |
| T12 | Compliance gate closed | unit + integration | every `google_places` operation (discovery, rating filter, listing panel) → `COMPLIANCE_BLOCKED`, 0 outbound requests; the campaign stays `draft` |
| T13 | Gate approval lifecycle | integration | approval with a mismatched `intended_use_text` → rejected; approval past `review_by` or revoked → gate closes immediately |
| T14 | No restricted Google data persisted | CI schema test + integration + live | denylist columns absent; after a full discovery run, a scan of every table, task payload, `task_events`, `api_usage`, logs and Storage for returned names, phones, `rating` and `userRatingCount` values finds nothing; the listing panel response has `Cache-Control: no-store` |
| T15 | Rating never used in scoring | unit | lead score identical with and without rating present in memory |
| T16 | Field mask → SKU | unit | `DISCOVERY_ENTERPRISE` (with and without rating) → Text Search Enterprise; `DISCOVERY_PRO` → Pro; `IDS_ONLY` → Essentials IDs Only; `LISTING_DISPLAY` → Place Details Enterprise |
| T17 | No other Google APIs | CI dependency/config test | no Maps JavaScript, Embed, Static, Routes, Geocoding or Autocomplete client or endpoint in the codebase; the only Google host is `places.googleapis.com` |

Live variants count toward the Definition of Done. Unit and fixture variants alone don't.

## Test strategy

| Layer | Tool | Counts toward Done? |
|---|---|---|
| Unit / property | pytest, hypothesis | required, not sufficient |
| DB integration | pytest + local Supabase | required, not sufficient |
| Contract (recorded fixtures, secrets scrubbed) | pytest | required, not sufficient |
| **Live capped** | manual `live-test` workflow, per-run caps (ENV §13), dedicated `live-test` campaign with its own budget, teardown deletes deployments | **required for Done** |
| Frontend | Vitest + React Testing Library | required |
| E2E | Playwright against a local stack with real DB | required |
| LLM evals | eval harness: commands, extraction faithfulness, reply classification, factuality | required where AI is involved |
| Diversity | multi-campaign dataset (Phase 6) | required for the builder |
| Generated-site QA | sandbox Playwright + axe (+ Lighthouse) | part of the product |

Rules:
- Test doubles live under `tests/` and are importable only when `JARVIS_ENV=test`.
- Live tests never run on every push. They need manual approval and can't exceed their caps (enforced by the same ToolGateway).
- Every live run records provider-reported usage next to our estimate.

## Deployment strategy
- `main` is always deployable. Short-lived branches, CI must be green.
- Dashboard: Cloudflare Pages from `main`. Edge Worker: `wrangler deploy` from CI.
- VPS: CI builds images → GHCR → SSH `docker compose pull && up -d`. Migrations run first and are additive. The worker drains on SIGTERM.
- Rollback: the previous image tag.

## Risks and mitigations

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| 1 | Google Maps Platform terms don't permit the Places-based prospect selection use | Med | High | Compliance gate (disabled until written approval), no content persisted, a swappable `DiscoveryProvider` (`manual_import` now, licensed data later) |
| 2 | Compliance design reduces data available about no-website prospects | High | Med | Independent sourcing plus operator confirmation. Budgeted extra SERP and Firecrawl calls |
| 3 | False "no website" labels | Med | High | Two checks, confidence scores, labelled multi-city accuracy gate |
| 4 | Templated-looking sites | Med | High | Strategy-first approach, diversity guard, multi-campaign dataset gate |
| 5 | Fabricated claims on sites | Med | High | Verified facts only, quote verification, factual QA |
| 6 | Supabase pause or data loss on Free | Med | High | Detection and alert, manual resume runbook, own backups, Pro upgrade path |
| 7 | Google charges despite caps (enforcement lag, misconfiguration) | Low | Med | App pre-call stop, Cloud quotas below app caps, alerts, key restrictions |
| 8 | Outreach can't send without a domain | High (today) | Med | Drafts work. Sending unlocks when you supply a domain |
| 9 | Deliverability or complaints | Med | High | Authentication, low volume, warm-up, immediate suppression |
| 10 | VPS single point of failure | Med | Med | Durable queue in Supabase, rebuild runbook, backups off the VPS |
| 11 | Sandbox escape | Low | High | No network, non-root, no Docker socket, static output. gVisor later |
| 12 | Prompt injection from crawled content | Med | Med | Data-only prompts, no dangerous tools, schema validation |
| 13 | Price or free-tier changes | Med | Med | Prices in config, caps below free tiers, `allow_paid=false` |
