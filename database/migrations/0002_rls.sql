-- Row Level Security: defence in depth. (Not FORCEd: the backend connects as the table owner / a BYPASSRLS role.) The backend uses a privileged role and ALSO
-- applies explicit workspace authorization on every query (never relying on RLS alone).
-- `authenticated` gets read-only access to rows in workspaces it belongs to.

create or replace function is_workspace_member(ws uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from memberships m where m.workspace_id = ws and m.user_id = auth.uid())
$$;

do $$ declare t text; begin
  foreach t in array array[
    'workspaces','memberships','system_settings','compliance_approvals','provider_integrations','commands',
    'campaigns','tasks','task_events','task_checkpoints','agent_executions','prospects','place_refs','evidence',
    'research_facts','contacts','website_verifications','scoring_rule_sets','qualification_scores','site_specs',
    'generated_sites','site_versions','qa_runs','qa_findings','deployments','sender_readiness','outreach_drafts',
    'outreach_authorizations','email_outbox','suppression_records','budget_limits','cost_reservations',
    'usage_events','cost_ledger','provider_usage_reports','audit_logs','notifications','app_users',
    'worker_heartbeats','dead_letters','backup_runs'] loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;

-- Read policies for authenticated users (no write policies: all writes go through the API).
do $$ declare t text; begin
  foreach t in array array[
    'system_settings','provider_integrations','commands','campaigns','tasks','task_events','agent_executions',
    'prospects','evidence','research_facts','contacts','website_verifications','scoring_rule_sets',
    'qualification_scores','site_specs','generated_sites','site_versions','deployments','outreach_drafts',
    'notifications','usage_events','audit_logs'] loop
    execute format('create policy %I on %I for select to authenticated using (is_workspace_member(workspace_id))',
                   t || '_member_read', t);
    execute format('grant select on %I to authenticated', t);
  end loop;
end $$;

create policy workspaces_member_read on workspaces for select to authenticated using (is_workspace_member(id));
grant select on workspaces to authenticated;
create policy memberships_self_read on memberships for select to authenticated using (user_id = auth.uid());
grant select on memberships to authenticated;
create policy app_users_self_read on app_users for select to authenticated using (id = auth.uid());
grant select on app_users to authenticated;
grant usage on schema public to authenticated;
grant usage on schema auth to authenticated;
grant execute on function auth.uid() to authenticated;
grant execute on function is_workspace_member(uuid) to authenticated;
-- Service-only tables (no authenticated policy): compliance_approvals, place_refs, cost_reservations,
-- cost_ledger, budget_limits, suppression_records, sender_readiness, email_outbox, provider_usage_reports,
-- outreach_authorizations, task_checkpoints, qa_runs, qa_findings, worker_heartbeats, dead_letters, backup_runs.
