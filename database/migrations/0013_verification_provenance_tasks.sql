-- Website verification detail, source provenance, cross-provider de-duplication, task lifecycle and provider evaluation.
-- Additive only: no existing row, column or value is removed or rewritten.

-- 1. Website verification: the precise outcome and the HTTP evidence behind it. A missing listed website is recorded as
--    such (listing_website = 'not_listed'), never as proof that no website exists.
alter table website_verifications add column website_check text check (website_check in (
  'NO_WEBSITE_LISTED', 'NO_WEBSITE_FOUND', 'WEBSITE_FOUND', 'WEBSITE_WORKING', 'WEBSITE_BROKEN_CONFIRMED', 'WEBSITE_BLOCKED',
  'WEBSITE_RATE_LIMITED', 'WEBSITE_AUTH_REQUIRED', 'WEBSITE_REDIRECTED', 'WEBSITE_UNKNOWN'));
alter table website_verifications add column listing_website text check (listing_website in ('listed', 'not_listed'));
alter table website_verifications add column http_evidence jsonb not null default '[]'::jsonb;

-- 2. Provenance: which provider (or which website) a piece of evidence came from. Facts that share an independence key
--    come from ONE source and are never counted as independent confirmations of each other.
alter table evidence add column source_provider text;
alter table evidence add column independence_key text;
create index evidence_independence_idx on evidence(prospect_id, independence_key);

-- 3. Cross-provider de-duplication keys (normalized phone, registrable domain, name+postcode), in their own table so
--    that no column of `prospects` (which can be linked to Google Places) can hold Places content. Keys are refused for
--    Places-discovered prospects.
create table prospect_keys (
  prospect_id uuid not null references prospects(id) on delete cascade,
  campaign_id uuid not null references campaigns(id) on delete cascade,
  workspace_id uuid not null references workspaces(id) on delete cascade,
  kind text not null check (kind in ('phone', 'domain', 'name')),
  key text not null check (length(key) between 1 and 200),
  primary key (prospect_id, kind)
);
create index prospect_keys_lookup_idx on prospect_keys(campaign_id, kind, key);
create function prospect_keys_no_places() returns trigger language plpgsql as $$
begin
  if (select discovery_provider from prospects where id = new.prospect_id) = 'google_places' then
    raise exception 'Google Places content may not be stored (prospect_keys for a google_places prospect)';
  end if;
  return new;
end $$;
create trigger prospect_keys_no_places before insert or update on prospect_keys
  for each row execute function prospect_keys_no_places();
alter table prospect_keys enable row level security;
create policy prospect_keys_member_read on prospect_keys for select to authenticated using (is_workspace_member(workspace_id));
grant select on prospect_keys to authenticated;

-- 4. Discovery providers: Geoapify joins; a campaign may name a secondary provider used when the primary runs out.
alter table campaigns drop constraint campaigns_discovery_provider_check;
alter table campaigns add constraint campaigns_discovery_provider_check
  check (discovery_provider in ('google_places', 'serp_api', 'geoapify', 'manual_import', 'licensed_business_data'));
alter table campaigns add column discovery_secondary text check (discovery_secondary in ('serp_api', 'geoapify'));

-- 5. Task lifecycle: cancellation is requested first and completes when in-flight work has stopped; a website-build task
--    can be started from the results of an earlier task.
alter type campaign_state add value if not exists 'cancel_requested';
alter table campaigns add column parent_campaign_id uuid references campaigns(id);
alter table campaigns add column work_kind text not null default 'discovery' check (work_kind in ('discovery', 'website_build'));
alter table campaigns add column cancel_requested_at timestamptz;
create index campaigns_parent_idx on campaigns(parent_campaign_id) where parent_campaign_id is not null;

-- 6. Provider evaluation runs (aggregate metrics only: no business records are stored by an evaluation).
create table provider_evaluations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  created_by uuid references app_users(id),
  providers text[] not null,
  plan jsonb not null,                      -- cities, categories, result limit
  status text not null default 'running' check (status in ('running', 'completed', 'failed')),
  results jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create index provider_evaluations_ws_idx on provider_evaluations(workspace_id, created_at desc);
alter table provider_evaluations enable row level security;
create policy provider_evaluations_member_read on provider_evaluations for select to authenticated
  using (is_workspace_member(workspace_id));
grant select on provider_evaluations to authenticated;

-- 7. Qualification v2: readiness status, evidence confidence, and what is missing / why (dimensions stay in components).
alter table qualification_scores add column readiness text check (readiness in ('qualified', 'needs_research', 'not_a_fit'));
alter table qualification_scores add column evidence_confidence smallint check (evidence_confidence between 0 and 100);
alter table qualification_scores add column missing text[] not null default '{}';
alter table qualification_scores add column reasons text[] not null default '{}';
