"""Task lifecycle through the real worker: honest cancellation (CANCEL_REQUESTED -> CANCELLED), pause/resume, the work
API (status, current operation, steps, real timings), switching from a search to a website build, site actions and
ownership. Providers are SIMULATED (tests/doubles.py)."""
import asyncio
import time
from uuid import UUID

import pytest

from jarvis.agents import objective as obj
from tests.conftest import login
from tests.doubles import Site
from tests.test_objective_and_serp_discovery import no_website_business, vet
from tests.test_workflow_e2e import run_until


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    import jarvis.db.queue as q
    import jarvis.worker.main as wm
    monkeypatch.setattr(q, "backoff_seconds", lambda *a, **k: 0.0)
    monkeypatch.setattr(wm, "CANCEL_POLL_S", 0.2)


async def start(client, deps, email, o):
    from jarvis.api.auth import Principal
    from jarvis.api.routes.campaigns import _create_campaign
    a = await login(client, email)
    me = Principal(UUID(a["me"]["user_id"]), None, UUID(a["me"]["workspace_id"]), "owner")
    cid = (await _create_campaign(deps, me, obj.resolve(o)["parameters"], command_id=None, budget_cap=None, per_site=None,
                                  objective=o))["campaign_id"]
    return a, me, cid


def vets(web, n=3, city="Austin", st="TX", state_name="Texas"):
    web.dns.add("www.bbb.org")
    web.sites["https://www.bbb.org/robots.txt"] = Site(404, "")
    rows = []
    for i in range(n):
        name, ph = f"Lifecycle Vet {chr(65 + i)}", f"(512) 555-02{i:02d}"
        no_website_business(web, name, ph, city, st, f"life-{i}")
        rows.append(vet(name, ph, f"{i + 1} Main St, {city}, {st} 7870{i}", pid=f"life{i}"))
    web.maps[f"{city}, {state_name}"] = rows


class RunningWorker:
    def __init__(self, deps):
        from jarvis.worker.main import Worker
        self.deps, self.w = deps, Worker(deps, worker_id="lifecycle", concurrency=3, poll_s=0.05, grace_s=5)

    async def __aenter__(self):
        self.task = asyncio.create_task(self.w.serve())
        return self

    async def __aexit__(self, *exc):
        self.w.stop.set()
        await self.task
        await self.deps.db.connect()


