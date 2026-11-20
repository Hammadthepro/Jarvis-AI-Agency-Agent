"""Objective rules, provider-specific policy, and the complete SerpApi workflow through the real worker.

Provider HTTP (SerpApi, Firecrawl, Cloudflare, Claude) is served by test doubles (tests/doubles.py); the worker, queue,
gateway, agents, renderer, Playwright browser QA and database are production code. These are SIMULATED-provider tests;
the live equivalents are in scripts/verify-live.sh."""
from decimal import Decimal

from jarvis.agents import objective as obj
from tests.conftest import login
from tests.doubles import Site
from tests.test_workflow_e2e import run_until


# ---------------------------------------------------------------- objective rules (pure)
def test_defaults_are_applied_instead_of_questions():
    r = obj.resolve({"task": "find_and_build_previews", "business_category": "Veterinary clinic",
                     "area": {"scope": "nationwide"}, "minimum_rating": 3.5})
    p = r["parameters"]
    assert r["missing"] is None and p.target_count == 10 and p.sites_to_build == 3
    assert p.location.nationwide and p.discovery_provider == "serp_api" and p.website_filter == ["ANY"]
    assert p.minimum_google_rating == Decimal("3.5") and p.missing_rating_policy == "include_for_review"
    assert "outreach_draft" not in p.stages and "deploy" in p.stages
    assert any("10 businesses" in d for d in r["defaults_applied"])
    assert "across the US rated at least 3.5" in r["summary"] and "3 tailored preview websites" in r["summary"]


def test_only_one_essential_question_at_a_time():
    assert obj.resolve({})["missing"]["field"] == "business_category"
    assert obj.resolve({"business_category": "dentist"})["missing"]["question"].startswith("Where should I look")
    assert obj.resolve({"business_category": "dentist", "area": {"scope": "city", "city": "Springfield"}})["missing"]["question"] \
        == "Which state is Springfield in?"
    ok = obj.resolve({"business_category": "dentist", "area": {"scope": "city", "city": "Denver"}})
    assert ok["missing"] is None and ok["parameters"].location.state == "CO"           # well-known city: no question


def test_merge_keeps_earlier_details_and_original_request():
    o = obj.merge({}, {"original_request": "Find customers and build their websites", "task": "find_and_build_previews"})
    o = obj.merge(o, {"business_category": "veterinary clinic"})
    o = obj.merge(o, {"area": {"scope": "nationwide"}, "minimum_rating": 3.5, "original_request": "Veterinary businesses."})
    assert o["original_request"] == "Find customers and build their websites" and o["task"] == "find_and_build_previews"
    assert o["business_category"] == "veterinary clinic" and o["area"] == {"scope": "nationwide"}
    o2 = obj.merge({**o, "status": "proposed", "campaign_id": "x"}, {"target_count": 20})
    assert o2["status"] == "gathering" and "campaign_id" not in o2 and o2["minimum_rating"] == 3.5
    fresh = obj.merge(o, {"new_objective": True, "business_category": "roofing"})
    assert fresh.get("minimum_rating") is None and fresh["business_category"] == "roofing"


def test_rating_is_never_loosened_and_only_options_exclude():
    r = obj.resolve({"business_category": "dentist", "area": {"scope": "state", "state": "TX"}, "minimum_rating": 3.7,
                     "website_preference": "no_website_only", "task": "find_prospects"})
    assert r["parameters"].minimum_google_rating == Decimal("4.0") and r["parameters"].website_filter == ["NO_WEBSITE"]
    assert r["parameters"].sites_to_build == 0 and "build" not in r["parameters"].stages


# ---------------------------------------------------------------- provider policy
async def test_provider_policy_is_decided_per_provider(deps, db):
    from jarvis.core import provider_policy
    from jarvis.core.integrations import DISABLED
    from tests.test_core_db import new_ws
    ws, _uid = await new_ws(db)
    async with db.acquire() as conn:
        places = await provider_policy.evaluate(conn, deps, ws, "google_places", "prospect_discovery")
        serp = await provider_policy.evaluate(conn, deps, ws, "serp_api", "prospect_discovery")
        fc = await provider_policy.evaluate(conn, deps, ws, "firecrawl", "website_retrieval")
        lic = await provider_policy.evaluate(conn, deps, ws, "licensed_business_data", "prospect_discovery")
        assert places["allowed"] is False and places["restricted"] and "Google Places API (New)" in places["reason"]
        assert serp["allowed"] is True and fc["allowed"] is True and lic["allowed"] is False
        DISABLED.add("serp_api")
        try:
            off = await provider_policy.evaluate(conn, deps, ws, "serp_api", "prospect_discovery")
        finally:
            DISABLED.discard("serp_api")
        assert off["allowed"] is False and "disabled in Integrations" in off["reason"]
        assert (await provider_policy.evaluate(conn, deps, ws, "google_places", "prospect_discovery"))["allowed"] is False


