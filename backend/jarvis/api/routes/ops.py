"""Health, identity, settings, budgets, integrations, compliance, usage, voice, outreach, unsubscribe, webhooks."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from jarvis.agents import outreach
from jarvis.api.auth import CurrentPrincipal, Principal, create_workspace
from jarvis.core.compliance import INTENDED_USE, gate_status
from jarvis.core.errors import (
    ComplianceBlocked,
    Forbidden,
    InvalidTransition,
    JarvisError,
    NotFound,
    ProviderError,
    ValidationFailed,
)
from jarvis.db.audit import audit
from jarvis.integrations.base import CallContext
from jarvis.integrations.resend import verify_svix

public = APIRouter()
router = APIRouter()
PROVIDERS = ["anthropic", "google_places", "serp_api", "firecrawl", "cloudflare", "resend", "livekit"]


def J(x):  # noqa: N802
    return json.loads(json.dumps(x, default=str))


# ---------------- public ----------------
@public.get("/health")
async def health():
    return {"status": "ok"}


@public.get("/ready")
async def ready(request: Request):
    """Ready only when the database is reachable AND every migration is applied (a missing table such as
    integration_credentials is reported here, not discovered later as a 500)."""
    from jarvis.db.migrate import status_conn
    db = request.app.state.db
    ok = await db.health()
    body = {"database": db.state, "detail": db.last_error, "migrations": None}
    if ok:
        async with db.acquire() as conn:
            st = await status_conn(conn)
        body["migrations"] = {"ok": st.ok, "applied": len(st.applied), "pending": st.pending, "modified": st.modified,
                              "detail": None if st.ok else st.describe()}
        ok = st.ok
    return JSONResponse(body, status_code=200 if ok else 503)


class DevTokenIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")


@public.post("/auth/dev-token")
async def dev_token(body: DevTokenIn, request: Request):
    """Local/test only: mints an HS256 token in the same shape as Supabase Auth. Disabled in production."""
    s = request.app.state.settings
    if s.jarvis_env not in ("local", "test") or not s.supabase_jwt_secret:
        raise Forbidden("dev tokens are disabled in this environment")
    sub = str(uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis-dev:{body.email.lower()}"))
    now = int(time.time())
    claims = {"sub": sub, "email": body.email.lower(), "aud": s.supabase_jwt_audience, "role": "authenticated",
              "iat": now, "exp": now + 12 * 3600}
    if s.supabase_jwt_issuer:
        claims["iss"] = s.supabase_jwt_issuer
    return {"access_token": jwt.encode(claims, s.supabase_jwt_secret.get_secret_value(), algorithm="HS256"), "user_id": sub}


@public.get("/u/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str):
    return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width"><title>Unsubscribe</title>
<form method=post action="/u/{token}" style="font-family:system-ui;max-width:30rem;margin:4rem auto">
<h1>Unsubscribe</h1><p>Confirm you no longer want to receive emails from us.</p><button type=submit>Unsubscribe</button></form>""")


@public.post("/u/{token}")
async def unsubscribe(token: str, request: Request):
    """One-click unsubscribe (RFC 8058). Fails closed: success is shown only after the suppression
    record is committed in the authoritative database."""
    s = request.app.state.settings
    if not s.unsubscribe_signing_key:
        raise JarvisError("unsubscribe is not configured")
    ws, email = outreach.parse_unsubscribe_token(s.unsubscribe_signing_key.get_secret_value(), token)
    db = request.app.state.db
    async with db.transaction() as conn:
        await outreach.suppress(conn, ws, "email", email, "opt_out", "unsubscribe_link")
        await conn.execute("""update outreach_drafts set state='opted_out' where workspace_id=$1 and state in ('draft','authorized','queued')
                              and contact_id in (select id from contacts where workspace_id=$1 and lower(value)=$2)""", ws, email.lower())
        await audit(conn, workspace_id=ws, actor_type="system", actor_id="unsubscribe", action="suppression.opt_out",
                    entity_type="email_hash", entity_id=outreach.value_hash("email", email))
    return HTMLResponse("<p style='font-family:system-ui'>You have been unsubscribed. You will not receive further emails from us.</p>")


