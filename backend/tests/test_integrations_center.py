"""Integrations Center: encrypted credential storage, admin-only management, no secret leakage, real-provider
validation semantics (provider HTTP simulated by httpx MockTransport: these are NOT live-provider tests),
explicit states, disable/remove, worker propagation."""
import base64
import json
import os

import httpx
import pytest

from jarvis.core import integrations as I
from jarvis.core.errors import IntegrationState, ProviderError
from jarvis.security.vault import DecryptionFailed, EncryptionNotConfigured, Vault, parse_key, rotate
from tests.conftest import login

KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()
SERP_KEY = "a1b2c3d4" * 8                       # format-valid test value, not a real key
ADMIN = "admin@integrations.dev"


@pytest.fixture
async def center(deps, db):
    """API client whose environment has an encryption key and one deployment admin."""
    from jarvis.api.app import create_app
    await db.execute("delete from integration_credentials; delete from integration_status")
    s = deps.settings.model_copy(update={"jarvis_encryption_key": I.SecretStr(KEY), "jarvis_admin_emails": ADMIN})
    for a in (deps.places, deps.serp, deps.firecrawl, deps.cloudflare, deps.email):
        a.settings = s
    deps.settings = s
    app = create_app(s, deps)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.app = app
        yield c
    await db.execute("delete from integration_credentials; delete from integration_status")
    I.DISABLED.clear()


def serp_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def cards(r):
    return {c["provider"]: c for c in r.json()["integrations"]}


# ------------------------------------------------------------------ vault
def test_vault_roundtrip_binding_wrong_key_and_format():
    v = Vault(parse_key(KEY))
    env = v.encrypt("serp_api", "api_key", "secret-value")
    assert "secret-value" not in json.dumps(env) and v.decrypt("serp_api", "api_key", env) == "secret-value"
    with pytest.raises(DecryptionFailed):                       # bound to provider + field
        v.decrypt("firecrawl", "api_key", env)
    other = Vault(os.urandom(32))
    with pytest.raises(DecryptionFailed, match="different key"):
        other.decrypt("serp_api", "api_key", env)
    tampered = {**env, "c": base64.b64encode(b"x" * 40).decode()}
    with pytest.raises(DecryptionFailed, match="tampered"):
        v.decrypt("serp_api", "api_key", tampered)
    for bad in (None, "", "not base64!!", base64.urlsafe_b64encode(b"short").decode()):
        with pytest.raises(EncryptionNotConfigured):
            parse_key(bad)


async def test_rotation_reencrypts_under_new_key(db):
    await db.execute("delete from integration_credentials")
    old, new = os.urandom(32), os.urandom(32)
    v_old = Vault(old)
    await db.execute("insert into integration_credentials(provider, secrets, key_id) values ('serp_api', $1, $2)",
                     {"api_key": v_old.encrypt("serp_api", "api_key", "k-123")}, v_old.kid)
    v = Vault(new, previous=old)
    assert await rotate(db, v) == 1
    row = await db.fetchrow("select secrets, key_id from integration_credentials where provider='serp_api'")
    assert row["key_id"] == v.kid and Vault(new).decrypt("serp_api", "api_key", row["secrets"]["api_key"]) == "k-123"
    await db.execute("delete from integration_credentials")


async def test_database_rejects_plaintext_secret(db):
    import asyncpg
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("insert into integration_credentials(provider, secrets) values ('serp_api', $1)", {"api_key": "plaintext"})


