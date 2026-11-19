"""Budget reservations, gateway behaviour, queue semantics, recovery, schema guarantees."""
import asyncio
import uuid

import pytest

from jarvis.core import budget
from jarvis.core.errors import BudgetBlocked, IntegrationState, ProviderError
from jarvis.db import queue
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call


async def new_ws(db):
    from jarvis.api.auth import create_workspace
    uid = uuid.uuid4()
    await db.execute("insert into app_users(id, email) values ($1,$2)", uid, f"{uid}@t.dev")
    return await create_workspace(db, uid, "t"), uid


async def test_concurrent_reservations_never_exceed_cap(db, monkeypatch):
    from jarvis.core import ratelimit
    monkeypatch.setitem(ratelimit.provider_rate_limits(), "serp_api", {"capacity": 1000, "per_seconds": 1})  # isolate the budget cap
    ws, _ = await new_ws(db)
    await db.execute("update budget_limits set cap = 10 where workspace_id=$1 and provider='serp_api'", ws)
    calls = []

    async def one(i):
        async def fn():
            calls.append(i)
            return CallOutcome(None, 1, 25000)
        try:
            await gateway_call(CallContext(db, ws), provider="serp_api", operation="search", unit="search", units=1,
                               estimated_micros=25000, fn=fn)
            return True
        except BudgetBlocked:
            return False
    results = await asyncio.gather(*[one(i) for i in range(50)])
    assert sum(results) == 10 and len(calls) == 10   # blocked calls never reached the provider
    used = await db.fetchval("select sum(units) from cost_reservations where workspace_id=$1 and status='committed'", ws)
    assert used == 10


async def test_no_global_limit_fails_closed(db):
    ws, _ = await new_ws(db)
    await db.execute("delete from budget_limits where workspace_id=$1 and scope='global'", ws)
    async with db.transaction() as conn:
        with pytest.raises(BudgetBlocked):
            await budget.reserve(conn, workspace_id=ws, provider="firecrawl", unit="credit", units=1, amount_micros=1)


async def test_ambiguous_timeout_keeps_reservation_and_definite_error_releases(db):
    ws, _ = await new_ws(db)

    async def amb():
        raise ProviderError("firecrawl", IntegrationState.TEMPORARILY_UNAVAILABLE, "read timeout", billing_ambiguous=True)

    async def definite():
        raise ProviderError("firecrawl", IntegrationState.INVALID_CREDENTIALS, "401", http_status=401)
    for fn in (amb, definite):
        with pytest.raises(ProviderError):
            await gateway_call(CallContext(db, ws), provider="firecrawl", operation="scrape", unit="credit", units=1,
                               estimated_micros=3800, fn=fn)
    rows = await db.fetch("select status from cost_reservations where workspace_id=$1 order by created_at", ws)
    assert [r["status"] for r in rows] == ["ambiguous", "released"]
    st = await db.fetchval("select state from provider_integrations where workspace_id=$1 and provider='firecrawl'", ws)
    assert st == "INVALID_CREDENTIALS"


async def test_kill_switch_blocks_before_reservation(db):
    ws, _ = await new_ws(db)
    await db.execute("update system_settings set global_kill_switch=true where workspace_id=$1", ws)

    async def fn():
        raise AssertionError("must not be called")
    with pytest.raises(ProviderError) as ei:
        await gateway_call(CallContext(db, ws), provider="serp_api", operation="search", unit="search", units=1,
                           estimated_micros=1, fn=fn)
    assert ei.value.state == IntegrationState.DISABLED
    assert await db.fetchval("select count(*) from cost_reservations where workspace_id=$1", ws) == 0


async def test_queue_idempotent_enqueue_and_no_double_claim(db):
    ws, _ = await new_ws(db)
    async with db.transaction() as conn:
        a = await queue.enqueue(conn, workspace_id=ws, task_type="x.test", idempotency_key=f"k-{ws}")
        b = await queue.enqueue(conn, workspace_id=ws, task_type="x.test", idempotency_key=f"k-{ws}")
        for i in range(20):
            await queue.enqueue(conn, workspace_id=ws, task_type="x.test", idempotency_key=f"k-{ws}-{i}")
    assert a == b
    claimed = await asyncio.gather(*[queue.claim(db, f"w{i}", 30, ["x.test"]) for i in range(30)])
    ids = [t.id for t in claimed if t]
    assert len(ids) == len(set(ids)) == 21


