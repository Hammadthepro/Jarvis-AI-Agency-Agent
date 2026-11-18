# JARVIS — Database Schema

Status: **Proposed, revision 2.** PostgreSQL 15+ on Supabase. The migrations in
`database/migrations/` will be the source of truth. This document describes intent.

Revision 2: `places_cache` removed. Google-derived data is isolated in `place_refs`, which holds
place_id plus optional lat/lng that expires within 30 days. Added `usage_counters`,
`provider_usage_reports`, `sending_readiness`, `db_health_events` and `backup_runs`.

## 1. Conventions

- PK `id uuid default gen_random_uuid()`, except append-only logs, which use `bigint identity`.
- `created_at` / `updated_at timestamptz` everywhere (`set_updated_at` trigger).
- Soft delete (`deleted_at`) only on `campaigns`, `prospects`, `generated_sites`, `outreach_campaigns`.
- Postgres enums for every state (§4).
- Money in `bigint` micro-dollars (1 USD = 1,000,000).
- `owner_id uuid references auth.users(id)` on top-level rows. Children inherit ownership through `campaign_id`.
- **Provenance** on every independently sourced value: `source_type`, `source_ref`, `retrieved_at`, `retention_policy`.

## 2. Google Maps data boundary

| Rule | Enforcement |
|---|---|
| Place IDs may be stored indefinitely | `place_refs.place_id` |
| Lat/lng may be cached for at most 30 consecutive days | `place_refs.lat/lng` are nullable, with `coords_expires_at` checked as `≤ coords_fetched_at + 30 days`. A nightly job sets them to null. Default config: **don't store coordinates at all** (`PLACES_STORE_COORDINATES=false`) |
| Names, addresses, phone, website, hours, ratings, reviews and photos from Places are never stored | No columns exist for them on Google-linked tables. Task `payload` and `result` JSON for Places-calling tasks are validated by Pydantic models that contain no Places fields, and the write is rejected otherwise. Logs redact Places response bodies |
| Google-derived and independent data are kept separate | Google-derived data lives only in `place_refs`. Every business attribute lives in `research_facts` / `prospect_contacts` / `website_audits` with a non-Google `source_type`. `source_type = 'google_places'` is **not a valid value** in those tables (check constraint) |
| Regression protection | A CI schema test introspects tables linked to `place_refs` and fails on columns matching a denylist (`name`, `display_name`, `formatted_address`, `address`, `phone`, `website`, `rating`, `review`, `hours`, `photo`) unless they're on an allow-list of independently sourced tables |

## 3. Tables

### 3.1 Identity, configuration, limits

**users** (profile, 1:1 with `auth.users`)
| column | type | notes |
|---|---|---|
| id | uuid PK | = `auth.users.id` |
| display_name | text | |
| role | `user_role` | `owner`, `operator`, `viewer` |
| timezone | text | |

**system_settings** (one row per owner)
| column | type | notes |
|---|---|---|
| owner_id | uuid PK | |
| global_kill_switch | boolean | default false |
| provider_kill_switches | jsonb | per provider |
| daily_spend_cap_micros / monthly_spend_cap_micros | bigint | defaults $5 / $35 (see cost model §6.4) |
| max_concurrent_builds | int | default 1 |
| model_routing | jsonb | overrides of the defaults in ARCHITECTURE §4.1 |
| scoring_rule_set_id | uuid FK | active rule set |

**usage_limits** (configured caps; hard stops)
| column | type | notes |
|---|---|---|
| id, owner_id | | |
| provider | text | `google_places`, `serp_api`, `firecrawl`, `anthropic`, `cloudflare`, `email` |
| unit | text | e.g. `text_search_enterprise`, `text_search_pro`, `place_details_pro`, `place_details_enterprise`, `search`, `credit`, `usd_micros` |
| period | `limit_period` | `day`, `month`, `campaign` |
| cap | bigint | |
| free_allowance | bigint null | for display. A cap above this needs `allow_paid=true` |
| allow_paid | boolean | default false. Changing it is audited |
Unique `(owner_id, provider, unit, period)`.

