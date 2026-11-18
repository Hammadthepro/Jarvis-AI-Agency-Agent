"""Commands, campaigns, manual import, live progress (SSE)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from jarvis.agents import command as cmd
from jarvis.agents.deps import Deps
from jarvis.api.auth import CurrentPrincipal, Principal
from jarvis.core.compliance import gate_status
from jarvis.core.errors import ComplianceBlocked, InvalidTransition, NotFound, ProviderError, ValidationFailed
from jarvis.db.audit import audit, emit
from jarvis.db.queue import enqueue, request_cancel
from jarvis.integrations.base import CallContext

router = APIRouter()


def _deps(request: Request) -> Deps:
    return request.app.state.deps


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    channel: str = Field("text", pattern="^(text|voice)$")


class AnswersIn(BaseModel):
    answers: dict


class ConfirmIn(BaseModel):
    parameters: dict | None = None
    budget_cap_usd: Decimal | None = Field(None, ge=0, le=10000)
    per_site_cap_usd: Decimal | None = Field(None, ge=0, le=1000)


def _jsonable(d: dict) -> dict:
    return json.loads(json.dumps(d, default=str))


async def plan_estimate(deps: Deps, ws: UUID, p: cmd.CampaignParameters) -> dict:
    """Worst-case unit/cost estimate from the yield model (docs/API_COST_MODEL.md §1.1)."""
    from jarvis.core.budget import claude_cost, unit_price
    r = 12 if p.minimum_google_rating is not None else 15
    d, w, v = 0.7, 0.2, 0.6
    no_site = "NO_WEBSITE" in p.website_filter
    per = r * d * (w * v if no_site else 1)
    places = 0 if p.discovery_provider != "google_places" else int(-(-p.target_count // max(per, 0.1)))
    serp = int(p.target_count * (3.75 if no_site else 1.4))
    fc = int(p.target_count * (2.9 if no_site else 1.0))
    claude = claude_cost("claude-haiku-5-5", 8000, 2000) * fc + p.sites_to_build * 1_560_000 \
        + claude_cost("claude-sonnet-5-5", 6000, 1200) * p.sites_to_build
    usd = places * unit_price("google_places", "text_search_enterprise") + serp * unit_price("serp_api", "search") \
        + fc * unit_price("firecrawl", "credit") + claude
    return {"places_requests_max": places, "serp_searches_max": serp, "firecrawl_credits_max": fc,
            "claude_usd_max": round(claude / 1e6, 2), "estimated_usd_max_if_paid": round(usd / 1e6, 2),
            "assumptions": {"results_per_request": r, "operational_share": d, "no_website_share": w, "confirmed_share": v},
            "note": "Units within free allowances cost $0; caps stop billable calls before limits are exceeded."}


async def _render(request: Request, row) -> dict:
    deps = _deps(request)
    out = dict(row)
    out["id"] = str(out["id"])
    if row["parsed"] and row["status"] in ("awaiting_confirmation",):
        p = cmd.CampaignParameters.model_validate(row["parsed"])
        out["plan"] = await plan_estimate(deps, row["workspace_id"], p)
        async with deps.db.acquire() as conn:
            out["compliance"] = await gate_status(conn, row["workspace_id"], "google_places_prospect_selection") \
                if p.discovery_provider == "google_places" else {"open": True}
    return _jsonable(out)


@router.post("/commands")
async def create_command(body: CommandIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    deps = _deps(request)
    request.app.state.limiter.hit(f"cmd:{me.user_id}", cost=3)
    res = cmd.deterministic_parse(body.text)
    parser_error = None
    if res.parameters is None and (any(q["field"] in ("niche", "location") for q in res.questions)) and deps.claude.configured:
        try:
            out = await deps.claude.structured(
                CallContext(deps.db, me.workspace_id), model=deps.settings.claude_model_fast, system=cmd.CLAUDE_SYSTEM,
                user=body.text, schema=cmd.CLAUDE_SCHEMA, max_tokens=600, operation="command.parse")
            res = cmd.from_claude(out.data)
        except ProviderError as exc:
            parser_error = f"{exc.state.value}: {exc.message[:200]}"
    status = "awaiting_confirmation" if res.parameters else "needs_clarification"
    row = await deps.db.fetchrow(
        """insert into commands(workspace_id, user_id, channel, raw_text, parser, parsed, questions, status, error)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9) returning *""",
        me.workspace_id, me.user_id, body.channel, body.text, res.parser,
        _jsonable(res.parameters.model_dump()) if res.parameters else _jsonable(res.draft), res.questions, status, parser_error)
    return await _render(request, row)


@router.get("/commands")
async def list_commands(request: Request, me: Principal = CurrentPrincipal, limit: int = 30):
    rows = await _deps(request).db.fetch("""select id, raw_text, channel, parser, status, campaign_id, questions, error, created_at
                                            from commands where workspace_id=$1 order by created_at desc limit $2""",
                                         me.workspace_id, min(limit, 100))
    return [_jsonable(dict(r)) for r in rows]


async def _own_command(deps: Deps, me: Principal, command_id: UUID):
    row = await deps.db.fetchrow("select * from commands where id=$1 and workspace_id=$2", command_id, me.workspace_id)
    if row is None:
        raise NotFound("command not found")
    return row


@router.post("/commands/{command_id}/answer")
async def answer(command_id: UUID, body: AnswersIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    deps = _deps(request)
    row = await _own_command(deps, me, command_id)
    if row["status"] not in ("needs_clarification", "awaiting_confirmation"):
        raise InvalidTransition(f"command is {row['status']}")
    res = cmd.apply_answers(row["parsed"] or {}, body.answers)
    status = "awaiting_confirmation" if res.parameters else "needs_clarification"
    new = await deps.db.fetchrow("""update commands set parsed=$2, questions=$3, status=$4 where id=$1 returning *""", command_id,
                                 _jsonable(res.parameters.model_dump()) if res.parameters else _jsonable(res.draft), res.questions, status)
    return await _render(request, new)


@router.post("/commands/{command_id}/confirm")
async def confirm(command_id: UUID, body: ConfirmIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    deps = _deps(request)
    row = await _own_command(deps, me, command_id)
    if row["status"] != "awaiting_confirmation":
        raise InvalidTransition(f"command is {row['status']}")
    data = {**(row["parsed"] or {}), **(body.parameters or {})}
    try:
        p = cmd.CampaignParameters.model_validate(data)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    p.require_complete()
    return await _create_campaign(deps, me, p, command_id=command_id, budget_cap=body.budget_cap_usd,
                                  per_site=body.per_site_cap_usd)


async def _create_campaign(deps: Deps, me: Principal, p: cmd.CampaignParameters, *, command_id: UUID | None,
                           budget_cap: Decimal | None, per_site: Decimal | None, import_rows: list[dict] | None = None) -> dict:
    defaults = json.loads((__import__("jarvis.core.budget", fromlist=["RES"]).RES / "default_limits.json").read_text())
    async with deps.db.transaction() as conn:
        if p.discovery_provider == "google_places":
            st = await gate_status(conn, me.workspace_id, "google_places_prospect_selection")
            if not st["open"]:
                if command_id:
                    await conn.execute("update commands set status='rejected', error=$2 where id=$1", command_id,
                                       f"COMPLIANCE_BLOCKED: {st['reason']}")
                raise ComplianceBlocked(st["reason"], details={"scope": st["scope"], "intended_use": st["intended_use"],
                                                               "alternative": "Import a business list you have the right to use "
                                                                              "(Campaigns > Import)."})
        name = f"{p.niche.title()} · {p.location.label()}"
        cid = await conn.fetchval(
            """insert into campaigns(workspace_id, command_id, name, niche, location, target_count, discovery_provider,
                   minimum_google_rating, missing_rating_policy, website_filter, stages, sites_to_build, state,
                   budget_cap_micros, per_site_cap_micros, authorized_at, authorized_by, current_stage)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'authorized',$13,$14,now(),$15,'queued') returning id""",
            me.workspace_id, command_id, name, p.niche, p.location.model_dump(), p.target_count, p.discovery_provider,
            p.minimum_google_rating, p.missing_rating_policy, p.website_filter, p.stages, p.sites_to_build,
            int(budget_cap * 1_000_000) if budget_cap is not None else defaults["campaign_default_cap_micros"],
            int(per_site * 1_000_000) if per_site is not None else defaults["site_default_cap_micros"], me.user_id)
        if command_id:
            await conn.execute("update commands set status='confirmed', campaign_id=$2 where id=$1", command_id, cid)
        for i, r in enumerate(import_rows or []):
            pid = await conn.fetchval(
                """insert into prospects(workspace_id, campaign_id, discovery_provider, external_ref, business_category)
                   values ($1,$2,'manual_import',$3,$4) returning id""", me.workspace_id, cid, f"row-{i + 1}", p.niche)
            ev = await conn.fetchval(
                """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt)
                   values ($1,$2,'manual_import',$3,'indefinite',$4) returning id""",
                me.workspace_id, pid, f"import:{r.get('source') or 'operator'}:row-{i + 1}", json.dumps(r))
            for key in ("business_name", "phone", "city", "state"):
                if r.get(key):
                    await conn.execute(
                        """insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote, confidence)
                           values ($1,$2,$3,$4,'verified',$5,$6,0.9)""",
                        me.workspace_id, pid, key, r[key], ev, str(r[key]))
            if r.get("website"):
                await conn.execute(
                    """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy)
                       values ($1,$2,'manual_import',$3,'indefinite')""", me.workspace_id, pid, f"website:{r['website']}")
        await enqueue(conn, workspace_id=me.workspace_id, campaign_id=cid, task_type="campaign.start",
                      idempotency_key=f"c:{cid}:start", priority=5)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="campaign.authorized",
                    entity_type="campaign", entity_id=cid, to_state="authorized", data=_jsonable(p.model_dump()))
        await emit(conn, workspace_id=me.workspace_id, campaign_id=cid, event_type="state",
                   message=f"Campaign authorized: {name} (target {p.target_count})")
    return {"campaign_id": str(cid), "state": "authorized"}


class ImportRow(BaseModel):
    business_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(None, max_length=40)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=2)
    website: str | None = Field(None, max_length=500)
    source: str | None = Field(None, max_length=200)


class ImportIn(BaseModel):
    niche: str
    location: cmd.Location
    rows: list[ImportRow] = Field(min_length=1, max_length=1000)
    rights_attestation: bool
    website_filter: list[cmd.WebsiteFilter] = Field(default_factory=lambda: ["ANY"])
    sites_to_build: int = Field(0, ge=0, le=50)
    stages: list[cmd.Stage] | None = None
    budget_cap_usd: Decimal | None = None


@router.post("/campaigns/import")
async def import_campaign(body: ImportIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    if not body.rights_attestation:
        raise ValidationFailed("you must attest that you have the right to use this business list")
    stages = body.stages or (cmd.ALL_STAGES if body.sites_to_build else ["discovery", "verification", "research", "qualification"])
    p = cmd.CampaignParameters(niche=body.niche.lower(), location=body.location, target_count=len(body.rows),
                               discovery_provider="manual_import", website_filter=body.website_filter,
                               sites_to_build=body.sites_to_build, stages=stages)
    return await _create_campaign(_deps(request), me, p, command_id=None, budget_cap=body.budget_cap_usd, per_site=None,
                                  import_rows=[r.model_dump() for r in body.rows])


@router.get("/campaigns")
async def list_campaigns(request: Request, me: Principal = CurrentPrincipal):
    rows = await _deps(request).db.fetch(
        """select c.id, c.name, c.niche, c.location, c.target_count, c.state, c.current_stage, c.blocked_reason, c.discovery_provider,
                  c.minimum_google_rating, c.missing_rating_policy, c.website_filter, c.sites_to_build, c.created_at, c.budget_cap_micros,
                  (select count(*) from prospects p where p.campaign_id = c.id) prospects,
                  (select count(*) from prospects p where p.campaign_id = c.id and p.state = 'qualified') qualified,
                  (select count(*) from generated_sites g where g.campaign_id = c.id and g.state = 'deployed') deployed,
                  (select coalesce(sum(amount_micros)::bigint,0) from cost_reservations r where r.campaign_id = c.id
                     and r.status in ('committed','ambiguous','reserved')) spent_micros
           from campaigns c where c.workspace_id = $1 and c.deleted_at is null order by c.created_at desc""", me.workspace_id)
    return [_jsonable(dict(r)) for r in rows]


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: UUID, request: Request, me: Principal = CurrentPrincipal):
    db = _deps(request).db
    c = await db.fetchrow("select * from campaigns where id=$1 and workspace_id=$2", campaign_id, me.workspace_id)
    if c is None:
        raise NotFound("campaign not found")
    funnel = await db.fetch("""select state, count(*) n from prospects where campaign_id=$1 group by state""", campaign_id)
    web = await db.fetch("""select v.status, v.site_health, count(*) n from website_verifications v join prospects p on p.id = v.prospect_id
                            where p.campaign_id = $1 group by 1,2""", campaign_id)
    tasks = await db.fetch("""select task_type, status, count(*) n from tasks where campaign_id=$1 group by 1,2""", campaign_id)
    costs = await db.fetch("""select provider, status, sum(amount_micros)::bigint micros, sum(units)::bigint units from cost_reservations
                              where campaign_id=$1 group by 1,2""", campaign_id)
    return _jsonable({"campaign": dict(c), "funnel": [dict(r) for r in funnel], "website": [dict(r) for r in web],
                      "tasks": [dict(r) for r in tasks], "costs": [dict(r) for r in costs]})


@router.post("/campaigns/{campaign_id}/{action}")
async def campaign_action(campaign_id: UUID, action: str, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    deps = _deps(request)
    if action not in ("pause", "resume", "cancel", "retry"):
        raise NotFound("unknown action")
    async with deps.db.transaction() as conn:
        c = await conn.fetchrow("select * from campaigns where id=$1 and workspace_id=$2 for update", campaign_id, me.workspace_id)
        if c is None:
            raise NotFound("campaign not found")
        allowed = {"pause": {"running", "authorized"}, "resume": {"paused", "blocked"}, "cancel": {"running", "authorized", "paused", "blocked",
                   "requires_human_review"}, "retry": {"blocked", "failed"}}[action]
        if c["state"] not in allowed:
            raise InvalidTransition(f"cannot {action} a {c['state']} campaign")
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
            if c["state"] == "authorized":
                await enqueue(conn, workspace_id=me.workspace_id, campaign_id=campaign_id, task_type="campaign.start",
                              idempotency_key=f"c:{campaign_id}:start")
            new = "running"
        else:
            await conn.execute("update campaigns set state='cancelled' where id=$1", campaign_id)
            await request_cancel(conn, workspace_id=me.workspace_id, campaign_id=campaign_id)
            new = "cancelled"
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action=f"campaign.{action}",
                    entity_type="campaign", entity_id=campaign_id, from_state=c["state"], to_state=new)
        await emit(conn, workspace_id=me.workspace_id, campaign_id=campaign_id, event_type="state", message=f"Campaign {new} by operator")
    return {"state": new}


@router.get("/events")
async def events(request: Request, me: Principal = CurrentPrincipal, campaign_id: UUID | None = None,
                 last_event_id: str | None = Header(None), after: int | None = None):
    """Server-sent events of task_events, resumable via Last-Event-ID."""
    db = _deps(request).db
    cursor = int(last_event_id or after or 0)
    if cursor == 0:
        cursor = (await db.fetchval("select coalesce(max(id),0) from task_events where workspace_id=$1", me.workspace_id)) - 50

    async def gen():
        nonlocal cursor
        idle = 0
        while not await request.is_disconnected():
            rows = await db.fetch("""select id, campaign_id, task_id, event_type, message, data, created_at from task_events
                                     where workspace_id = $1 and id > $2 and ($3::uuid is null or campaign_id = $3)
                                     order by id limit 200""", me.workspace_id, cursor, campaign_id)
            for r in rows:
                cursor = r["id"]
                yield f"id: {r['id']}\nevent: {r['event_type']}\ndata: {json.dumps(_jsonable(dict(r)))}\n\n"
            idle = 0 if rows else idle + 1
            if idle and idle % 15 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


class VoiceIn(BaseModel):
    user_id: UUID
    workspace_id: UUID
    text: str = Field(min_length=1, max_length=2000)


@router.post("/internal/voice/command")
async def voice_command(body: VoiceIn, request: Request, x_voice_agent_key: str | None = Header(None)):
    """Called only by the JARVIS LiveKit voice agent. Same command pipeline as text; returns text to speak."""
    import hmac as _h

    from jarvis.api.auth import Forbidden, Unauthorized
    from jarvis.api.auth import Principal as P
    s = request.app.state.settings
    if not s.voice_agent_shared_secret or not x_voice_agent_key or not _h.compare_digest(
            x_voice_agent_key, s.voice_agent_shared_secret.get_secret_value()):
        raise Unauthorized("voice agent key invalid or voice NOT_CONFIGURED")
    deps = _deps(request)
    role = await deps.db.fetchval("select role from memberships where workspace_id=$1 and user_id=$2", body.workspace_id, body.user_id)
    if role is None:
        raise Forbidden("not a member of this workspace")
    me = P(body.user_id, None, body.workspace_id, role)
    text = body.text.strip()
    if text.lower().rstrip(".!") in ("confirm", "yes confirm", "authorize", "authorise", "go ahead"):
        row = await deps.db.fetchrow("""select id from commands where workspace_id=$1 and user_id=$2 and status='awaiting_confirmation'
                                        order by created_at desc limit 1""", body.workspace_id, body.user_id)
        if not row:
            return {"say": "There is nothing waiting for confirmation."}
        try:
            out = await confirm(row["id"], ConfirmIn(), request, me)
        except ComplianceBlocked as exc:
            return {"say": f"I can't start that campaign: {exc.message}"}
        return {"say": "Campaign authorized. I'll report progress as I go.", "campaign_id": out["campaign_id"]}
    res = await create_command(CommandIn(text=text, channel="voice"), request, me)
    if res["status"] == "needs_clarification":
        return {"say": res["questions"][0]["prompt"], "command_id": res["id"]}
    p = res.get("parsed") or {}
    loc = ", ".join(x for x in ((p.get("location") or {}).get("city"), (p.get("location") or {}).get("state")) if x)
    summary = f"I'll look for {p.get('target_count')} {p.get('niche')} businesses in {loc}"
    if p.get("minimum_google_rating"):
        summary += f" rated at least {p['minimum_google_rating']}"
    if res.get("compliance") and not res["compliance"].get("open"):
        summary += ". Google Places discovery is blocked pending compliance approval, so I can't start this one; import a business list instead"
        return {"say": summary + ".", "command_id": res["id"]}
    return {"say": summary + ". Say confirm to authorize.", "command_id": res["id"]}
