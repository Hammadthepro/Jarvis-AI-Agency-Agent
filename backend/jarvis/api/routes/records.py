"""Prospects, evidence, sites, QA, deployments, overview and activity. Every query is scoped to the
caller's workspace. No Google Places content is ever returned except by the live, attributed,
gated /google-listing endpoint (Cache-Control: no-store)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from jarvis.api.auth import CurrentPrincipal, Principal
from jarvis.core.errors import NotFound, ValidationFailed
from jarvis.core.retention import resolve_evidence
from jarvis.db.audit import audit
from jarvis.integrations.base import CallContext

router = APIRouter()


def J(x) -> dict | list:  # noqa: N802
    return json.loads(json.dumps(x, default=str))


@router.get("/overview")
async def overview(request: Request, me: Principal = CurrentPrincipal):
    db = request.app.state.db
    q = lambda sql: db.fetchval(sql, me.workspace_id)  # noqa: E731
    return J({
        "prospects_discovered": await q("select count(*) from prospects where workspace_id=$1"),
        "prospects_verified": await q("""select count(distinct prospect_id) from website_verifications where workspace_id=$1
                                         and status in ('VERIFIED_HAS_WEBSITE','VERIFIED_NO_WEBSITE')"""),
        "prospects_no_website": await q("""select count(distinct prospect_id) from website_verifications where workspace_id=$1
                                           and status='VERIFIED_NO_WEBSITE'"""),
        "prospects_uncertain": await q("""select count(distinct prospect_id) from website_verifications where workspace_id=$1
                                          and status in ('WEBSITE_UNCERTAIN','CONFLICTING_EVIDENCE','VERIFICATION_FAILED')"""),
        "prospects_qualified": await q("select count(*) from prospects where workspace_id=$1 and state='qualified'"),
        "sites_generated": await q("select count(*) from generated_sites where workspace_id=$1 and current_version_id is not null"),
        "sites_qa_passed": await q("select count(*) from generated_sites where workspace_id=$1 and state in ('passed','deployed')"),
        "previews_deployed": await q("select count(*) from deployments where workspace_id=$1 and status='verified'"),
        "outreach_drafts": await q("select count(*) from outreach_drafts where workspace_id=$1"),
        "campaigns_running": await q("select count(*) from campaigns where workspace_id=$1 and state='running'"),
        "spend_month_micros": await q("""select coalesce(sum(amount_micros)::bigint,0) from cost_reservations where workspace_id=$1
                                         and status in ('committed','ambiguous','reserved') and created_at >= date_trunc('month', now())"""),
        "workers": [dict(r) for r in await db.fetch("""select worker_id, last_seen_at, version, db_state,
                                                       (last_seen_at > now() - interval '2 minutes') alive from worker_heartbeats
                                                       order by last_seen_at desc limit 10""")],
        "queue_depth": await q("select count(*) from tasks where workspace_id=$1 and status in ('pending','retry_wait')"),
        "dead_letters": await q("""select count(*) from dead_letters d join tasks t on t.id = d.task_id where t.workspace_id=$1"""),
        "db_state": db.state,
    })


@router.get("/prospects")
async def prospects(request: Request, me: Principal = CurrentPrincipal, campaign_id: UUID | None = None,
                    state: str | None = None, website: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0):
    rows = await request.app.state.db.fetch(
        """select p.id, p.campaign_id, p.discovery_provider, p.state, p.review_reason, p.business_category, p.rejection_reason,
                  p.created_at, (p.place_id is not null) has_google_place,
                  (select value #>> '{}' from research_facts f where f.prospect_id = p.id and f.fact_key='business_name'
                     and f.status='verified' order by created_at limit 1) business_name,
                  v.status website_status, v.site_health, v.confidence website_confidence, v.final_url website_url,
                  v.website_check, v.listing_website,
                  s.total score, s.readiness, s.evidence_confidence, s.missing, g.id site_id, g.state site_state,
                  (select url from deployments d join site_versions sv on sv.id = d.site_version_id where sv.site_id = g.id
                     and d.status = 'verified' order by d.created_at desc limit 1) preview_url,
                  (select state from outreach_drafts o where o.prospect_id = p.id order by created_at desc limit 1) outreach_state
           from prospects p
           left join lateral (select * from website_verifications w where w.prospect_id = p.id order by checked_at desc limit 1) v on true
           left join lateral (select total, readiness, evidence_confidence, missing from qualification_scores q
                              where q.prospect_id = p.id order by computed_at desc limit 1) s on true
           left join generated_sites g on g.prospect_id = p.id
           where p.workspace_id = $1 and p.deleted_at is null and ($2::uuid is null or p.campaign_id = $2)
             and ($3::text is null or p.state::text = $3) and ($4::text is null or v.status::text = $4)
           order by s.total desc nulls last, p.created_at desc limit $5 offset $6""",
        me.workspace_id, campaign_id, state, website, min(limit, 500), offset)
    out = [dict(r) for r in rows]
    if q:
        out = [r for r in out if q.lower() in (r["business_name"] or "").lower()]
    return J(out)


async def _prospect(db, me: Principal, pid: UUID):
    p = await db.fetchrow("select * from prospects where id=$1 and workspace_id=$2", pid, me.workspace_id)
    if p is None:
        raise NotFound("prospect not found")
    return p


@router.get("/prospects/{pid}")
async def prospect_detail(pid: UUID, request: Request, me: Principal = CurrentPrincipal):
    db = request.app.state.db
    p = dict(await _prospect(db, me, pid))
    p.pop("place_id", None)  # place_id is internal; listing is fetched live via /google-listing
    return J({
        "prospect": p,
        "facts": [dict(r) for r in await db.fetch("""select f.id, f.fact_key, f.value #>> '{}' as value, f.status, f.quote, f.confidence,
                                                     e.source_type, e.source_ref, e.retrieved_at, e.source_provider,
                                                     e.independence_key from research_facts f
                                                     left join evidence e on e.id = f.evidence_id where f.prospect_id=$1
                                                     order by f.fact_key, f.created_at""", pid)],
        "verifications": [{**{k: v for k, v in dict(r).items() if k != "evidence_ids"},
                           "evidence_refs": await resolve_evidence(db, list(r["evidence_ids"]))}
                          for r in await db.fetch("""select id, status, site_health, reason, url, final_url, listing_sources,
                                                     checks, evidence_ids, confidence, checked_at, website_check, listing_website,
                                                     http_evidence from website_verifications
                                                     where prospect_id=$1 order by checked_at desc""", pid)],
        "evidence": [dict(r) for r in await db.fetch("""select id, source_type, source_ref, retrieved_at, left(content_excerpt, 400) excerpt
                                                       from evidence where prospect_id=$1 order by retrieved_at desc limit 100""", pid)],
        "contacts": [dict(r) for r in await db.fetch("""select contact_type, value, verification, is_public_business_contact,
                                                       harvesting_prohibited from contacts where prospect_id=$1""", pid)],
        "sources": [dict(r) for r in await db.fetch(
            """select coalesce(e.independence_key, e.source_type::text) source, min(e.source_provider) provider,
                      count(distinct f.fact_key) facts, array_agg(distinct f.fact_key) fields, min(e.retrieved_at) first_seen
               from research_facts f join evidence e on e.id = f.evidence_id where f.prospect_id=$1 and f.status='verified'
               group by 1 order by 3 desc""", pid)],
        "scores": [dict(r) for r in await db.fetch("""select total, components, rule_set_version, computed_at, readiness,
                                                     evidence_confidence, missing, reasons from qualification_scores
                                                     where prospect_id=$1 order by computed_at desc""", pid)],
    })


@router.get("/prospects/{pid}/google-listing")
async def google_listing(pid: UUID, request: Request, me: Principal = CurrentPrincipal):
    deps = request.app.state.deps
    p = await _prospect(deps.db, me, pid)
    if not p["place_id"]:
        raise ValidationFailed("this prospect was not discovered via Google Places")
    data = await deps.places.listing(CallContext(deps.db, me.workspace_id, campaign_id=p["campaign_id"]), p["place_id"])
    return JSONResponse({**data, "cache": "none"}, headers={"Cache-Control": "no-store"})


@router.post("/prospects/{pid}/confirm-identity")
async def confirm_identity(pid: UUID, body: dict, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    db = request.app.state.db
    await _prospect(db, me, pid)
    name, how = (body.get("business_name") or "").strip(), (body.get("how_confirmed") or "").strip()
    if len(name) < 2 or len(how) < 5:
        raise ValidationFailed("business_name and how_confirmed (e.g. 'called the business on 2026-11-18') are required")
    async with db.transaction() as conn:
        ev = await conn.fetchval("""insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt)
                                    values ($1,$2,'manual',$3,'indefinite',$4) returning id""",
                                 me.workspace_id, pid, f"operator:{me.user_id}", how)
        await conn.execute("""insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote, confidence)
                              values ($1,$2,'business_name',$3,'verified',$4,$5,1.0)""", me.workspace_id, pid, name, ev, name)
        await conn.execute("""update prospects set state = case when state='requires_human_review' then 'website_verified'::prospect_state
                              else state end, review_reason = null where id=$1""", pid)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="prospect.identity_confirmed",
                    entity_type="prospect", entity_id=pid, data={"how": how})
    return {"ok": True}


@router.get("/sites")
async def sites(request: Request, me: Principal = CurrentPrincipal, campaign_id: UUID | None = None):
    rows = await request.app.state.db.fetch(
        """select g.id, g.slug, g.state, g.campaign_id, g.prospect_id, g.repair_cycles, g.cost_micros, g.created_at, g.updated_at,
                  v.version, ss.design, ss.model, (ss.spec->>'headline') headline,
                  (select status from qa_runs q where q.site_version_id = v.id order by started_at desc limit 1) qa_status,
                  (select runner from qa_runs q where q.site_version_id = v.id order by started_at desc limit 1) qa_runner,
                  (select url from deployments d where d.site_version_id = v.id and d.status='verified' order by created_at desc limit 1) preview_url,
                  (select value #>> '{}' from research_facts f where f.prospect_id = g.prospect_id and f.fact_key='business_name'
                     and f.status='verified' limit 1) business_name,
                  (select d.status from deployments d where d.site_version_id = v.id order by created_at desc limit 1) deploy_status,
                  g.last_error, g.last_error_at,
                  (select json_build_object('task', t.task_type, 'status', t.status, 'attempts', t.attempts, 'max_attempts', t.max_attempts)
                     from tasks t where t.subject_id = g.id and t.task_type in ('site.build','site.deploy')
                     order by t.created_at desc limit 1) last_task
           from generated_sites g
           left join lateral (select * from site_versions sv where sv.site_id = g.id order by version desc limit 1) v on true
           left join site_specs ss on ss.id = v.spec_id
           where g.workspace_id=$1 and ($2::uuid is null or g.campaign_id=$2) order by g.created_at desc""", me.workspace_id, campaign_id)
    return J([dict(r) for r in rows])


@router.get("/sites/{site_id}")
async def site_detail(site_id: UUID, request: Request, me: Principal = CurrentPrincipal):
    db = request.app.state.db
    g = await db.fetchrow("""select g.*, (select value #>> '{}' from research_facts f where f.prospect_id = g.prospect_id
                                        and f.fact_key='business_name' and f.status='verified' limit 1) business_name
                             from generated_sites g where g.id=$1 and g.workspace_id=$2""", site_id, me.workspace_id)
    if g is None:
        raise NotFound("site not found")
    versions = await db.fetch("""select v.id, v.version, v.reason, v.file_count, v.total_bytes, v.artifact_sha256, v.created_at,
                                        ss.spec, ss.design, ss.model, ss.prompt_version from site_versions v join site_specs ss on ss.id = v.spec_id
                                 where v.site_id=$1 order by v.version desc""", site_id)
    qa = await db.fetch("""select q.id, q.site_version_id, q.status, q.runner, q.report->'metrics' metrics, q.started_at, q.finished_at,
                                  (select json_agg(f) from qa_findings f where f.qa_run_id = q.id) findings
                           from qa_runs q join site_versions v on v.id = q.site_version_id where v.site_id=$1 order by q.started_at desc""", site_id)
    deps_ = await db.fetch("""select d.* from deployments d join site_versions v on v.id = d.site_version_id where v.site_id=$1
                              order by d.created_at desc""", site_id)
    return J({"site": dict(g), "versions": [dict(r) for r in versions], "qa_runs": [dict(r) for r in qa],
              "deployments": [dict(r) for r in deps_]})


@router.get("/sites/{site_id}/artifact/{version}/{path:path}")
async def site_artifact(site_id: UUID, version: int, path: str, request: Request, me: Principal = CurrentPrincipal):
    """Serves built files and QA screenshots of the caller's own sites (for the Website Studio)."""
    db = request.app.state.db
    row = await db.fetchrow("""select v.artifact_path from site_versions v join generated_sites g on g.id = v.site_id
                               where g.id=$1 and g.workspace_id=$2 and v.version=$3""", site_id, me.workspace_id, version)
    if not row or not row["artifact_path"]:
        raise NotFound("artifact not found")
    root = Path(row["artifact_path"]).parent.resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise NotFound("file not found")
    return FileResponse(target, headers={"Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self' data:",
                                         "Cache-Control": "no-store"})