**usage_counters** (the locked counter behind every reservation)
| column | type | notes |
|---|---|---|
| owner_id, provider, unit, period_start | composite PK | `period_start` = day or month start in UTC |
| reserved | bigint | units reserved by in-flight calls |
| consumed | bigint | units committed |
Reservation algorithm (one transaction): `select … for update` on every applicable counter (day, month, campaign) → if `consumed + reserved + n > cap` for any of them, return `BUDGET_BLOCKED` → otherwise `reserved += n`. On completion: `reserved -= n; consumed += actual`. On failure before the request is sent: `reserved -= n`. A sweeper releases reservations belonging to dead leases.

**scoring_rule_sets**: id, owner_id, version, rules jsonb, is_active (partial unique: one active per owner).

### 3.2 Commands, campaigns, tasks

**commands**: id, owner_id, channel (`text`/`voice`), raw_text, parsed_intent jsonb, status (`received`, `parsed`, `needs_clarification`, `awaiting_confirmation`, `executed`, `rejected`), campaign_id.

**campaigns**
| column | type | notes |
|---|---|---|
| id, owner_id, name | | |
| niche | text | |
| location | jsonb | city/state/country; geo tiles are derived at run time |
| target_count | int | 1–1000 |
| scope | `campaign_stage[]` | authorised stages |
| state / resume_state | `campaign_state` | |
| authorized_at / authorized_by | | |
| budget_cap_micros | bigint | default $5 |
| estimated_cost_micros | bigint | worst-case estimate shown at authorisation |
| kill_switch | boolean | |
| deleted_at | | |

**campaign_settings** (1:1): max_places_requests, max_serp_queries_per_prospect (2), max_crawl_pages_per_prospect (5), max_sites (5), max_repair_cycles (3), max_opus_escalations (2), research_top_n (20), min_qualification_score (60), daily_task_limit (500).

**agent_tasks** (queue and execution record)
| column | type | notes |
|---|---|---|
| id, campaign_id, parent_task_id | | |
| task_type, agent_role | | |
| subject_type / subject_id | | |
| status | `task_status` | |
| priority | smallint | |
| payload / result | jsonb | Pydantic-validated. **Must not contain Places content** |
| error_code / error_detail | | |
| attempts / max_attempts | int | check `attempts ≤ max_attempts` |
| idempotency_key | text unique | |
| run_after, leased_by, lease_expires_at, started_at, finished_at | | |
| estimated_cost_micros / actual_cost_micros | bigint | |
Indexes: partial claim index `(priority desc, run_after) where status in ('pending','retry_wait')`, `(campaign_id, status)`, `(lease_expires_at) where status in ('leased','running')`.

**task_events** (append-only, drives SSE): id bigint, task_id, campaign_id, event_type, message, data jsonb. 90-day retention.

**worker_heartbeats**: worker_id PK, started_at, last_seen_at, version, current_task_id, db_state.

### 3.3 Google-derived reference (the only Google table)

**place_refs**
| column | type | notes |
|---|---|---|
| place_id | text PK | stored indefinitely (permitted) |
| owner_id | uuid | |
| first_seen_at | timestamptz | |
| last_seen_at | timestamptz | when Text Search last returned it |
| lat / lng | double precision null | only if `PLACES_STORE_COORDINATES=true` |
| coords_fetched_at / coords_expires_at | timestamptz null | check `coords_expires_at <= coords_fetched_at + interval '30 days'`. Purged nightly |
| last_sku | text | billing SKU of the last fetch (metadata, not content) |

### 3.4 Prospects and independent evidence

