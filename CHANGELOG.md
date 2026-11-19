# JARVIS Changelog

## Revision 8: reliable development startup, Codespaces, no sign-in during development (2026-11-19)
- **One database configuration:** every process reads the repository-root `.env`; the default `DATABASE_URL` matches `docker-compose.dev.yml` (previously the code defaulted to a different database with no password, causing password failures and missing tables when a terminal lacked the export). Empty `.env` values mean "not set". `postgresql+asyncpg://` URLs are accepted. Production requires an explicit `DATABASE_URL`.
- **Migrations gate startup:** `/ready` reports pending or modified migrations (503) and the worker refuses to start until they are applied. The migrator has a `status` command and clear errors for wrong passwords, missing databases and failed migrations.
- **One-command startup:** `scripts/dev-up.sh`, `dev-status.sh`, `dev-down.sh` (PID and log tracking, health checks, no duplicate processes, lock against concurrent runs, never deletes data). `.devcontainer/` starts everything on every Codespace start and fast-forwards to `origin/main` when safe. Postgres health check in `docker-compose.dev.yml`.
- **No sign-in during development:** the login page is removed. An explicit local development access mode (`JARVIS_ENV=local` + `JARVIS_LOCAL_DEV_ACCESS=true`) acts as one local operator; settings refuse it in any other environment.
- **Dashboard to API:** the browser calls `/api` on the dashboard's own origin (Vite proxy), so Codespaces never needs the API's address or a public API port.
- Tests: `test_dev_environment.py` and `scripts/tests/dev-startup-test.sh` (real Docker startup workflow).

## Revision 7: Integrations Center, voice agent and Command Center (2026-11-19)
- **Integrations Center:** provider credentials can be saved, tested, disabled and removed from the dashboard by deployment administrators. Stored AES-256-GCM encrypted (`JARVIS_ENCRYPTION_KEY`, previously unused); fails closed without a key; never returned, logged or echoed. Explicit states including NOT_TESTED, PERMISSION_DENIED and CONNECTION_FAILED. Changes reach every API instance and worker. Migration 0007.
- **Voice:** one conversation router for voice and text (clarification answers, confirm, cancel, status, new commands). The agent heartbeats, narrates real progress, accepts interrupt and end controls from the session owner only, and can fetch LiveKit credentials saved in the dashboard. Voice is OPERATIONAL only after LiveKit validation and a live agent heartbeat. Tokens: 10 minutes, microphone only. Migration 0008.
- **Dashboard:** new navigation, Command Center (attention items, active work, integration health, spend), Integrations, Voice Agent and Usage & Costs pages.
- **Fixes found by live checks:** Resend rejects keys with HTTP 400, now reported as INVALID_CREDENTIALS; provider errors show the provider's own message.
- **Parser:** "prepare two redesign previews" and similar phrasings now enable the build, QA and deploy stages.

