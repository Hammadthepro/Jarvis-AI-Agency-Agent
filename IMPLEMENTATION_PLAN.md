# JARVIS — Implementation Plan

Status: **Proposed, revision 2. No phase starts until the architecture review is approved.**

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
| 3 | Discovery (Places, compliant) | 1 | 3 d | $0 (free tier) |
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
- Command eval (40 commands, including ambiguous ones): ≥ 95% field accuracy; 100% of ambiguous cases ask instead of guessing.
- **Live:** the eval runs on Haiku 5.5 (capped at $0.50) and its cost appears in the ledger. The provider-reported Claude usage (from response `usage`) matches the token counts.
- A browser refresh mid-run loses no events.

## Phase 3: Discovery (Google Places, compliant)

Tasks
1. Places adapter with the fixed mask constants (`DISCOVERY_ENTERPRISE`, `DISCOVERY_PRO`, `IDS_ONLY`, `DETAILS_DISPLAY`, `DETAILS_CONTACT`) and a unit test of mask → SKU for each.
2. Discovery page task: Text Search, handled in memory only, dedupe by place_id, `OPERATIONAL` filter, and only `place_refs`/`prospects` persisted. It hands off to inline first-pass verification (Phase 4 plugs in here; in Phase 3 it records candidates as `discovered`).
3. Geo tiling plus niche synonyms. 20 results per page, 60 per query.
4. Payload models with no Places fields. Log redaction of Places bodies. CI denylist test now active.
5. Lead detail "Show Google Maps listing" (live `DETAILS_DISPLAY` call), with the `<GoogleMapsAttribution>` component meeting the logo/text rules, in a visually separated container.
6. Google Cloud quotas configured (documented runbook step). Cap alerts.

Acceptance
- **Live (capped at 3 requests):** "plumbing, Houston" returns real operational places. The DB contains only place_ids (verified by a query across every table for the returned names and phone numbers, which must find nothing). `api_usage` shows 3 Enterprise units.
- **Cap test, live:** set the daily cap to 2 and request 3 pages. The third request isn't sent (no increment in the Google Cloud console metrics the next day, plus our adapter counter).
- With the key removed, the result is `NOT_CONFIGURED` with no rows created.
- Attribution component passes the visual and snapshot tests.

## Phase 4: Website verification

Tasks
1. HTTP/DNS probe with the SSRF guard (tests: private IPs, redirect-to-private, rebinding).
2. SerpApi adapter (query templates, hash-only storage of Places-derived queries, usage sync from the account endpoint).
3. Directory/aggregator/social blocklist for "official site" detection.
4. Identity matcher and quality signals ("outdated").
5. Decision logic (ARCHITECTURE §3.5) inside the discovery page task. `website_audits` with evidence and confidence. Website URLs persisted only after our probe confirms identity.

Acceptance
- **Labelled multi-city dataset:** 60 real businesses across 3 metros and 4 niches, labelled by hand (status + official URL). ≥ 90% status agreement, and **0** `none_found` where a functioning official site is findable through the top-2 SERP queries.
- **Live (capped: Places 2 requests, SerpApi 5 searches):** an end-to-end page task produces audits with evidence. SerpApi provider-reported usage matches our ledger.
- SSRF suite passes.

## Phase 5: Research, independent sourcing, qualification

Tasks
1. Firecrawl adapter: robots.txt check, bounded crawl, credit caps, usage sync.
2. Independent identity sourcing for no-website prospects (directory, social and licence pages through SerpApi + single-page Firecrawl). Operator confirmation flow (`source_type = manual`).
3. Haiku extraction with exact quotes. A deterministic quote verifier marks facts `verified`. Conflict detection, with Opus escalation only when the rules can't resolve a conflict.
4. Qualification engine (35/20/15/10/10/10), versioned rules. Unknown inputs never get full points. No Places-derived inputs.
5. Lead detail: facts by status, sources, score breakdown, kept visually separate from the Google Maps panel.

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
- **Live pilot:** 5 sites for 5 real qualified prospects from a real campaign, with zero console errors, zero serious or critical axe violations, mobile Lighthouse performance ≥ 90 (target), and every claim traced to a verified fact.
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
| 1 | Google terms restrict Places use for lead generation | Med | High | No content persisted, a swappable discovery adapter, legal review |
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
