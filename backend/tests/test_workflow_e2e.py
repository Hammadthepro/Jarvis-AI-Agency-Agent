"""End-to-end workflow through the real worker, queue, gateway, agents, renderer and Playwright QA.
Provider HTTP is served by test doubles (tests/doubles.py); everything else is production code."""
import asyncio
import json
import subprocess

from jarvis.worker.main import Worker
from tests.conftest import login
from tests.doubles import Site

BBB_A = "https://www.bbb.org/us/tx/houston/profile/plumber/bayou-city-plumbing-co-0915"
BBB_C = "https://www.bbb.org/us/tx/houston/profile/plumber/old-pipe-plumbing-0916"


def setup_web(web):
    # A: no website anywhere; independent BBB profile confirms identity
    web.serp['"Bayou City Plumbing Co"'] = {"organic_results": [
        {"position": 1, "title": "Bayou City Plumbing Co - Yelp", "link": "https://www.yelp.com/biz/bayou-city-plumbing-co"},
        {"position": 2, "title": "Bayou City Plumbing Co | BBB", "link": BBB_A, "snippet": "Plumber in Houston"}]}
    web.serp['"(713) 555-0142"'] = {"organic_results": [{"position": 1, "title": "Bayou City Plumbing Co | BBB", "link": BBB_A}]}
    web.dns.add("www.bbb.org")
    profile = ("<html><title>Bayou City Plumbing Co | BBB</title><body><h1>Bayou City Plumbing Co</h1>"
               "<p>Phone: (713) 555-0142</p><p>Houston, TX</p></body></html>")
    web.sites[BBB_A] = Site(200, profile)
    web.sites["https://www.bbb.org/robots.txt"] = Site(404, "")
    web.firecrawl_pages[BBB_A] = ("# Bayou City Plumbing Co\nPhone: (713) 555-0142\n\n- Drain cleaning\n- Water heater repair\n"
                                  "- Leak detection\n\nServing Houston and Harris County.")
    # B: listed website works and matches -> filtered out by NO_WEBSITE/BROKEN filter
    web.dns.add("heightsdrainpros.com")
    web.sites["https://heightsdrainpros.com"] = Site(200, "<html><head><meta name=\"viewport\" content=\"width=device-width\"><title>Heights Drain Pros</title></head>"
                                                     "<body>Heights Drain Pros (713) 555-0177 Houston © 2026</body></html>")
    # C: listed website broken on both probes
    web.serp['"Old Pipe Plumbing"'] = {"organic_results": [{"position": 1, "title": "Old Pipe Plumbing | BBB", "link": BBB_C}]}
    web.sites[BBB_C] = Site(200, "<html><title>Old Pipe Plumbing</title><body>Old Pipe Plumbing (713) 555-0199 Houston</body></html>")
    web.firecrawl_pages[BBB_C] = "# Old Pipe Plumbing\n\n- Repiping\n\nServing Houston."


async def run_until(deps, cid, states=("completed", "blocked", "failed"), timeout=120):
    w = Worker(deps, worker_id="test-worker", concurrency=3, poll_s=0.05, grace_s=5)
    srv = asyncio.create_task(w.serve())
    try:
        for _ in range(int(timeout / 0.25)):
            st = await deps.db.fetchval("select state from campaigns where id=$1", cid)
            if st in states:
                return st
            await asyncio.sleep(0.25)
        raise AssertionError(f"campaign stuck in {st}")
    finally:
        w.stop.set()
        await srv
        await deps.db.connect()


