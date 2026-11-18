# JARVIS — Database Schema

Status: **Proposed.** PostgreSQL 15+ on Supabase. Migrations will live in
`database/migrations/` (Supabase CLI) and are the source of truth; this document describes intent.

## 1. Conventions

- PK: `id uuid primary key default gen_random_uuid()`.
- Every table: `created_at timestamptz not null default now()`, `updated_at timestamptz not null default now()` (trigger `set_updated_at`).
- Soft delete (`deleted_at timestamptz`) only on: `campaigns`, `prospects`, `generated_sites`, `outreach_campaigns`. Everything else is either append-only or hard-deleted by retention jobs.
- Enums as Postgres `enum` types (listed in §4) so invalid states cannot be written.
- Money: `bigint` **micro-dollars** (`1 USD = 1_000_000`). Never floats.
- Ownership: every top-level row has `owner_id uuid references auth.users(id)`; child rows inherit ownership through their campaign. RLS policies key off this.
- Provenance columns on any externally sourced value: `source_type`, `source_ref`, `retrieved_at`, `retention_policy`.

## 2. Tables

### 2.1 Identity & configuration

**users** (profile, 1:1 with `auth.users`)
| column | type | notes |
|---|---|---|
| id | uuid PK | = `auth.users.id` |
| display_name | text | |
| role | `user_role` | `owner` \| `operator` \| `viewer` |
| agency_name | text | used in outreach footer |
| agency_postal_address | text | required before any outreach send (CAN-SPAM) |
| timezone | text | default `America/Chicago` |

**system_settings** (single row per owner)
| column | type | notes |
|---|---|---|
| owner_id | uuid PK FK users | |
| global_kill_switch | boolean | default `false`; when true worker claims nothing |
| daily_spend_cap_micros | bigint | default 5_000_000 ($5) |
| monthly_spend_cap_micros | bigint | default 50_000_000 ($50) |
| max_concurrent_builds | int | default 1 |
| provider_kill_switches | jsonb | `{"serp_api": false, ...}` |
| default_models | jsonb | model routing overrides |
| scoring_rule_set_id | uuid FK scoring_rule_sets | active rule set |

**scoring_rule_sets**
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| owner_id | uuid FK | |
| version | int | unique per owner |
| rules | jsonb | weights + rule params, validated by Pydantic |
| is_active | boolean | partial unique index: one active per owner |

### 2.2 Commands, campaigns, tasks

**commands**
| column | type | notes |
|---|---|---|
| id, owner_id | | |
| channel | `command_channel` | `text` \| `voice` |
| raw_text | text | |
| parsed_intent | jsonb | validated `CommandIntent` |
| status | `command_status` | `received` → `parsed` → `needs_clarification` / `awaiting_confirmation` → `executed` / `rejected` |
| campaign_id | uuid FK null | |

**campaigns**
| column | type | notes |
|---|---|---|
| id, owner_id | | |
| name | text | |
| niche | text | e.g. `plumbing` |
| location | jsonb | `{city, state, country, lat, lng, radius_m}` |
| target_count | int | check > 0 and ≤ 1000 |
| scope | `campaign_stage[]` | which stages are authorised |
| state | `campaign_state` | |
| resume_state | `campaign_state` null | for pause/review |
| authorized_at, authorized_by | | |
| budget_cap_micros | bigint | hard per-campaign cap |
| spent_micros | bigint | denormalised, maintained by ledger trigger |
| kill_switch | boolean | |
| deleted_at | | |
Indexes: `(owner_id, state)`, `(owner_id, created_at desc)`.

**campaign_settings** (1:1 campaign)
| column | type | notes |
|---|---|---|
| campaign_id | uuid PK FK | |
| max_places_queries | int | default 10 |
| max_serp_queries_per_prospect | int | default 2 |
| max_crawl_pages_per_prospect | int | default 5 |
| max_sites | int | default 5 |
| max_repair_cycles | int | default 3 |
| research_top_n | int | default 20 |
| min_qualification_score | int | default 60 |
| daily_task_limit | int | default 500 |