@public.post("/webhooks/resend")
async def resend_webhook(request: Request):
    s = request.app.state.settings
    if not s.resend_webhook_secret:
        raise JarvisError("webhook secret not configured")
    body = await request.body()
    h = request.headers
    if not verify_svix(s.resend_webhook_secret.get_secret_value(), h.get("svix-id", ""), h.get("svix-timestamp", ""), body,
                       h.get("svix-signature", "")):
        raise Forbidden("invalid webhook signature")
    ev = json.loads(body)
    typ, data = ev.get("type", ""), ev.get("data", {})
    msg_id = data.get("email_id")
    db = request.app.state.db
    row = await db.fetchrow("""select o.workspace_id, o.draft_id, c.value email from email_outbox o join outreach_drafts d on d.id = o.draft_id
                               left join contacts c on c.id = d.contact_id where o.provider_message_id = $1""", msg_id)
    if row is None:
        return {"ignored": True}
    async with db.transaction() as conn:
        if typ in ("email.bounced", "email.complained"):
            reason = "bounce" if typ == "email.bounced" else "complaint"
            await outreach.suppress(conn, row["workspace_id"], "email", row["email"], reason, typ)
            await outreach.suppress(conn, row["workspace_id"], "domain", row["email"].split("@")[1], reason, typ)
            await conn.execute("update outreach_drafts set state=$2 where id=$1", row["draft_id"], "bounced" if reason == "bounce" else "opted_out")
        elif typ == "email.delivered":
            pass
        await audit(conn, workspace_id=row["workspace_id"], actor_type="system", actor_id="resend", action=f"webhook.{typ}",
                    entity_type="outreach_draft", entity_id=row["draft_id"])
    return {"ok": True}


# ---------------- authenticated ----------------
@router.get("/me")
async def me_(request: Request, me: Principal = CurrentPrincipal):
    db = request.app.state.db
    ws = await db.fetch("""select w.id, w.name, m.role from memberships m join workspaces w on w.id = m.workspace_id
                           where m.user_id=$1 order by m.created_at""", me.user_id)
    s = request.app.state.settings
    local = s.local_dev_access_active and bool(me.email) and me.email.lower() == s.jarvis_local_dev_email.lower()
    return J({"user_id": me.user_id, "email": me.email, "workspace_id": me.workspace_id, "role": me.role,
              "auth_mode": "local_development" if local else "token", "environment": s.jarvis_env,
              "workspaces": [dict(r) for r in ws]})


@router.post("/workspaces")
async def new_workspace(body: dict, request: Request, me: Principal = CurrentPrincipal):
    name = (body.get("name") or "").strip()
    if not 1 <= len(name) <= 120:
        raise ValidationFailed("name required")
    return {"workspace_id": str(await create_workspace(request.app.state.db, me.user_id, name))}


class SettingsIn(BaseModel):
    global_kill_switch: bool | None = None
    provider_kill_switches: dict[str, bool] | None = None
    agency_name: str | None = Field(None, max_length=120)
    terms_of_use_url: str | None = Field(None, pattern=r"^https://", max_length=500)
    privacy_policy_url: str | None = Field(None, pattern=r"^https://", max_length=500)


@router.get("/settings")
async def get_settings_(request: Request, me: Principal = CurrentPrincipal):
    s = request.app.state.settings
    row = await request.app.state.db.fetchrow("select * from system_settings where workspace_id=$1", me.workspace_id)
    from jarvis.ops.backup import backup_status
    async with request.app.state.db.acquire() as conn:
        backup, b = await backup_status(conn)
    return J({"settings": dict(row) if row else {}, "models": {"fast": s.claude_model_fast, "build": s.claude_model_build,
              "escalation": s.claude_model_escalation}, "environment": s.jarvis_env, "qa_runner_mode": s.qa_runner_mode,
              "preview_project": s.cloudflare_pages_preview_project, "preview_ttl_days": s.preview_ttl_days,
              "backup": backup, "last_backup": b})


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    db = request.app.state.db
    upd = body.model_dump(exclude_none=True)
    if not upd:
        return {"ok": True}
    async with db.transaction() as conn:
        for k, v in upd.items():
            await conn.execute(f"update system_settings set {k} = $2 where workspace_id = $1", me.workspace_id, v)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="settings.updated", data=J(upd))
    return {"ok": True}


@router.get("/settings/limits")
async def get_limits(request: Request, me: Principal = CurrentPrincipal):
    rows = await request.app.state.db.fetch("select * from budget_limits where workspace_id=$1 order by scope, provider, unit, period",
                                            me.workspace_id)
    return J([dict(r) for r in rows])


class LimitIn(BaseModel):
    cap: int = Field(ge=0)
    allow_paid: bool = False


