"""JARVIS work items ("tasks" to the owner): one per campaign, with a clear lifecycle, the operation actually running,
completed and remaining steps, real timings and honest control (cancel / pause / resume / retry / switch).

Lifecycle (owner-facing) <- campaign state:
  QUEUED <- authorized · RUNNING <- running · PAUSED <- paused · WAITING <- blocked, requires_human_review
  COMPLETED <- completed · FAILED <- failed · CANCEL_REQUESTED <- cancel_requested · CANCELLED <- cancelled
Everything here is read from the database (tasks, task_events, usage_events, generated_sites, deployments): nothing is
estimated. There is no percentage: progress is the list of stages and their real state."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from uuid import UUID

from jarvis.core.errors import InvalidTransition, NotFound, ValidationFailed
from jarvis.db.audit import audit, emit
from jarvis.db.queue import enqueue, request_cancel

LIFECYCLE = {"draft": "QUEUED", "authorized": "QUEUED", "running": "RUNNING", "paused": "PAUSED", "blocked": "WAITING",
             "requires_human_review": "WAITING", "completed": "COMPLETED", "failed": "FAILED",
             "cancel_requested": "CANCEL_REQUESTED", "cancelled": "CANCELLED"}
TERMINAL = {"completed", "failed", "cancelled"}
ACTIVE = {"authorized", "running", "paused", "blocked", "requires_human_review", "cancel_requested"}
STAGES = [("discovery", "Find businesses"), ("verification", "Verify identity and websites"), ("research", "Research"),
          ("qualification", "Qualify"), ("build", "Build websites"), ("qa", "Browser QA"), ("deploy", "Deploy previews")]
STAGE_TASK = {"discovery": ("campaign.start", "discovery.listings", "discovery.serp", "discovery.page"),
              "verification": ("verify.prospect",), "research": ("research.prospect",), "qualification": ("qualify.campaign",),
              "build": ("site.build",), "qa": (), "deploy": ("site.deploy",)}
PROVIDER_LABEL = {"serp_api": "SerpApi", "geoapify": "Geoapify", "google_places": "Google Places"}
SITE_OPERATION = {"queued": "Waiting to build a website", "strategy": "Generating website structure and content",
                  "generating": "Generating website content", "building": "Writing website files",
                  "qa": "Running isolated browser QA", "repair": "Fixing QA findings and rebuilding"}


def operation_for(task_type: str, payload: dict, site_state: str | None = None) -> str:
    if task_type in ("discovery.listings", "discovery.serp"):
        return f"Searching {PROVIDER_LABEL.get(payload.get('provider') or 'serp_api', 'listings')} and checking each business's website"
    if task_type == "discovery.page":
        return "Searching Google Places and checking each business's website"
    return {"campaign.start": "Understanding the request and starting", "verify.prospect": "Checking website URLs",
            "research.prospect": "Researching businesses and their services", "qualify.campaign": "Calculating qualification",
            "site.build": SITE_OPERATION.get(site_state or "", "Building a website"), "site.deploy": "Deploying the preview",
            "site.qa": "Running isolated browser QA", "outreach.draft": "Drafting outreach (not sent)",
            "campaign.finalize": "Finishing up", "provider.evaluate": "Comparing discovery providers"}.get(task_type, task_type)


def _iso(x):
    return x.isoformat() if x else None


async def summary(db, workspace_id: UUID, campaign_id: UUID, *, detail: bool = False) -> dict:
    from jarvis.workflow.progress import campaign_progress
    c = await db.fetchrow("select * from campaigns where id=$1 and workspace_id=$2", campaign_id, workspace_id)
    if c is None:
        raise NotFound("task not found")
    tasks = await db.fetch(
        """select t.task_type, t.status::text status, t.attempts, t.payload, t.created_at, t.started_at, t.finished_at,
                  t.subject_id, t.error_code, left(t.error_detail, 300) error_detail, g.state::text site_state
           from tasks t left join generated_sites g on g.id = t.subject_id and t.task_type in ('site.build','site.deploy')
           where t.campaign_id=$1 order by t.created_at""", campaign_id)
    running = [t for t in tasks if t["status"] in ("running", "leased")]
    pending = [t for t in tasks if t["status"] in ("pending", "retry_wait")]
    state = c["state"]
    if state == "cancel_requested":
        op = ("Stopping: waiting for the current step to finish (" +
              ", ".join(sorted({operation_for(t["task_type"], t["payload"], t["site_state"]) for t in running})) + ")") \
            if running else "Stopping"
    elif running:
        op = "; ".join(sorted({operation_for(t["task_type"], t["payload"], t["site_state"]) for t in running}))
    elif state == "paused":
        op = "Paused"
    elif pending:
        op = "Waiting in the queue: " + operation_for(pending[0]["task_type"], pending[0]["payload"], pending[0]["site_state"])
    elif state in ("blocked", "requires_human_review"):
        op = c["blocked_reason"] or "Waiting for you"
    else:
        op = {"completed": "Done", "failed": "Failed", "cancelled": "Cancelled", "authorized": "Queued"}.get(state, state)
    steps = []
    for key, label in STAGES:
        if key not in (c["stages"] or []) and not (key == "verification" and "discovery" in (c["stages"] or [])):
            continue
        ts = [t for t in tasks if t["task_type"] in STAGE_TASK[key]]
        if key == "verification" and not ts:
            # listing providers verify each business inside the discovery task itself
            ts = [t for t in tasks if t["task_type"] in ("discovery.listings", "discovery.serp", "discovery.page")]
        if key == "qa":
            ts = [t for t in tasks if t["task_type"] == "site.build"]
            st = ("done" if ts and all(t["status"] == "succeeded" for t in ts) else
                  "running" if any(t["site_state"] in ("qa", "repair") for t in ts) else "pending")
        elif not ts:
            st = "pending" if state not in TERMINAL else "skipped"
        elif any(t["status"] in ("running", "leased") for t in ts):
            st = "running"
        elif any(t["status"] in ("pending", "retry_wait") for t in ts):
            st = "queued"
        elif any(t["status"] in ("failed", "dead", "blocked") for t in ts):
            st = "failed" if all(t["status"] != "succeeded" for t in ts) else "partly_failed"
        elif any(t["status"] == "cancelled" for t in ts):
            st = "cancelled"
        else:
            st = "done"
        steps.append({"stage": key, "label": label, "state": st})
    started = c["authorized_at"] or c["created_at"]
    ended = max((t["finished_at"] for t in tasks if t["finished_at"]), default=None) if state in TERMINAL else None
    elapsed = ((ended or datetime.now(UTC)) - started).total_seconds()
    prog = await campaign_progress(db, workspace_id, campaign_id)
    out = {"id": str(c["id"]), "name": c["name"], "kind": c["work_kind"], "status": LIFECYCLE.get(state, state.upper()),
           "state": state, "command": (c["objective"] or {}).get("original_request"),
           "objective": (c["objective"] or {}), "current_operation": op, "steps": steps,
           "completed_steps": [s["label"] for s in steps if s["state"] == "done"],
           "remaining_steps": [s["label"] for s in steps if s["state"] in ("pending", "queued", "running")],
           "started_at": _iso(started), "updated_at": _iso(c["updated_at"]), "completed_at": _iso(ended),
           "elapsed_s": round(elapsed, 1), "result": prog.get("say"), "counts": prog.get("counts"),
           "error": c["blocked_reason"], "cancel_requested_at": _iso(c["cancel_requested_at"]),
           "parent_task_id": str(c["parent_campaign_id"]) if c["parent_campaign_id"] else None,
           "conversation_command_id": str(c["command_id"]) if c["command_id"] else None,
           "running_now": len(running), "retries": sum(max(0, t["attempts"] - 1) for t in tasks)}
    if detail:
        out["timings"] = await timings(db, campaign_id, tasks)
        out["activity"] = [{**dict(r), "created_at": _iso(r["created_at"])} for r in await db.fetch(
            """select message, event_type, created_at from task_events where campaign_id=$1 order by id desc limit 40""",
            campaign_id)]
        out["failures"] = [{"task": t["task_type"], "status": t["status"], "attempts": t["attempts"], "code": t["error_code"],
                            "detail": t["error_detail"]} for t in tasks if t["status"] in ("failed", "dead", "blocked")]
    return out


async def timings(db, campaign_id: UUID, tasks=None) -> dict:
    """Measured durations per stage (wall clock and busy time), queue waiting, retries, and per-provider latency, calls,
    failures, tokens and estimated cost (from usage_events, recorded by the gateway on every provider call)."""
    rows = await db.fetch(
        """select task_type, count(*) n, sum(greatest(attempts - 1, 0)) retries,
                  extract(epoch from max(finished_at) - min(started_at)) wall_s,
                  sum(extract(epoch from finished_at - started_at)) busy_s,
                  avg(extract(epoch from started_at - created_at)) queue_wait_s
           from tasks where campaign_id=$1 and started_at is not null group by task_type order by min(started_at)""", campaign_id)
    prov = await db.fetch(
        """select provider, operation, count(*) calls, count(*) filter (where outcome <> 'ok') failures,
                  round(avg(latency_ms)) avg_ms, max(latency_ms) max_ms, sum(coalesce(tokens_in,0)) tokens_in,
                  sum(coalesce(tokens_out,0)) tokens_out, sum(estimated_cost_micros) cost_micros
           from usage_events where campaign_id=$1 group by provider, operation order by provider, operation""", campaign_id)
    f = lambda v: round(float(v), 2) if v is not None else None  # noqa: E731
    return {"stages": [{"task": r["task_type"], "tasks": r["n"], "retries": int(r["retries"] or 0), "wall_s": f(r["wall_s"]),
                        "busy_s": f(r["busy_s"]), "queue_wait_s": f(r["queue_wait_s"])} for r in rows],
            "providers": [{**{k: r[k] for k in ("provider", "operation", "calls", "failures", "max_ms")},
                           "avg_ms": int(r["avg_ms"]) if r["avg_ms"] is not None else None,
                           "tokens_in": int(r["tokens_in"]), "tokens_out": int(r["tokens_out"]),
                           "estimated_cost_usd": round(int(r["cost_micros"] or 0) / 1e6, 4)} for r in prov]}


async def list_work(db, workspace_id: UUID, limit: int = 20) -> list[dict]:
    ids = await db.fetch("""select id from campaigns where workspace_id=$1 and deleted_at is null
                            order by (state::text in ('running','authorized','paused','blocked','requires_human_review',
                                                      'cancel_requested')) desc, created_at desc limit $2""",
                         workspace_id, min(limit, 50))
    return [await summary(db, workspace_id, r["id"]) for r in ids]


async def control(db, me, campaign_id: UUID, action: str) -> dict:
    """pause / resume / cancel / retry. Cancel is honest: with a step still executing the task is CANCEL_REQUESTED until
    that step stops (the worker interrupts it at its next lease heartbeat); results already produced are kept."""
    if action not in ("pause", "resume", "cancel", "retry"):
        raise NotFound("unknown action")
    async with db.transaction() as conn:
        c = await conn.fetchrow("select * from campaigns where id=$1 and workspace_id=$2 for update", campaign_id, me.workspace_id)
        if c is None:
            raise NotFound("task not found")
        allowed = {"pause": {"running", "authorized"}, "resume": {"paused", "blocked"},
                   "cancel": {"running", "authorized", "paused", "blocked", "requires_human_review"},
                   "retry": {"blocked", "failed", "requires_human_review"}}[action]
        if c["state"] not in allowed:
            raise InvalidTransition(f"cannot {action} a task that is {LIFECYCLE.get(c['state'], c['state']).lower()}")
        if action == "pause":
            await conn.execute("update campaigns set state='paused' where id=$1", campaign_id)
            await conn.execute("""update tasks set status='blocked', error_code='PAUSED' where campaign_id=$1
                                  and status in ('pending','retry_wait')""", campaign_id)
            new = "paused"
        elif action in ("resume", "retry"):
            await conn.execute("update campaigns set state='running', blocked_reason=null where id=$1", campaign_id)
            await conn.execute("""update tasks set status='pending', run_after=now(), error_code=null,
                                  attempts=least(attempts, max_attempts - 1) where campaign_id=$1 and status in ('blocked','failed','dead')""",
                               campaign_id)
            if action == "retry":
                await conn.execute("""update generated_sites set state='queued', last_error=null where campaign_id=$1
                                      and state='failed'""", campaign_id)
            if c["state"] == "authorized":
                await enqueue(conn, workspace_id=me.workspace_id, campaign_id=campaign_id, task_type="campaign.start",
                              idempotency_key=f"c:{campaign_id}:start")
            new = "running"
        else:
            await request_cancel(conn, workspace_id=me.workspace_id, campaign_id=campaign_id)
            busy = await conn.fetchval("""select count(*) from tasks where campaign_id=$1 and status in ('running','leased')""",
                                       campaign_id)
            new = "cancel_requested" if busy else "cancelled"
            await conn.execute("update campaigns set state=$2::campaign_state, cancel_requested_at=now() where id=$1",
                               campaign_id, new)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action=f"campaign.{action}",
                    entity_type="campaign", entity_id=campaign_id, from_state=c["state"], to_state=new)
        await emit(conn, workspace_id=me.workspace_id, campaign_id=campaign_id, event_type="state",
                   message={"cancel_requested": "Cancellation requested: the current step will stop shortly",
                            "cancelled": "Cancelled", "paused": "Paused (a step already executing finishes first)",
                            "running": "Resumed"}[new])
    return {"state": new, "status": LIFECYCLE[new]}


async def finish_cancellation(db, campaign_id: UUID) -> bool:
    """Called by the worker after every task: a CANCEL_REQUESTED task becomes CANCELLED once nothing is executing."""
    async with db.transaction() as conn:
        c = await conn.fetchrow("select workspace_id, state::text state from campaigns where id=$1 for update", campaign_id)
        if c is None or c["state"] != "cancel_requested":
            return False
        if await conn.fetchval("select count(*) from tasks where campaign_id=$1 and status in ('running','leased')", campaign_id):
            return False
        await conn.execute("update campaigns set state='cancelled' where id=$1", campaign_id)
        await emit(conn, workspace_id=c["workspace_id"], campaign_id=campaign_id, event_type="state",
                   message="Cancelled: the last running step has stopped; results found so far are kept")
    return True


async def start_website_build(deps, me, *, parent_id: UUID | None, prospect_ids: list[UUID] | None = None,
                              count: int = 1) -> dict:
    """A website-build task from earlier results: builds, browser-QAs and privately deploys previews for the chosen
    prospects (default: the best qualified ones of `parent_id` without a site yet). Never builds for a prospect whose
    site is being built right now."""
    db = deps.db
    count = max(1, min(int(count or 1), 10))
    parent = await db.fetchrow("select * from campaigns where id=$1 and workspace_id=$2", parent_id, me.workspace_id) \
        if parent_id else None
    if prospect_ids:
        rows = await db.fetch("""select p.id, p.campaign_id from prospects p where p.id = any($1::uuid[]) and p.workspace_id=$2""",
                              prospect_ids, me.workspace_id)
    elif parent:
        rows = await db.fetch(
            """select p.id, p.campaign_id from prospects p
               left join lateral (select total, readiness from qualification_scores q where q.prospect_id=p.id
                                  order by computed_at desc limit 1) s on true
               where p.campaign_id=$1 and p.state <> 'rejected' and p.review_reason is null
                 and exists (select 1 from research_facts f where f.prospect_id=p.id and f.fact_key='business_name' and f.status='verified')
               order by (s.readiness = 'qualified') desc nulls last, s.total desc nulls last, p.created_at limit 50""", parent["id"])
    else:
        raise ValidationFailed("say which business to build a website for, or start from an earlier search")
    if not rows:
        raise ValidationFailed("there are no verified businesses to build a website for yet")
    busy = {r["prospect_id"] for r in await db.fetch(
        """select prospect_id from generated_sites where prospect_id = any($1::uuid[])
           and state in ('strategy','generating','building','qa','repair')""", [r["id"] for r in rows])}
    chosen = [r for r in rows if r["id"] not in busy][:count]
    if not chosen:
        raise ValidationFailed("the websites for those businesses are already being built")
    src = parent or await db.fetchrow("select * from campaigns where id=$1", chosen[0]["campaign_id"])
    from jarvis.core.budget import RES
    defaults = json.loads((RES / "default_limits.json").read_text())
    async with db.transaction() as conn:
        cid = await conn.fetchval(
            """insert into campaigns(workspace_id, name, niche, location, target_count, discovery_provider, website_filter, stages,
                   sites_to_build, state, budget_cap_micros, per_site_cap_micros, authorized_at, authorized_by, current_stage,
                   objective, parent_campaign_id, work_kind)
               values ($1,$2,$3,$4,$5,'manual_import','{ANY}','{build,qa,deploy}',$6,'authorized',$7,$8,now(),$9,'queued',$10,$11,
                       'website_build') returning id""",
            me.workspace_id, f"Website build · {src['niche'].title()}", src["niche"], src["location"], len(chosen), len(chosen),
            src["budget_cap_micros"] or defaults["campaign_default_cap_micros"],
            src["per_site_cap_micros"] or defaults["site_default_cap_micros"], me.user_id,
            {"task": "website_build", "original_request": f"Build {len(chosen)} preview website(s)",
             "from_task": str(src["id"])}, parent["id"] if parent else src["id"])
        for r in chosen:
            await conn.execute(
                """insert into generated_sites(workspace_id, prospect_id, campaign_id, slug) values ($1,$2,$3,$4)
                   on conflict (prospect_id) do update set campaign_id=excluded.campaign_id, state='queued', last_error=null""",
                me.workspace_id, r["id"], cid, f"{src['niche'].replace(' ', '-')}-{secrets.token_hex(4)}")
        await enqueue(conn, workspace_id=me.workspace_id, campaign_id=cid, task_type="campaign.start",
                      idempotency_key=f"c:{cid}:start", priority=6)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="website_build.started",
                    entity_type="campaign", entity_id=cid, to_state="authorized", data={"prospects": [str(r["id"]) for r in chosen]})
        await emit(conn, workspace_id=me.workspace_id, campaign_id=cid, event_type="state",
                   message=f"Website build queued for {len(chosen)} business(es)")
    return {"task_id": str(cid), "prospects": [str(r["id"]) for r in chosen], "skipped_busy": len(busy)}
