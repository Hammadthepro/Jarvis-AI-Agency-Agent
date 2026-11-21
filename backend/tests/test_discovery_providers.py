"""Discovery providers through the real worker: Geoapify, SerpApi + Geoapify with cross-provider de-duplication, and
provenance. Provider HTTP is SIMULATED (tests/doubles.py); everything else is production code."""
from uuid import UUID

import pytest

from jarvis.agents import objective as obj
from jarvis.core.errors import ValidationFailed
from tests.conftest import login
from tests.doubles import Site
from tests.test_objective_and_serp_discovery import no_website_business, vet
from tests.test_workflow_e2e import run_until


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    import jarvis.db.queue as q
    monkeypatch.setattr(q, "backoff_seconds", lambda *a, **k: 0.0)


def geo(pid, name, phone=None, website=None, postcode="92805", street="Main St", n="1"):
    props = {"place_id": pid, "name": name, "formatted": f"{n} {street}, Anaheim, CA {postcode}, United States",
             "housenumber": n, "street": street, "city": "Anaheim", "state": "California", "state_code": "CA",
             "postcode": postcode, "categories": ["healthcare", "healthcare.dentist"], "lat": 33.83, "lon": -117.91,
             "datasource": {"sourcename": "openstreetmap", "raw": {k: v for k, v in (("phone", phone), ("website", website)) if v}}}
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [-117.91, 33.83]}}


async def start(client, deps, email, o):
    from jarvis.api.auth import Principal
    from jarvis.api.routes.campaigns import _create_campaign
    a = await login(client, email)
    me = Principal(UUID(a["me"]["user_id"]), None, UUID(a["me"]["workspace_id"]), "owner")
    cid = (await _create_campaign(deps, me, obj.resolve(o)["parameters"], command_id=None, budget_cap=None, per_site=None,
                                  objective=o))["campaign_id"]
    return a, me, cid


def bbb(web):
    web.dns.add("www.bbb.org")
    web.sites["https://www.bbb.org/robots.txt"] = Site(404, "")


async def test_geoapify_discovery_and_a_missing_website_is_only_not_listed(client, deps, web, db):
    bbb(web)
    no_website_business(web, "Harbor Smiles Dental", "(714) 555-0201", "Anaheim", "CA", "harbor")
    web.geo_cities["Anaheim"] = "geo-anaheim"
    web.geo_places["geo-anaheim"] = [geo("g1", "Harbor Smiles Dental", phone="(714) 555-0201")]   # no website in OSM
    a, me, cid = await start(client, deps, "geo@t.dev", {"task": "find_prospects", "business_category": "dentist",
                                                          "area": {"scope": "city", "city": "Anaheim", "state": "CA"},
                                                          "target_count": 1, "discovery_source": "geoapify"})
    assert await run_until(deps, cid) == "completed"
    assert [r["path"] for r in web.geo_requests] == ["/v1/geocode/search", "/v2/places"]
    assert web.geo_requests[1]["categories"] == "healthcare.dentist" and web.geo_requests[1]["filter"] == "place:geo-anaheim"
    wv = await db.fetchrow("""select w.status::text, w.listing_website, w.website_check, w.checks from website_verifications w
                              join prospects p on p.id=w.prospect_id where p.campaign_id=$1::uuid""", cid)
    assert wv["listing_website"] == "not_listed"
    # concluded only after independent checks ran (search by name and phone, candidate domains)
    assert {ch["check"] for ch in wv["checks"]} >= {"serp_name_location", "serp_phone", "candidate_domain_dns"}
    assert wv["status"] == "VERIFIED_NO_WEBSITE" and wv["website_check"] == "NO_WEBSITE_FOUND"
    p = await db.fetchrow("""select p.discovery_provider, k.key phone_key from prospects p join prospect_keys k
                             on k.prospect_id=p.id and k.kind='phone' where p.campaign_id=$1::uuid""", cid)
    assert p["discovery_provider"] == "geoapify" and p["phone_key"] == "7145550201"
    ev = await db.fetchrow("""select source_provider, independence_key from evidence e join prospects p on p.id=e.prospect_id
                              where p.campaign_id=$1::uuid and e.source_ref like 'geoapify:%'""", cid)
    assert ev["source_provider"] == "geoapify" and ev["independence_key"] == "geoapify"