**agent_tasks** (queue + execution record)
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| campaign_id | uuid FK null | |
| parent_task_id | uuid FK null | task tree |
| task_type | text | e.g. `discovery.search`, `verify.prospect`, `site.build` |
| agent_role | `agent_role` | |
| subject_type / subject_id | text / uuid | e.g. `prospect` / id |
| status | `task_status` | |
| priority | smallint | |
| payload | jsonb | validated input |
| result | jsonb | validated output |
| error_code / error_detail | text / jsonb | mapped error |
| attempts / max_attempts | int | check attempts ≤ max_attempts |
| idempotency_key | text | **unique** |
| run_after | timestamptz | for backoff/scheduling |
| leased_by / lease_expires_at | text / timestamptz | |
| started_at / finished_at | timestamptz | |
| cost_micros | bigint | sum of ledger commits |
Indexes: partial `(priority desc, run_after) where status in ('pending','retry_wait')` (claim path), `(campaign_id, status)`, `(lease_expires_at) where status in ('leased','running')`.

Claim query:
```sql
update agent_tasks set status='leased', leased_by=$1, lease_expires_at=now()+interval '5 min', attempts=attempts+1
where id = (select id from agent_tasks
            where status in ('pending','retry_wait') and run_after <= now()
            order by priority desc, run_after
            for update skip locked limit 1)
returning *;
```

**task_events** (append-only, drives SSE)
| column | type | notes |
|---|---|---|
| id | bigint identity PK | monotonic for SSE `Last-Event-ID` |
| task_id, campaign_id | uuid | |
| event_type | text | `state_changed`, `progress`, `tool_call`, `error`, `log` |
| message | text | human-readable |
| data | jsonb | |
Index: `(campaign_id, id)`, `(task_id, id)`. Retention: 90 days.

**worker_heartbeats**
| worker_id text PK | started_at | last_seen_at | version | current_task_id |

### 2.3 Prospects & evidence

**prospects**
| column | type | notes |
|---|---|---|
| id, campaign_id | | |
| place_id | text | Google Place ID (retention: indefinite, per Places terms) |
| state | `prospect_state` | |
| canonical_name | text | **independently verified** name (null until verified) |
| normalized_phone | text | E.164, independently verified or null |
| business_category | text | our taxonomy |
| rejection_reason | text | |
| deleted_at | | |
Unique: `(campaign_id, place_id)`. Index: `(campaign_id, state)`, `(normalized_phone)`.

Dedup across campaigns: unique `(owner_id, place_id)` on a lightweight `prospect_identities`
view/materialised table so the same business is not prospected twice by two campaigns.

**places_cache** (Google Places content, transient)
| column | type | notes |
|---|---|---|
| place_id | text PK | |
| payload | jsonb | field-masked Places response |
| field_mask | text | |
| sku | text | billing SKU used |
| fetched_at | timestamptz | |
| expires_at | timestamptz | default `fetched_at + 30 days` (configurable lower; see compliance doc) |
Purged nightly where `expires_at < now()`. Never joined into exports. Displayed with Google attribution.

**prospect_sources** (every place we learned something about a prospect)
| column | type | notes |
|---|---|---|
| id, prospect_id | | |
| source_type | `source_type` | `google_places`, `serp_result`, `official_website`, `firecrawl`, `dns`, `http_probe`, `manual` |
| source_ref | text | URL, place_id, query id |
| retrieved_at | timestamptz | |
| retention_policy | `retention_policy` | `indefinite`, `cache_30d`, `transient`, `purge_on_reject` |
| raw_ref | text | Storage path for raw payload when retained |

**serp_queries** / **serp_results**
| serp_queries: id, task_id, prospect_id, query, engine, location, gl, hl, params jsonb, searched_at, serpapi_search_id, cost_micros |
| serp_results: id, serp_query_id, position int, title, url, displayed_link, snippet, result_type |
Index `serp_results(serp_query_id, position)`. Snippets are stored as *evidence*, never as facts.

**prospect_contacts**
| column | type | notes |
|---|---|---|
| id, prospect_id | | |
| contact_type | `contact_type` | `email`, `phone`, `contact_form`, `social` |
| value | text | |
| source_id | uuid FK prospect_sources | required |
| is_public_business_contact | boolean | only these may be used for outreach |
| verification | `fact_status` | |
| confidence | numeric(3,2) | 0–1 |
Unique `(prospect_id, contact_type, value)`.

