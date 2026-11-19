"""Scheduled data retention. Every rule is listed here; nothing else deletes data on a schedule.

| Data                                   | Rule                                                        |
|----------------------------------------|-------------------------------------------------------------|
| task_events                            | delete after 90 days (unless the campaign is under hold)    |
| evidence: raw pages (days_90)          | delete after 90 days unless cited by a fact or contact      |
| evidence: SERP results (days_180)      | delete after 180 days unless cited by a fact or contact     |
| place_refs coordinates                 | null after coords_expires_at (<= 30 days)                   |
| evidence/facts purge_on_reject         | delete 30 days after the prospect was rejected              |
| NEVER deleted by retention             | audit_logs, suppression_records, outreach_drafts, email_outbox, |
|                                        | outreach_authorizations, compliance_approvals, cost ledger,  |
|                                        | usage_events, backup_runs, legal_holds                      |

Legal holds (workspace, campaign or prospect scope) exempt every covered row. Each run is recorded in
retention_runs and audit_logs with per-rule counts."""

from __future__ import annotations

from jarvis.db.pool import Database

HELD = """(
  exists (select 1 from legal_holds h where h.released_at is null and h.workspace_id = {ws} and h.scope = 'workspace')
  or exists (select 1 from legal_holds h where h.released_at is null and h.scope = 'campaign' and h.campaign_id = {camp})
  or exists (select 1 from legal_holds h where h.released_at is null and h.scope = 'prospect' and h.prospect_id = {pros})
)"""
EVIDENCE_HELD = HELD.format(ws="e.workspace_id", camp="(select campaign_id from prospects p where p.id = e.prospect_id)",
                            pros="e.prospect_id")
UNCITED = """not exists (select 1 from research_facts f where f.evidence_id = e.id)
             and not exists (select 1 from contacts c where c.evidence_id = e.id)"""

RULES: dict[str, str] = {
    "task_events_90d": f"""delete from task_events t where t.created_at < now() - interval '90 days'
        and not {HELD.format(ws="t.workspace_id", camp="t.campaign_id", pros="null::uuid")}""",
    "raw_pages_90d": f"""delete from evidence e where e.retention_policy = 'days_90' and e.retrieved_at < now() - interval '90 days'
        and {UNCITED} and not {EVIDENCE_HELD}""",
    "serp_results_180d": f"""delete from evidence e where e.retention_policy = 'days_180' and e.retrieved_at < now() - interval '180 days'
        and {UNCITED} and not {EVIDENCE_HELD}""",
    "rejected_prospect_evidence_30d": f"""delete from evidence e where e.retention_policy = 'purge_on_reject'
        and exists (select 1 from prospects p where p.id = e.prospect_id and p.state = 'rejected' and p.updated_at < now() - interval '30 days')
        and not {EVIDENCE_HELD}""",
    "place_coordinates_30d": """update place_refs set lat = null, lng = null, coords_fetched_at = null, coords_expires_at = null
        where coords_expires_at < now()""",
}


async def run_retention(db: Database) -> dict[str, int]:
    run_id = await db.fetchval("insert into retention_runs(status) values ('running') returning id")
    counts: dict[str, int] = {}
    try:
        async with db.transaction() as conn:
            for name, sql in RULES.items():
                res = await conn.execute(sql)
                counts[name] = int(res.split()[-1])
            await conn.execute("update retention_runs set status='succeeded', finished_at=now(), counts=$2 where id=$1", run_id, counts)
            await conn.execute("""insert into audit_logs(actor_type, actor_id, action, data) values ('system','retention','retention.run',$1)""",
                               counts)
    except Exception as exc:
        await db.execute("update retention_runs set status='failed', finished_at=now(), detail=$2 where id=$1", run_id, str(exc)[:500])
        raise
    return counts
