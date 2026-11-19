-- Hardening: shared rate limiting, legal holds, retention runs, provider usage reconciliation, estimate tracking.

-- Shared token buckets (work across all API instances and worker processes).
create table rate_limit_buckets (
  bucket_key text primary key,
  tokens double precision not null,
  capacity double precision not null check (capacity > 0),
  refill_per_s double precision not null check (refill_per_s >= 0),
  updated_at timestamptz not null default clock_timestamp()
);

-- Legal holds: retention never deletes held records.
create table legal_holds (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  scope text not null check (scope in ('workspace', 'campaign', 'prospect')),
  campaign_id uuid references campaigns(id) on delete cascade,
  prospect_id uuid references prospects(id) on delete cascade,
  reason text not null check (length(reason) >= 5),
  created_by uuid not null references app_users(id),
  created_at timestamptz not null default now(),
  released_at timestamptz,
  released_by uuid references app_users(id),
  check ((scope = 'campaign') = (campaign_id is not null)),
  check ((scope = 'prospect') = (prospect_id is not null))
);
create index legal_holds_active_idx on legal_holds(workspace_id) where released_at is null;

create table retention_runs (
  id bigint generated always as identity primary key,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null check (status in ('running', 'succeeded', 'failed')),
  counts jsonb not null default '{}'::jsonb,
  detail text
);

-- Provider usage: account-level reports (provider accounts are per deployment, not per workspace),
-- with an explicit availability status. Missing data is recorded as unavailable, never as zero.
alter table provider_usage_reports alter column workspace_id drop not null;
alter table provider_usage_reports add column status text not null default 'ok'
  check (status in ('ok', 'unavailable', 'error', 'not_configured'));
alter table provider_usage_reports add column detail text;
alter table provider_usage_reports drop constraint provider_usage_reports_source_check;
alter table provider_usage_reports add constraint provider_usage_reports_source_check
  check (source in ('provider_api', 'manual_entry', 'billing_export', 'response_usage', 'none'));
alter table provider_usage_reports add check (status <> 'ok' or reported_units is not null or reported_cost_micros is not null);
create index usage_reports_latest_idx on provider_usage_reports(provider, unit, fetched_at desc);

-- Keep the original worst-case estimate after commit (commit overwrites amount_micros with the actual).
alter table cost_reservations add column estimated_micros bigint;
update cost_reservations set estimated_micros = amount_micros where estimated_micros is null;

alter table deployments add column upload_completed_at timestamptz;

alter table legal_holds enable row level security;
alter table rate_limit_buckets enable row level security;
alter table retention_runs enable row level security;