**prospects**
| column | type | notes |
|---|---|---|
| id, campaign_id | | |
| place_id | text FK place_refs | |
| state | `prospect_state` | |
| business_category | text | **our** taxonomy, derived from the campaign niche, not Places types |
| rejection_reason | text | |
| deleted_at | | |
Unique `(campaign_id, place_id)`. Cross-campaign dedupe: unique `(owner_id, place_id)` through `place_refs`.
The display name and contact details shown in the dashboard come from `research_facts` (independent). Where none are verified yet, the UI shows "Unverified" plus an on-demand **"Show Google Maps listing"** action (live Place Details Pro call, attributed, never stored).

**prospect_sources** (where independent evidence came from)
| column | type | notes |
|---|---|---|
| id, prospect_id | | |
| source_type | `source_type` | `serp_result`, `official_website`, `firecrawl_page`, `directory_page`, `social_page`, `licence_register`, `dns`, `http_probe`, `manual`. **`google_places` is not allowed** |
| source_ref | text | URL / SerpApi search ID |
| retrieved_at | timestamptz | |
| retention_policy | `retention_policy` | `indefinite`, `days_180`, `days_90`, `purge_on_reject` |
| raw_storage_path | text null | raw page in Storage when retained |

**serp_queries**
| column | type | notes |
|---|---|---|
| id, task_id, prospect_id | | |
| query_template | text | e.g. `name_city_state`, `phone` |
| query_hash | text | sha256 of the query string. **The literal string isn't stored when it contains Places-derived terms** |
| engine, location, gl, hl | text | |
| serpapi_search_id | text | lets us retrieve the search from SerpApi if needed |
| searched_at, estimated_cost_micros | | |

**serp_results**: id, serp_query_id, position, title, url, displayed_link, snippet, result_type. These are SerpApi results (independent of Places), stored as **evidence, never as verified facts**. 180-day retention.

**prospect_contacts**: id, prospect_id, contact_type (`email`, `phone`, `contact_form`, `social`), value, source_id (FK prospect_sources, required), is_public_business_contact, verification `fact_status`, confidence. Unique `(prospect_id, contact_type, value)`. Only `verified` + `is_public_business_contact` contacts can be used for outreach.

**website_audits**
| column | type | notes |
|---|---|---|
| id, prospect_id, task_id | | |
| status | `website_status` | `functioning`, `broken`, `outdated`, `none_found`, `unverified` |
| url / final_url | text null | persisted only when our own probe confirms identity (`source_type = http_probe`). A Places `websiteUri` that fails the identity check isn't stored |
| http_status, redirect_chain, tls_valid, dns_resolves | | |
| identity_match_score | numeric(3,2) | |
| quality_signals | jsonb | |
| methods | `verification_method[]` | e.g. `{maps_hint_probe, serp_search, dns, http, firecrawl_identity}`. `maps_hint_probe` records *that* a Places hint was used, not its value |
| evidence | jsonb | `[{source_id, observation}]` |
| confidence | numeric(3,2) | |
| checked_at | timestamptz | |

**research_facts**
| column | type | notes |
|---|---|---|
| id, prospect_id, task_id | | |
| fact_key | text | `business_name`, `phone`, `service`, `service_area`, `licence_number`, `brand_color`, … |
| value | jsonb | |
| status | `fact_status` | `verified`, `inferred`, `unknown`, `conflicting` |
| source_id | uuid FK prospect_sources | required unless `unknown` |
| quote | text | exact supporting text from the fetched source |
| confidence | numeric(3,2) | |
| conflicts_with | uuid[] | |
Check: `status <> 'verified' or (source_id is not null and quote is not null)`.

**lead_scores**: id, prospect_id, rule_set_id, total (0–100), components jsonb (`[{rule, max, awarded, explanation, evidence_ids}]`), inputs_hash, computed_at. Unique `(prospect_id, rule_set_id, inputs_hash)`. Inputs come only from independent facts and audits.

### 3.5 Websites

**generated_sites**: id, prospect_id, campaign_id, slug (unique, unguessable), state `site_state`, current_version_id, strategy jsonb, opus_escalations smallint, cost_micros, deleted_at.