**website_audits**
| column | type | notes |
|---|---|---|
| id, prospect_id, task_id | | |
| status | `website_status` | `functioning`, `broken`, `outdated`, `none_found`, `unverified` |
| url / final_url | text | |
| http_status, redirect_chain jsonb, tls_valid, dns_resolves | | |
| identity_match_score | numeric(3,2) | |
| quality_signals | jsonb | mobile viewport, https, copyright year, page weight, CMS fingerprint |
| methods | `verification_method[]` | |
| evidence | jsonb | list of `{source_id, observation}` |
| confidence | numeric(3,2) | |
| checked_at | timestamptz | |
Index `(prospect_id, checked_at desc)`; latest audit via `distinct on`.

**research_facts**
| column | type | notes |
|---|---|---|
| id, prospect_id, task_id | | |
| fact_key | text | e.g. `service`, `service_area`, `years_in_business`, `license_number`, `brand_color` |
| value | jsonb | |
| status | `fact_status` | `verified`, `inferred`, `unknown`, `conflicting` |
| source_id | uuid FK prospect_sources | required unless status=`unknown` |
| quote | text | exact supporting text snippet (for verification) |
| confidence | numeric(3,2) | |
| conflicts_with | uuid[] | other fact ids |
Index `(prospect_id, fact_key)`. Check: `status <> 'verified' or source_id is not null`.

**lead_scores**
| column | type | notes |
|---|---|---|
| id, prospect_id | | |
| rule_set_id | uuid FK | reproducibility |
| total | smallint | 0–100 |
| components | jsonb | `[{rule, max, awarded, explanation, evidence_ids}]` |
| inputs_hash | text | hash of facts used; same hash + rule set ⇒ same score |
| computed_at | timestamptz | |
Unique `(prospect_id, rule_set_id, inputs_hash)`. Index `(total desc)`.

### 2.4 Websites

**generated_sites**
| id, prospect_id, campaign_id, slug (unique), state `site_state`, current_version_id, strategy jsonb (audience, positioning, conversion plan, design direction), deleted_at |

**site_versions**
| id, site_id, version int, parent_version_id, source_storage_path, source_sha256, spec jsonb, facts_used uuid[] (must all be `verified`), generated_by_model, tokens_in, tokens_out, cost_micros, reason (`initial`/`repair`/`manual`) |
Unique `(site_id, version)`.

**site_builds**
| id, site_version_id, status `build_status`, sandbox_image_digest, started_at, finished_at, duration_ms, log_storage_path, artefact_storage_path, artefact_sha256, exit_code |

**qa_reports**
| id, site_build_id, passed boolean, checks jsonb (`[{check, viewport, passed, details}]`), console_errors int, broken_links int, a11y_violations int, lighthouse jsonb null, factual_check jsonb (each claim → fact id or `unsupported`), repair_instructions text |

**deployments**
| id, site_build_id, provider (`cloudflare_pages`), project_name, branch, cf_deployment_id, url, alias_url, status `deployment_status`, verified_at, verification jsonb, expires_at, deleted_at_provider, log jsonb |
Index `(status, expires_at)` for cleanup.

### 2.5 Outreach

**outreach_campaigns**
| id, campaign_id, owner_id, state, authorized_at, authorized_by, from_address, sending_domain, daily_send_limit (default 20), total_send_limit, follow_up_policy jsonb (`{max_follow_ups: 1, delay_days: 4}`), send_window jsonb (local hours), deleted_at |

**outreach_messages**
| id, outreach_campaign_id, prospect_id, contact_id, deployment_id, state `outreach_state`, sequence_step smallint, subject, body_text, body_html, observations jsonb (facts referenced → fact ids), provider_message_id, idempotency_key unique, scheduled_for, sent_at |
Unique `(outreach_campaign_id, prospect_id, sequence_step)`.

**outreach_replies**
| id, outreach_message_id, received_at, from_address, raw_storage_path, classification `reply_class` (`interested`, `not_interested`, `opt_out`, `question`, `auto_reply`, `bounce`, `other`), classifier_confidence, handled boolean |

**suppression_list**
| id, owner_id, kind (`email`, `domain`, `place_id`, `phone`), value_hash (sha256 of normalised value), reason (`opt_out`, `bounce`, `complaint`, `manual`, `negative_reply`), source_message_id, created_at |
Unique `(owner_id, kind, value_hash)`. **Never** soft-deleted or purged (needed to honour opt-outs).

### 2.6 Cost, usage, audit

