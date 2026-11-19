"""Bounded website repair loop: Sonnet repairs 1 and 2, Opus escalation on 3, success at each stage,
failure after the limit, unrepairable failures, and NO model call after the configured limit."""
import uuid

import pytest

from jarvis.agents import builder
from jarvis.integrations.base import CallContext
from tests.test_core_db import new_ws

FAIL_COPY = {"passed": False, "screenshots": [], "metrics": {},
             "findings": [{"check": "horizontal_overflow", "severity": "serious", "viewport": "mobile", "message": "overflow", "details": {}}]}
FAIL_CONTRAST = {"passed": False, "screenshots": [], "metrics": {},
                 "findings": [{"check": "axe:color-contrast", "severity": "serious", "viewport": "desktop", "message": "contrast", "details": {}}]}
FAIL_HARD = {"passed": False, "screenshots": [], "metrics": {},
             "findings": [{"check": "console_errors", "severity": "serious", "viewport": "desktop", "message": "boom", "details": {}}]}
PASS = {"passed": True, "screenshots": [], "metrics": {}, "findings": []}


async def make_site(db, ws):
    cid = await db.fetchval("""insert into campaigns(workspace_id, name, niche, location, target_count, discovery_provider, stages,
                               budget_cap_micros, per_site_cap_micros) values ($1,'B','plumbing','{"city":"Houston","state":"TX"}',1,
                               'manual_import','{build}',100000000,100000000) returning id""", ws)
    pid = await db.fetchval("""insert into prospects(workspace_id, campaign_id, discovery_provider, external_ref, business_category)
                               values ($1,$2,'manual_import',$3,'plumbing') returning id""", ws, cid, uuid.uuid4().hex)
    ev = await db.fetchval("""insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy)
                              values ($1,$2,'manual_import','row-1','indefinite') returning id""", ws, pid)
    for k, v in (("business_name", "Bayou City Plumbing Co"), ("phone", "+17135550142"), ("service", "Drain cleaning"),
                 ("service", "Water heater repair"), ("service_area", "Houston and Harris County")):
        await db.execute("""insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote, confidence)
                            values ($1,$2,$3,$4,'verified',$5,$6,0.9)""", ws, pid, k, v, ev, v)
    sid = await db.fetchval("insert into generated_sites(workspace_id, prospect_id, campaign_id, slug) values ($1,$2,$3,$4) returning id",
                            ws, pid, cid, f"t-{uuid.uuid4().hex[:8]}")
    return cid, sid


@pytest.mark.parametrize("script,expect_state,expect_models,expect_cycles", [
    ([PASS], "passed", ["s"], 0),
    ([FAIL_COPY, PASS], "passed", ["s", "s"], 1),
    ([FAIL_COPY, FAIL_COPY, PASS], "passed", ["s", "s", "s"], 2),
    ([FAIL_COPY, FAIL_COPY, FAIL_COPY, PASS], "passed", ["s", "s", "s", "o"], 3),
    ([FAIL_COPY, FAIL_COPY, FAIL_COPY, FAIL_COPY, PASS], "requires_human_review", ["s", "s", "s", "o"], None),
    ([FAIL_HARD, PASS], "requires_human_review", ["s"], None),
    ([FAIL_CONTRAST, FAIL_CONTRAST, PASS], "passed", ["s"], 2),
])
async def test_repair_loop(db, deps, fake_claude, monkeypatch, script, expect_state, expect_models, expect_cycles):
    ws, _ = await new_ws(db)
    cid, sid = await make_site(db, ws)
    reports = list(script)
    qa_calls = []

    async def fake_qa(settings, site_dir, facts, artifact_dir, http=None):
        qa_calls.append(site_dir)
        assert (site_dir / "index.html").exists()      # every cycle renders a real build before QA
        return reports.pop(0), "test-qa"
    monkeypatch.setattr(builder, "run_qa", fake_qa)
    saved = {}

    async def save(step, state):
        saved[step] = state
    fake_claude.calls.clear()
    out = await builder.build_site(deps, CallContext(db, ws, None, cid, sid), sid, {}, save)
    models = ["o" if "opus" in c["model"] else "s" for c in fake_claude.calls if "sitespec" in c["system"]]
    assert out["state"] == expect_state
    assert models == expect_models, models
    if expect_cycles is not None:
        assert out["cycles"] == expect_cycles
    assert await db.fetchval("select state from generated_sites where id=$1", sid) == expect_state
    versions = await db.fetchval("select count(*) from site_versions where site_id=$1", sid)
    assert versions == len(qa_calls) and len(qa_calls) <= 4    # initial + at most 3 repairs
    findings = await db.fetchval("""select count(*) from qa_findings f join qa_runs r on r.id=f.qa_run_id
                                    join site_versions v on v.id=r.site_version_id where v.site_id=$1""", sid)
    assert findings == sum(len(r["findings"]) for r in script[:len(qa_calls)])
    if script == [FAIL_CONTRAST, FAIL_CONTRAST, PASS]:
        assert saved["repair"]["design"]["contrast_boost"] == 2


async def test_no_model_call_after_limit_even_on_resume(db, deps, fake_claude, monkeypatch):
    """A task resumed from a checkpoint at the last cycle cannot exceed the limit."""
    ws, _ = await new_ws(db)
    cid, sid = await make_site(db, ws)

    async def always_fail(settings, site_dir, facts, artifact_dir, http=None):
        return FAIL_COPY, "test-qa"
    monkeypatch.setattr(builder, "run_qa", always_fail)
    cps = {}

    async def save(step, state):
        cps[step] = state
    await builder.build_site(deps, CallContext(db, ws, None, cid, sid), sid, cps, save)
    n = len(fake_claude.calls)
    await db.execute("update generated_sites set state='repair' where id=$1", sid)
    out = await builder.build_site(deps, CallContext(db, ws, None, cid, sid), sid, cps, save)
    assert out["state"] == "requires_human_review" and len(fake_claude.calls) == n
