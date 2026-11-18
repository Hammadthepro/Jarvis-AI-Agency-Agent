-- JARVIS core schema. Applied by `python -m jarvis.db.migrate`.
-- Compatible with Supabase Postgres and vanilla Postgres 15+.
-- Google Places API (New) content is NEVER stored: only place_id (and optional
-- coordinates with <= 30 day retention) lives in place_refs.

create extension if not exists pgcrypto;

-- ---------- Supabase compatibility shims (no-ops on Supabase) ----------
create schema if not exists auth;
do $$ begin
  if not exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                 where n.nspname = 'auth' and p.proname = 'uid') then
    execute $f$create function auth.uid() returns uuid language sql stable as
      'select nullif(current_setting(''request.jwt.claim.sub'', true), '''')::uuid'$f$;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
end $$;

create or replace function set_updated_at() returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

-- ---------- Identity & tenancy ----------
create table app_users (
  id uuid primary key,                         -- = Supabase auth.users.id (JWT sub)
  email text,
  display_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table workspaces (
  id uuid primary key default gen_random_uuid(),
  name text not null check (length(name) between 1 and 120),
  created_by uuid not null references app_users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create type member_role as enum ('owner', 'operator', 'viewer');
create table memberships (
  workspace_id uuid not null references workspaces(id) on delete cascade,
  user_id uuid not null references app_users(id) on delete cascade,
  role member_role not null,
  created_at timestamptz not null default now(),
  primary key (workspace_id, user_id)
);
create index memberships_user_idx on memberships(user_id);

create table system_settings (
  workspace_id uuid primary key references workspaces(id) on delete cascade,
  global_kill_switch boolean not null default false,
  provider_kill_switches jsonb not null default '{}'::jsonb,
  model_routing jsonb not null default '{}'::jsonb,
  max_concurrent_builds int not null default 1 check (max_concurrent_builds between 1 and 8),
  agency_name text,
  terms_of_use_url text,
  privacy_policy_url text,
  updated_at timestamptz not null default now()
);

-- ---------- Compliance ----------
create type compliance_scope as enum (
  'google_places_prospect_selection', 'google_places_listing_display',
  'google_places_technical_testing', 'outreach_sending');
create type approver_type as enum ('google', 'legal_counsel', 'operator');
create table compliance_approvals (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  scope compliance_scope not null,
  intended_use_text text not null,
  intended_use_version int not null,
  approver_type approver_type not null,
  approver_name text not null,
  document_ref text not null,
  approved_at timestamptz not null,
  review_by timestamptz not null,
  recorded_by uuid not null references app_users(id),
  revoked_at timestamptz,
  revoked_by uuid references app_users(id),
  created_at timestamptz not null default now(),
  check (approver_type <> 'operator' or scope = 'google_places_technical_testing'),
  check (review_by > approved_at)
);
create unique index compliance_active_uq on compliance_approvals(workspace_id, scope) where revoked_at is null;

-- ---------- Provider integrations ----------
create type integration_state as enum (
  'CONNECTED', 'NOT_CONFIGURED', 'INVALID_CREDENTIALS', 'PERMISSION_DENIED', 'QUOTA_EXCEEDED',
  'RATE_LIMITED', 'PROVIDER_ERROR', 'TEMPORARILY_UNAVAILABLE', 'DISABLED', 'BLOCKED_PENDING_APPROVAL');
create table provider_integrations (
  workspace_id uuid not null references workspaces(id) on delete cascade,
  provider text not null,
  configured boolean not null default false,       -- credential present
  state integration_state not null default 'NOT_CONFIGURED',
  last_checked_at timestamptz,                      -- last real connection test
  last_test_ok boolean,
  detail text,
  updated_at timestamptz not null default now(),
  primary key (workspace_id, provider)
);

-- ---------- Commands, campaigns ----------
create type command_status as enum ('received', 'needs_clarification', 'awaiting_confirmation', 'confirmed', 'rejected', 'failed');
create table commands (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  user_id uuid not null references app_users(id),
  channel text not null check (channel in ('text', 'voice')),
  raw_text text not null check (length(raw_text) between 1 and 2000),
  parser text,                                      -- 'deterministic' | 'claude'
  parsed jsonb,
  questions jsonb not null default '[]'::jsonb,
  status command_status not null default 'received',
  campaign_id uuid,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index commands_ws_idx on commands(workspace_id, created_at desc);

create type campaign_state as enum (
  'draft', 'authorized', 'running', 'paused', 'completed', 'cancelled', 'failed', 'requires_human_review', 'blocked');
create table campaigns (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  command_id uuid references commands(id),
  name text not null,
  niche text not null,
  location jsonb not null,
  target_count int not null check (target_count between 1 and 1000),
  discovery_provider text not null check (discovery_provider in ('google_places', 'manual_import', 'licensed_business_data')),
  minimum_google_rating numeric(2,1)
    check (minimum_google_rating is null or (minimum_google_rating between 0.5 and 5.0
           and minimum_google_rating * 2 = floor(minimum_google_rating * 2))),
  missing_rating_policy text check (missing_rating_policy in ('exclude', 'include_for_review')),
  website_filter text[] not null default '{ANY}',
  stages text[] not null,
  sites_to_build int not null default 0 check (sites_to_build between 0 and 50),
  state campaign_state not null default 'draft',
  current_stage text,
  blocked_reason text,
  budget_cap_micros bigint not null check (budget_cap_micros >= 0),
  per_site_cap_micros bigint not null check (per_site_cap_micros >= 0),
  kill_switch boolean not null default false,
  authorized_at timestamptz,
  authorized_by uuid references app_users(id),
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((minimum_google_rating is null) = (missing_rating_policy is null)),
  check (minimum_google_rating is null or discovery_provider = 'google_places')
);
create index campaigns_ws_idx on campaigns(workspace_id, created_at desc);
alter table commands add constraint commands_campaign_fk foreign key (campaign_id) references campaigns(id);

-- ---------- Durable tasks ----------
create type task_status as enum ('pending', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'dead', 'cancelled', 'blocked');
create table tasks (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  campaign_id uuid references campaigns(id) on delete cascade,
  parent_task_id uuid references tasks(id),
  task_type text not null,
  subject_id uuid,
  status task_status not null default 'pending',
  priority smallint not null default 0,
  payload jsonb not null default '{}'::jsonb,
  result jsonb,
  error_code text,
  error_detail text,
  attempts int not null default 0,
  max_attempts int not null default 5 check (max_attempts between 1 and 20),
  idempotency_key text not null unique,
  run_after timestamptz not null default now(),
  leased_by text,
  lease_expires_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  cancel_requested boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (attempts <= max_attempts)
);
create index tasks_claim_idx on tasks(priority desc, run_after) where status in ('pending', 'retry_wait');
create index tasks_lease_idx on tasks(lease_expires_at) where status in ('leased', 'running');
create index tasks_campaign_idx on tasks(campaign_id, status);

create table task_events (
  id bigint generated always as identity primary key,
  workspace_id uuid not null references workspaces(id) on delete cascade,
  task_id uuid references tasks(id) on delete cascade,
  campaign_id uuid references campaigns(id) on delete cascade,
  event_type text not null,
  message text not null,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index task_events_campaign_idx on task_events(campaign_id, id);
create index task_events_ws_idx on task_events(workspace_id, id);

create table task_checkpoints (
  task_id uuid not null references tasks(id) on delete cascade,
  step text not null,
  state jsonb not null,
  created_at timestamptz not null default now(),
  primary key (task_id, step)
);

create table agent_executions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  task_id uuid references tasks(id) on delete cascade,
  agent text not null,
  status text not null check (status in ('running', 'succeeded', 'failed')),
  input_ref jsonb not null default '{}'::jsonb,
  output_ref jsonb,
  error text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);
create index agent_exec_task_idx on agent_executions(task_id);

create table worker_heartbeats (
  worker_id text primary key,
  started_at timestamptz not null,
  last_seen_at timestamptz not null,
  version text not null,
  current_task_id uuid,
  db_state text not null default 'healthy'
);

create table dead_letters (
  id bigint generated always as identity primary key,
  task_id uuid not null references tasks(id) on delete cascade,
  reason text not null,
  snapshot jsonb not null,
  created_at timestamptz not null default now()
);

-- ---------- Prospects & evidence ----------
create table place_refs (
  workspace_id uuid not null references workspaces(id) on delete cascade,
  place_id text not null,                          -- permitted indefinitely
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  lat double precision,
  lng double precision,
  coords_fetched_at timestamptz,
  coords_expires_at timestamptz,
  primary key (workspace_id, place_id),
  check (coords_expires_at is null or coords_expires_at <= coords_fetched_at + interval '30 days'),
  check ((lat is null) = (coords_fetched_at is null))
);

create type prospect_state as enum ('discovered', 'identity_verified', 'website_verified', 'researched', 'qualified', 'rejected', 'requires_human_review');
create table prospects (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  campaign_id uuid not null references campaigns(id) on delete cascade,
  discovery_provider text not null,
  place_id text,                                    -- google_places only
  external_ref text,                                -- manual_import row ref / licensed provider id
  state prospect_state not null default 'discovered',
  review_reason text check (review_reason in ('identity_unconfirmed', 'discovery_filter_unconfirmed')),
  business_category text not null,
  rejection_reason text,
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (workspace_id, place_id) references place_refs(workspace_id, place_id),
  check (place_id is not null or external_ref is not null)
);
create unique index prospects_campaign_place_uq on prospects(campaign_id, place_id) where place_id is not null;
create unique index prospects_campaign_ext_uq on prospects(campaign_id, external_ref) where external_ref is not null;
create index prospects_ws_idx on prospects(workspace_id, campaign_id, state);

create type source_type as enum ('serp_result', 'official_website', 'firecrawl_page', 'directory_page', 'social_page',
  'licence_register', 'dns', 'http_probe', 'manual', 'manual_import');
create table evidence (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  source_type source_type not null,                 -- google_places is deliberately not a member
  source_ref text not null,
  retrieved_at timestamptz not null default now(),
  retention_policy text not null check (retention_policy in ('indefinite', 'days_180', 'days_90', 'purge_on_reject')),
  content_excerpt text,                             -- independent source text (never Places content)
  content_sha256 text
);
create index evidence_prospect_idx on evidence(prospect_id);

-- Business identity: independently sourced facts only.
create type fact_status as enum ('verified', 'inferred', 'unknown', 'conflicting');
create table research_facts (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  fact_key text not null,
  value jsonb not null,
  status fact_status not null,
  evidence_id uuid references evidence(id) on delete cascade,
  quote text,
  confidence numeric(3,2) not null check (confidence between 0 and 1),
  created_at timestamptz not null default now(),
  check (status <> 'verified' or (evidence_id is not null and quote is not null))
);
create index facts_prospect_idx on research_facts(prospect_id, fact_key);

create table contacts (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  contact_type text not null check (contact_type in ('email', 'phone', 'contact_form', 'social')),
  value text not null,
  evidence_id uuid not null references evidence(id) on delete cascade,
  is_public_business_contact boolean not null default false,
  harvesting_prohibited boolean not null default false, -- source site forbids automated address collection
  verification fact_status not null,
  created_at timestamptz not null default now(),
  unique (prospect_id, contact_type, value)
);

create type website_verification_status as enum (
  'VERIFIED_HAS_WEBSITE', 'VERIFIED_NO_WEBSITE', 'WEBSITE_UNCERTAIN', 'CONFLICTING_EVIDENCE', 'VERIFICATION_FAILED');
create table website_verifications (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  task_id uuid references tasks(id) on delete set null,
  status website_verification_status not null,
  site_health text check (site_health in ('working', 'broken', 'outdated')),
  reason text,
  url text,
  final_url text,
  listing_sources text[] not null default '{}',
  checks jsonb not null default '[]'::jsonb,
  evidence_ids uuid[] not null default '{}',
  confidence numeric(3,2) not null check (confidence between 0 and 1),
  checked_at timestamptz not null default now(),
  check (status <> 'VERIFIED_HAS_WEBSITE' or site_health is not null)
);
create index wv_prospect_idx on website_verifications(prospect_id, checked_at desc);

create table scoring_rule_sets (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  version int not null,
  rules jsonb not null,
  is_active boolean not null default false,
  created_at timestamptz not null default now(),
  unique (workspace_id, version)
);
create unique index scoring_active_uq on scoring_rule_sets(workspace_id) where is_active;

create table qualification_scores (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  rule_set_version int not null,
  total smallint not null check (total between 0 and 100),
  components jsonb not null,
  inputs_hash text not null,
  computed_at timestamptz not null default now(),
  unique (prospect_id, rule_set_version, inputs_hash)
);

-- ---------- Websites ----------
create type site_state as enum ('queued', 'strategy', 'generating', 'building', 'qa', 'repair', 'passed', 'deployed',
  'failed', 'requires_human_review', 'expired');
create table site_specs (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  spec jsonb not null,
  design jsonb not null,
  facts_used uuid[] not null,
  model text not null,
  prompt_version text not null,
  created_at timestamptz not null default now()
);

create table generated_sites (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  campaign_id uuid not null references campaigns(id) on delete cascade,
  slug text not null unique,
  state site_state not null default 'queued',
  current_version_id uuid,
  repair_cycles int not null default 0,
  cost_micros bigint not null default 0,
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (prospect_id)
);

create table site_versions (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  site_id uuid not null references generated_sites(id) on delete cascade,
  version int not null,
  spec_id uuid not null references site_specs(id),
  reason text not null check (reason in ('initial', 'repair', 'escalation', 'manual')),
  artifact_path text,
  artifact_sha256 text,
  file_count int,
  total_bytes bigint,
  created_at timestamptz not null default now(),
  unique (site_id, version)
);
alter table generated_sites add constraint gs_version_fk foreign key (current_version_id) references site_versions(id);

create table qa_runs (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  site_version_id uuid not null references site_versions(id) on delete cascade,
  status text not null check (status in ('running', 'passed', 'failed', 'error')),
  runner text not null,                             -- 'docker-sandbox' | 'local-subprocess'
  report jsonb,
  screenshots text[] not null default '{}',
  started_at timestamptz not null default now(),
  finished_at timestamptz
);
create table qa_findings (
  id uuid primary key default gen_random_uuid(),
  qa_run_id uuid not null references qa_runs(id) on delete cascade,
  check_name text not null,
  severity text not null check (severity in ('critical', 'serious', 'moderate', 'minor')),
  viewport text,
  message text not null,
  details jsonb not null default '{}'::jsonb
);
create index qa_findings_run_idx on qa_findings(qa_run_id);

create table deployments (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  site_version_id uuid not null references site_versions(id) on delete cascade,
  provider text not null default 'cloudflare_pages',
  project_name text not null,
  branch text not null,
  provider_deployment_id text,
  url text,
  status text not null check (status in ('pending', 'uploaded', 'verified', 'failed', 'deleted', 'unknown_provider_result')),
  verification jsonb,
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index deployments_status_idx on deployments(status, expires_at);

-- ---------- Outreach ----------
create table sender_readiness (
  workspace_id uuid primary key references workspaces(id) on delete cascade,
  postal_address text,
  postal_address_type text check (postal_address_type in ('street', 'po_box', 'cmra_mailbox')),
  postal_address_attested_at timestamptz,
  postal_address_attested_by uuid references app_users(id),
  sending_domain text,
  from_address text,
  reply_to text,
  spf_ok boolean not null default false,
  dkim_ok boolean not null default false,
  dmarc_ok boolean not null default false,
  provider_domain_verified boolean not null default false,
  dns_checked_at timestamptz,
  unsubscribe_endpoint_ok boolean not null default false,
  reply_to_confirmed boolean not null default false,
  updated_at timestamptz not null default now()
);

create type draft_state as enum ('draft', 'authorized', 'queued', 'sending', 'sent', 'unknown_provider_result',
  'failed', 'suppressed', 'bounced', 'opted_out', 'replied', 'closed');
create table outreach_drafts (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  campaign_id uuid not null references campaigns(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  contact_id uuid references contacts(id),
  deployment_id uuid references deployments(id),
  state draft_state not null default 'draft',
  subject text not null,
  body_text text not null,
  observations jsonb not null,                      -- [{fact_id, text}]
  model text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (campaign_id, prospect_id)
);

create table outreach_authorizations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  campaign_id uuid not null references campaigns(id) on delete cascade,
  authorized_by uuid not null references app_users(id),
  daily_limit int not null check (daily_limit between 1 and 200),
  total_limit int not null check (total_limit >= 1),
  readiness_snapshot jsonb not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create table email_outbox (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  draft_id uuid not null references outreach_drafts(id) on delete cascade unique,
  idempotency_key text not null unique,
  status text not null check (status in ('pending', 'sending', 'sent', 'unknown_provider_result', 'failed', 'cancelled')),
  provider_message_id text,
  attempts int not null default 0,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table suppression_records (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  kind text not null check (kind in ('email', 'domain', 'place_id', 'phone')),
  value_hash text not null,
  reason text not null check (reason in ('opt_out', 'bounce', 'complaint', 'manual', 'negative_reply')),
  source text,
  created_at timestamptz not null default now(),
  unique (workspace_id, kind, value_hash)
);

-- ---------- Cost & usage ----------
create table budget_limits (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  scope text not null check (scope in ('global', 'provider', 'campaign', 'site')),
  provider text,                                    -- for scope=provider
  unit text not null,                               -- 'usd_micros' or provider unit e.g. 'text_search_enterprise'
  period text not null check (period in ('day', 'month', 'total')),
  cap bigint not null check (cap >= 0),
  free_allowance bigint,
  allow_paid boolean not null default false,
  updated_at timestamptz not null default now(),
  unique (workspace_id, scope, provider, unit, period)
);

create table cost_reservations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  task_id uuid references tasks(id) on delete set null,
  campaign_id uuid references campaigns(id) on delete set null,
  site_id uuid references generated_sites(id) on delete set null,
  provider text not null,
  unit text not null,
  units bigint not null check (units >= 0),
  amount_micros bigint not null check (amount_micros >= 0),
  status text not null check (status in ('reserved', 'committed', 'released', 'ambiguous')),
  created_at timestamptz not null default now(),
  settled_at timestamptz
);
create index reservations_open_idx on cost_reservations(workspace_id, provider, status) where status in ('reserved', 'ambiguous');

create table usage_events (
  id bigint generated always as identity primary key,
  workspace_id uuid not null references workspaces(id) on delete cascade,
  task_id uuid references tasks(id) on delete set null,
  campaign_id uuid references campaigns(id) on delete set null,
  site_id uuid references generated_sites(id) on delete set null,
  reservation_id uuid references cost_reservations(id),
  provider text not null,
  operation text not null,
  sku text,
  model text,
  units bigint not null default 0,
  tokens_in bigint,
  tokens_out bigint,
  outcome text not null check (outcome in ('ok', 'error', 'ambiguous', 'blocked')),
  http_status int,
  latency_ms int,
  estimated_cost_micros bigint not null default 0,
  pricing_version text not null,
  created_at timestamptz not null default now()
);
create index usage_ws_time_idx on usage_events(workspace_id, created_at);
create index usage_campaign_idx on usage_events(campaign_id);

create table cost_ledger (
  id bigint generated always as identity primary key,
  workspace_id uuid not null references workspaces(id) on delete cascade,
  reservation_id uuid references cost_reservations(id),
  campaign_id uuid references campaigns(id) on delete set null,
  site_id uuid references generated_sites(id) on delete set null,
  provider text not null,
  entry_type text not null check (entry_type in ('commit', 'adjustment')),
  amount_micros bigint not null,
  created_at timestamptz not null default now()
);

create table provider_usage_reports (
  id bigint generated always as identity primary key,
  workspace_id uuid not null references workspaces(id) on delete cascade,
  provider text not null,
  period_start timestamptz not null,
  period_end timestamptz not null,
  unit text not null,
  reported_units bigint,
  reported_cost_micros bigint,
  source text not null check (source in ('provider_api', 'manual_entry', 'billing_export')),
  raw jsonb,
  fetched_at timestamptz not null default now()
);

-- ---------- Audit, notifications, backups ----------
create table audit_logs (
  id bigint generated always as identity primary key,
  workspace_id uuid references workspaces(id) on delete cascade,
  actor_type text not null check (actor_type in ('user', 'agent', 'system')),
  actor_id text,
  action text not null,
  entity_type text,
  entity_id text,
  from_state text,
  to_state text,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index audit_entity_idx on audit_logs(entity_type, entity_id, created_at);
create index audit_ws_idx on audit_logs(workspace_id, created_at desc);

create table notifications (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  level text not null check (level in ('info', 'warning', 'error')),
  title text not null,
  body text not null,
  read_at timestamptz,
  created_at timestamptz not null default now()
);

create table backup_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null check (status in ('running', 'succeeded', 'failed')),
  destination text not null,
  offsite boolean not null,
  size_bytes bigint,
  sha256 text,
  detail text,
  restore_tested_at timestamptz
);

-- updated_at triggers
do $$ declare t text; begin
  foreach t in array array['app_users','workspaces','commands','campaigns','tasks','prospects','generated_sites',
                           'deployments','outreach_drafts','email_outbox','system_settings','provider_integrations',
                           'sender_readiness','budget_limits'] loop
    execute format('create trigger %I_updated before update on %I for each row execute function set_updated_at()', t, t);
  end loop;
end $$;