async def test_secondary_provider_is_deduplicated_across_providers(client, deps, web, db):
    bbb(web)
    for name, ph, slug in (("Harbor Smiles Dental", "(714) 555-0201", "harbor"), ("Canyon Family Dentistry", "(714) 555-0202", "canyon"),
                           ("Harbor Smiles Dental Group", "(714) 555-0299", "harbor2")):
        no_website_business(web, name, ph, "Anaheim", "CA", slug)
    web.maps["Anaheim, California"] = [vet("Harbor Smiles Dental", "(714) 555-0201", "1 Main St, Anaheim, CA 92805", pid="s1")]
    web.geo_cities["Anaheim"] = "geo-anaheim"
    web.geo_places["geo-anaheim"] = [
        geo("g1", "Harbor Smiles Dental", phone="+1 714-555-0201"),                       # same business (phone + name)
        geo("g2", "Canyon Family Dentistry", phone="(714) 555-0202", postcode="92806", street="Canyon Rd", n="9"),
        geo("g3", "Harbor Smiles Dental Group", phone="(714) 555-0299", postcode="92807", street="Lincoln Ave", n="50")]
    a, me, cid = await start(client, deps, "dedup@t.dev", {"task": "find_prospects", "business_category": "dentist",
                                                            "area": {"scope": "city", "city": "Anaheim", "state": "CA"},
                                                            "target_count": 3, "discovery_source": "serp_api",
                                                            "secondary_source": "geoapify"})
    assert await run_until(deps, cid) == "completed"
    rows = await db.fetch("""select p.discovery_provider, k.key phone_key from prospects p join prospect_keys k
                             on k.prospect_id=p.id and k.kind='phone' where p.campaign_id=$1::uuid order by p.created_at""", cid)
    assert [(r["discovery_provider"], r["phone_key"]) for r in rows] == [
        ("serp_api", "7145550201"), ("geoapify", "7145550202"), ("geoapify", "7145550299")]   # similar name kept separate
    srcs = await db.fetch("""select distinct e.independence_key from evidence e join prospects p on p.id=e.prospect_id
                             join prospect_keys k on k.prospect_id=p.id and k.kind='phone'
                             where p.campaign_id=$1::uuid and k.key='7145550201' and e.source_type='serp_result'
                             and (e.source_ref like 'serpapi:%' or e.source_ref like 'geoapify:%')""", cid)
    assert {r["independence_key"] for r in srcs} == {"serp_api", "geoapify"}                 # two independent listings
    ev = await db.fetch("select data from task_events where campaign_id=$1::uuid", cid)
    assert any((e["data"] or {}).get("duplicate_cross_provider") == 1 for e in ev)


async def test_geoapify_without_a_category_is_refused_up_front(client, deps):
    with pytest.raises(ValidationFailed, match="Geoapify has no category for 'roofing'"):
        await start(client, deps, "geocat@t.dev", {"task": "find_prospects", "business_category": "roofing",
                                                    "area": {"scope": "city", "city": "Denver"}, "discovery_source": "geoapify"})


async def test_geoapify_not_configured_is_reported(deps, db):
    from jarvis.core import provider_policy
    from tests.test_core_db import new_ws
    ws, _ = await new_ws(db)
    deps.geoapify.settings = deps.settings.model_copy(update={"geoapify_api_key": None})
    async with db.acquire() as conn:
        r = await provider_policy.evaluate(conn, deps, ws, "geoapify", "prospect_discovery")
    assert r["allowed"] is False and r["not_configured"] and "GEOAPIFY_API_KEY" in r["reason"]


def test_dedup_keys_do_not_merge_similar_names():
    from jarvis.discovery.providers import name_key, phone_key, same_business
    assert phone_key("+1 (714) 555-0201") == phone_key("714.555.0201") == "7145550201"
    assert name_key("Harbor Smiles Dental", "92805") != name_key("Harbor Smiles Orthodontics", "92805")
    assert name_key("Harbor Smiles Dental", "92805") != name_key("Harbor Smiles Dental", "92807")     # another branch
    assert same_business("Harbor Smiles Dental", "Harbor Smiles Dental LLC") and not same_business("Acme Dental", "Zen Spa")