**site_versions**: id, site_id, version, parent_version_id, source_storage_path, source_sha256, spec jsonb, facts_used uuid[] (all must be `verified`), model, tokens_in, tokens_out, cache_read_tokens, estimated_cost_micros, reason (`initial`, `repair`, `escalation`, `manual`). Unique `(site_id, version)`.

**site_builds**: id, site_version_id, status, sandbox_image_digest, started_at, finished_at, duration_ms, log_storage_path, artefact_storage_path, artefact_sha256, exit_code.

**qa_reports**: id, site_build_id, passed, checks jsonb, console_errors, broken_links, a11y_violations, lighthouse jsonb, factual_check jsonb, diversity jsonb (similarity scores against other sites), repair_instructions.

**deployments**: id, site_build_id, provider, project_name, branch, cf_deployment_id, url, status, verified_at, verification jsonb, expires_at, deleted_at_provider.

### 3.6 Outreach

**sending_readiness** (one row per owner; recomputed on change and before every send)
| column | type | notes |
|---|---|---|
| owner_id | uuid PK | |
| postal_address | text | legitimate address: current street address, a USPS-registered PO Box, or a private mailbox registered with a CMRA |
| postal_address_type | `postal_address_type` | `street`, `po_box`, `cmra_mailbox` |
| postal_address_attested_at / by | | the owner attests the address is current and registered |
| sending_domain | text null | |
| spf_ok / dkim_ok / dmarc_ok | boolean | from a DNS check |
| domain_verified_by_provider | boolean | from the email provider API |
| dns_checked_at | timestamptz | stale after 24 h → re-check before sending |
| from_address / reply_to | text | must be on `sending_domain`. `pages.dev`, `workers.dev` and free-mail domains are rejected |
| unsubscribe_endpoint_ok | boolean | live check of the edge Worker |
| provider_configured | boolean | |
| ready | boolean generated | `true` only when every required item passes |

**outreach_campaigns**: id, campaign_id, owner_id, state, authorized_at/by, daily_send_limit (20), total_send_limit, follow_up_policy jsonb, send_window jsonb, deleted_at. **Authorising requires `sending_readiness.ready`**. Creating one, and generating drafts, doesn't.

**outreach_messages**: id, outreach_campaign_id (nullable for standalone drafts), prospect_id, contact_id (nullable while drafting), deployment_id, state, sequence_step, subject, body_text, body_html, observations jsonb (fact ids), provider_message_id, idempotency_key unique, scheduled_for, sent_at.

**outreach_replies**: id, outreach_message_id, received_at, from_address, raw_storage_path, classification `reply_class`, classifier_confidence, handled.

**suppression_list**: id, owner_id, kind (`email`, `domain`, `place_id`, `phone`), value_hash, reason, source_message_id, created_at. Unique `(owner_id, kind, value_hash)`. Never purged.

### 3.7 Cost, usage, health, audit

**api_usage** (our estimate for each call)
| id bigint, task_id, campaign_id, provider, operation, sku, model, request_count, tokens_in, tokens_out, cache_read_tokens, units jsonb, status, http_status, latency_ms, **estimated_cost_micros**, pricing_version, occurred_at |

**provider_usage_reports** (what the provider says; never overwritten with estimates)
| column | type | notes |
|---|---|---|
| id, owner_id, provider | | |
| period_start / period_end | timestamptz | |
| unit | text | |
| reported_units | bigint null | |
| reported_cost_micros | bigint null | |
| source | `usage_report_source` | `provider_api`, `manual_entry`, `billing_export` |
| fetched_at | timestamptz | |
| raw | jsonb | |
View `v_usage_reconciliation`: estimated vs reported per provider/period, with the variance and staleness.

**budget_ledger**: id bigint, owner_id, campaign_id, task_id, entry_type (`reserve`, `commit`, `release`, `adjustment`), amount_micros, provider, api_usage_id, created_at.