async def wait_for(cond, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await cond():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_cancel_while_a_step_runs_is_cancel_requested_then_cancelled(client, deps, web, db):
    vets(web, 3)
    slow = asyncio.Event()
    orig = web.fetch

    async def slow_fetch(url, **kw):                     # keep verification busy so a step is running when we cancel
        await slow.wait()
        return await orig(url, **kw)
    deps.fetch = slow_fetch
    a, me, cid = await start(client, deps, "cancel@t.dev", {"task": "find_prospects", "business_category": "veterinary clinic",
                                                             "area": {"scope": "city", "city": "Austin"}, "target_count": 3})
    async with RunningWorker(deps):
        assert await wait_for(lambda: db.fetchval("""select count(*) > 0 from tasks where campaign_id=$1::uuid
                                                     and task_type='discovery.listings' and status='running'""", cid))
        w = (await client.get(f"/work/{cid}", headers=a["headers"])).json()
        assert w["status"] == "RUNNING" and "Searching SerpApi" in w["current_operation"]
        r = (await client.post(f"/work/{cid}/cancel", headers=a["headers"])).json()
        assert r["status"] == "CANCEL_REQUESTED"                       # a step is executing: not claimed as cancelled yet
        w = (await client.get(f"/work/{cid}", headers=a["headers"])).json()
        assert w["status"] == "CANCEL_REQUESTED" and w["current_operation"].startswith("Stopping")
        t0 = time.monotonic()
        assert await wait_for(lambda: db.fetchval("select state::text='cancelled' from campaigns where id=$1::uuid", cid), 10)
        stop_s = time.monotonic() - t0
    print(f"cancel-to-stopped: {stop_s:.2f}s")
    assert stop_s < 5
    assert await db.fetchval("select count(*) from tasks where campaign_id=$1::uuid and status in ('running','leased','pending')", cid) == 0
    w = (await client.get(f"/work/{cid}", headers=a["headers"])).json()
    assert w["status"] == "CANCELLED" and w["current_operation"] == "Cancelled"
    slow.set()


async def test_pause_resume_and_the_work_api_shows_real_steps_and_timings(client, deps, web, db):
    vets(web, 2)
    a, me, cid = await start(client, deps, "pause@t.dev", {"task": "find_prospects", "business_category": "veterinary clinic",
                                                            "area": {"scope": "city", "city": "Austin"}, "target_count": 2})
    r = (await client.post(f"/work/{cid}/pause", headers=a["headers"])).json()
    assert r["status"] == "PAUSED"
    assert (await client.get(f"/work/{cid}", headers=a["headers"])).json()["current_operation"] == "Paused"
    assert (await client.post(f"/work/{cid}/resume", headers=a["headers"])).json()["status"] == "RUNNING"
    assert await run_until(deps, cid) == "completed"
    w = (await client.get(f"/work/{cid}", headers=a["headers"])).json()
    assert w["status"] == "COMPLETED" and w["completed_at"] and w["elapsed_s"] > 0
    assert [s["label"] for s in w["steps"]][:4] == ["Find businesses", "Verify identity and websites", "Research", "Qualify"]
    assert all(s["state"] == "done" for s in w["steps"][:4]) and w["remaining_steps"] == []
    t = w["timings"]
    assert {s["task"] for s in t["stages"]} >= {"discovery.listings", "research.prospect", "qualify.campaign"}
    assert all(s["wall_s"] is not None and s["queue_wait_s"] is not None for s in t["stages"])
    serp = [p for p in t["providers"] if p["provider"] == "serp_api"]
    assert serp and serp[0]["calls"] >= 1 and serp[0]["avg_ms"] is not None       # measured by the gateway
    assert w["activity"] and isinstance(w["activity"][0]["message"], str)


async def test_switch_from_search_to_building_a_website(client, deps, web, db):
    vets(web, 2)
    a, me, cid = await start(client, deps, "switch@t.dev", {"task": "find_prospects", "business_category": "veterinary clinic",
                                                             "area": {"scope": "city", "city": "Austin"}, "target_count": 2})
    assert await run_until(deps, cid) == "completed"
    r = await client.post("/work/website-build", headers=a["headers"], json={"from_task_id": cid, "count": 1})
    assert r.status_code == 200, r.text
    bid = r.json()["task_id"]
    assert await run_until(deps, bid, timeout=240) == "completed"
    w = (await client.get(f"/work/{bid}", headers=a["headers"])).json()
    assert w["kind"] == "website_build" and w["parent_task_id"] == cid
    sites = (await client.get(f"/sites?campaign_id={bid}", headers=a["headers"])).json()
    assert len(sites) == 1 and sites[0]["state"] == "deployed" and sites[0]["preview_url"].endswith(".pages.dev")
    # the same prospect is not built twice concurrently; asking again while nothing is building creates a fresh task
    sid = sites[0]["id"]
    r = await client.post(f"/sites/{sid}/qa", headers=a["headers"])
    assert r.status_code == 200 and r.json()["queued"] == "qa"
    assert (await client.post(f"/sites/{sid}/qa", headers=a["headers"])).status_code == 409    # already queued
    from jarvis.worker.main import Worker
    w2 = Worker(deps, worker_id="qa-again", concurrency=1, poll_s=0.05, grace_s=5)
    srv = asyncio.create_task(w2.serve())
    assert await wait_for(lambda: db.fetchval("""select count(*) = 2 from qa_runs q join site_versions v on v.id=q.site_version_id
                                                 where v.site_id=$1::uuid and q.status in ('passed','failed')""", sid), 60)
    w2.stop.set()
    await srv
    await deps.db.connect()


async def test_conversation_can_stop_a_search_and_start_building(client, deps, web, db, fake_claude):
    """Text (and voice, same router): 'Stop searching and start building a website.'"""
    from pydantic import SecretStr

    from tests.test_conversation_agent import SECRET, Script, msg
    s = deps.settings.model_copy(update={"jarvis_conversation_agent": "auto", "voice_agent_shared_secret": SecretStr(SECRET)})
    deps.settings = client.app.state.settings = client.app.state.base_settings = s
    agent = Script(fake_claude)
    vets(web, 2)
    a, me, cid = await start(client, deps, "talkswitch@t.dev", {"task": "find_prospects", "business_category": "veterinary clinic",
                                                                 "area": {"scope": "city", "city": "Austin"}, "target_count": 2})
    assert await run_until(deps, cid) == "completed"
    # a second search is running; the owner switches
    vets(web, 2, "Denver", "CO", "Colorado")
    a2 = a
    from jarvis.api.routes.campaigns import _create_campaign
    o = {"task": "find_prospects", "business_category": "veterinary clinic", "area": {"scope": "city", "city": "Denver"},
         "target_count": 5}
    cid2 = (await _create_campaign(deps, me, obj.resolve(o)["parameters"], command_id=None, budget_cap=None, per_site=None,
                                   objective=o))["campaign_id"]
    await db.execute("""insert into conversation_state(workspace_id, user_id, focus) values ($1,$2,$3)
                        on conflict (workspace_id, user_id) do update set focus=excluded.focus""",
                     me.workspace_id, me.user_id, {"campaign_id": cid2})
    agent.queue += [msg(tools=[("build_websites", {"count": 1, "stop_current_search": True})]),
                    msg("Stopped the Denver search. Building a preview for Lifecycle Vet A now.")]
    r = (await client.post("/conversation/turn", headers=a2["headers"], json={"text": "Stop searching and start building a website."})).json()
    res = agent.tool_results()[0]
    assert res["started"] is True and res["previous_search"]["task_id"] == cid2
    assert res["previous_search"]["status"] in ("CANCELLED", "CANCEL_REQUESTED")
    assert await db.fetchval("select state::text from campaigns where id=$1::uuid", cid2) in ("cancelled", "cancel_requested")
    assert await db.fetchval("select work_kind from campaigns where id=$1::uuid", res["task_id"]) == "website_build"
    assert r["campaign_id"] == res["task_id"] and res["from_results_of"] == cid          # the earlier search's results


async def test_other_users_tasks_are_not_visible_or_controllable(client, deps, web):
    vets(web, 1)
    a, me, cid = await start(client, deps, "owner1@t.dev", {"task": "find_prospects", "business_category": "veterinary clinic",
                                                             "area": {"scope": "city", "city": "Austin"}, "target_count": 1})
    b = await login(client, "intruder@t.dev")
    assert (await client.get(f"/work/{cid}", headers=b["headers"])).status_code == 404
    assert (await client.post(f"/work/{cid}/cancel", headers=b["headers"])).status_code == 404
    assert cid not in [w["id"] for w in (await client.get("/work", headers=b["headers"])).json()]