async def test_provider_evaluation_compares_on_the_same_plan_and_stores_aggregates_only(client, deps, web, db):
    """Real worker run of the evaluation against SIMULATED providers: Geoapify and SerpApi run; Google Places is BLOCKED
    (no technical-testing approval). No business record is created; the results hold no names, phones or URLs."""
    web.geo_cities["Anaheim"] = "geo-anaheim"
    web.geo_places["geo-anaheim"] = [geo("g1", "Harbor Smiles Dental", phone="(714) 555-0201"),
                                     geo("g2", "Canyon Family Dentistry", website="https://canyondental.example")]
    web.maps["Anaheim, California"] = [vet("Harbor Smiles Dental", "(714) 555-0201", "1 Main St, Anaheim, CA 92805", pid="s1")]
    a = await login(client, "eval@t.dev")
    plan = {"providers": ["google_places", "geoapify", "serp_api"], "cities": [{"city": "Anaheim", "state": "CA"}],
            "categories": ["dentist"], "limit": 20}
    r = await client.post("/discovery/evaluations", headers=a["headers"], json=plan)
    assert r.status_code == 400 and "confirm_cost" in r.text                         # real requests need explicit consent
    r = await client.post("/discovery/evaluations", headers=a["headers"], json={**plan, "confirm_cost": True})
    eid = r.json()["evaluation_id"]
    import asyncio

    from jarvis.worker.main import Worker
    w = Worker(deps, worker_id="eval-worker", concurrency=1, poll_s=0.05, grace_s=5)
    srv = asyncio.create_task(w.serve())
    for _ in range(80):
        st = await db.fetchval("select status from provider_evaluations where id=$1::uuid", eid)
        if st != "running":
            break
        await asyncio.sleep(0.25)
    w.stop.set()
    await srv
    await deps.db.connect()
    res = (await client.get(f"/discovery/evaluations/{eid}", headers=a["headers"])).json()["results"]
    gp, ga, sp = res["providers"]["google_places"], res["providers"]["geoapify"], res["providers"]["serp_api"]
    assert gp["status"] == "BLOCKED" and "approval" in gp["reason"].lower()
    assert ga["status"] == "COMPLETED" and ga["totals"]["returned"] == 2 and ga["totals"]["website_listed_pct"] == 50.0
    assert ga["requests"] == 2 and ga["estimated_cost_usd"] == 0.0                  # 1 geocode + 1 places request
    assert sp["status"] == "COMPLETED" and sp["totals"]["phone_listed_pct"] == 100.0
    assert res["overlap"]["geoapify~serp_api"]["shared_phone_or_domain"] == 1
    blob = str(res)
    assert "Harbor" not in blob and "555-0201" not in blob and "canyondental" not in blob      # aggregates only
    assert await db.fetchval("select count(*) from prospects where workspace_id=$1::uuid", a["me"]["workspace_id"]) == 0


async def test_dedup_keys_are_refused_for_google_places_prospects(db):
    """Places content is never stored: the database itself rejects de-duplication keys for a Places prospect."""
    import asyncpg

    from tests.test_core_db import new_ws
    ws, _ = await new_ws(db)
    cid = await db.fetchval("""insert into campaigns(workspace_id, name, niche, location, target_count, discovery_provider, stages,
                               budget_cap_micros, per_site_cap_micros) values ($1,'x','dentist','{}',1,'google_places','{discovery}',0,0)
                               returning id""", ws)
    await db.execute("insert into place_refs(workspace_id, place_id) values ($1,'ChIJx')", ws)
    pid = await db.fetchval("""insert into prospects(workspace_id, campaign_id, discovery_provider, place_id, business_category)
                               values ($1,$2,'google_places','ChIJx','dentist') returning id""", ws, cid)
    with pytest.raises(asyncpg.RaiseError, match="Google Places content may not be stored"):
        await db.execute("insert into prospect_keys(prospect_id, campaign_id, workspace_id, kind, key) values ($1,$2,$3,'phone','7145550201')",
                         pid, cid, ws)
