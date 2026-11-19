-- Evidence tombstones: when evidence is purged (retention or any other delete), a content-free record keeps
-- the id resolvable, so website_verifications.evidence_ids never dangle. Additive; no existing row is changed.
--
-- A tombstone keeps provenance metadata only: source type, the source host (no path, query or snippet),
-- the original retrieval time, the retention policy, hashes, and when and why the row was purged.
-- It never keeps content_excerpt or the full source_ref (SERP links and page text are not retained).

create table evidence_tombstones (
  evidence_id uuid primary key,                       -- the purged evidence.id
  workspace_id uuid not null references workspaces(id) on delete cascade,
  prospect_id uuid not null references prospects(id) on delete cascade,
  source_type source_type,                            -- null only for references purged before this migration
  source_host text,
  source_ref_sha256 text,
  content_sha256 text,
  retrieved_at timestamptz,
  retention_policy text,
  purged_at timestamptz not null default now(),
  purge_rule text not null
);
create index evidence_tombstones_prospect_idx on evidence_tombstones(prospect_id);

create or replace function evidence_tombstone() returns trigger language plpgsql as $$
begin
  -- Skip when the prospect itself is being deleted: its verifications go with it, so nothing can dangle.
  if exists (select 1 from prospects p where p.id = old.prospect_id) then
    insert into evidence_tombstones(evidence_id, workspace_id, prospect_id, source_type, source_host, source_ref_sha256,
                                    content_sha256, retrieved_at, retention_policy, purge_rule)
    values (old.id, old.workspace_id, old.prospect_id, old.source_type,
            nullif(lower(substring(old.source_ref from '^[a-zA-Z][a-zA-Z0-9+.-]*://([^/?#:@]+)')), ''),
            encode(sha256(convert_to(old.source_ref, 'UTF8')), 'hex'),
            old.content_sha256, old.retrieved_at, old.retention_policy,
            coalesce(nullif(current_setting('jarvis.purge_rule', true), ''), 'manual_delete'))
    on conflict (evidence_id) do nothing;
  end if;
  return old;
end $$;

create trigger evidence_tombstone_after_delete after delete on evidence
  for each row execute function evidence_tombstone();

-- Backfill: references that already dangle (purged before this migration) get a metadata-free tombstone,
-- so they resolve as "expired" instead of pointing at nothing.
insert into evidence_tombstones(evidence_id, workspace_id, prospect_id, purged_at, purge_rule)
select distinct on (ref) ref, v.workspace_id, v.prospect_id, v.checked_at, 'purged_before_tombstones'
from website_verifications v, unnest(v.evidence_ids) ref
where not exists (select 1 from evidence e where e.id = ref)
order by ref, v.checked_at
on conflict (evidence_id) do nothing;

-- Integrity view: verification references that resolve to neither live evidence nor a tombstone. Must be empty.
create view dangling_evidence_refs as
select v.id as verification_id, v.workspace_id, v.prospect_id, ref as evidence_id
from website_verifications v, unnest(v.evidence_ids) ref
where not exists (select 1 from evidence e where e.id = ref)
  and not exists (select 1 from evidence_tombstones t where t.evidence_id = ref);

alter table evidence_tombstones enable row level security;
create policy evidence_tombstones_member_read on evidence_tombstones for select to authenticated
  using (is_workspace_member(workspace_id));
grant select on evidence_tombstones to authenticated;
