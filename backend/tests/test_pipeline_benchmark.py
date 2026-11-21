"""Pipeline timing benchmark (SIMULATED providers with a fixed, identical latency on every call).

Runs one SerpApi discovery campaign (8 businesses, discovery + verification + research + qualification) through the real
worker, queue, verifier, researcher and scorer. Every provider HTTP call waits PROVIDER_LATENCY_S and every website fetch
waits FETCH_LATENCY_S, so before/after numbers are comparable. It is NOT a measurement of the live providers.

    JARVIS_BENCH=1 .venv/bin/pytest -q -s tests/test_pipeline_benchmark.py
"""
import asyncio
import os
import time

import httpx
import pytest

from jarvis.agents import objective as obj
from tests.conftest import login
from tests.doubles import Site
from tests.test_objective_and_serp_discovery import no_website_business, vet
from tests.test_workflow_e2e import run_until

pytestmark = pytest.mark.skipif(os.environ.get("JARVIS_BENCH") != "1", reason="benchmark: set JARVIS_BENCH=1")
PROVIDER_LATENCY_S = 0.15
FETCH_LATENCY_S = 0.10
N = 8


def slow_web(web, deps):
    base = web.transport()

    async def handler(req):
        await asyncio.sleep(PROVIDER_LATENCY_S)
        return base.handler(req)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    for c in (deps.serp, deps.firecrawl, deps.cloudflare):
        c.http = http
    orig_fetch = web.fetch

    async def fetch(url, **kw):
        await asyncio.sleep(FETCH_LATENCY_S)
        return await orig_fetch(url, **kw)
    deps.fetch = fetch


async def test_benchmark_discovery_to_qualification(client, deps, web, db, monkeypatch):
    import jarvis.db.queue as q
    monkeypatch.setattr(q, "backoff_seconds", lambda *a, **k: 0.0)
    web.dns.add("www.bbb.org")
    web.sites["https://www.bbb.org/robots.txt"] = Site(404, "")
    listings = []
    for i in range(N):
        name, phone = f"Bench Animal Clinic {chr(65 + i)}", f"(512) 555-01{i:02d}"
        no_website_business(web, name, phone, "Austin", "TX", f"bench-{i}")
        listings.append(vet(name, phone, f"{i + 1} Congress Ave, Austin, TX 78701", pid=f"bench{i}"))
    web.maps["Austin, Texas"] = listings
    slow_web(web, deps)
    a = await login(client, "bench@t.dev")
    from uuid import UUID

    from jarvis.api.auth import Principal
    from jarvis.api.routes.campaigns import _create_campaign
    me = Principal(UUID(a["me"]["user_id"]), None, UUID(a["me"]["workspace_id"]), "owner")
    o = {"task": "find_prospects", "business_category": "veterinary clinic", "area": {"scope": "city", "city": "Austin"},
         "target_count": N}
    t0 = time.monotonic()
    cid = (await _create_campaign(deps, me, obj.resolve(o)["parameters"], command_id=None, budget_cap=None, per_site=None,
                                  objective=o))["campaign_id"]
    final = await run_until(deps, cid, timeout=600)
    total = time.monotonic() - t0
    rows = await db.fetch("""select task_type, count(*) n, sum(extract(epoch from finished_at - started_at)) busy,
                                    min(started_at) first_start, max(finished_at) last_end
                             from tasks where campaign_id=$1::uuid group by task_type order by min(started_at)""", cid)
    print(f"\nBENCH final={final} total_s={total:.2f} prospects={N}")
    for r in rows:
        span = (r["last_end"] - r["first_start"]).total_seconds()
        print(f"BENCH stage={r['task_type']:20s} tasks={r['n']:3d} wall_s={span:6.2f} busy_s={float(r['busy']):6.2f}")
    assert final == "completed"
