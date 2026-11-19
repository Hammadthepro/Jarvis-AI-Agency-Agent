"""Integrations Center: provider specs, encrypted credential storage, effective settings and real validation.

Credentials come from two sources. Values saved in the dashboard (encrypted, `integration_credentials`) take
precedence over environment variables; removing them falls back to the environment. The merged settings are
applied to every adapter in the process (`apply_effective_settings`). The worker reloads them when they change.

Validation always calls the real provider, using free endpoints only. A provider is CONNECTED only after
such a call succeeds. Missing credentials are NOT_CONFIGURED; an admin-disabled provider is DISABLED and no
calls are made; Google Places is BLOCKED_PENDING_APPROVAL while its compliance gate is closed."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from jarvis.config import ALLOWED_MODELS, Settings
from jarvis.core.errors import IntegrationState, NotFound, ProviderError, ValidationFailed
from jarvis.security.vault import Vault

# Display states. Internal provider states that mean "could not complete the call" are shown as CONNECTION_FAILED
# with the precise cause kept in `provider_state` and `detail`.
DISPLAY_STATE = {
    IntegrationState.TEMPORARILY_UNAVAILABLE.value: "CONNECTION_FAILED",
    IntegrationState.PROVIDER_ERROR.value: "CONNECTION_FAILED",
}

# Providers whose calls are disabled deployment-wide by an administrator (checked by the gateway and by
# non-gateway callers such as LiveKit token minting). Per-process; refreshed with the effective settings.
DISABLED: set[str] = set()


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    secret: bool
    setting: str                      # Settings attribute it populates
    required: bool = True
    pattern: str | None = None        # format check (never echoes the value back)
    hint: str = ""
    choices: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    name: str
    purpose: str
    fields: tuple[FieldSpec, ...]
    capabilities: tuple[str, ...]
    docs_url: str
    managed_in_dashboard: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


_MODELS = tuple(sorted(ALLOWED_MODELS))
SPECS: dict[str, ProviderSpec] = {s.key: s for s in (
    ProviderSpec("anthropic", "Anthropic (Claude)",
                 "Understands commands, extracts verified facts from research, writes website copy and outreach drafts.",
                 (FieldSpec("api_key", "API key", True, "anthropic_api_key", pattern=r"^sk-ant-[A-Za-z0-9_\-]{20,}$",
                            hint="Claude Console API key (sk-ant-…). A Claude.ai or Claude Code subscription is not API access."),
                  FieldSpec("model_fast", "Fast model", False, "claude_model_fast", required=False, choices=_MODELS,
                            default="claude-haiku-5-5"),
                  FieldSpec("model_build", "Build model", False, "claude_model_build", required=False, choices=_MODELS,
                            default="claude-sonnet-5-5"),
                  FieldSpec("model_escalation", "Escalation model", False, "claude_model_escalation", required=False,
                            choices=_MODELS, default="claude-opus-5-5")),
                 ("command_parsing_fallback", "research_extraction", "website_copy", "qa_fact_check", "outreach_drafts"),
                 "https://console.anthropic.com/settings/keys",
                 notes=("Metered. Spend is capped by Settings > Budget limits before every call.",)),
    ProviderSpec("serp_api", "SerpApi", "Independent web search used to verify whether a business has a website and to research it.",
                 (FieldSpec("api_key", "API key", True, "serpapi_api_key", pattern=r"^[A-Za-z0-9]{32,128}$"),),
                 ("website_verification", "business_research", "usage_reporting"), "https://serpapi.com/manage-api-key",
                 notes=("Free plan: limited searches per month. JARVIS stops at the configured cap.",)),
    ProviderSpec("firecrawl", "Firecrawl", "Extracts clean text from business websites for research.",
                 (FieldSpec("api_key", "API key", True, "firecrawl_api_key", pattern=r"^fc-[A-Za-z0-9]{16,}$"),),
                 ("page_extraction", "credit_reporting"), "https://www.firecrawl.dev/app/api-keys"),
    ProviderSpec("cloudflare", "Cloudflare Pages", "Hosts generated website previews (Direct Upload, one branch per site).",
                 (FieldSpec("api_token", "API token", True, "cloudflare_api_token", pattern=r"^[A-Za-z0-9_\-]{32,}$",
                            hint="API token with Account > Cloudflare Pages > Edit."),
                  FieldSpec("account_id", "Account ID", False, "cloudflare_account_id", pattern=r"^[0-9a-f]{32}$"),
                  FieldSpec("preview_project", "Preview project", False, "cloudflare_pages_preview_project", required=False,
                            pattern=r"^[a-z0-9][a-z0-9-]{0,56}[a-z0-9]$", default="jarvis-previews")),
                 ("preview_deployment", "deployment_verification", "preview_cleanup"),
                 "https://dash.cloudflare.com/profile/api-tokens"),
    ProviderSpec("livekit", "LiveKit", "Real-time voice: the browser and the JARVIS voice agent meet in a LiveKit room.",
                 (FieldSpec("url", "Server URL", False, "livekit_url", pattern=r"^(wss|https|ws|http)://[A-Za-z0-9.\-:]+/?$",
                            hint="e.g. wss://your-project.livekit.cloud"),
                  FieldSpec("api_key", "API key", True, "livekit_api_key", pattern=r"^[A-Za-z0-9]{8,64}$"),
                  FieldSpec("api_secret", "API secret", True, "livekit_api_secret", pattern=r"^[A-Za-z0-9]{20,128}$")),
                 ("voice_sessions", "scoped_access_tokens"), "https://cloud.livekit.io",
                 notes=("Voice also needs the JARVIS voice agent process running (apps/voice) with VOICE_AGENT_SHARED_SECRET.",)),
    ProviderSpec("resend", "Resend", "Sends outreach email after every sending gate passes (never automatic).",
                 (FieldSpec("api_key", "API key", True, "resend_api_key", pattern=r"^re_[A-Za-z0-9_]{16,}$"),
                  FieldSpec("webhook_secret", "Webhook signing secret", True, "resend_webhook_secret", required=False,
                            pattern=r"^whsec_[A-Za-z0-9+/=]{16,}$")),
                 ("email_sending", "domain_status", "delivery_webhooks"), "https://resend.com/api-keys",
                 notes=("Sending also requires your own verified sending domain, a postal address and an unsubscribe key "
                        "(Outreach > Sender readiness).",)),
    ProviderSpec("google_places", "Google Places API (New)", "Prospect discovery and rating filter. RESTRICTED.",
                 (FieldSpec("api_key", "API key", True, "google_places_api_key", pattern=r"^AIza[0-9A-Za-z_\-]{35}$"),),
                 ("prospect_discovery", "rating_filter", "listing_display"),
                 "https://console.cloud.google.com/google/maps-apis/credentials",
                 notes=("Blocked until written approval from Google or qualified counsel is recorded "
                        "(Settings > Google Places compliance gate).",)),
    ProviderSpec("supabase", "Supabase", "Production database and user authentication.",
                 (), ("database", "authentication"), "https://supabase.com/dashboard", managed_in_dashboard=False,
                 notes=("Infrastructure configuration: set DATABASE_URL, SUPABASE_URL and SUPABASE_JWKS_URL in the server "
                        "environment. It cannot be changed from the browser.",)),
)}
PROVIDERS = list(SPECS)


def spec(provider: str) -> ProviderSpec:
    if provider not in SPECS:
        raise NotFound(f"unknown provider {provider!r}")
    return SPECS[provider]


# ---------------------------------------------------------------- input validation
def validate_input(provider: str, secrets: dict[str, str], config: dict[str, str], existing: dict | None) -> None:
    sp = spec(provider)
    if not sp.managed_in_dashboard:
        raise ValidationFailed(f"{sp.name} is infrastructure configuration and is managed in the server environment")
    known = {f.name: f for f in sp.fields}
    errors: dict[str, str] = {}
    for name, value in {**secrets, **config}.items():
        f = known.get(name)
        if f is None:
            errors[name] = "unknown field"
            continue
        if f.secret != (name in secrets):
            errors[name] = "must be sent as a secret" if f.secret else "is not a secret field"
            continue
        if value != value.strip() or not value:
            errors[name] = "must be non-empty with no surrounding whitespace"
        elif f.choices and value not in f.choices:
            errors[name] = f"must be one of {list(f.choices)}"
        elif f.pattern and not re.fullmatch(f.pattern, value):
            errors[name] = f"has an invalid format{(': ' + f.hint) if f.hint else ''}"
    have = set(secrets) | set(config) | set((existing or {}).get("secrets", {})) | set((existing or {}).get("config", {}))
    for f in sp.fields:
        if f.required and f.name not in have:
            errors[f.name] = "is required"
    if errors:   # messages name fields only; values are never echoed
        raise ValidationFailed("invalid credential fields", details={"fields": errors})


# ---------------------------------------------------------------- storage
async def stored(db) -> dict[str, dict]:
    return {r["provider"]: dict(r) for r in await db.fetch("select * from integration_credentials")}


async def save(db, vault: Vault | None, provider: str, secrets: dict[str, str], config: dict[str, str], user_id) -> None:
    async with db.transaction() as conn:
        row = await conn.fetchrow("select secrets, config, key_id from integration_credentials where provider=$1 for update", provider)
        existing = dict(row) if row else None
        validate_input(provider, secrets, config, existing)
        enc = dict((existing or {}).get("secrets") or {})
        if secrets:
            assert vault is not None, "secrets require a vault"
            enc.update({k: vault.encrypt(provider, k, v) for k, v in secrets.items()})
        kid = vault.kid if vault is not None else (existing or {}).get("key_id")
        cfg = {**((existing or {}).get("config") or {}), **config}
        await conn.execute("""insert into integration_credentials(provider, secrets, config, key_id, updated_by)
                              values ($1,$2,$3,$4,$5) on conflict (provider) do update set secrets=excluded.secrets,
                              config=excluded.config, key_id=excluded.key_id, updated_by=excluded.updated_by, updated_at=now()""",
                           provider, enc, cfg, kid, user_id)
        # A credential change invalidates the previous validation result.
        await conn.execute("delete from integration_status where provider=$1", provider)
        await conn.execute("""insert into audit_logs(actor_type, actor_id, action, entity_type, data)
                              values ('user',$1,'integration.credentials_saved','integration',$2)""",
                           str(user_id), {"provider": provider, "secret_fields": sorted(secrets), "config_fields": sorted(config)})


async def remove(db, provider: str, user_id, fields: list[str] | None = None) -> None:
    spec(provider)
    async with db.transaction() as conn:
        row = await conn.fetchrow("select secrets, config from integration_credentials where provider=$1 for update", provider)
        if row is None:
            raise NotFound("no credentials stored in the dashboard for this provider")
        if fields:
            secrets = {k: v for k, v in row["secrets"].items() if k not in fields}
            config = {k: v for k, v in row["config"].items() if k not in fields}
            await conn.execute("update integration_credentials set secrets=$2, config=$3, updated_by=$4, updated_at=now() where provider=$1",
                               provider, secrets, config, user_id)
        else:
            await conn.execute("delete from integration_credentials where provider=$1", provider)
        await conn.execute("delete from integration_status where provider=$1", provider)
        await conn.execute("""insert into audit_logs(actor_type, actor_id, action, entity_type, data)
                              values ('user',$1,'integration.credentials_removed','integration',$2)""",
                           str(user_id), {"provider": provider, "fields": fields or "all"})


async def set_enabled(db, provider: str, enabled: bool, user_id) -> None:
    sp = spec(provider)
    if not sp.managed_in_dashboard:
        raise ValidationFailed(f"{sp.name} cannot be disabled from the dashboard")
    async with db.transaction() as conn:
        await conn.execute("""insert into integration_credentials(provider, enabled, updated_by) values ($1,$2,$3)
                              on conflict (provider) do update set enabled=excluded.enabled, updated_by=excluded.updated_by, updated_at=now()""",
                           provider, enabled, user_id)
        await conn.execute("""insert into audit_logs(actor_type, actor_id, action, entity_type, data)
                              values ('user',$1,$2,'integration',$3)""",
                           str(user_id), "integration.enabled" if enabled else "integration.disabled", {"provider": provider})


# ---------------------------------------------------------------- effective settings
def effective_settings(base: Settings, rows: dict[str, dict], vault: Vault | None) -> tuple[Settings, dict[str, dict]]:
    """Merges dashboard credentials over the environment. Returns (settings, per-provider source info)."""
    upd: dict[str, Any] = {}
    info: dict[str, dict] = {}
    for p, sp in SPECS.items():
        row = rows.get(p) or {}
        src: dict[str, str] = {}
        for f in sp.fields:
            env_val = getattr(base, f.setting)
            if f.name in (row.get("secrets") or {}):
                if vault is None:
                    src[f.name] = "dashboard_unreadable"
                    continue
                val = vault.decrypt(p, f.name, row["secrets"][f.name])
                upd[f.setting] = SecretStr(val)
                src[f.name] = "dashboard"
            elif f.name in (row.get("config") or {}):
                upd[f.setting] = row["config"][f.name]
                src[f.name] = "dashboard"
            elif env_val:
                src[f.name] = "environment"
        if p == "resend" and "resend_api_key" in upd:
            upd["email_provider"] = "resend"
        info[p] = {"sources": src, "enabled": row.get("enabled", True), "updated_at": row.get("updated_at")}
    return base.model_copy(update=upd), info


async def load_effective(db, base: Settings) -> tuple[Settings, dict[str, dict], str | None]:
    rows = await stored(db)
    vault, err = None, None
    if any(r.get("secrets") for r in rows.values()):
        try:
            vault = Vault.from_settings(base)
        except Exception as exc:   # fail closed: stored secrets are ignored, the reason is reported
            err = getattr(exc, "message", str(exc))
    try:
        eff, info = effective_settings(base, rows, vault)
    except Exception as exc:
        err = getattr(exc, "message", str(exc))
        eff, info = effective_settings(base, {}, None)
    return eff, info, err


def apply_to_adapters(deps, livekit, eff: Settings, info: dict[str, dict]) -> None:
    deps.settings = eff
    for a in (deps.places, deps.serp, deps.firecrawl, deps.cloudflare, deps.email):
        a.settings = eff
    deps.claude.reconfigure(eff)
    if livekit is not None:
        livekit.settings = eff
    DISABLED.clear()
    DISABLED.update(p for p, i in info.items() if not i.get("enabled", True))


async def refresh(app_or_deps, base: Settings, livekit=None) -> dict:
    """Loads stored credentials and applies them to the adapters of this process."""
    deps = getattr(app_or_deps, "deps", app_or_deps)
    eff, info, err = await load_effective(deps.db, base)
    apply_to_adapters(deps, livekit, eff, info)
    return {"info": info, "error": err}


async def ensure_fresh(state) -> None:
    """Re-applies stored credentials in this API process if they changed anywhere (another instance, the worker
    host, or a direct database change). Cheap: one aggregate query."""
    v = await credentials_version(state.db)
    if v != getattr(state, "credentials_version", None):
        res = await refresh(state, state.base_settings, state.livekit)
        state.credentials_error = res["error"]
        state.credentials_version = v


async def credentials_version(db) -> str:
    v = await db.fetchval("select coalesce(max(updated_at)::text, '') || ':' || count(*) from integration_credentials")
    return v or ""


# ---------------------------------------------------------------- configured / validation
def configured(provider: str, deps, livekit) -> bool:
    return {"anthropic": deps.claude.configured, "google_places": deps.places.configured, "serp_api": deps.serp.configured,
            "firecrawl": deps.firecrawl.configured, "cloudflare": deps.cloudflare.configured, "resend": deps.email.configured,
            "livekit": livekit.configured,
            "supabase": bool(deps.settings.supabase_url and (deps.settings.supabase_jwks_url or deps.settings.supabase_jwt_secret))
            }[provider]


def missing_fields(provider: str, deps, livekit) -> list[str]:
    if provider == "supabase":
        s = deps.settings
        return [n for n, v in (("SUPABASE_URL", s.supabase_url),
                               ("SUPABASE_JWKS_URL or SUPABASE_JWT_SECRET", s.supabase_jwks_url or s.supabase_jwt_secret)) if not v]
    if provider == "resend" and deps.settings.resend_api_key:
        return []
    return [f.label for f in spec(provider).fields if f.required and not getattr(deps.settings, f.setting)]


async def validate(provider: str, deps, livekit, db, workspace_id) -> tuple[dict, dict | None]:
    """Calls the real provider (free endpoints only). Returns (result, quota). Raises ProviderError."""
    s = deps.settings
    if provider == "anthropic":
        models = {s.claude_model_fast, s.claude_model_build, s.claude_model_escalation}
        res = {m: await deps.claude.validate_model(m) for m in sorted(models)}
        return {"models": sorted(models), "validated": res}, None
    if provider == "serp_api":
        acct = await deps.serp.account()
        if acct.get("error"):
            low = str(acct["error"]).lower()
            state = IntegrationState.INVALID_CREDENTIALS if "invalid" in low and "key" in low else \
                IntegrationState.QUOTA_EXCEEDED if "run out" in low or "limit" in low else IntegrationState.PROVIDER_ERROR
            raise ProviderError("serp_api", state, str(acct["error"])[:300])
        quota = {k: acct.get(k) for k in ("plan_name", "searches_per_month", "this_month_usage", "plan_searches_left",
                                          "total_searches_left") if k in acct}
        if acct.get("total_searches_left") == 0 or acct.get("plan_searches_left") == 0:
            raise ProviderError("serp_api", IntegrationState.QUOTA_EXCEEDED, "no searches left on this SerpApi plan this month")
        return {"account": quota.get("plan_name")}, quota
    if provider == "firecrawl":
        data = (await deps.firecrawl.credit_usage()).get("data") or {}
        quota = {k: data.get(k) for k in ("remaining_credits", "plan_credits", "billing_period_start", "billing_period_end") if k in data}
        if data.get("remaining_credits") == 0:
            raise ProviderError("firecrawl", IntegrationState.QUOTA_EXCEEDED, "no Firecrawl credits remaining")
        return {}, quota
    if provider == "cloudflare":
        tok = await deps.cloudflare.verify_token()
        status = (tok.get("result") or {}).get("status")
        if status and status != "active":
            raise ProviderError("cloudflare", IntegrationState.INVALID_CREDENTIALS, f"token status is {status}")
        projects = await deps.cloudflare.list_projects()
        names = [p.get("name") for p in projects]
        return {"token_status": status, "pages_projects": len(names),
                "preview_project": s.cloudflare_pages_preview_project,
                "preview_project_exists": s.cloudflare_pages_preview_project in names}, None
    if provider == "livekit":
        return await livekit.check(), None
    if provider == "resend":
        doms = await deps.email.list_domains()
        verified = [d.get("name") for d in doms if d.get("status") == "verified"]
        return {"domains": len(doms), "verified_domains": verified}, None
    if provider == "google_places":
        from jarvis.integrations.base import CallContext
        from jarvis.integrations.google_places import IDS_ONLY
        page = await deps.places.text_search(CallContext(db, workspace_id), text_query="plumber in Austin, TX", min_rating=None,
                                             include_service_area=True, page_size=1, mask=IDS_ONLY,
                                             scope="google_places_technical_testing")
        return {"returned_ids": len(page.places), "sku": page.sku}, None
    if provider == "supabase":
        if not await db.health():
            raise ProviderError("supabase", IntegrationState.TEMPORARILY_UNAVAILABLE, "database is unreachable")
        out: dict[str, Any] = {"database": "reachable"}
        if s.supabase_jwks_url:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(s.supabase_jwks_url)
            except httpx.HTTPError as exc:
                raise ProviderError("supabase", IntegrationState.TEMPORARILY_UNAVAILABLE, f"JWKS unreachable: {exc}") from exc
            if r.status_code != 200 or not r.json().get("keys"):
                raise ProviderError("supabase", IntegrationState.PROVIDER_ERROR, f"JWKS returned HTTP {r.status_code} with no keys")
            out["jwks_keys"] = len(r.json()["keys"])
        return out, None
    raise NotFound("unknown provider")


async def record_status(db, provider: str, state: str, detail: str | None, *, capabilities: list[str], quota: dict | None,
                        user_id, ok: bool) -> None:
    now = datetime.now(UTC)
    await db.execute(
        """insert into integration_status(provider, state, detail, capabilities, quota, last_tested_at, last_success_at,
                                          last_error_at, last_error, tested_by)
           values ($1, $2::integration_state, $3::text, $4, $5, $6::timestamptz,
                   case when $7::boolean then $6::timestamptz end, case when $7::boolean then null else $6::timestamptz end,
                   case when $7::boolean then null else $3::text end, $8)
           on conflict (provider) do update set state=excluded.state, detail=excluded.detail, capabilities=excluded.capabilities,
             quota=coalesce(excluded.quota, integration_status.quota), last_tested_at=excluded.last_tested_at,
             last_success_at=coalesce(excluded.last_success_at, integration_status.last_success_at),
             last_error_at=coalesce(excluded.last_error_at, integration_status.last_error_at),
             last_error=case when $7::boolean then integration_status.last_error else excluded.last_error end, tested_by=excluded.tested_by""",
        provider, state, detail, capabilities, quota, now, ok, user_id)