@router.put("/settings/limits/{limit_id}")
async def put_limit(limit_id: uuid.UUID, body: LimitIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    db = request.app.state.db
    async with db.transaction() as conn:
        row = await conn.fetchrow("select * from budget_limits where id=$1 and workspace_id=$2 for update", limit_id, me.workspace_id)
        if row is None:
            raise NotFound("limit not found")
        if row["free_allowance"] is not None and body.cap > row["free_allowance"] and not body.allow_paid:
            raise ValidationFailed("cap exceeds the provider's free allowance; set allow_paid=true to permit paid usage")
        await conn.execute("update budget_limits set cap=$2, allow_paid=$3 where id=$1", limit_id, body.cap, body.allow_paid)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="limit.updated",
                    entity_type="budget_limit", entity_id=limit_id, data={"from": row["cap"], "to": body.cap, "allow_paid": body.allow_paid})
    return {"ok": True}


def _missing(p: str) -> str:
    return {"anthropic": "Set ANTHROPIC_API_KEY (Claude Console API key; a Claude Code subscription is not API access)",
            "google_places": "Set GOOGLE_PLACES_API_KEY (Google Cloud project with only Places API (New) enabled)",
            "serp_api": "Set SERPAPI_API_KEY", "firecrawl": "Set FIRECRAWL_API_KEY",
            "cloudflare": "Set CLOUDFLARE_API_TOKEN (Pages:Edit) and CLOUDFLARE_ACCOUNT_ID",
            "resend": "Set EMAIL_PROVIDER=resend and RESEND_API_KEY (requires your own sending domain)",
            "livekit": "Set LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET"}[p]


class ApprovalIn(BaseModel):
    scope: str
    approver_type: str = Field(pattern="^(google|legal_counsel|operator)$")
    approver_name: str = Field(min_length=2, max_length=200)
    document_ref: str = Field(min_length=3, max_length=500)
    intended_use_text: str
    approved_at: datetime
    review_by: datetime


@router.get("/compliance")
async def compliance(request: Request, me: Principal = CurrentPrincipal):
    async with request.app.state.db.acquire() as conn:
        gates = [await gate_status(conn, me.workspace_id, s) for s in INTENDED_USE]
    rows = await request.app.state.db.fetch("""select id, scope, approver_type, approver_name, document_ref, approved_at, review_by,
                                               revoked_at, created_at from compliance_approvals where workspace_id=$1 order by created_at desc""",
                                            me.workspace_id)
    return J({"gates": gates, "approvals": [dict(r) for r in rows],
              "intended_use": {k: {"version": v[0], "text": v[1]} for k, v in INTENDED_USE.items()}})


@router.post("/compliance/approvals")
async def record_approval(body: ApprovalIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    if body.scope not in INTENDED_USE:
        raise ValidationFailed("unknown scope")
    version, text = INTENDED_USE[body.scope]
    if body.intended_use_text.strip() != text:
        raise ValidationFailed("intended_use_text must exactly match the current intended-use statement for this scope")
    if body.approver_type == "operator" and body.scope != "google_places_technical_testing":
        raise ValidationFailed("operator self-approval is only allowed for technical testing")
    async with request.app.state.db.transaction() as conn:
        await conn.execute("update compliance_approvals set revoked_at=now(), revoked_by=$3 where workspace_id=$1 and scope=$2 and revoked_at is null",
                           me.workspace_id, body.scope, me.user_id)
        aid = await conn.fetchval("""insert into compliance_approvals(workspace_id, scope, intended_use_text, intended_use_version, approver_type,
                                        approver_name, document_ref, approved_at, review_by, recorded_by)
                                     values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) returning id""", me.workspace_id, body.scope, text, version,
                                  body.approver_type, body.approver_name, body.document_ref, body.approved_at, body.review_by, me.user_id)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="compliance.approval_recorded",
                    entity_type="compliance_approval", entity_id=aid, data={"scope": body.scope, "approver_type": body.approver_type,
                                                                              "document_ref": body.document_ref})
    return {"id": str(aid)}


@router.delete("/compliance/approvals/{approval_id}")
async def revoke(approval_id: uuid.UUID, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    async with request.app.state.db.transaction() as conn:
        res = await conn.execute("""update compliance_approvals set revoked_at=now(), revoked_by=$3 where id=$1 and workspace_id=$2
                                    and revoked_at is null""", approval_id, me.workspace_id, me.user_id)
        if not res.endswith("1"):
            raise NotFound("active approval not found")
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="compliance.approval_revoked",
                    entity_type="compliance_approval", entity_id=approval_id)
    return {"ok": True}


