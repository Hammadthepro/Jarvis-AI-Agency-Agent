"""Evidence retention integrity: purged evidence leaves a content-free tombstone, verification references never
dangle, verification summaries survive, legal holds prevent both deletion and tombstoning."""
import json
import re
import uuid
from pathlib import Path
from uuid import UUID

from jarvis.core.retention import resolve_evidence, run_retention
from tests.conftest import login
from tests.test_core_db import new_ws
from tests.test_retention_usage_scheduler import remaining, seed

MIGRATION = Path(__file__).resolve().parents[2] / "database" / "migrations" / "0005_evidence_tombstones.sql"
CHECKS = [{"check": "serp_query_1", "result": "no official site in top 10"}, {"check": "listed_site_probe", "result": "dns_failure"}]


async def add_verification(db, ws, pid, ev_ids):
    return await db.fetchval("""insert into website_verifications(workspace_id, prospect_id, status, reason, checks, evidence_ids, confidence)
                                values ($1,$2,'VERIFIED_NO_WEBSITE','independent checks found no site',$3,$4,0.85) returning id""",
                             ws, pid, CHECKS, ev_ids)


async def seed_with_verification(db, ws, uid):
    cid, pid, ids = await seed(db, ws, uid)
    await db.execute("update evidence set source_ref = 'https://www.Example-Plumbing.com/about?q=secret#x', content_excerpt = 'SERP snippet text', "
                     "content_sha256 = 'abc' where id = $1", ids["serp_old"])
    vid = await add_verification(db, ws, pid, [ids["serp_old"], ids["page_old"], ids["serp_new"], ids["serp_old_cited"]])
    return cid, pid, ids, vid


async def test_purged_evidence_becomes_tombstone_and_refs_never_dangle(db):
    ws, uid = await new_ws(db)
    cid, pid, ids, vid = await seed_with_verification(db, ws, uid)
    counts = await run_retention(db)
    assert counts["dangling_evidence_refs"] == 0
    assert "serp_old" not in await remaining(db, ids) and "page_old" not in await remaining(db, ids)

    t = await db.fetchrow("select * from evidence_tombstones where evidence_id=$1", ids["serp_old"])
    assert t["purge_rule"] == "serp_results_180d" and t["source_type"] == "serp_result" and t["retention_policy"] == "days_180"
    assert t["source_host"] == "www.example-plumbing.com" and t["content_sha256"] == "abc" and len(t["source_ref_sha256"]) == 64
    assert (await db.fetchval("select purge_rule from evidence_tombstones where evidence_id=$1", ids["page_old"])) == "raw_pages_90d"
    # No provider content or full source reference is retained in the tombstone.
    cols = {r["column_name"] for r in await db.fetch("select column_name from information_schema.columns where table_name='evidence_tombstones'")}
    assert not cols & {"content_excerpt", "source_ref", "quote", "snippet"}
    assert "secret" not in json.dumps({k: str(v) for k, v in dict(t).items()}) and "SERP snippet" not in str(dict(t))

    # The verification record and its summary are untouched; every reference resolves.
    v = await db.fetchrow("select status, reason, checks, evidence_ids from website_verifications where id=$1", vid)
    assert v["status"] == "VERIFIED_NO_WEBSITE" and v["checks"] == CHECKS and len(v["evidence_ids"]) == 4
    refs = {r["id"]: r for r in await resolve_evidence(db, list(v["evidence_ids"]))}
    assert refs[ids["serp_old"]]["state"] == refs[ids["page_old"]]["state"] == "expired"
    assert refs[ids["serp_new"]]["state"] == refs[ids["serp_old_cited"]]["state"] == "live"
    assert await db.fetchval("select count(*) from dangling_evidence_refs") == 0


async def test_legal_hold_prevents_deletion_and_tombstoning(db):
    ws, uid = await new_ws(db)
    cid, pid, ids, vid = await seed_with_verification(db, ws, uid)
    hold = await db.fetchval("""insert into legal_holds(workspace_id, scope, prospect_id, reason, created_by)
                                values ($1,'prospect',$2,'regulator request for test',$3) returning id""", ws, pid, uid)
    await run_retention(db)
    assert await remaining(db, ids) == set(ids)
    assert await db.fetchval("select count(*) from evidence_tombstones where prospect_id=$1", pid) == 0
    await db.execute("update legal_holds set released_at=now(), released_by=$2 where id=$1", hold, uid)
    await run_retention(db)
    assert await db.fetchval("select count(*) from evidence_tombstones where prospect_id=$1", pid) == 2


async def test_any_evidence_delete_is_tombstoned_and_prospect_delete_cascades_cleanly(db):
    ws, uid = await new_ws(db)
    cid, pid, ids, vid = await seed_with_verification(db, ws, uid)
    await db.execute("delete from evidence where id=$1", ids["serp_new"])     # ad hoc delete, outside retention
    assert await db.fetchval("select purge_rule from evidence_tombstones where evidence_id=$1", ids["serp_new"]) == "manual_delete"
    assert await db.fetchval("select count(*) from dangling_evidence_refs where verification_id=$1", vid) == 0
    await db.execute("delete from prospects where id=$1", pid)                # whole prospect gone: nothing left to dangle
    assert await db.fetchval("select count(*) from evidence_tombstones where prospect_id=$1", pid) == 0
    assert await db.fetchval("select count(*) from website_verifications where id=$1", vid) == 0


async def test_backfill_resolves_references_purged_before_tombstones(db):
    ws, uid = await new_ws(db)
    cid, pid, ids, _ = await seed_with_verification(db, ws, uid)
    gone = uuid.uuid4()                                                       # a reference purged before migration 0005
    vid = await add_verification(db, ws, pid, [gone, ids["serp_new"]])
    assert await db.fetchval("select count(*) from dangling_evidence_refs where verification_id=$1", vid) == 1
    backfill = re.search(r"insert into evidence_tombstones\(evidence_id, workspace_id, prospect_id, purged_at, purge_rule\).*?;",
                         MIGRATION.read_text(), re.S).group(0)
    await db.execute(backfill)                                                # the migration's own statement (idempotent)
    await db.execute(backfill)
    assert await db.fetchval("select count(*) from dangling_evidence_refs") == 0
    t = await db.fetchrow("select source_type, purge_rule from evidence_tombstones where evidence_id=$1", gone)
    assert t["source_type"] is None and t["purge_rule"] == "purged_before_tombstones"
    assert (await resolve_evidence(db, [gone]))[0]["state"] == "expired"


async def test_prospect_detail_shows_expired_references(client, db):
    a = await login(client, "tomb@ev.dev")
    ws, uid = UUID(str(a["me"]["workspace_id"])), UUID(str(a["me"]["user_id"]))
    cid, pid, ids, vid = await seed_with_verification(db, ws, uid)
    await run_retention(db)
    body = (await client.get(f"/prospects/{pid}", headers=a["headers"])).json()
    v = next(x for x in body["verifications"] if x["id"] == str(vid))
    states = {r["id"]: r["state"] for r in v["evidence_refs"]}
    assert states[str(ids["serp_old"])] == "expired" and states[str(ids["serp_new"])] == "live"
    assert v["checks"] == CHECKS and "evidence_ids" not in v
    expired = next(r for r in v["evidence_refs"] if r["id"] == str(ids["serp_old"]))
    assert "excerpt" not in expired and "source_ref" not in expired and expired["purge_rule"] == "serp_results_180d"