# ---------------------------------------------------------------- the complete workflow through the real worker
def vet(title, phone, address, rating=4.6, reviews=40, website=None, open_state="Open · Closes 6 PM", pid=None):
    return {"title": title, "place_id": pid or title.replace(" ", "")[:20], "phone": phone, "address": address, "rating": rating,
            "reviews": reviews, "website": website, "type": "Veterinarian", "open_state": open_state}


def no_website_business(web, name, phone, city, st, slug):
    """Independent evidence for a business without a website: a BBB profile found by name and by phone."""
    bbb = f"https://www.bbb.org/us/{st.lower()}/{city.lower().replace(' ', '-')}/profile/veterinarian/{slug}-0001"
    web.serp[f'"{name}"'] = {"organic_results": [
        {"position": 1, "title": f"{name} - Yelp", "link": f"https://www.yelp.com/biz/{slug}"},
        {"position": 2, "title": f"{name} | BBB", "link": bbb, "snippet": f"Veterinarian in {city}"}]}
    web.serp[f'"{phone}"'] = {"organic_results": [{"position": 1, "title": f"{name} | BBB", "link": bbb}]}
    web.sites[bbb] = Site(200, f"<html><title>{name} | BBB</title><body><h1>{name}</h1><p>Phone: {phone}</p>"
                               f"<p>{city}, {st}</p></body></html>")
    web.firecrawl_pages[bbb] = (f"# {name}\nPhone: {phone}\n\n- Wellness exams\n- Vaccinations\n- Dental cleaning\n\n"
                                f"Serving {city} and nearby.")


