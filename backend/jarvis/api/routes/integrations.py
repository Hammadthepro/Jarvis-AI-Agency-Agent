"""Integrations Center API.

Reading status: any workspace member. Testing a connection: workspace owner or deployment admin (free provider
endpoints only). Saving, removing, enabling or disabling credentials: deployment admins only
(JARVIS_ADMIN_USER_IDS / JARVIS_ADMIN_EMAILS), because provider accounts are shared by the whole deployment.
Secret values are accepted write-only: no response, log or audit entry ever contains them."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from jarvis.api.auth import CurrentPrincipal, Forbidden, Principal
from jarvis.core import integrations as I
from jarvis.core.compliance import gate_status
from jarvis.core.errors import ComplianceBlocked, IntegrationState, ProviderError
from jarvis.security.vault import Vault

router = APIRouter()


def is_admin(request: Request, me: Principal) -> bool:
    s = request.app.state.base_settings
    return str(me.user_id).lower() in s.admin_user_ids or bool(me.email and me.email.lower() in s.admin_emails)


def require_admin(request: Request, me: Principal) -> None:
    if not is_admin(request, me):
        s = request.app.state.base_settings
        if not (s.admin_user_ids or s.admin_emails):
            raise Forbidden("no deployment administrator is configured: set JARVIS_ADMIN_EMAILS or JARVIS_ADMIN_USER_IDS "
                            "in the API environment to allow credential management")
        raise Forbidden("managing provider credentials requires a deployment administrator")


def _vault_status(request: Request) -> dict:
    try:
        v = Vault.from_settings(request.app.state.base_settings)
        return {"ready": True, "key_id": v.kid, "detail": None}
    except Exception as exc:
        return {"ready": False, "key_id": None, "detail": getattr(exc, "message", str(exc))}


async def _refresh(request: Request) -> dict:
    st = request.app.state
    res = await I.refresh(st, st.base_settings, st.livekit)
    st.credentials_error = res["error"]
    return res


@router.get("/integrations")
async def list_integrations(request: Request, me: Principal = CurrentPrincipal):
    st = request.app.state
    db, deps, lk = st.db, st.deps, st.livekit
    _eff, info, load_err = await I.load_effective(db, st.base_settings)
    status = {r["provider"]: dict(r) for r in await db.fetch("select * from integration_status")}
    runtime = {r["provider"]: dict(r) for r in await db.fetch(
        "select provider, state, detail, last_checked_at from provider_integrations where workspace_id=$1", me.workspace_id)}
    usage = {r["provider"]: dict(r) for r in await db.fetch(
        """select distinct on (provider) provider, unit, reported_units, status, detail, fetched_at from provider_usage_reports
           where workspace_id is null or workspace_id=$1 order by provider, fetched_at desc""", me.workspace_id)}
    async with db.acquire() as conn:
        gate = await gate_status(conn, me.workspace_id, "google_places_prospect_selection")
    stored = await I.stored(db)
    cards = []
    for p, sp in I.SPECS.items():
        inf, row, srow = info.get(p, {}), stored.get(p) or {}, status.get(p) or {}
        configured = I.configured(p, deps, lk)
        enabled = inf.get("enabled", True)
        if not enabled:
            state, detail = "DISABLED", "Disabled by an administrator. No calls are made."
        elif not configured:
            state, detail = "NOT_CONFIGURED", "Missing: " + ", ".join(I.missing_fields(p, deps, lk))
        elif p == "google_places" and not gate["open"]:
            state, detail = IntegrationState.BLOCKED_PENDING_APPROVAL.value, gate.get("reason")
        elif srow:
            state, detail = I.DISPLAY_STATE.get(srow["state"], srow["state"]), srow.get("detail")
        else:
            state, detail = "NOT_TESTED", "Configured but not yet validated against the provider. Run Test connection."
        fields = []
        for f in sp.fields:
            src = inf.get("sources", {}).get(f.name)
            entry = {"name": f.name, "label": f.label, "secret": f.secret, "required": f.required, "hint": f.hint,
                     "choices": list(f.choices), "default": f.default, "set": bool(src), "source": src}
            if f.secret:
                entry["display"] = "••••••••" if src else None        # never the value, not even a suffix
            else:
                entry["display"] = getattr(deps.settings, f.setting) if src else None
            fields.append(entry)
        rt = runtime.get(p)
        cards.append({
            "provider": p, "name": sp.name, "purpose": sp.purpose, "docs_url": sp.docs_url, "notes": list(sp.notes),
            "managed_in_dashboard": sp.managed_in_dashboard, "configured": configured, "enabled": enabled,
            "state": state, "provider_state": srow.get("state"), "detail": detail, "tested": bool(srow.get("last_success_at")),
            "last_checked_at": srow.get("last_tested_at"), "last_success_at": srow.get("last_success_at"),
            "last_error": srow.get("last_error"), "last_error_at": srow.get("last_error_at"),
            "capabilities": list(sp.capabilities), "validated_capabilities": srow.get("capabilities") or [],
            "quota": srow.get("quota"), "usage": usage.get(p), "fields": fields,
            "has_dashboard_credentials": bool(row.get("secrets") or row.get("config")),
            "runtime": {"state": rt["state"], "detail": rt["detail"], "at": rt["last_checked_at"]} if rt and rt["state"] != "CONNECTED" else None,
            "compliance": gate if p == "google_places" else None,
        })
    return JSONResponse(content=_json({"integrations": cards, "can_manage": is_admin(request, me), "vault": _vault_status(request),
                                       "credentials_error": load_err}))


class CredentialsIn(BaseModel):
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    config: dict[str, str] = Field(default_factory=dict)


@router.put("/integrations/{provider}/credentials")
async def save_credentials(provider: str, body: CredentialsIn, request: Request, me: Principal = CurrentPrincipal):
    require_admin(request, me)
    I.spec(provider)
    secrets = {k: v.get_secret_value() for k, v in body.secrets.items()}
    # Secrets are only ever stored encrypted: without a valid key this raises ENCRYPTION_NOT_CONFIGURED (fail closed).
    vault = Vault.from_settings(request.app.state.base_settings) if secrets else None
    await I.save(request.app.state.db, vault, provider, secrets, body.config, me.user_id)
    res = await _refresh(request)
    return {"ok": True, "provider": provider, "state": "NOT_TESTED", "credentials_error": res["error"]}


@router.delete("/integrations/{provider}/credentials")
async def remove_credentials(provider: str, request: Request, fields: str | None = None, me: Principal = CurrentPrincipal):
    require_admin(request, me)
    await I.remove(request.app.state.db, provider, me.user_id, [f for f in (fields or "").split(",") if f] or None)
    await _refresh(request)
    return {"ok": True}


@router.post("/integrations/{provider}/enable")
async def enable(provider: str, request: Request, me: Principal = CurrentPrincipal):
    require_admin(request, me)
    await I.set_enabled(request.app.state.db, provider, True, me.user_id)
    await _refresh(request)
    return {"ok": True, "enabled": True}


@router.post("/integrations/{provider}/disable")
async def disable(provider: str, request: Request, me: Principal = CurrentPrincipal):
    require_admin(request, me)
    await I.set_enabled(request.app.state.db, provider, False, me.user_id)
    await _refresh(request)
    return {"ok": True, "enabled": False}


@router.post("/integrations/{provider}/test")
async def test_integration(provider: str, request: Request, me: Principal = CurrentPrincipal):
    """Calls the real provider using free endpoints only. CONNECTED is returned only when that call succeeds."""
    if not is_admin(request, me):
        me.require("owner")
    sp = I.spec(provider)
    st = request.app.state
    db, deps, lk = st.db, st.deps, st.livekit
    await _refresh(request)
    if provider in I.DISABLED:
        return {"provider": provider, "state": "DISABLED", "detail": "Disabled by an administrator. No call was made."}
    if not I.configured(provider, deps, lk):
        detail = "Missing: " + ", ".join(I.missing_fields(provider, deps, lk))
        return {"provider": provider, "state": "NOT_CONFIGURED", "detail": detail}
    try:
        result, quota = await I.validate(provider, deps, lk, db, me.workspace_id)
    except ProviderError as exc:
        state = I.DISPLAY_STATE.get(exc.state.value, exc.state.value)
        await I.record_status(db, provider, exc.state.value, exc.message[:300], capabilities=[], quota=None, user_id=me.user_id, ok=False)
        await _workspace_state(db, me.workspace_id, provider, exc.state.value, exc.message[:300], False)
        await _audit_test(db, me, provider, state)
        return {"provider": provider, "state": state, "provider_state": exc.state.value, "detail": exc.message[:300],
                "http_status": exc.upstream_status}
    except ComplianceBlocked as exc:
        await _audit_test(db, me, provider, "BLOCKED_PENDING_APPROVAL")
        return {"provider": provider, "state": "BLOCKED_PENDING_APPROVAL", "detail": exc.message}
    await I.record_status(db, provider, "CONNECTED", None, capabilities=list(sp.capabilities), quota=quota, user_id=me.user_id, ok=True)
    await _workspace_state(db, me.workspace_id, provider, "CONNECTED", None, True)
    await _audit_test(db, me, provider, "CONNECTED")
    return JSONResponse(content=_json({"provider": provider, "state": "CONNECTED", "result": result, "quota": quota,
                                       "capabilities": list(sp.capabilities)}))


async def _workspace_state(db, ws, provider, state, detail, ok) -> None:
    if provider == "supabase":
        return
    await db.execute("""insert into provider_integrations(workspace_id, provider, configured, state, last_checked_at, last_test_ok, detail)
                        values ($1,$2,true,$3,now(),$4,$5) on conflict (workspace_id, provider) do update set state=excluded.state,
                        last_checked_at=now(), last_test_ok=excluded.last_test_ok, detail=excluded.detail, configured=true""",
                     ws, provider, state, ok, detail)


async def _audit_test(db, me: Principal, provider: str, state: str) -> None:
    await db.execute("""insert into audit_logs(workspace_id, actor_type, actor_id, action, entity_type, data)
                        values ($1,'user',$2,'integration.tested','integration',$3)""",
                     me.workspace_id, str(me.user_id), {"provider": provider, "state": state})


def _json(x):
    return json.loads(json.dumps(x, default=str))