async def test_full_workflow_manual_import(client, deps, web):
    setup_web(web)
    a = await login(client, "e2e@t.dev")
    h = a["headers"]
    await client.put("/settings", headers=h, json={"agency_name": "Napollo Preview Studio"})
    r = await client.post("/campaigns/import", headers=h, json={
        "niche": "plumbing", "location": {"city": "Houston", "state": "TX"}, "rights_attestation": True,
        "website_filter": ["NO_WEBSITE", "WEBSITE_BROKEN"], "sites_to_build": 1, "rows": [
            {"business_name": "Bayou City Plumbing Co", "phone": "(713) 555-0142", "city": "Houston", "state": "TX"},
            {"business_name": "Heights Drain Pros", "phone": "713-555-0177", "website": "https://heightsdrainpros.com"},
            {"business_name": "Old Pipe Plumbing", "phone": "713-555-0199", "website": "https://oldpipeplumbing.com"}]})
    assert r.status_code == 200, r.text
    cid = r.json()["campaign_id"]
    final = await run_until(deps, cid)
    assert final == "completed", await deps.db.fetchval("select blocked_reason from campaigns where id=$1", cid)

    pros = {p["business_name"]: p for p in (await client.get(f"/prospects?campaign_id={cid}", headers=h)).json()}
    a_, b_, c_ = pros["Bayou City Plumbing Co"], pros["Heights Drain Pros"], pros["Old Pipe Plumbing"]
    assert a_["website_status"] == "VERIFIED_NO_WEBSITE" and a_["state"] == "qualified" and a_["score"] >= 80
    assert b_["website_status"] == "VERIFIED_HAS_WEBSITE" and b_["site_health"] == "working" and b_["state"] == "rejected"
    assert c_["website_status"] == "VERIFIED_HAS_WEBSITE" and c_["site_health"] == "broken"
    # research: invented fact (quote not on page) dropped, real ones kept with quotes
    detail = (await client.get(f"/prospects/{a_['id']}", headers=h)).json()
    services = [f["value"] for f in detail["facts"] if f["fact_key"] == "service"]
    assert "Drain cleaning" in services and "Invented service" not in services
    assert all(f["quote"] for f in detail["facts"] if f["status"] == "verified")
    v = detail["verifications"][0]
    assert {c["check"] for c in v["checks"]} >= {"serp_name_location", "serp_phone", "candidate_domain_dns"}
    # site built, QA'd in a real browser, deployed and verified
    sites = (await client.get(f"/sites?campaign_id={cid}", headers=h)).json()
    assert len(sites) == 1 and sites[0]["state"] == "deployed" and sites[0]["qa_status"] == "passed"
    assert sites[0]["qa_runner"] == "local-subprocess" and sites[0]["preview_url"].endswith(".pages.dev")
    sd = (await client.get(f"/sites/{sites[0]['id']}", headers=h)).json()
    assert sd["deployments"][0]["status"] == "verified" and sd["deployments"][0]["provider_deployment_id"]
    html = (await client.get(f"/sites/{sites[0]['id']}/artifact/{sd['versions'][0]['version']}/dist/index.html", headers=h)).text
    assert "Bayou City Plumbing Co" in html and "Not affiliated" in html and "noindex" in html
    for banned in ("testimonial", "award", "5 stars", "Invented service"):
        assert banned not in html
    # outreach draft from verified evidence, nothing sent
    drafts = (await client.get("/outreach/drafts", headers=h)).json()
    assert len(drafts) == 1 and drafts[0]["state"] == "draft" and drafts[0]["preview_url"] == sites[0]["preview_url"]
    assert web.sent == []
    # costs recorded (estimated) per provider, campaign and site
    usage = (await client.get("/usage", headers=h)).json()
    prov = {u["provider"]: u for u in usage["summary"]}
    assert prov["serp_api"]["jarvis"]["committed_units"] == 2 and prov["anthropic"]["jarvis"]["committed_micros"] > 0
    assert prov["anthropic"]["jarvis"]["estimated_micros"] >= prov["anthropic"]["jarvis"]["committed_micros"]  # worst case kept
    assert prov["google_places"]["jarvis"]["calls"] == 0 and prov["google_places"]["provider_reported"]["status"] in ("never_synced", "unavailable", "not_configured")
    assert prov["serp_api"]["jarvis"]["committed_micros"] > 0
    assert any(c["micros"] > 0 for c in usage["per_campaign"]) and usage["per_site"][0]["cost_micros"] > 0
    ov = (await client.get("/overview", headers=h)).json()
    assert ov["previews_deployed"] == 1 and ov["prospects_no_website"] == 1


async def test_worker_crash_recovery(client, deps, web):
    """A task whose worker dies is reclaimed after lease expiry and completes (at-least-once)."""
    setup_web(web)
    a = await login(client, "crash@t.dev")
    r = await client.post("/campaigns/import", headers=a["headers"], json={
        "niche": "plumbing", "location": {"city": "Houston", "state": "TX"}, "rights_attestation": True,
        "website_filter": ["NO_WEBSITE"], "rows": [{"business_name": "Bayou City Plumbing Co", "phone": "(713) 555-0142"}]})
    cid = r.json()["campaign_id"]
    from jarvis.db import queue
    t = await queue.claim(deps.db, "dead-worker", 30, ["campaign.start"])   # worker claims then "crashes"
    assert t is not None
    await deps.db.execute("update tasks set lease_expires_at = now() - interval '1 second' where id=$1", t.id)
    assert await run_until(deps, cid) == "completed"
    st = await deps.db.fetch("select status, attempts from tasks where id=$1", t.id)
    assert st[0]["status"] == "succeeded" and st[0]["attempts"] == 2


