"""Shared Postgres rate limiting: atomic under concurrency and enforced across separate API instances
and separate worker database pools (simulating multiple processes)."""
import asyncio

import httpx

from jarvis.api.app import create_app
from jarvis.core.errors import IntegrationState, ProviderError
from jarvis.core.ratelimit import PgRateLimiter
from jarvis.db.pool import Database
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call
from tests.test_core_db import new_ws


async def test_concurrent_takes_never_exceed_capacity(db, dsn):
    pools = [Database(dsn, pool_size=10) for _ in range(3)]       # three independent "processes"
    for p in pools:
        await p.connect()
    try:
        lims = [PgRateLimiter(p) for p in pools]
        res = await asyncio.gather(*[lims[i % 3].take("t:conc", capacity=10, refill_per_s=0.0001) for i in range(90)])
        assert sum(r.allowed for r in res) == 10
        denied = next(r for r in res if not r.allowed)
        assert denied.retry_after_s > 0
    finally:
        for p in pools:
            await p.close()


async def test_refill_over_time(db):
    lim = PgRateLimiter(db)
    assert (await lim.take("t:refill", capacity=1, refill_per_s=20)).allowed
    assert not (await lim.take("t:refill", capacity=1, refill_per_s=20)).allowed
    await asyncio.sleep(0.08)
    assert (await lim.take("t:refill", capacity=1, refill_per_s=20)).allowed


async def test_api_limit_shared_across_two_api_instances(db, dsn, deps):
    """Two separately created API apps (own DB pools) share one per-user bucket."""
    settings = deps.settings
    apps = []
    for _ in range(2):
        d = Database(dsn, pool_size=5)
        await d.connect()
        a = create_app(settings, deps.__class__(**{**deps.__dict__, "db": d}))
        a.state.limiter.limits = {"ip": {"capacity": 1000, "per_seconds": 60}, "user": {"capacity": 6, "per_seconds": 3600},
                                  "commands": {"capacity": 100, "per_seconds": 60}}
        apps.append((a, d))
    clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://t") for a, _ in apps]
    try:
        tok = (await clients[0].post("/auth/dev-token", json={"email": "rl@t.dev"})).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        codes = await asyncio.gather(*[clients[i % 2].get("/campaigns", headers=h) for i in range(14)])
        statuses = [c.status_code for c in codes]
        assert statuses.count(200) == 6 and statuses.count(429) == 8
        assert codes[statuses.index(429)].json()["error"]["code"] == "RATE_LIMITED"
    finally:
        for c in clients:
            await c.aclose()
        for _, d in apps:
            await d.close()


async def test_provider_limit_shared_across_workers_and_blocks_before_reservation(db, dsn):
    ws, _ = await new_ws(db)
    await db.execute("""insert into rate_limit_buckets(bucket_key, tokens, capacity, refill_per_s)
                        values ('provider:serp_api', 3, 3, 0.0001) on conflict (bucket_key) do update set tokens = 3""")
    pools = [Database(dsn, pool_size=5) for _ in range(2)]
    for p in pools:
        await p.connect()
    sent = []

    async def call(pool):
        async def fn():
            sent.append(1)
            return CallOutcome(None, 1, 25000)
        try:
            await gateway_call(CallContext(pool, ws), provider="serp_api", operation="search", unit="search", units=1,
                               estimated_micros=25000, fn=fn)
            return "ok"
        except ProviderError as exc:
            assert exc.state == IntegrationState.RATE_LIMITED and exc.retryable
            return "limited"
    try:
        out = await asyncio.gather(*[call(pools[i % 2]) for i in range(8)])
    finally:
        for p in pools:
            await p.close()
    assert out.count("ok") == 3 and len(sent) == 3
    assert await db.fetchval("select count(*) from cost_reservations where workspace_id=$1", ws) == 3