@router.get("/usage")
async def usage(request: Request, me: Principal = CurrentPrincipal):
    """Usage this month, kept as separate figures:
    estimated (worst-case reserved before each call), reserved (still open), committed (units from provider
    responses, priced locally), ambiguous (outcome unknown, still counted), released (provably not billed), and
    provider-reported (from usage sync; status 'unavailable'/'not_configured'/'never_synced' means UNKNOWN, never zero).

    Every JARVIS money figure is a local estimate (`jarvis_cost_basis = local_estimate`). `reconciled` is true only
    when the provider itself reported account-level usage. `invoice_status` is CONFIRMED only for an imported
    provider billing export; otherwise UNAVAILABLE (or MANUAL_ENTRY_UNVERIFIED)."""
    db = request.app.state.db
    rows = await db.fetch(
        """select provider,
                  coalesce(sum(estimated_micros) filter (where status <> 'released'), 0)::bigint estimated_micros,
                  coalesce(sum(amount_micros) filter (where status = 'reserved'), 0)::bigint reserved_micros,
                  coalesce(sum(amount_micros) filter (where status = 'committed'), 0)::bigint committed_micros,
                  coalesce(sum(amount_micros) filter (where status = 'ambiguous'), 0)::bigint ambiguous_micros,
                  coalesce(sum(estimated_micros) filter (where status = 'released'), 0)::bigint released_micros,
                  coalesce(sum(units) filter (where status = 'committed'), 0)::bigint committed_units,
                  count(*) filter (where status = 'ambiguous') ambiguous_calls, count(*) calls
           from cost_reservations where workspace_id = $1 and created_at >= date_trunc('month', now()) group by provider""",
        me.workspace_id)
    reported = await db.fetch(
        """select distinct on (provider) provider, unit, reported_units, reported_cost_micros, cost_basis, source, status, detail,
                  raw, fetched_at
           from provider_usage_reports where (workspace_id is null or workspace_id = $1)
           order by provider, fetched_at desc""", me.workspace_id)
    invoiced = {r["provider"]: r["cost_basis"] for r in await db.fetch(
        """select distinct on (provider) provider, cost_basis from provider_usage_reports
           where (workspace_id is null or workspace_id = $1) and cost_basis in ('provider_invoice', 'manual_entry')
             and period_end >= date_trunc('month', now()) order by provider, (cost_basis = 'provider_invoice') desc, fetched_at desc""",
        me.workspace_id)}
    tokens = await db.fetchrow("""select coalesce(sum(tokens_in + tokens_out), 0)::bigint t, count(*) n from usage_events
                                  where workspace_id = $1 and provider = 'anthropic' and outcome = 'ok'
                                    and created_at >= date_trunc('month', now())""", me.workspace_id)
    daily = await db.fetch("""select date_trunc('day', created_at) as day, provider, sum(amount_micros)::bigint micros from cost_reservations
                              where workspace_id=$1 and status in ('committed','ambiguous','reserved') and created_at > now() - interval '31 days'
                              group by 1,2 order by 1""", me.workspace_id)
    per_campaign = await db.fetch("""select c.id, c.name, coalesce(sum(r.amount_micros) filter (where r.status in ('committed','ambiguous','reserved')),0)::bigint micros
                                     from campaigns c left join cost_reservations r on r.campaign_id = c.id where c.workspace_id=$1 group by 1,2
                                     order by 3 desc limit 50""", me.workspace_id)
    per_site = await db.fetch("""select g.id, g.slug, g.cost_micros from generated_sites g where g.workspace_id=$1 order by g.cost_micros desc limit 50""",
                              me.workspace_id)
    est = {r["provider"]: dict(r) for r in rows}
    rep = {r["provider"]: dict(r) for r in reported}
    empty = {"estimated_micros": 0, "reserved_micros": 0, "committed_micros": 0, "ambiguous_micros": 0, "released_micros": 0,
             "committed_units": 0, "ambiguous_calls": 0, "calls": 0}
    summary = []
    for p in sorted(set(est) | set(PROVIDERS)):
        e = {k: v for k, v in est.get(p, empty).items() if k != "provider"}
        r = rep.get(p) or {"status": "never_synced", "detail": "usage sync has not run for this provider"}
        if p == "anthropic":
            e["response_tokens"], e["responses"] = tokens["t"], tokens["n"]   # provider-reported per response
        account_reported = r.get("status") == "ok" and r.get("source") != "response_usage"
        invoice = {"provider_invoice": "CONFIRMED", "manual_entry": "MANUAL_ENTRY_UNVERIFIED"}.get(invoiced.get(p), "UNAVAILABLE")
        summary.append({"provider": p, "jarvis": e, "jarvis_cost_basis": "local_estimate", "provider_reported": r,
                        "reconciled": account_reported, "invoice_status": invoice,
                        "unknown_spend": not account_reported and (e["committed_micros"] > 0 or e["ambiguous_micros"] > 0)})
    return J({"summary": summary, "daily": [dict(r) for r in daily], "per_campaign": [dict(r) for r in per_campaign],
              "per_site": [dict(r) for r in per_site]})