async def test_nationwide_veterinary_objective_runs_to_verified_previews(client, deps, web, db):
    web.dns.add("www.bbb.org")
    web.sites["https://www.bbb.org/robots.txt"] = Site(404, "")
    no_website_business(web, "Happy Paws Animal Clinic", "(212) 555-0101", "New York", "NY", "happy-paws")
    no_website_business(web, "Sunset Pet Hospital", "(323) 555-0102", "Los Angeles", "CA", "sunset-pet")
    web.dns.add("lakeshorevet.com")
    web.sites["https://lakeshorevet.com"] = Site(200, '<html><head><meta name="viewport" content="width=device-width">'
                                                 "<title>Lakeshore Vet</title></head><body>Lakeshore Vet (312) 555-0103 "
                                                 "Chicago © 2026</body></html>")
    web.maps["New York, New York"] = [
        vet("Happy Paws Animal Clinic", "(212) 555-0101", "10 W 4th St, New York, NY 10012"),
        vet("Lowrated Vet", "(212) 555-0109", "1 Main St, New York, NY 10001", rating=3.0),               # below 3.5
        vet("Gone Pets", "(212) 555-0108", "2 Main St, New York, NY 10001", open_state="Permanently closed")]
    web.maps["Los Angeles, California"] = [
        vet("Sunset Pet Hospital", "(323) 555-0102", "5 Sunset Blvd, Los Angeles, CA 90028"),
        vet("Newcomer Vet", "(323) 555-0107", "9 Vine St, Los Angeles, CA 90038", rating=None, reviews=None)]  # unrated
    web.maps["Chicago, Illinois"] = [vet("Lakeshore Vet", "(312) 555-0103", "3 Lake St, Chicago, IL 60601",
                                         website="https://lakeshorevet.com")]
    a = await login(client, "vetflow@t.dev")
    from jarvis.agents.command import ALL_STAGES
    o = {"original_request": "Find potential customers for website development. Build their websites and show me.",
         "task": "find_and_build_previews", "business_category": "veterinary clinic", "area": {"scope": "nationwide"},
         "target_count": 4, "minimum_rating": 3.5, "unrated_policy": "include_for_review",
         "website_preference": "prefer_no_or_poor_website", "previews_to_build": 2}
    p = obj.resolve(o)["parameters"]
    from jarvis.api.auth import Principal
    from jarvis.api.routes.campaigns import _create_campaign
    me = Principal(__import__("uuid").UUID(a["me"]["user_id"]), None, __import__("uuid").UUID(a["me"]["workspace_id"]), "owner")
    out = await _create_campaign(deps, me, p, command_id=None, budget_cap=None, per_site=None, objective=o)
    cid = out["campaign_id"]
    from jarvis.api.routes.conversation import campaign_progress
    assert (await campaign_progress(db, me.workspace_id, __import__("uuid").UUID(cid)))["say"].startswith("It's queued")

    final = await run_until(deps, cid, timeout=240)
    assert final == "completed", await db.fetchval("select blocked_reason from campaigns where id=$1::uuid", cid)
    assert {r["location"][:8] for r in web.maps_requests} >= {"New York", "Los Ange", "Chicago,"}
    assert all(r["engine"] == "google_maps" and r["z"] == "11" for r in web.maps_requests)
    assert not [c for c in web.calls if "places.googleapis.com" in c[1]]                # Places never used

    pros = {p["business_name"]: p for p in (await client.get(f"/prospects?campaign_id={cid}", headers=a["headers"])).json()}
    assert set(pros) == {"Happy Paws Animal Clinic", "Sunset Pet Hospital", "Newcomer Vet", "Lakeshore Vet"}
    assert pros["Happy Paws Animal Clinic"]["website_status"] == "VERIFIED_NO_WEBSITE" and pros["Happy Paws Animal Clinic"]["state"] == "qualified"
    assert pros["Lakeshore Vet"]["site_health"] == "working" and pros["Lakeshore Vet"]["state"] != "qualified"
    assert pros["Newcomer Vet"]["review_reason"] == "discovery_filter_unconfirmed"     # unrated: kept for review
    rating = await db.fetchval("""select f.value::text from research_facts f join prospects p on p.id=f.prospect_id
                                  where p.campaign_id=$1::uuid and f.fact_key='listing_rating' and p.external_ref like 'serp:Happy%'""", cid)
    assert rating == "4.6"                                                             # SerpApi listing data is stored with evidence

    sites = (await client.get(f"/sites?campaign_id={cid}", headers=a["headers"])).json()
    built = {s["prospect_id"] for s in sites}
    assert built == {pros["Happy Paws Animal Clinic"]["id"], pros["Sunset Pet Hospital"]["id"]}   # unrated one not built
    assert all(s["state"] == "deployed" and s["qa_status"] == "passed" and s["preview_url"].endswith(".pages.dev") for s in sites)
    assert web.sent == []                                                              # no outreach

    prog = await campaign_progress(db, me.workspace_id, __import__("uuid").UUID(cid))
    assert prog["say"].startswith("Done. 4 businesses verified, 3 qualified, and 2 preview websites are ready.")
    assert "1 business kept for your review" in prog["say"]
    # proactive updates in the owner's conversation, from real state changes only (no duplicates)
    updates = [r["content"] for r in await db.fetch(
        """select content from conversation_messages where workspace_id=$1 and meta->>'kind'='progress'
           and meta->>'campaign_id'=$2 order by id""", me.workspace_id, cid)]
    print("progress updates:", updates)
    assert updates[-1] == prog["say"] and len(updates) == len(set(updates))
    assert updates[0] == "I'm searching SerpApi for 4 veterinary clinic businesses now."
    assert any("Building 2 preview websites" in u for u in updates)
    # what the conversation shows the owner: each preview with its business
    from tests.test_conversation_agent import SECRET  # noqa: F401  (same fixtures module)
    rows = await db.fetch("""select d.url, (select f.value #>> '{}' from research_facts f where f.prospect_id=g.prospect_id
                                    and f.fact_key='business_name' limit 1) business
                             from deployments d join site_versions v on v.id=d.site_version_id join generated_sites g on g.id=v.site_id
                             where g.campaign_id=$1::uuid and d.status='verified'""", cid)
    assert {r["business"] for r in rows} == {"Happy Paws Animal Clinic", "Sunset Pet Hospital"}
    _ = ALL_STAGES


async def test_owner_exclusions_are_enforced_in_discovery(client, deps, web, db):
    web.dns.add("www.bbb.org")
    web.sites["https://www.bbb.org/robots.txt"] = Site(404, "")
    no_website_business(web, "Austin Paws", "(512) 555-0111", "Austin", "TX", "austin-paws")
    web.maps["Austin, Texas"] = [vet("Banfield Pet Hospital", "(512) 555-0110", "1 Congress Ave, Austin, TX 78701"),
                                 vet("Austin Paws", "(512) 555-0111", "2 Congress Ave, Austin, TX 78701")]
    a = await login(client, "excl@t.dev")
    o = {"business_category": "veterinary clinic", "area": {"scope": "city", "city": "Austin"}, "target_count": 2,
         "task": "find_prospects", "exclusions": ["Banfield"]}
    from uuid import UUID

    from jarvis.api.auth import Principal
    from jarvis.api.routes.campaigns import _create_campaign
    me = Principal(UUID(a["me"]["user_id"]), None, UUID(a["me"]["workspace_id"]), "owner")
    cid = (await _create_campaign(deps, me, obj.resolve(o)["parameters"], command_id=None, budget_cap=None, per_site=None,
                                  objective=o))["campaign_id"]
    assert await run_until(deps, cid) == "completed"
    names = {p["business_name"] for p in (await client.get(f"/prospects?campaign_id={cid}", headers=a["headers"])).json()}
    assert names == {"Austin Paws"}