**api_usage**
| id bigint, task_id, campaign_id, provider, operation, sku, model, request_count, tokens_in, tokens_out, cache_read_tokens, units jsonb, status `provider_status`, http_status, latency_ms, estimated_cost_micros, occurred_at |
Index `(provider, occurred_at)`, `(campaign_id)`.

**budget_ledger**
| id bigint, owner_id, campaign_id, task_id, entry_type (`reserve`, `commit`, `release`, `adjustment`), amount_micros, provider, api_usage_id, created_at |
Available budget = cap − Σcommit − Σ(open reserve). Reservation + check happens in one transaction with `select … for update` on the campaign row.

**audit_events** (append-only)
| id bigint, owner_id, actor_type (`user`, `agent`, `system`), actor_id, action, entity_type, entity_id, from_state, to_state, reason, data jsonb, occurred_at |
Index `(entity_type, entity_id, occurred_at)`. Retention: indefinite.

## 3. Relationships (summary)

```
users 1─* campaigns 1─1 campaign_settings
campaigns 1─* agent_tasks 1─* task_events
campaigns 1─* prospects 1─* prospect_sources / prospect_contacts / website_audits / research_facts / lead_scores
prospects ─ place_id ─ places_cache (non-FK, transient)
prospects 1─* generated_sites 1─* site_versions 1─* site_builds 1─1 qa_reports, 1─* deployments
campaigns 1─* outreach_campaigns 1─* outreach_messages 1─* outreach_replies
agent_tasks 1─* api_usage 1─* budget_ledger
```

## 4. Enums

`campaign_state`: draft, authorized, queued, discovering, verifying, researching, qualifying, building, qa, preview_deployed, outreach_authorized, completed, paused, cancelled, failed, requires_human_review
`prospect_state`: discovered, identity_verified, website_verified, researched, qualified, rejected
`site_state`: queued, strategy, generating, building, qa, repair, passed, deployed, failed, requires_human_review, expired, archived
`outreach_state`: draft, authorized, queued, sent, replied, follow_up, closed, suppressed, bounced, opted_out, failed
`task_status`: pending, leased, running, retry_wait, succeeded, failed, cancelled, blocked
`website_status`: functioning, broken, outdated, none_found, unverified
`fact_status`: verified, inferred, unknown, conflicting
`retention_policy`: indefinite, cache_30d, transient, purge_on_reject
`provider_status`: ok, not_configured, error, quota_exceeded, rate_limited, budget_blocked, disabled

## 5. Row Level Security

RLS is **enabled on every table** with no default access.

| Role | Access |
|---|---|
| `service_role` (API + worker, server only) | bypasses RLS — key never leaves the server |
| `authenticated` | `select` on rows where `owner_id = auth.uid()` (directly, or via `campaigns.owner_id` join for child tables). No insert/update/delete — all writes go through the API so policy checks can't be bypassed. |
| `anon` | none |

Example:
```sql
alter table prospects enable row level security;
create policy prospects_owner_read on prospects for select to authenticated
using (exists (select 1 from campaigns c where c.id = prospects.campaign_id and c.owner_id = auth.uid()));
```
`places_cache`, `budget_ledger`, `api_usage`, `suppression_list`: service role only (no `authenticated` policy).

## 6. Retention & provenance

| Data | Retention | Mechanism |
|---|---|---|
| `place_id` | indefinite | permitted by Places terms |
| `places_cache.payload` | ≤ 30 days, then purged; refreshed on demand | nightly purge job |
| SERP results/snippets | 180 days | purge job |
| Firecrawl raw pages | 90 days (Storage); extracted facts keep the quote + URL | purge job |
| Research facts for rejected prospects | 30 days after rejection | `purge_on_reject` |
| Outreach messages, replies | 3 years (proof of compliance) | — |
| Suppression list | forever | — |
| Audit events | forever | — |
| Task events | 90 days | purge job |

Every fact used in a generated site must reference a `research_facts` row with `status='verified'`
and a `source_id`. `site_versions.facts_used` records which ones, so any claim on a preview can be
traced to its source.

## 7. Backup & recovery

- Supabase Free: daily backups retained a short period, no PITR.
- Additional: weekly `pg_dump` (schema + data, excluding `places_cache`) from the worker host to
  encrypted off-site storage; restore drill in Phase 9.
- Site sources and build artefacts in Supabase Storage are content-addressed (sha256) and reproducible from `site_versions`.