## Revision 6: consistency and data-integrity audit (2026-11-19)
- **Docker architecture docs:** every document now states that only `sandbox-runner` mounts the Docker socket. A static regression test fails if docs, scripts, CI or config describe the removed socket proxy as current.
- **Evidence retention integrity:** migration 0005 adds content-free `evidence_tombstones` (trigger on every evidence delete), a `dangling_evidence_refs` integrity view (recorded on each retention run) and a backfill. Verification references resolve as live or expired.
- **Usage accounting:** migration 0006 adds `cost_basis`. Anthropic account-level usage is `unavailable`; JARVIS costs are labelled `local_estimate`; `/usage` reports `invoice_status` (`UNAVAILABLE` unless a billing export is imported). Constraints refuse mislabelled costs.
- **Backups:** the restore drill fails on a missing checksum or inconsistent row counts; a bucket on the server's own public IP is not independent. R2 is documented as unverified; status stays `BACKUP_NOT_PROTECTED`.
- **Bug fix:** concurrent first requests from a new user could create two workspaces (commands then 404'd). Now exactly one.
- **Status accuracy:** acceptance criteria split into implemented / test-verified / live-verified / deployed. Criteria previously shown as fully green without live verification (for example 19) corrected.

## Revision 5: hardening before live provider testing (2026-11-19)
- **Shared rate limiting:** Postgres token buckets for the API (IP, user, commands) and provider throughput, across all instances. The in-process limiter was removed.
- **Sandbox:** a dedicated sandbox runner (the only Docker-socket holder, with one fixed operation) replaces the generic socket proxy, which allowed privileged container creation. Images are pinned by digest; `--pull=never`; production requires the runner mode and a pinned image. Base image pinned by digest.
- **Retention:** one scheduled module covering all rules, including 180-day SERP cleanup, legal holds, protected record types, and run logs.
- **Provider usage reconciliation:** scheduled hourly, account-level reports with explicit statuses. Unknown is never shown as zero. `/usage` separates estimated, reserved, committed, ambiguous, released and provider-reported.
- **Scheduler fix:** the advisory lock is now taken and released on the same connection (previously it could leak across pooled connections). Per-job timing is stored in `scheduled_jobs`.
- **Repair loop:** dedicated tests for all stages and the hard stop.
- **Voice:** sessions bound to the authenticated participant, explicit close-on-disconnect, closed HTTP client, non-deprecated interruption config, offline tests, and a credentialed live test.
- **Backups:** boto3 off-site upload with checksum verification, a restore drill from the off-site copy, and `PROTECTED` only for an independent destination with a recent drill.
- **Production config:** least-privilege per-service env files, read-only containers with dropped capabilities, migrate-first startup, worker heartbeat healthcheck, and deploy retry that never re-uploads.
- **Deploy script:** `rsync --delete` no longer deletes the server's `env/*.env` secrets.
- Migrations 0003 and 0004 (additive; existing records preserved).

## Revision 4: implementation (2026-11-18)
- Implemented the full platform. See docs/IMPLEMENTATION_STATUS.md for verified status, test results, blockers and decisions that differ from revision 3 (structured-spec renderer, Postgres-checkpointed engine, VERIFIED_* website statuses, API-served unsubscribe, and a generic Docker socket proxy for sandbox access, which revision 5 removed and replaced with the dedicated sandbox runner).
- New docs: docs/IMPLEMENTATION_STATUS.md, docs/OPERATIONS.md. README rewritten with run instructions.

## Revision 3 (2026-11-18)

**Product requirements (new document):** `docs/PRODUCT_REQUIREMENTS.md`
- PR-2, dynamic Google rating filter: optional `minimum_google_rating`, no default, 0.5 steps with no silent rounding, mapped to Text Search `minRating`, re-checked in memory, explicit missing-rating policy (`exclude` / `include_for_review`). Ratings and review counts are never persisted and are shown only in the attributed Google Places listing panel.
- PR-3, evidence-based website availability: `WEBSITE_LISTED`, `WEBSITE_WORKING`, `WEBSITE_BROKEN`, `NO_WEBSITE`, `WEBSITE_UNKNOWN`. A missing `websiteUri` never means `NO_WEBSITE`. `NO_WEBSITE` requires completed independent checks.
- PR-4, Google Places compliance gate: the intended use is stated precisely. Places discovery and qualification stay disabled until Google or qualified counsel confirms that use in writing. `DiscoveryProvider` is replaceable (`manual_import` now, licensed data later). SerpApi's Maps engines are no longer offered as a workaround.

**API contracts (new document):** `docs/API_CONTRACTS.md`. Error codes (`COMPLIANCE_BLOCKED`, …), `CampaignParameters`, command-parsing fixtures, `WebsiteAssessment`, the listing-panel response, compliance-approval endpoints, the Places adapter, `PlaceInMemory`, and `DiscoveryProvider`.

**Google API scope and naming:**
- Google Places API (New) is the only Google Maps Platform API used. Text Search (`places:searchText`) is primary; Place Details serves only the listing panel.
- Maps JavaScript, Embed, Static, Routes, Geocoding, Autocomplete and Nearby Search are explicitly excluded (ARCHITECTURE §5.0).
- Terminology standardised: "Google Maps Platform" for terms, billing and quotas; "Google Places API (New)" for the integration; "Google Maps Content" for the data. The listing panel was renamed "Google Places listing" (its attribution text stays "Google Maps", as Google requires).

**Field masks and costs:**
- Re-verified per-field SKUs: `rating`, `userRatingCount` and `websiteUri` are Enterprise; `displayName` and `businessStatus` are Pro; `attributions` is Essentials (IDs Only).
- `attributions` added to every mask.
- `DETAILS_DISPLAY` and `DETAILS_CONTACT` replaced by `LISTING_DISPLAY` (Place Details Enterprise).
- Cost model now uses a yield model based on confirmed pagination (≤ 20 per page, ≤ 60 per query, short pages, inconsistent results).
- `NO_WEBSITE` campaigns need about 80–106 requests and about 375 SERP searches per 100 prospects. The SerpApi free tier covers about 65 confirmed no-website prospects a month.
- Scenarios recalculated. The mandatory $45 budget is unchanged.

**Files changed:** ARCHITECTURE.md, IMPLEMENTATION_PLAN.md, ENVIRONMENT_VARIABLES.md, README.md, .env.example, docs/API_COST_MODEL.md, docs/DATABASE_SCHEMA.md, docs/SECURITY_AND_COMPLIANCE.md, docs/SYSTEM_DIAGRAM.md. New: docs/PRODUCT_REQUIREMENTS.md, docs/API_CONTRACTS.md, docs/VERIFICATION_REGISTER.md, CHANGELOG.md.

## Revision 2 (2026-11-18)
- Places content no longer persisted (`places_cache` removed; Place IDs only).
- VPS made a required recurring cost.
- Model routing changed to Haiku / Sonnet, with Opus for escalation only.
- App-level billing hard stops added, with estimated vs reported usage.
- Supabase resilience and own backups added.
- Outreach sending-readiness gate added.
- Costs split into mandatory and optional.
- Capped live tests and a multi-campaign diversity dataset added.

## Revision 1 (2026-11-18)
Initial architecture documents.
