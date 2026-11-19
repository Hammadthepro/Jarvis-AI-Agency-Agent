# JARVIS Changelog

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
- Implemented the full platform. See docs/IMPLEMENTATION_STATUS.md for verified status, test results, blockers and decisions that differ from revision 3 (structured-spec renderer, Postgres-checkpointed engine, VERIFIED_* website statuses, API-served unsubscribe, socket-proxy sandbox access).
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