# ------------------------------------------------------------------ authorization and fail-closed
async def test_only_admins_manage_and_missing_key_fails_closed(center, deps, db):
    a = await login(center, "operator@integrations.dev")
    r = await center.put("/integrations/serp_api/credentials", headers=a["headers"], json={"secrets": {"api_key": SERP_KEY}})
    assert r.status_code == 403 and "administrator" in r.json()["error"]["message"]
    assert (await center.get("/integrations", headers=a["headers"])).json()["can_manage"] is False
    # Admin, but no encryption key: refused, nothing stored.
    adm = await login(center, ADMIN)
    center.app.state.base_settings = center.app.state.base_settings.model_copy(update={"jarvis_encryption_key": None})
    r = await center.put("/integrations/serp_api/credentials", headers=adm["headers"], json={"secrets": {"api_key": SERP_KEY}})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ENCRYPTION_NOT_CONFIGURED"
    assert await db.fetchval("select count(*) from integration_credentials") == 0
    body = (await center.get("/integrations", headers=adm["headers"])).json()
    assert body["vault"]["ready"] is False and "JARVIS_ENCRYPTION_KEY" in body["vault"]["detail"]


async def test_no_admin_configured_explains_setting(center):
    center.app.state.base_settings = center.app.state.base_settings.model_copy(update={"jarvis_admin_emails": ""})
    a = await login(center, ADMIN)
    r = await center.put("/integrations/serp_api/credentials", headers=a["headers"], json={"secrets": {"api_key": SERP_KEY}})
    assert r.status_code == 403 and "JARVIS_ADMIN_EMAILS" in r.json()["error"]["message"]


# ------------------------------------------------------------------ save / never leak / validate
async def test_save_masks_secret_everywhere_and_requires_test(center, deps, db):
    a = await login(center, ADMIN)
    r = await center.put("/integrations/serp_api/credentials", headers=a["headers"], json={"secrets": {"api_key": SERP_KEY}})
    assert r.status_code == 200 and SERP_KEY not in r.text
    lst = await center.get("/integrations", headers=a["headers"])
    assert SERP_KEY not in lst.text
    c = cards(lst)["serp_api"]
    f = c["fields"][0]
    assert f["set"] and f["source"] == "dashboard" and f["display"] == "••••••••"
    assert c["state"] == "NOT_TESTED"                          # configured is not connected
    raw = await db.fetchval("select secrets::text from integration_credentials where provider='serp_api'")
    assert SERP_KEY not in raw and '"kid"' in raw
    logs = await db.fetchval("select string_agg(data::text, ' ') from audit_logs where action like 'integration.%'")
    assert SERP_KEY not in (logs or "")
    assert deps.serp.settings.serpapi_api_key.get_secret_value() == SERP_KEY   # applied to the live adapter


async def test_invalid_input_never_echoes_values(center):
    a = await login(center, ADMIN)
    bad = "bad key with spaces"
    r = await center.put("/integrations/serp_api/credentials", headers=a["headers"], json={"secrets": {"api_key": bad}})
    assert r.status_code == 400 and r.json()["error"]["details"]["fields"]["api_key"] and bad not in r.text
    r = await center.put("/integrations/serp_api/credentials", headers=a["headers"], json={"secrets": ["sk-leak-me"]})
    assert r.status_code == 422 and "sk-leak-me" not in r.text
    r = await center.put("/integrations/serp_api/credentials", headers=a["headers"], json={"config": {"api_key": SERP_KEY}})
    assert r.status_code == 400 and SERP_KEY not in r.text      # secret sent as plain config is refused
    r = await center.put("/integrations/supabase/credentials", headers=a["headers"], json={"config": {"x": "y"}})
    assert r.status_code == 400 and "server environment" in r.json()["error"]["message"]