**db_health_events**: id bigint, component (`api`, `worker`), state (`healthy`, `degraded`, `unreachable`, `paused_suspected`), detail, occurred_at.

**backup_runs**: id, started_at, finished_at, status, size_bytes, sha256, destination, restore_tested_at.

**audit_events** (append-only): id bigint, owner_id, actor_type, actor_id, action, entity_type, entity_id, from_state, to_state, reason, data, occurred_at. Kept indefinitely.

## 4. Enums

`campaign_state`: draft, authorized, queued, discovering, verifying, researching, qualifying, building, qa, preview_deployed, outreach_authorized, completed, paused, cancelled, failed, requires_human_review
`prospect_state`: discovered, identity_verified, website_verified, researched, qualified, rejected, requires_human_review
`site_state`: queued, strategy, generating, building, qa, repair, passed, deployed, failed, requires_human_review, expired, archived
`outreach_state`: draft, authorized, queued, sent, replied, follow_up, closed, suppressed, bounced, opted_out, failed
`task_status`: pending, leased, running, retry_wait, succeeded, failed, cancelled, blocked
`website_status`: functioning, broken, outdated, none_found, unverified
`fact_status`: verified, inferred, unknown, conflicting
`source_type`: serp_result, official_website, firecrawl_page, directory_page, social_page, licence_register, dns, http_probe, manual
`retention_policy`: indefinite, days_180, days_90, purge_on_reject
`provider_status`: ok, not_configured, error, quota_exceeded, rate_limited, budget_blocked, disabled
`limit_period`: day, month, campaign
`usage_report_source`: provider_api, manual_entry, billing_export
`postal_address_type`: street, po_box, cmra_mailbox

## 5. Relationships

```
users 1─* campaigns 1─1 campaign_settings
campaigns 1─* agent_tasks 1─* task_events
place_refs 1─* prospects (one per campaign)
prospects 1─* prospect_sources / prospect_contacts / website_audits / research_facts / lead_scores / serp_queries
prospects 1─* generated_sites 1─* site_versions 1─* site_builds 1─1 qa_reports, 1─* deployments
campaigns 1─* outreach_campaigns 1─* outreach_messages 1─* outreach_replies
agent_tasks 1─* api_usage 1─* budget_ledger;  usage_limits ↔ usage_counters
```

## 6. Row Level Security

RLS is enabled on every table, with no default access.
- `service_role` (API and worker, server-only key): bypasses RLS.
- `authenticated`: `select` only, on rows owned through `owner_id` or `campaigns.owner_id`. No direct writes. All writes go through the API, so policy can't be bypassed.
- Service role only (no `authenticated` policy): `place_refs`, `usage_counters`, `budget_ledger`, `api_usage`, `provider_usage_reports`, `suppression_list`, `sending_readiness`.
- `anon`: nothing.

## 7. Retention

| Data | Retention |
|---|---|
| `place_refs.place_id` | indefinite (permitted) |
| `place_refs.lat/lng` | ≤ 30 days, off by default |
| Any other Places content | **never persisted** |
| SERP queries/results | 180 days |
| Raw fetched pages (Storage) | 90 days. Extracted facts keep their quote and URL |
| Facts for rejected prospects | 30 days after rejection |
| Task events | 90 days |
| Site artefacts | last 2 builds per site. Deleted on preview expiry + 30 days |
| Outreach messages and replies | 3 years |
| Suppression list, audit events | indefinite |
| Backups | 7 daily + 4 weekly |

## 8. Backup and recovery

- **Supabase Free provides no managed backups and no PITR** (check the current plan page). We don't rely on Supabase for recovery.
- Daily `pg_dump --format=custom` from the VPS backup container → encrypted (age) → stored on the VPS and copied off-site (recommended: Cloudflare R2 free tier). Each run is recorded in `backup_runs`.
- A monthly restore test into a scratch database, recorded in `backup_runs.restore_tested_at`.
- Storage objects are reproducible from `site_versions` (sources) and rebuildable. Raw pages are disposable.