@router.post("/usage/sync")
async def usage_sync_now(request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    from jarvis.core.usage_sync import sync_provider_usage
    return await sync_provider_usage(request.app.state.deps)


class ManualReportIn(BaseModel):
    provider: str
    unit: str
    reported_units: int | None = None
    reported_cost_usd: Decimal | None = None
    period_start: datetime
    period_end: datetime


@router.post("/usage/reports")
async def manual_report(body: ManualReportIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    await request.app.state.db.execute(
        """insert into provider_usage_reports(workspace_id, provider, period_start, period_end, unit, reported_units, reported_cost_micros, source,
                                              cost_basis)
           values ($1,$2,$3,$4,$5,$6,$7,'manual_entry', case when $7::bigint is null then null else 'manual_entry' end)""",
        me.workspace_id, body.provider, body.period_start, body.period_end, body.unit,
        body.reported_units, int(body.reported_cost_usd * 1_000_000) if body.reported_cost_usd is not None else None)
    return {"ok": True}


VOICE_TOKEN_TTL_S = 600
AGENT_FRESH_S = 90


@router.get("/voice/status")
async def voice_status(request: Request, me: Principal = CurrentPrincipal):
    """Voice is OPERATIONAL only when LiveKit is configured, the last real LiveKit validation succeeded, the voice
    agent shared secret is set, and a voice agent process has heartbeated recently. Otherwise each missing piece
    is listed; nothing is reported as ready on configuration alone."""
    from jarvis.core.integrations import DISABLED, ensure_fresh
    await ensure_fresh(request.app.state)
    st, db = request.app.state, request.app.state.db
    lk = st.livekit
    status = await db.fetchrow("select state, last_success_at, detail from integration_status where provider='livekit'")
    agent = await db.fetchrow(f"""select agent_id, last_seen_at, version, registered, worker_id, process_ready_at, last_error,
                                         started_at from voice_agent_heartbeats
                                  where last_seen_at > now() - interval '{AGENT_FRESH_S} seconds' order by last_seen_at desc limit 1""")
    checks = {
        "livekit_configured": lk.configured and "livekit" not in DISABLED,
        "livekit_validated": bool(status and status["state"] == "CONNECTED"),
        "agent_secret_configured": bool(st.settings.voice_agent_shared_secret),
        "agent_online": agent is not None,
        # A live parent process is not enough: the worker must be registered with LiveKit and a job process must have
        # actually started (prewarm ran in it). Otherwise rooms are dispatched to a worker that cannot serve them.
        "agent_registered": bool(agent and agent["registered"]),
        "agent_job_process_ready": bool(agent and agent["process_ready_at"]),
    }
    missing = []
    if "livekit" in DISABLED:
        missing.append("LiveKit is disabled in the Integrations Center.")
    elif not lk.configured:
        missing.append("LiveKit server URL, API key and API secret (Integrations > LiveKit).")
    elif not checks["livekit_validated"]:
        missing.append("A successful LiveKit connection test (Integrations > LiveKit > Test connection)"
                       + (f": last result {status['state']} ({status['detail']})" if status else "."))
    if not checks["agent_secret_configured"]:
        missing.append("VOICE_AGENT_SHARED_SECRET in the API environment (the same value in the voice agent).")
    err = f" Last worker error: {agent['last_error']}" if agent and agent["last_error"] else ""
    if not checks["agent_online"]:
        missing.append("A running JARVIS voice agent (apps/voice: python agent.py start). No heartbeat in the last "
                       f"{AGENT_FRESH_S} seconds.")
    elif not checks["agent_registered"]:
        missing.append("The voice agent is running but not registered with LiveKit." + err)
    elif not checks["agent_job_process_ready"]:
        missing.append("The voice agent is registered but no job process has started, so it cannot join rooms." + err
                       + " See .jarvis/logs/voice.log.")
    state = "OPERATIONAL" if all(checks.values()) else ("DISABLED" if "livekit" in DISABLED else
                                                         "NOT_CONFIGURED" if not lk.configured else "NOT_READY")
    return J({"state": state, "checks": checks, "missing": missing,
              "agent": dict(agent) if agent else None, "token_ttl_s": VOICE_TOKEN_TTL_S})


class VoiceEndIn(BaseModel):
    room: str = Field(min_length=6, max_length=120)


@router.post("/voice/end")
async def voice_end(body: VoiceEndIn, request: Request, me: Principal = CurrentPrincipal):
    """Terminates a voice session the caller started (deletes the LiveKit room, which ends the agent session)."""
    db = request.app.state.db
    owned = await db.fetchval("""select exists(select 1 from audit_logs where action='voice.session_started' and entity_id=$1
                                 and actor_id=$2 and workspace_id=$3)""", body.room, str(me.user_id), me.workspace_id)
    if not owned:
        raise NotFound("voice session not found")
    lk = request.app.state.livekit
    detail = None
    try:
        await lk.delete_room(body.room)
    except ProviderError as exc:
        detail = f"{exc.state.value}: {exc.message[:200]}"
    await db.execute("""insert into audit_logs(workspace_id, actor_type, actor_id, action, entity_type, entity_id, data)
                        values ($1,'user',$2,'voice.session_ended','livekit_room',$3,$4)""",
                     me.workspace_id, str(me.user_id), body.room, {"detail": detail})
    return {"ok": detail is None, "detail": detail}


@router.post("/voice/token")
async def voice_token(request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    from jarvis.core.integrations import DISABLED, ensure_fresh
    await ensure_fresh(request.app.state)
    lk = request.app.state.livekit
    if "livekit" in DISABLED:
        return JSONResponse({"state": "DISABLED", "detail": "LiveKit is disabled in the Integrations Center"}, status_code=424)
    if not lk.configured:
        return JSONResponse({"state": "NOT_CONFIGURED", "detail": _missing("livekit")}, status_code=424)
    room = f"jarvis-{me.workspace_id}-{uuid.uuid4().hex[:8]}"
    meta = json.dumps({"workspace_id": str(me.workspace_id), "user_id": str(me.user_id)})
    tok = lk.token(identity=f"user-{me.user_id}", name=me.email or "operator", room=room, ttl_s=VOICE_TOKEN_TTL_S, metadata=meta,
                   dispatch_agent=request.app.state.settings.voice_agent_name)
    await request.app.state.db.execute("""insert into audit_logs(workspace_id, actor_type, actor_id, action, entity_type, entity_id)
                                          values ($1,'user',$2,'voice.session_started','livekit_room',$3)""", me.workspace_id, str(me.user_id), room)
    return {"url": lk.settings.livekit_url, "token": tok, "room": room, "expires_in_s": VOICE_TOKEN_TTL_S}


# ---------------- outreach ----------------
class SenderIn(BaseModel):
    postal_address: str | None = Field(None, max_length=300)
    postal_address_type: str | None = Field(None, pattern="^(street|po_box|cmra_mailbox)$")
    attest_postal_address: bool = False
    sending_domain: str | None = Field(None, max_length=200)
    from_address: str | None = Field(None, max_length=200)
    reply_to: str | None = Field(None, max_length=200)


@router.get("/outreach/readiness")
async def get_readiness(request: Request, me: Principal = CurrentPrincipal):
    return J(await outreach.readiness(request.app.state.deps, me.workspace_id))


@router.put("/outreach/sender")
async def put_sender(body: SenderIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    db = request.app.state.db
    async with db.transaction() as conn:
        await conn.execute("insert into sender_readiness(workspace_id) values ($1) on conflict do nothing", me.workspace_id)
        for k in ("postal_address", "postal_address_type", "sending_domain", "from_address", "reply_to"):
            v = getattr(body, k)
            if v is not None:
                await conn.execute(f"update sender_readiness set {k}=$2 where workspace_id=$1", me.workspace_id, v.strip())
        if body.sending_domain is not None:
            await conn.execute("""update sender_readiness set spf_ok=false, dkim_ok=false, dmarc_ok=false, provider_domain_verified=false,
                                  dns_checked_at=null where workspace_id=$1""", me.workspace_id)
        if body.attest_postal_address:
            await conn.execute("""update sender_readiness set postal_address_attested_at=now(), postal_address_attested_by=$2
                                  where workspace_id=$1""", me.workspace_id, me.user_id)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="sender.updated",
                    data=J(body.model_dump(exclude_none=True)))
    return J(await outreach.readiness(request.app.state.deps, me.workspace_id))


@router.post("/outreach/sender/check-dns")
async def check_dns(request: Request, me: Principal = CurrentPrincipal):
    """Checks SPF/DMARC via DNS-over-HTTPS and DKIM/domain verification via the email provider."""
    me.require("owner")
    deps = request.app.state.deps
    r = await deps.db.fetchrow("select sending_domain from sender_readiness where workspace_id=$1", me.workspace_id)
    if not r or not r["sending_domain"]:
        raise ValidationFailed("set a sending domain first")
    dom = r["sending_domain"]
    import httpx
    async with httpx.AsyncClient(timeout=10) as http:
        async def txt(name: str) -> list[str]:
            resp = await http.get("https://cloudflare-dns.com/dns-query", params={"name": name, "type": "TXT"},
                                  headers={"accept": "application/dns-json"})
            return [a.get("data", "").strip('"') for a in resp.json().get("Answer", [])]
        spf = any(t.startswith("v=spf1") for t in await txt(dom))
        dmarc = any(t.startswith("v=DMARC1") for t in await txt(f"_dmarc.{dom}"))
    dkim, verified = False, False
    if deps.email.configured:
        st = await deps.email.domain_status(dom)
        verified = st.get("status") == "verified"
        dkim = verified
    await deps.db.execute("""update sender_readiness set spf_ok=$2, dmarc_ok=$3, dkim_ok=$4, provider_domain_verified=$5, dns_checked_at=now()
                             where workspace_id=$1""", me.workspace_id, spf, dmarc, dkim, verified)
    return {"spf": spf, "dmarc": dmarc, "dkim": dkim, "provider_verified": verified}


@router.get("/outreach/drafts")
async def drafts(request: Request, me: Principal = CurrentPrincipal):
    rows = await request.app.state.db.fetch(
        """select d.id, d.campaign_id, d.prospect_id, d.state, d.subject, d.body_text, d.observations, d.created_at,
                  (d.contact_id is not null) has_contact, dep.url preview_url,
                  (select value #>> '{}' from research_facts f where f.prospect_id = d.prospect_id and f.fact_key='business_name'
                     and f.status='verified' limit 1) business_name
           from outreach_drafts d left join deployments dep on dep.id = d.deployment_id where d.workspace_id=$1 order by d.created_at desc""",
        me.workspace_id)
    return J([dict(r) for r in rows])


class AuthorizeIn(BaseModel):
    daily_limit: int = Field(ge=1, le=200)
    total_limit: int = Field(ge=1, le=5000)


@router.post("/outreach/campaigns/{campaign_id}/authorize")
async def authorize_outreach(campaign_id: uuid.UUID, body: AuthorizeIn, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    deps = request.app.state.deps
    ready = await outreach.readiness(deps, me.workspace_id)
    if not ready["ready"]:
        raise ComplianceBlocked("sender readiness incomplete; sending cannot be authorized",
                                details={"failing": [i for i in ready["items"] if not i["ok"]]})
    async with deps.db.transaction() as conn:
        if not await conn.fetchval("select 1 from campaigns where id=$1 and workspace_id=$2", campaign_id, me.workspace_id):
            raise NotFound("campaign not found")
        aid = await conn.fetchval("""insert into outreach_authorizations(workspace_id, campaign_id, authorized_by, daily_limit, total_limit,
                                     readiness_snapshot) values ($1,$2,$3,$4,$5,$6) returning id""",
                                  me.workspace_id, campaign_id, me.user_id, body.daily_limit, body.total_limit, ready)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="outreach.authorized",
                    entity_type="campaign", entity_id=campaign_id, data=body.model_dump())
    return {"authorization_id": str(aid)}


@router.post("/outreach/drafts/{draft_id}/approve")
async def approve_draft(draft_id: uuid.UUID, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    db = request.app.state.db
    async with db.transaction() as conn:
        d = await conn.fetchrow("select state from outreach_drafts where id=$1 and workspace_id=$2 for update", draft_id, me.workspace_id)
        if d is None:
            raise NotFound("draft not found")
        if d["state"] != "draft":
            raise InvalidTransition(f"draft is {d['state']}")
        await conn.execute("update outreach_drafts set state='authorized' where id=$1", draft_id)
        await audit(conn, workspace_id=me.workspace_id, actor_type="user", actor_id=str(me.user_id), action="outreach.draft_approved",
                    entity_type="outreach_draft", entity_id=draft_id, from_state="draft", to_state="authorized")
    return {"state": "authorized"}


@router.post("/outreach/drafts/{draft_id}/send")
async def send(draft_id: uuid.UUID, request: Request, me: Principal = CurrentPrincipal):
    me.require("owner")
    deps = request.app.state.deps
    return await outreach.send_draft(deps, CallContext(deps.db, me.workspace_id), draft_id, str(me.user_id))


@router.post("/outreach/suppressions")
async def add_suppression(body: dict, request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    kind, value = body.get("kind"), (body.get("value") or "").strip()
    if kind not in ("email", "domain", "phone") or not value:
        raise ValidationFailed("kind (email|domain|phone) and value required")
    async with request.app.state.db.transaction() as conn:
        await outreach.suppress(conn, me.workspace_id, kind, value, "manual", f"user:{me.user_id}")
    return {"ok": True, "hash": hashlib.sha256(f"{kind}:{value.lower()}".encode()).hexdigest()[:12]}


@router.get("/notifications")
async def notifications(request: Request, me: Principal = CurrentPrincipal):
    rows = await request.app.state.db.fetch("select * from notifications where workspace_id=$1 order by created_at desc limit 50", me.workspace_id)
    return J([dict(r) for r in rows])


_ = timedelta


@router.get("/system/readiness")
async def system_readiness(request: Request, me: Principal = CurrentPrincipal):
    """Production-readiness checklist computed from real configuration and records."""
    deps, db, s = request.app.state.deps, request.app.state.db, request.app.state.settings
    async with db.acquire() as conn:
        gate = await gate_status(conn, me.workspace_id, "google_places_prospect_selection")
    st = await db.fetchrow("select terms_of_use_url, privacy_policy_url, agency_name from system_settings where workspace_id=$1", me.workspace_id)
    from jarvis.ops.backup import backup_status
    async with db.acquire() as conn:
        bstatus, _b = await backup_status(conn)
    workers = await db.fetchval("select count(*) from worker_heartbeats where last_seen_at > now() - interval '2 minutes'")
    items = [
        ("database", db.state == "healthy", db.state),
        ("worker_alive", workers > 0, f"{workers} live worker(s)"),
        ("auth_production", s.jarvis_env != "production" or bool(s.supabase_jwks_url or s.supabase_jwt_secret), "Supabase JWT verification configured"),
        ("claude_api", deps.claude.configured, "ANTHROPIC_API_KEY"),
        ("discovery_provider", deps.serp.configured or (gate["open"] and deps.places.configured),
         ("SerpApi configured" if deps.serp.configured else "SerpApi not configured (SERPAPI_API_KEY)") + "; Google Places API (New): "
         + ("approved + configured" if gate["open"] and deps.places.configured else
            f"BLOCKED_PENDING_APPROVAL: {gate['reason']}" if not gate["open"] else "approved, not configured")),
        ("search_provider", deps.serp.configured, "SERPAPI_API_KEY"),
        ("extraction_provider", deps.firecrawl.configured, "FIRECRAWL_API_KEY"),
        ("preview_deployment", deps.cloudflare.configured, "CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID"),
        ("voice", request.app.state.livekit.configured and bool(s.voice_agent_shared_secret), "LiveKit + VOICE_AGENT_SHARED_SECRET"),
        ("terms_of_use_published", bool(st and st["terms_of_use_url"]), "Public Terms of Use URL"),
        ("privacy_policy_published", bool(st and st["privacy_policy_url"]), "Public Privacy Policy URL"),
        ("offsite_backup", bstatus == "PROTECTED", bstatus),
    ]
    outreach_ready = (await outreach.readiness(deps, me.workspace_id))["ready"]
    items.append(("outreach_sending", outreach_ready, "sender readiness checklist (see Outreach)"))
    return {"ready": all(ok for _k, ok, _d in items), "items": [{"key": k, "ok": ok, "detail": d} for k, ok, d in items]}