# ------------------------------------------------------------------ connection tests and state mapping
@pytest.mark.parametrize("response,state", [
    (httpx.Response(200, json={"plan_name": "Free", "searches_per_month": 250, "this_month_usage": 3, "plan_searches_left": 247}),
     "CONNECTED"),
    (httpx.Response(401, json={"error": "Invalid API key."}), "INVALID_CREDENTIALS"),
    (httpx.Response(200, json={"error": "Invalid API key. Your API key should be here"}), "INVALID_CREDENTIALS"),
    (httpx.Response(429, json={"error": "rate limited"}), "RATE_LIMITED"),
    (httpx.Response(200, json={"plan_name": "Free", "plan_searches_left": 0}), "QUOTA_EXCEEDED"),
    (httpx.Response(500, text="boom"), "CONNECTION_FAILED"),
    ("connect_error", "CONNECTION_FAILED"),
])
async def test_test_connection_states(center, deps, db, response, state):
    a = await login(center, ADMIN)

    def handler(req: httpx.Request):
        assert "account.json" in req.url.path
        if response == "connect_error":
            raise httpx.ConnectError("refused", request=req)
        return response
    deps.serp.http = serp_transport(handler)
    r = await center.post("/integrations/serp_api/test", headers=a["headers"])
    assert r.json()["state"] == state, r.text
    c = cards(await center.get("/integrations", headers=a["headers"]))["serp_api"]
    assert c["state"] == state
    if state == "CONNECTED":
        assert c["last_success_at"] and c["quota"]["plan_searches_left"] == 247 and c["validated_capabilities"]
    else:
        assert c["last_error"] and c["last_success_at"] is None


async def test_missing_credentials_and_places_gate(center, deps):
    a = await login(center, ADMIN)
    c = cards(await center.get("/integrations", headers=a["headers"]))
    assert c["livekit"]["state"] == "NOT_CONFIGURED" and "Server URL" in c["livekit"]["detail"]
    assert c["google_places"]["state"] == "BLOCKED_PENDING_APPROVAL"
    assert (await center.post("/integrations/livekit/test", headers=a["headers"])).json()["state"] == "NOT_CONFIGURED"
    assert c["supabase"]["managed_in_dashboard"] is False


async def test_disable_blocks_calls_and_remove_falls_back(center, deps, db):
    from jarvis.integrations.base import CallContext, CallOutcome, gateway_call
    from tests.test_core_db import new_ws
    a = await login(center, ADMIN)
    await center.put("/integrations/serp_api/credentials", headers=a["headers"], json={"secrets": {"api_key": SERP_KEY}})
    assert (await center.post("/integrations/serp_api/disable", headers=a["headers"])).status_code == 200
    assert cards(await center.get("/integrations", headers=a["headers"]))["serp_api"]["state"] == "DISABLED"
    assert (await center.post("/integrations/serp_api/test", headers=a["headers"])).json()["state"] == "DISABLED"
    ws, _ = await new_ws(db)
    called = []

    async def fn():
        called.append(1)
        return CallOutcome(None, 1, 0)
    with pytest.raises(ProviderError) as ei:
        await gateway_call(CallContext(db, ws), provider="serp_api", operation="search", unit="search", units=1,
                           estimated_micros=0, fn=fn)
    assert ei.value.state == IntegrationState.DISABLED and not called
    await center.post("/integrations/serp_api/enable", headers=a["headers"])
    assert "serp_api" not in I.DISABLED
    r = await center.delete("/integrations/serp_api/credentials", headers=a["headers"])
    assert r.status_code == 200
    # falls back to the environment value
    assert deps.serp.settings.serpapi_api_key.get_secret_value() == "s" * 64
    acts = [x["action"] for x in await db.fetch("select action from audit_logs where action like 'integration.%' order by id")]
    assert {"integration.credentials_saved", "integration.disabled", "integration.enabled", "integration.credentials_removed"} <= set(acts)


async def test_worker_picks_up_saved_credentials(center, deps, db):
    from jarvis.worker.main import Worker
    w = Worker(deps, worker_id="cred-test")
    assert await w.refresh_credentials() is False                 # nothing stored yet: env untouched
    a = await login(center, ADMIN)
    w.base_settings = center.app.state.base_settings
    await center.put("/integrations/firecrawl/credentials", headers=a["headers"], json={"secrets": {"api_key": "fc-" + "z" * 24}})
    deps.firecrawl.settings = deps.firecrawl.settings.model_copy(update={"firecrawl_api_key": None})   # simulate a separate process
    assert await w.refresh_credentials() is True
    assert deps.firecrawl.settings.firecrawl_api_key.get_secret_value() == "fc-" + "z" * 24