async def test_t14_no_google_content_persisted(client, deps, web):
    """Places discovery with an approved gate: names, phones, websites and ratings returned by Places must not be stored."""
    from datetime import UTC, datetime, timedelta

    from jarvis.core.compliance import INTENDED_USE
    a = await login(client, "places@t.dev")
    h = a["headers"]
    now = datetime.now(UTC)
    await client.post("/compliance/approvals", headers=h, json={"scope": "google_places_prospect_selection", "approver_type": "legal_counsel",
                      "approver_name": "Test Counsel", "document_ref": "TEST-ONLY", "approved_at": now.isoformat(),
                      "review_by": (now + timedelta(days=30)).isoformat(),
                      "intended_use_text": INTENDED_USE["google_places_prospect_selection"][1]})
    web.places_pages.append({"places": [
        {"id": "ChIJzzA", "displayName": {"text": "Zyxwvut Plumbing"}, "businessStatus": "OPERATIONAL", "rating": 4.7,
         "nationalPhoneNumber": "(281) 555-0987", "websiteUri": "https://zyxwvut-plumbing.example"},
        {"id": "ChIJzzB", "displayName": {"text": "Qwertyu Rooter"}, "businessStatus": "OPERATIONAL"},          # unrated -> excluded
        {"id": "ChIJzzC", "displayName": {"text": "Lowrated Pipes"}, "businessStatus": "OPERATIONAL", "rating": 3.0},
        {"id": "ChIJzzD", "displayName": {"text": "Closedco"}, "businessStatus": "CLOSED_PERMANENTLY", "rating": 4.9}]})
    r = await client.post("/commands", headers=h, json={"text": "Find 3 plumbers in Houston with a minimum Google rating of 3.5"})
    cmd = r.json()
    r = await client.post(f"/commands/{cmd['id']}/answer", headers=h, json={"answers": {"missing_rating_policy": "exclude"}})
    r = await client.post(f"/commands/{cmd['id']}/confirm", headers=h, json={})
    assert r.status_code == 200, r.text
    cid = r.json()["campaign_id"]
    await run_until(deps, cid)
    req = web.places_requests[0]
    assert req["body"]["minRating"] == 3.5 and "places.rating" in req["mask"] and "places.attributions" in req["mask"]
    rows = await deps.db.fetch("select place_id, state from prospects where campaign_id=$1", cid)
    assert {r["place_id"] for r in rows} == {"ChIJzzA"}          # unrated, low-rated and closed never persisted
    dump = subprocess.run(["pg_dump", "--data-only", deps.settings.database_url], capture_output=True, text=True, check=True).stdout
    import re
    for secret in ("Zyxwvut", "Qwertyu", "Lowrated", "Closedco", "555-0987", "2815550987", "zyxwvut-plumbing"):
        assert secret not in dump, f"Google-derived value persisted: {secret}"
    assert not re.search(r"(?<![\d.:])4\.7(?![\d])", dump), "Google rating value persisted"
    ev = await deps.db.fetch("select data from task_events where campaign_id=$1", cid)
    assert any((e["data"] or {}).get("unrated_dropped") == 1 for e in ev)


async def test_places_not_configured_blocks_campaign(client, deps, web):
    from datetime import UTC, datetime, timedelta

    from jarvis.core.compliance import INTENDED_USE
    a = await login(client, "nocred@t.dev")
    h = a["headers"]
    now = datetime.now(UTC)
    await client.post("/compliance/approvals", headers=h, json={"scope": "google_places_prospect_selection", "approver_type": "google",
                      "approver_name": "Google", "document_ref": "CASE-1", "approved_at": now.isoformat(),
                      "review_by": (now + timedelta(days=30)).isoformat(), "intended_use_text": INTENDED_USE["google_places_prospect_selection"][1]})
    deps.places.settings = deps.settings.model_copy(update={"google_places_api_key": None})
    r = await client.post("/commands", headers=h, json={"text": "Find 5 electricians in Austin"})
    r = await client.post(f"/commands/{r.json()['id']}/confirm", headers=h, json={})
    cid = r.json()["campaign_id"]
    assert await run_until(deps, cid) == "blocked"
    reason = await deps.db.fetchval("select blocked_reason from campaigns where id=$1", cid)
    assert reason.startswith("NOT_CONFIGURED") and "GOOGLE_PLACES_API_KEY" in reason
    _ = json


async def test_missing_serpapi_blocks_campaign_honestly(client, deps, web):
    """T8-adjacent: verification can't complete without SerpApi -> campaign BLOCKED with the exact reason (not 'completed')."""
    a = await login(client, "noserp@t.dev")
    deps.serp.settings = deps.settings.model_copy(update={"serpapi_api_key": None})
    r = await client.post("/campaigns/import", headers=a["headers"], json={
        "niche": "plumbing", "location": {"city": "Houston", "state": "TX"}, "rights_attestation": True,
        "website_filter": ["NO_WEBSITE"], "rows": [{"business_name": "Bayou City Plumbing Co", "phone": "(713) 555-0142"}]})
    cid = r.json()["campaign_id"]
    assert await run_until(deps, cid) == "blocked"
    reason = await deps.db.fetchval("select blocked_reason from campaigns where id=$1", cid)
    assert reason.startswith("NOT_CONFIGURED") and "SERPAPI_API_KEY" in reason
    assert await deps.db.fetchval("select count(*) from website_verifications w join prospects p on p.id=w.prospect_id where p.campaign_id=$1", cid) == 0