async def test_retry_backoff_dead_letter_and_lease_recovery(db):
    ws, _ = await new_ws(db)
    async with db.transaction() as conn:
        await queue.enqueue(conn, workspace_id=ws, task_type="y.test", idempotency_key=f"y-{ws}", max_attempts=2)
    t = await queue.claim(db, "w1", 30, ["y.test"])
    assert await queue.fail(db, t, "w1", code="E", detail="boom", retryable=True) == "retry_wait"
    await db.execute("update tasks set run_after = now() where id=$1", t.id)
    t2 = await queue.claim(db, "w2", 1, ["y.test"])
    assert t2.attempts == 2
    await db.execute("update tasks set lease_expires_at = now() - interval '1 second' where id=$1", t.id)
    assert await queue.recover_expired_leases(db) >= 1
    st = await db.fetchval("select status from tasks where id=$1", t.id)
    assert st == "dead"
    assert await db.fetchval("select count(*) from dead_letters where task_id=$1", t.id) == 1


async def test_schema_has_no_google_content_columns(db):
    """T14 (schema part): no column on Google-linked tables can hold Places content."""
    cols = await db.fetch("""select table_name, column_name from information_schema.columns
                             where table_schema='public' and table_name in ('place_refs','prospects')""")
    ALLOWED = {"review_reason"}  # internal workflow reason code, not review content
    deny = ("name", "display_name", "formatted_address", "address", "phone", "website", "rating", "user_rating_count",
            "review", "stars", "hours", "photo")
    bad = [(c["table_name"], c["column_name"]) for c in cols if any(d == c["column_name"] or c["column_name"].startswith(d + "_")
                                                                   for d in deny) and c["column_name"] not in ALLOWED]
    assert bad == []
    st = await db.fetch("select unnest(enum_range(null::source_type))::text v")
    assert "google_places" not in {r["v"] for r in st}


async def test_rls_isolation_for_authenticated_role(db):
    ws_a, ua = await new_ws(db)
    ws_b, ub = await new_ws(db)
    await db.execute("""insert into campaigns(workspace_id, name, niche, location, target_count, discovery_provider, stages,
                        budget_cap_micros, per_site_cap_micros) values ($1,'A','plumbing','{}',1,'manual_import','{discovery}',0,0)""", ws_a)
    async with db.transaction() as conn:
        await conn.execute("set local role authenticated")
        await conn.execute("select set_config('request.jwt.claim.sub', $1, true)", str(ub))
        assert await conn.fetchval("select count(*) from campaigns where workspace_id=$1", ws_a) == 0
        with pytest.raises(Exception):
            await conn.execute("insert into notifications(workspace_id, level, title, body) values ($1,'info','x','y')", ws_b)
    async with db.transaction() as conn:
        await conn.execute("set local role authenticated")
        await conn.execute("select set_config('request.jwt.claim.sub', $1, true)", str(ua))
        assert await conn.fetchval("select count(*) from campaigns where workspace_id=$1", ws_a) == 1
        with pytest.raises(Exception):
            await conn.fetchval("select count(*) from cost_reservations")


async def test_kill_switch_is_scoped_to_its_workspace(db):
    ws_a, _ = await new_ws(db)
    ws_b, _ = await new_ws(db)
    await db.execute("update system_settings set global_kill_switch=true where workspace_id=$1", ws_a)
    async with db.transaction() as conn:
        await queue.enqueue(conn, workspace_id=ws_a, task_type="ks.test", idempotency_key=f"ks-{ws_a}")
        await queue.enqueue(conn, workspace_id=ws_b, task_type="ks.test", idempotency_key=f"ks-{ws_b}")
    t1 = await queue.claim(db, "w", 30, ["ks.test"])
    t2 = await queue.claim(db, "w", 30, ["ks.test"])
    assert t1 is not None and t1.workspace_id == ws_b and t2 is None