async def test_other_provider_validations(center, deps):
    a = await login(center, ADMIN)

    def cf(req: httpx.Request):
        if req.url.path.endswith("/tokens/verify"):
            return httpx.Response(200, json={"result": {"status": "active"}, "success": True})
        if req.url.path.endswith("/pages/projects"):
            return httpx.Response(200, json={"result": [{"name": "jarvis-previews"}], "success": True})
        return httpx.Response(404)
    deps.cloudflare.http = serp_transport(cf)
    r = (await center.post("/integrations/cloudflare/test", headers=a["headers"])).json()
    assert r["state"] == "CONNECTED" and r["result"]["preview_project_exists"] is True

    deps.cloudflare.http = serp_transport(lambda req: httpx.Response(403, json={"errors": [{"message": "no Pages access"}]}))
    assert (await center.post("/integrations/cloudflare/test", headers=a["headers"])).json()["state"] == "PERMISSION_DENIED"

    deps.email.http = serp_transport(lambda req: httpx.Response(200, json={"data": [{"name": "agency.example", "status": "verified"}]}))
    r = (await center.post("/integrations/resend/test", headers=a["headers"])).json()
    assert r["state"] == "CONNECTED" and r["result"]["verified_domains"] == ["agency.example"]
    deps.email.http = serp_transport(lambda req: httpx.Response(401, json={"message": "API key is invalid"}))
    assert (await center.post("/integrations/resend/test", headers=a["headers"])).json()["state"] == "INVALID_CREDENTIALS"

    deps.firecrawl.http = serp_transport(lambda req: httpx.Response(200, json={"data": {"remaining_credits": 0, "plan_credits": 500}}))
    assert (await center.post("/integrations/firecrawl/test", headers=a["headers"])).json()["state"] == "QUOTA_EXCEEDED"
    deps.firecrawl.http = serp_transport(lambda req: httpx.Response(402, json={"error": "Payment required"}))
    assert (await center.post("/integrations/firecrawl/test", headers=a["headers"])).json()["state"] == "QUOTA_EXCEEDED"


def test_rejected_key_reported_as_400_maps_to_invalid_credentials():
    """Resend answers a rejected API key with HTTP 400 (body captured from the real API on 2026-11-19)."""
    from jarvis.integrations.base import map_http_error
    real = httpx.Response(400, json={"statusCode": 400, "message": "API key is invalid", "name": "validation_error"})
    assert map_http_error("resend", real).state == IntegrationState.INVALID_CREDENTIALS
    other = httpx.Response(400, json={"message": "missing field 'to'", "name": "validation_error"})
    assert map_http_error("resend", other).state == IntegrationState.PROVIDER_ERROR


async def test_api_instance_picks_up_changes_made_elsewhere(center, deps, db):
    """Another API instance saves or removes credentials: this instance applies the change on its next request."""
    a = await login(center, ADMIN)
    v = Vault(parse_key(KEY))
    await center.get("/integrations", headers=a["headers"])                          # baseline version
    await db.execute("insert into integration_credentials(provider, secrets, key_id) values ('firecrawl', $1, $2)",
                     {"api_key": v.encrypt("firecrawl", "api_key", "fc-" + "q" * 24)}, v.kid)
    c = cards(await center.get("/integrations", headers=a["headers"]))["firecrawl"]
    assert c["fields"][0]["source"] == "dashboard" and c["state"] == "NOT_TESTED"
    assert deps.firecrawl.settings.firecrawl_api_key.get_secret_value() == "fc-" + "q" * 24
    await db.execute("delete from integration_credentials where provider='firecrawl'")
    await center.get("/integrations", headers=a["headers"])
    assert deps.firecrawl.settings.firecrawl_api_key.get_secret_value() == "fc-test"   # back to the environment value
