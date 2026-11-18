"""Outreach sending gates, transactional outbox, ambiguous results, suppression, unsubscribe."""
import uuid

from jarvis.agents.outreach import unsubscribe_token
from tests.conftest import login


async def seed_draft(db, ws, *, email="owner@bizplumbing.example", prohibited=False):
    cid = await db.fetchval("""insert into campaigns(workspace_id, name, niche, location, target_count, discovery_provider, stages,
                               budget_cap_micros, per_site_cap_micros, state) values ($1,'C','plumbing','{}',1,'manual_import','{discovery}',0,0,'completed')
                               returning id""", ws)
    pid = await db.fetchval("""insert into prospects(workspace_id, campaign_id, discovery_provider, external_ref, business_category)
                               values ($1,$2,'manual_import',$3,'plumbing') returning id""", ws, cid, uuid.uuid4().hex)
    ev = await db.fetchval("""insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy)
                              values ($1,$2,'official_website','https://bizplumbing.example','indefinite') returning id""", ws, pid)
    ct = await db.fetchval("""insert into contacts(workspace_id, prospect_id, contact_type, value, evidence_id, is_public_business_contact,
                              harvesting_prohibited, verification) values ($1,$2,'email',$3,$4,$5,$6,'verified') returning id""",
                           ws, pid, email, ev, not prohibited, prohibited)
    sid = await db.fetchval("insert into generated_sites(workspace_id, prospect_id, campaign_id, slug, state) values ($1,$2,$3,$4,'deployed') returning id",
                            ws, pid, cid, f"s-{uuid.uuid4().hex[:8]}")
    spec = await db.fetchval("""insert into site_specs(workspace_id, prospect_id, spec, design, facts_used, model, prompt_version)
                                values ($1,$2,'{}','{}','{}','m','v') returning id""", ws, pid)
    vid = await db.fetchval("insert into site_versions(workspace_id, site_id, version, spec_id, reason) values ($1,$2,1,$3,'initial') returning id", ws, sid, spec)
    dep = await db.fetchval("""insert into deployments(workspace_id, site_version_id, project_name, branch, status, url)
                               values ($1,$2,'p','b','verified','https://b.p.pages.dev') returning id""", ws, vid)
    did = await db.fetchval("""insert into outreach_drafts(workspace_id, campaign_id, prospect_id, contact_id, deployment_id, subject, body_text, observations)
                               values ($1,$2,$3,$4,$5,'A website concept','Hello','[]') returning id""", ws, cid, pid, ct, dep)
    return cid, did


async def make_ready(client, db, h, ws):
    await client.put("/settings", headers=h, json={"agency_name": "Napollo Preview Studio", "terms_of_use_url": "https://agency.example/terms",
                                                     "privacy_policy_url": "https://agency.example/privacy"})
    await client.put("/outreach/sender", headers=h, json={"postal_address": "1200 Smith St Ste 1600, Houston, TX 77002", "postal_address_type": "street",
                     "attest_postal_address": True, "sending_domain": "agency.example", "from_address": "hello@agency.example", "reply_to": "hello@agency.example"})
    # DNS/provider verification is checked over the network in production (POST /outreach/sender/check-dns); set here for the test
    await db.execute("""update sender_readiness set spf_ok=true, dkim_ok=true, dmarc_ok=true, provider_domain_verified=true, dns_checked_at=now(),
                        unsubscribe_endpoint_ok=true, reply_to_confirmed=true where workspace_id=$1""", ws)


async def test_send_requires_authorization_then_sends_once(client, deps, web):
    a = await login(client, "send@t.dev")
    h, ws = a["headers"], uuid.UUID(a["me"]["workspace_id"])
    cid, did = await seed_draft(deps.db, ws)
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.status_code == 403                                              # not ready
    await make_ready(client, deps.db, h, ws)
    assert (await client.get("/outreach/readiness", headers=h)).json()["ready"] is True
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.status_code == 403 and "authorized" in r.json()["error"]["message"]   # draft not approved
    await client.post(f"/outreach/drafts/{did}/approve", headers=h)
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.status_code == 403 and "authorization" in r.json()["error"]["message"]  # campaign not authorized
    assert (await client.post(f"/outreach/campaigns/{cid}/authorize", headers=h, json={"daily_limit": 5, "total_limit": 5})).status_code == 200
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.json()["status"] == "sent" and len(web.sent) == 1
    msg = web.sent[0]
    assert "1200 Smith St" in msg["text"] and "/u/" in msg["text"] and msg["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert not msg["subject"].lower().startswith("re:")
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.status_code == 403 and len(web.sent) == 1                       # never resent


async def test_ambiguous_result_not_retried(client, deps, web):
    a = await login(client, "amb@t.dev")
    h, ws = a["headers"], uuid.UUID(a["me"]["workspace_id"])
    cid, did = await seed_draft(deps.db, ws)
    await make_ready(client, deps.db, h, ws)
    await client.post(f"/outreach/drafts/{did}/approve", headers=h)
    await client.post(f"/outreach/campaigns/{cid}/authorize", headers=h, json={"daily_limit": 5, "total_limit": 5})
    web.resend_mode = "timeout"
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.json()["status"] == "unknown_provider_result"
    web.resend_mode = "ok"
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.status_code == 403 and web.sent == []                           # ambiguous send is never blindly retried
    ob = await deps.db.fetchval("select status from email_outbox where draft_id=$1", did)
    assert ob == "unknown_provider_result"


async def test_suppression_rechecked_and_unsubscribe_fails_closed(client, deps, web):
    a = await login(client, "supp@t.dev")
    h, ws = a["headers"], uuid.UUID(a["me"]["workspace_id"])
    cid, did = await seed_draft(deps.db, ws, email="stop@bizplumbing.example")
    await make_ready(client, deps.db, h, ws)
    await client.post(f"/outreach/drafts/{did}/approve", headers=h)
    await client.post(f"/outreach/campaigns/{cid}/authorize", headers=h, json={"daily_limit": 5, "total_limit": 5})
    tok = unsubscribe_token(deps.settings.unsubscribe_signing_key.get_secret_value(), ws, "stop@bizplumbing.example")
    assert (await client.post(f"/u/{tok}tampered")).status_code == 400       # invalid token: no success shown
    r = await client.post(f"/u/{tok}")
    assert r.status_code == 200 and "unsubscribed" in r.text
    assert await deps.db.fetchval("select count(*) from suppression_records where workspace_id=$1 and reason='opt_out'", ws) == 1
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert web.sent == []


async def test_harvesting_prohibited_contact_blocked(client, deps, web):
    a = await login(client, "harv@t.dev")
    h, ws = a["headers"], uuid.UUID(a["me"]["workspace_id"])
    cid, did = await seed_draft(deps.db, ws, prohibited=True)
    await make_ready(client, deps.db, h, ws)
    await client.post(f"/outreach/drafts/{did}/approve", headers=h)
    await client.post(f"/outreach/campaigns/{cid}/authorize", headers=h, json={"daily_limit": 5, "total_limit": 5})
    r = await client.post(f"/outreach/drafts/{did}/send", headers=h)
    assert r.status_code == 403 and web.sent == []
