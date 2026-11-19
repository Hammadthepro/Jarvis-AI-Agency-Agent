"""Retention rules incl. 180-day SERP cleanup and legal holds; scheduled usage reconciliation; scheduler locking."""
import uuid

from jarvis.core.retention import RULES, run_retention
from jarvis.core.usage_sync import sync_provider_usage
from jarvis.worker.main import Worker
from tests.test_core_db import new_ws


async def seed(db, ws, uid):
    cid = await db.fetchval("""insert into campaigns(workspace_id, name, niche, location, target_count, discovery_provider, stages,
                               budget_cap_micros, per_site_cap_micros) values ($1,'R','plumbing','{}',1,'manual_import','{discovery}',0,0)
                               returning id""", ws)
    pid = await db.fetchval("""insert into prospects(workspace_id, campaign_id, discovery_provider, external_ref, business_category)
                               values ($1,$2,'manual_import',$3,'plumbing') returning id""", ws, cid, uuid.uuid4().hex)

    async def ev(policy, days, ref):
        return await db.fetchval("""insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, retrieved_at)
                                    values ($1,$2,$3,$4,$5, now() - make_interval(days => $6)) returning id""",
                                 ws, pid, "serp_result" if policy == "days_180" else "firecrawl_page", ref, policy, days)
    ids = {"serp_old": await ev("days_180", 181, "serp-old"), "serp_new": await ev("days_180", 179, "serp-new"),
           "serp_old_cited": await ev("days_180", 400, "serp-cited"), "page_old": await ev("days_90", 91, "page-old"),
           "page_new": await ev("days_90", 30, "page-new"), "keep_forever": await ev("indefinite", 3000, "official")}
    await db.execute("""insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote, confidence)
                        values ($1,$2,'service','"x"','verified',$3,'x',0.9)""", ws, pid, ids["serp_old_cited"])
    await db.execute("insert into task_events(workspace_id, campaign_id, event_type, message, created_at) values ($1,$2,'x','old',now()-interval '91 days')", ws, cid)
    await db.execute("insert into task_events(workspace_id, campaign_id, event_type, message, created_at) values ($1,$2,'x','new',now()-interval '10 days')", ws, cid)
    await db.execute("insert into audit_logs(workspace_id, actor_type, action, created_at) values ($1,'system','old.audit', now()-interval '5000 days')", ws)
    await db.execute("""insert into suppression_records(workspace_id, kind, value_hash, reason, created_at)
                        values ($1,'email',$2,'opt_out', now()-interval '5000 days')""", ws, uuid.uuid4().hex)
    return cid, pid, ids


async def remaining(db, ids):
    rows = await db.fetch("select id from evidence where id = any($1::uuid[])", list(ids.values()))
    have = {r["id"] for r in rows}
    return {k for k, v in ids.items() if v in have}


async def test_retention_rules_and_protected_records(db):
    ws, uid = await new_ws(db)
    cid, pid, ids = await seed(db, ws, uid)
    counts = await run_retention(db)
    assert set(counts) == set(RULES) | {"dangling_evidence_refs"} and counts["dangling_evidence_refs"] == 0 and counts["serp_results_180d"] >= 1 and counts["raw_pages_90d"] >= 1
    assert await remaining(db, ids) == {"serp_new", "serp_old_cited", "page_new", "keep_forever"}
    msgs = [r["message"] for r in await db.fetch("select message from task_events where campaign_id=$1", cid)]
    assert msgs == ["new"]
    assert await db.fetchval("select count(*) from audit_logs where workspace_id=$1 and action='old.audit'", ws) == 1
    assert await db.fetchval("select count(*) from suppression_records where workspace_id=$1", ws) == 1
    assert await db.fetchval("select status from retention_runs order by id desc limit 1") == "succeeded"


async def test_legal_hold_blocks_retention_until_released(db):
    ws, uid = await new_ws(db)
    cid, pid, ids = await seed(db, ws, uid)
    hold = await db.fetchval("""insert into legal_holds(workspace_id, scope, campaign_id, reason, created_by)
                                values ($1,'campaign',$2,'litigation hold for test',$3) returning id""", ws, cid, uid)
    await run_retention(db)
    assert await remaining(db, ids) == set(ids)
    assert await db.fetchval("select count(*) from task_events where campaign_id=$1", cid) == 2
    await db.execute("update legal_holds set released_at=now(), released_by=$2 where id=$1", hold, uid)
    await run_retention(db)
    assert "serp_old" not in await remaining(db, ids)


async def test_usage_sync_never_reports_missing_as_zero(db, deps):
    out = await sync_provider_usage(deps)
    assert out["serp_api"] == "ok" and out["anthropic"] == "unavailable"   # no account-level Anthropic usage API
    assert out["google_places"] == "unavailable" and out["livekit"] == "not_configured"
    rows = {r["provider"]: r for r in await db.fetch("""select distinct on (provider) * from provider_usage_reports
                                                         order by provider, fetched_at desc""")}
    assert rows["serp_api"]["reported_units"] == 3 and rows["serp_api"]["workspace_id"] is None
    for p in ("google_places", "cloudflare", "resend", "livekit"):
        assert rows[p]["reported_units"] is None and rows[p]["reported_cost_micros"] is None and rows[p]["status"] != "ok"
    deps.serp.settings = deps.settings.model_copy(update={"serpapi_api_key": None})
    out = await sync_provider_usage(deps)
    assert out["serp_api"] == "not_configured"