@router.get("/deployments")
async def deployments(request: Request, me: Principal = CurrentPrincipal):
    rows = await request.app.state.db.fetch("""select d.*, v.site_id, g.campaign_id from deployments d
                                               join site_versions v on v.id = d.site_version_id join generated_sites g on g.id = v.site_id
                                               where d.workspace_id=$1 order by d.created_at desc limit 200""", me.workspace_id)
    return J([dict(r) for r in rows])


@router.get("/activity")
async def activity(request: Request, me: Principal = CurrentPrincipal, limit: int = 100):
    db = request.app.state.db
    return J({
        "tasks": [dict(r) for r in await db.fetch("""select id, campaign_id, task_type, status, attempts, max_attempts, error_code,
                                                     left(error_detail, 300) error_detail, created_at, started_at, finished_at
                                                     from tasks where workspace_id=$1 order by created_at desc limit $2""",
                                                  me.workspace_id, min(limit, 300))],
        "events": [dict(r) for r in await db.fetch("""select id, campaign_id, event_type, message, created_at from task_events
                                                      where workspace_id=$1 order by id desc limit $2""", me.workspace_id, min(limit, 300))],
        "audit": [dict(r) for r in await db.fetch("""select id, actor_type, actor_id, action, entity_type, entity_id, from_state, to_state,
                                                     created_at from audit_logs where workspace_id=$1 order by id desc limit $2""",
                                                  me.workspace_id, min(limit, 300))],
        "provider_errors": [dict(r) for r in await db.fetch("""select provider, operation, outcome, http_status, created_at from usage_events
                                                               where workspace_id=$1 and outcome in ('error','ambiguous','blocked')
                                                               order by id desc limit 50""", me.workspace_id)],
        "dead_letters": [dict(r) for r in await db.fetch("""select d.id, d.task_id, d.reason, d.created_at from dead_letters d
                                                            join tasks t on t.id = d.task_id where t.workspace_id=$1 order by d.id desc limit 50""",
                                                         me.workspace_id)],
    })
