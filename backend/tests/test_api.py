"""API: authentication, workspace isolation, roles, compliance gate (T12/T13), commands."""
from datetime import UTC, datetime, timedelta

from jarvis.core.compliance import INTENDED_USE
from tests.conftest import login


async def test_auth_required(client):
    assert (await client.get("/campaigns")).status_code == 401
    assert (await client.get("/campaigns", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).status_code == 200


async def test_cross_workspace_isolation(client):
    a = await login(client, "a@iso.dev")
    b = await login(client, "b@iso.dev")
    r = await client.post("/campaigns/import", headers=a["headers"], json={
        "niche": "plumbing", "location": {"city": "Houston", "state": "TX"}, "rights_attestation": True,
        "rows": [{"business_name": "Iso Plumbing", "phone": "713-555-0100"}]})
    assert r.status_code == 200, r.text
    cid = r.json()["campaign_id"]
    assert (await client.get(f"/campaigns/{cid}", headers=a["headers"])).status_code == 200
    assert (await client.get(f"/campaigns/{cid}", headers=b["headers"])).status_code == 404
    assert cid not in [c["id"] for c in (await client.get("/campaigns", headers=b["headers"])).json()]
    assert (await client.post(f"/campaigns/{cid}/cancel", headers=b["headers"])).status_code == 404
    pros = (await client.get("/prospects", headers=b["headers"])).json()
    assert pros == []
    # forged workspace header
    h = {**b["headers"], "X-Workspace-Id": a["me"]["workspace_id"]}
    assert (await client.get("/campaigns", headers=h)).status_code == 403


async def test_viewer_cannot_write(client):
    a = await login(client, "owner@roles.dev")
    v = await login(client, "viewer@roles.dev")
    db = client.app.state.db
    await db.execute("insert into memberships(workspace_id, user_id, role) values ($1,$2,'viewer')",
                     a["me"]["workspace_id"], v["me"]["user_id"])
    h = {**v["headers"], "X-Workspace-Id": a["me"]["workspace_id"]}
    assert (await client.post("/commands", headers=h, json={"text": "Find 3 plumbers in Houston"})).status_code == 403
    assert (await client.get("/campaigns", headers=h)).status_code == 200


async def test_t12_places_gate_blocks_with_zero_requests(client, deps, web):
    a = await login(client, "gate@t.dev")
    r = await client.post("/commands", headers=a["headers"], json={"text": "Find 100 plumbers in Houston with at least a 3.5 Google rating and no website."})
    body = r.json()
    assert body["status"] == "needs_clarification" and body["questions"][0]["field"] == "missing_rating_policy"
    r = await client.post(f"/commands/{body['id']}/answer", headers=a["headers"], json={"answers": {"missing_rating_policy": "exclude"}})
    body = r.json()
    assert body["status"] == "awaiting_confirmation" and body["compliance"]["open"] is False
    assert body["plan"]["places_requests_max"] > 0
    r = await client.post(f"/commands/{body['id']}/confirm", headers=a["headers"], json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "COMPLIANCE_BLOCKED"
    assert not [c for c in web.calls if "places.googleapis.com" in c[1]]
    # listing panel also gated
    integ = {i["provider"]: i for i in (await client.get("/integrations", headers=a["headers"])).json()}
    assert integ["google_places"]["state"] == "BLOCKED_PENDING_APPROVAL"
    assert integ["livekit"]["state"] == "NOT_CONFIGURED"


async def test_t13_approval_lifecycle(client):
    a = await login(client, "approve@t.dev")
    h = a["headers"]
    now = datetime.now(UTC)
    base = {"scope": "google_places_prospect_selection", "approver_type": "legal_counsel", "approver_name": "Counsel LLP",
            "document_ref": "OPINION-2026-11", "approved_at": now.isoformat(), "review_by": (now + timedelta(days=180)).isoformat()}
    r = await client.post("/compliance/approvals", headers=h, json={**base, "intended_use_text": "something else"})
    assert r.status_code == 400
    r = await client.post("/compliance/approvals", headers=h, json={**base, "approver_type": "operator",
                                                                     "intended_use_text": INTENDED_USE[base["scope"]][1]})
    assert r.status_code == 400
    r = await client.post("/compliance/approvals", headers=h, json={**base, "intended_use_text": INTENDED_USE[base["scope"]][1]})
    assert r.status_code == 200
    gates = {g["scope"]: g for g in (await client.get("/compliance", headers=h)).json()["gates"]}
    assert gates["google_places_prospect_selection"]["open"] is True
    await client.delete(f"/compliance/approvals/{r.json()['id']}", headers=h)
    gates = {g["scope"]: g for g in (await client.get("/compliance", headers=h)).json()["gates"]}
    assert gates["google_places_prospect_selection"]["open"] is False
    # expired approval closes the gate
    r = await client.post("/compliance/approvals", headers=h, json={**base, "intended_use_text": INTENDED_USE[base["scope"]][1],
                          "approved_at": (now - timedelta(days=400)).isoformat(), "review_by": (now - timedelta(days=1)).isoformat()})
    gates = {g["scope"]: g for g in (await client.get("/compliance", headers=h)).json()["gates"]}
    assert gates["google_places_prospect_selection"]["open"] is False and "expired" in gates["google_places_prospect_selection"]["reason"]


async def test_limit_above_free_requires_allow_paid(client):
    a = await login(client, "limits@t.dev")
    lims = (await client.get("/settings/limits", headers=a["headers"])).json()
    serp = next(x for x in lims if x["provider"] == "serp_api")
    r = await client.put(f"/settings/limits/{serp['id']}", headers=a["headers"], json={"cap": 1000})
    assert r.status_code == 400
    r = await client.put(f"/settings/limits/{serp['id']}", headers=a["headers"], json={"cap": 1000, "allow_paid": True})
    assert r.status_code == 200


async def test_outreach_send_blocked_until_ready(client):
    a = await login(client, "out@t.dev")
    rd = (await client.get("/outreach/readiness", headers=a["headers"])).json()
    assert rd["ready"] is False and {i["key"] for i in rd["items"] if not i["ok"]} >= {"postal_address", "sending_domain"}
    cs = await client.post("/campaigns/import", headers=a["headers"], json={"niche": "plumbing", "location": {"city": "Houston", "state": "TX"},
                           "rights_attestation": True, "rows": [{"business_name": "Out Plumbing"}]})
    r = await client.post(f"/outreach/campaigns/{cs.json()['campaign_id']}/authorize", headers=a["headers"],
                          json={"daily_limit": 5, "total_limit": 10})
    assert r.status_code == 403 and r.json()["error"]["code"] == "COMPLIANCE_BLOCKED"
    r = await client.put("/outreach/sender", headers=a["headers"], json={"sending_domain": "x.pages.dev", "postal_address": "N/A",
                                                                         "postal_address_type": "street", "attest_postal_address": True})
    items = {i["key"]: i["ok"] for i in r.json()["items"]}
    assert items["sending_domain"] is False and items["postal_address"] is False
