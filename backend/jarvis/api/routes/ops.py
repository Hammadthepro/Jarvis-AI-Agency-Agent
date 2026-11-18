"""Health, identity, settings, budgets, integrations, compliance, usage, voice, outreach, unsubscribe, webhooks."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from jarvis.agents import outreach
from jarvis.api.auth import CurrentPrincipal, Principal, create_workspace
from jarvis.core.compliance import INTENDED_USE, gate_status
from jarvis.core.errors import (ComplianceBlocked, Forbidden, InvalidTransition, IntegrationState, JarvisError, NotFound,
                                ProviderError, ValidationFailed)
from jarvis.db.audit import audit
from jarvis.integrations.base import CallContext
from jarvis.integrations.google_places import IDS_ONLY
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
    ok = await request.app.state.db.health()
    body = {"database": request.app.state.db.state, "detail": request.app.state.db.last_error}
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
    return J({"user_id": me.user_id, "email": me.email, "workspace_id": me.workspace_id, "role": me.role,
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
    return J({"settings": dict(row) if row else {}, "models": {"fast": s.claude_model_fast, "build": s.claude_model_build,
              "escalation": s.claude_model_escalation}, "environment": s.jarvis_env, "qa_runner_mode": s.qa_runner_mode,
              "preview_project": s.cloudflare_pages_preview_project, "preview_ttl_days": s.preview_ttl_days,
              "backup": "PROTECTED" if s.backup_offsite_configured else "BACKUP_NOT_PROTECTED"})


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


def _configured(request: Request, provider: str) -> bool:
    d = request.app.state.deps
    return {"anthropic": d.claude.configured, "google_places": d.places.configured, "serp_api": d.serp.configured,
            "firecrawl": d.firecrawl.configured, "cloudflare": d.cloudflare.configured, "resend": d.email.configured,
            "livekit": request.app.state.livekit.configured}[provider]


@router.get("/integrations")
async def integrations(request: Request, me: Principal = CurrentPrincipal):
    db = request.app.state.db
    rows = {r["provider"]: dict(r) for r in await db.fetch("select * from provider_integrations where workspace_id=$1", me.workspace_id)}
    async with db.acquire() as conn:
        gate = await gate_status(conn, me.workspace_id, "google_places_prospect_selection")
    out = []
    for p in PROVIDERS:
        configured = _configured(request, p)
        r = rows.get(p, {})
        state = (r.get("state") or "NOT_CONFIGURED") if configured else "NOT_CONFIGURED"
        if p == "google_places" and configured and not gate["open"]:
            state = IntegrationState.BLOCKED_PENDING_APPROVAL.value
        if not configured:
            state = "NOT_CONFIGURED"
        out.append({"provider": p, "configured": configured, "state": state, "tested": bool(r.get("last_test_ok")),
                    "last_checked_at": r.get("last_checked_at"), "detail": r.get("detail") if configured else _missing(p),
                    "compliance": gate if p == "google_places" else None})
    return J(out)


def _missing(p: str) -> str:
    return {"anthropic": "Set ANTHROPIC_API_KEY (Claude Console API key; a Claude Code subscription is not API access)",
            "google_places": "Set GOOGLE_PLACES_API_KEY (Google Cloud project with only Places API (New) enabled)",
            "serp_api": "Set SERPAPI_API_KEY", "firecrawl": "Set FIRECRAWL_API_KEY",
            "cloudflare": "Set CLOUDFLARE_API_TOKEN (Pages:Edit) and CLOUDFLARE_ACCOUNT_ID",
            "resend": "Set EMAIL_PROVIDER=resend and RESEND_API_KEY (requires your own sending domain)",
            "livekit": "Set LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET"}[p]


async def _record_test(db, ws, provider: str, ok: bool, state: str, detail: str | None) -> None:
    await db.execute("""insert into provider_integrations(workspace_id, provider, configured, state, last_checked_at, last_test_ok, detail)
                        values ($1,$2,true,$3,now(),$4,$5) on conflict (workspace_id, provider) do update set state=excluded.state,
                        last_checked_at=now(), last_test_ok=excluded.last_test_ok, detail=excluded.detail, configured=true""",
                     ws, provider, state, ok, detail)


@router.post("/integrations/{provider}/test")
async def test_integration(provider: str, request: Request, me: Principal = CurrentPrincipal):
    """Connection tests use free endpoints only. Google Places uses the Essentials (IDs Only) SKU, which
    Google lists as having no charge, and still requires the technical-testing compliance scope."""
    me.require("owner")
    d, db = request.app.state.deps, request.app.state.db
    if provider not in PROVIDERS:
        raise NotFound("unknown provider")
    if not _configured(request, provider):
        return {"provider": provider, "state": "NOT_CONFIGURED", "detail": _missing(provider)}
    try:
        if provider == "anthropic":
            res = {m: await d.claude.validate_model(m) for m in {d.settings.claude_model_fast, d.settings.claude_model_build,
                                                                d.settings.claude_model_escalation}}
        elif provider == "serp_api":
            acct = await d.serp.account()
            res = {k: acct.get(k) for k in ("plan_name", "searches_per_month", "this_month_usage", "plan_searches_left")}
            await _report(db, me.workspace_id, "serp_api", "search", acct.get("this_month_usage"), acct)
        elif provider == "firecrawl":
            res = await d.firecrawl.credit_usage()
            await _report(db, me.workspace_id, "firecrawl", "credit_remaining", (res.get("data") or {}).get("remaining_credits"), res)
        elif provider == "cloudflare":
            res = await d.cloudflare.verify_token()
        elif provider == "resend":
            r = await db.fetchrow("select sending_domain from sender_readiness where workspace_id=$1", me.workspace_id)
            res = await d.email.domain_status(r["sending_domain"]) if r and r["sending_domain"] else {"note": "no sending domain set"}
        elif provider == "livekit":
            res = await request.app.state.livekit.check()
        else:
            page = await d.places.text_search(CallContext(db, me.workspace_id), text_query="plumber in Austin, TX", min_rating=None,
                                              include_service_area=True, page_size=1, mask=IDS_ONLY,
                                              scope="google_places_technical_testing")
            res = {"returned_ids": len(page.places), "sku": page.sku}
    except ProviderError as exc:
        await _record_test(db, me.workspace_id, provider, False, exc.state.value, exc.message[:300])
        return {"provider": provider, "state": exc.state.value, "detail": exc.message[:300]}
    except ComplianceBlocked as exc:
        return {"provider": provider, "state": "BLOCKED_PENDING_APPROVAL", "detail": exc.message}
    await _record_test(db, me.workspace_id, provider, True, "CONNECTED", None)
    return J({"provider": provider, "state": "CONNECTED", "result": res})


async def _report(db, ws, provider, unit, units, raw) -> None:
    now = datetime.now(UTC)
    await db.execute("""insert into provider_usage_reports(workspace_id, provider, period_start, period_end, unit, reported_units, source, raw)
                        values ($1,$2,$3,$4,$5,$6,'provider_api',$7)""", ws, provider, now.replace(day=1, hour=0, minute=0, second=0,
                     microsecond=0), now, unit, units, raw)


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
    db = request.app.state.db
    est = await db.fetch("""select provider, status, date_trunc('month', created_at) as mon, sum(units)::bigint units, sum(amount_micros)::bigint micros, count(*) calls
                            from cost_reservations where workspace_id=$1 group by 1,2,3 order by 3 desc, 1""", me.workspace_id)
    daily = await db.fetch("""select date_trunc('day', created_at) as day, provider, sum(amount_micros)::bigint micros from cost_reservations
                              where workspace_id=$1 and status in ('committed','ambiguous','reserved') and created_at > now() - interval '31 days'
                              group by 1,2 order by 1""", me.workspace_id)
    reported = await db.fetch("""select distinct on (provider, unit) provider, unit, reported_units, reported_cost_micros, source, fetched_at
                                 from provider_usage_reports where workspace_id=$1 order by provider, unit, fetched_at desc""", me.workspace_id)
    per_campaign = await db.fetch("""select c.id, c.name, coalesce(sum(r.amount_micros) filter (where r.status in ('committed','ambiguous','reserved')),0)::bigint micros
                                     from campaigns c left join cost_reservations r on r.campaign_id = c.id where c.workspace_id=$1 group by 1,2
                                     order by 3 desc limit 50""", me.workspace_id)
    per_site = await db.fetch("""select g.id, g.slug, g.cost_micros from generated_sites g where g.workspace_id=$1 order by g.cost_micros desc limit 50""",
                              me.workspace_id)
    rep = {r["provider"]: dict(r) for r in reported}
    providers = sorted({r["provider"] for r in est} | set(PROVIDERS))
    summary = []
    for p in providers:
        rows = [r for r in est if r["provider"] == p and r["mon"] == max((x["mon"] for x in est), default=None)]
        summary.append({"provider": p,
                        "estimated": {"committed_micros": sum(r["micros"] for r in rows if r["status"] == "committed"),
                                      "reserved_micros": sum(r["micros"] for r in rows if r["status"] == "reserved"),
                                      "ambiguous_micros": sum(r["micros"] for r in rows if r["status"] == "ambiguous"),
                                      "units": sum(r["units"] for r in rows if r["status"] != "released"),
                                      "calls": sum(r["calls"] for r in rows)},
                        "reported": rep.get(p) or "not_available"})
    return J({"summary": summary, "daily": [dict(r) for r in daily], "per_campaign": [dict(r) for r in per_campaign],
              "per_site": [dict(r) for r in per_site]})


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
        """insert into provider_usage_reports(workspace_id, provider, period_start, period_end, unit, reported_units, reported_cost_micros, source)
           values ($1,$2,$3,$4,$5,$6,$7,'manual_entry')""", me.workspace_id, body.provider, body.period_start, body.period_end, body.unit,
        body.reported_units, int(body.reported_cost_usd * 1_000_000) if body.reported_cost_usd is not None else None)
    return {"ok": True}


@router.post("/voice/token")
async def voice_token(request: Request, me: Principal = CurrentPrincipal):
    me.require("operator")
    lk = request.app.state.livekit
    if not lk.configured:
        return JSONResponse({"state": "NOT_CONFIGURED", "detail": _missing("livekit")}, status_code=424)
    room = f"jarvis-{me.workspace_id}-{uuid.uuid4().hex[:8]}"
    meta = json.dumps({"workspace_id": str(me.workspace_id), "user_id": str(me.user_id)})
    tok = lk.token(identity=f"user-{me.user_id}", name=me.email or "operator", room=room, ttl_s=900, metadata=meta)
    await request.app.state.db.execute("""insert into audit_logs(workspace_id, actor_type, actor_id, action, entity_type, entity_id)
                                          values ($1,'user',$2,'voice.session_started','livekit_room',$3)""", me.workspace_id, str(me.user_id), room)
    return {"url": request.app.state.settings.livekit_url, "token": tok, "room": room}


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