async def test_usage_api_distinguishes_figures(client, deps):
    from tests.conftest import login
    a = await login(client, "usage@t.dev")
    await client.post("/usage/sync", headers=a["headers"])
    u = {x["provider"]: x for x in (await client.get("/usage", headers=a["headers"])).json()["summary"]}
    assert set(u["serp_api"]["jarvis"]) >= {"estimated_micros", "reserved_micros", "committed_micros", "ambiguous_micros", "released_micros"}
    assert u["serp_api"]["provider_reported"]["status"] == "ok" and u["serp_api"]["reconciled"] is True
    assert u["google_places"]["provider_reported"]["status"] == "unavailable" and u["google_places"]["reconciled"] is False


async def test_scheduler_runs_due_jobs_once_under_lock(db, deps):
    await db.execute("delete from scheduled_jobs")
    w1, w2 = Worker(deps, worker_id="s1"), Worker(deps, worker_id="s2")
    ran = await w1.run_due_jobs()
    assert set(ran) == {"lease_recovery", "retention", "usage_sync", "preview_cleanup"}
    assert await w2.run_due_jobs() == []              # not due again yet
    async with db.acquire() as conn:                   # another worker holds the scheduler lock
        assert await conn.fetchval("select pg_try_advisory_lock(424242)")
        await db.execute("update scheduled_jobs set last_started_at = now() - interval '2 days'")
        assert await w2.run_due_jobs() == []
        await conn.fetchval("select pg_advisory_unlock(424242)")
    assert len(await w2.run_due_jobs()) == 4
    st = {r["name"]: r["last_status"] for r in await db.fetch("select name, last_status from scheduled_jobs")}
    assert set(st.values()) == {"succeeded"}


async def test_anthropic_local_cost_is_never_reported_as_provider_billing(client, deps, db):
    """Locally priced Claude cost is a labelled estimate; account-level usage stays UNAVAILABLE; no invoice is implied."""
    from tests.conftest import login
    a = await login(client, "anth@t.dev")
    ws = a["me"]["workspace_id"]
    await db.execute("""insert into usage_events(workspace_id, provider, operation, tokens_in, tokens_out, estimated_cost_micros, outcome, pricing_version)
                        values ($1,'anthropic','messages',1000,500,4200,'ok','test')""", ws)
    assert (await sync_provider_usage(deps))["anthropic"] == "unavailable"
    r = await db.fetchrow("select * from provider_usage_reports where provider='anthropic' order by fetched_at desc limit 1")
    assert r["status"] == "unavailable" and r["reported_cost_micros"] is None and r["reported_units"] is None
    assert r["cost_basis"] is None and r["source"] == "none"
    assert r["raw"]["jarvis_cost_basis"] == "local_estimate" and r["raw"]["jarvis_response_tokens"] >= 1500
    assert "UNAVAILABLE" in r["detail"] and "not a provider invoice" in r["detail"]

    await client.post("/usage/sync", headers=a["headers"])
    u = {x["provider"]: x for x in (await client.get("/usage", headers=a["headers"])).json()["summary"]}
    an = u["anthropic"]
    assert an["reconciled"] is False and an["invoice_status"] == "UNAVAILABLE" and an["jarvis_cost_basis"] == "local_estimate"
    assert an["provider_reported"]["status"] == "unavailable" and an["jarvis"]["response_tokens"] == 1500
    for x in u.values():                              # no provider shows a confirmed invoice without a billing export
        assert x["invoice_status"] == "UNAVAILABLE" and x["jarvis_cost_basis"] == "local_estimate"

    # A manually entered cost is labelled as such, never as a confirmed invoice.
    await client.post("/usage/reports", headers=a["headers"], json={
        "provider": "anthropic", "unit": "usd", "reported_cost_usd": "1.25", "period_start": "2026-11-01T00:00:00Z",
        "period_end": "2099-01-01T00:00:00Z"})
    u = {x["provider"]: x for x in (await client.get("/usage", headers=a["headers"])).json()["summary"]}
    assert u["anthropic"]["invoice_status"] == "MANUAL_ENTRY_UNVERIFIED"


async def test_database_refuses_mislabelled_costs(db):
    import asyncpg
    import pytest
    base = """insert into provider_usage_reports(workspace_id, provider, period_start, period_end, unit, reported_cost_micros, source, cost_basis)
              values (null,'anthropic',now(),now(),'usd',100,$1,$2)"""
    for source, basis in (("response_usage", "provider_invoice"), ("response_usage", "provider_reported"),
                          ("provider_api", None), ("manual_entry", "provider_invoice")):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.execute(base, source, basis)
    with pytest.raises(ValueError):
        from jarvis.core.usage_sync import _write
        await _write(db, "anthropic", "usd", "ok", cost=1)
