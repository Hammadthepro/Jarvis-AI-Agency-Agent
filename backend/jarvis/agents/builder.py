"""Agent 7: Website strategy + builder, with the bounded QA/repair loop (Agent 8).

Flow per site (each step checkpointed in task_checkpoints, so a restarted task resumes):
  facts -> SiteSpec (Sonnet, structured, verified facts only) -> factual guard (deterministic)
  -> design (deterministic + diversity guard) -> render (static, CSP) -> browser QA (sandbox)
  -> repair (cycles 1-2: Sonnet / design tokens; cycle 3: Opus) -> passed | requires_human_review
A site is never `passed` because rendering succeeded; it must pass browser QA."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from uuid import UUID

from jarvis.agents.deps import Deps
from jarvis.agents.qa import QaRunnerError, run_qa
from jarvis.db.audit import audit, emit
from jarvis.integrations.base import CallContext
from jarvis.sites.design import Design, choose_design, design_from_dict
from jarvis.sites.render import render_site

PROMPT_VERSION = "sitespec-v1"
MAX_REPAIR_CYCLES = 3

SITESPEC_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["strategy", "meta_title", "meta_description", "eyebrow", "headline", "subheadline", "primary_cta",
                 "services_heading", "services", "about_heading", "about", "service_area", "service_area_short", "faq",
                 "cta_heading", "cta_body"],
    "properties": {
        "strategy": {"type": "object", "additionalProperties": False,
                     "required": ["audience", "positioning", "conversion_plan", "tone"],
                     "properties": {"audience": {"type": "string"}, "positioning": {"type": "string"},
                                    "conversion_plan": {"type": "string"}, "tone": {"type": "string"}}},
        "meta_title": {"type": "string"}, "meta_description": {"type": "string"},
        "eyebrow": {"type": "string"}, "headline": {"type": "string"}, "subheadline": {"type": "string"},
        "primary_cta": {"type": "string"}, "services_heading": {"type": "string"},
        "services": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                     "required": ["name", "description", "fact_id"],
                     "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                                    "fact_id": {"type": "string"}}}},
        "about_heading": {"type": "string"}, "about": {"type": "array", "items": {"type": "string"}},
        "service_area": {"type": ["string", "null"]}, "service_area_short": {"type": ["string", "null"]},
        "faq": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["q", "a"],
                                           "properties": {"q": {"type": "string"}, "a": {"type": "string"}}}},
        "cta_heading": {"type": "string"}, "cta_body": {"type": "string"},
    },
}
SITESPEC_SYSTEM = f"""You are the website strategist and copywriter for a website-development agency. You write the
content for a concept preview website for ONE local US business. Rules (strict):
- Use ONLY the verified facts provided. Do not invent services, prices, years, licences, certifications, awards,
  reviews, testimonials, ratings, guarantees, staff names, statistics or locations.
- Every service you list must cite the fact_id of a verified 'service' fact.
- No superlatives you cannot support ("best", "#1", "top-rated", "award-winning", "trusted by thousands").
- No numbers except those that appear in the facts.
- meta_title <= 60 characters, meta_description <= 155 characters, headline <= 60 characters.
- Write a distinct strategy for this business: audience, positioning, conversion plan, tone. Vary structure and
  voice; do not reuse generic template phrasing.
- FAQ answers may only restate facts. 0-4 FAQ items. service_area may be null when unknown.
Prompt version: {PROMPT_VERSION}."""
UNSUPPORTED = [r"\btestimonial", r"\breview(s|ed)?\b", r"\baward", r"\bcertified\b", r"\blicen[cs]ed\b", r"\binsured\b",
               r"\bbonded\b", r"\bguarantee", r"#1\b", r"\bbest\b", r"\btop[- ]rated\b", r"\b\d+\s*\+?\s*years?\b",
               r"\bsince \d{4}\b", r"\bstars?\b", r"\brated\b", r"\bfamily[- ]owned\b", r"\bfree estimate", r"\$\s*\d"]


async def load_facts(db, prospect_id: UUID) -> dict:
    rows = await db.fetch(
        """select id, fact_key, value #>> '{}' as value, quote from research_facts
           where prospect_id = $1 and status = 'verified' order by created_at""", prospect_id)
    facts: dict = {"by_id": {str(r["id"]): r["fact_key"] for r in rows}, "list": [
        {"id": str(r["id"]), "key": r["fact_key"], "value": r["value"], "quote": r["quote"]} for r in rows]}
    get = lambda k: next((f for f in facts["list"] if f["key"] == k), None)  # noqa: E731
    facts["business_name"] = (get("business_name") or {}).get("value")
    ph = get("phone")
    facts["phone"] = re.sub(r"\D", "", ph["value"])[-10:] if ph else None
    facts["services"] = [f for f in facts["list"] if f["key"] == "service"]
    email = await db.fetchval(
        """select value from contacts where prospect_id = $1 and contact_type = 'email' and verification = 'verified'
           and is_public_business_contact and not harvesting_prohibited order by created_at limit 1""", prospect_id)
    facts["email"] = email
    return facts


def business_location(facts: dict, campaign_location: dict) -> str:
    city = next((f["value"] for f in facts["list"] if f["key"] == "city"), None)
    st = next((f["value"] for f in facts["list"] if f["key"] == "state"), None)
    if city or st:
        return ", ".join(x for x in (city, st) if x)
    if campaign_location.get("nationwide"):
        return "United States"
    return ", ".join(x for x in (campaign_location.get("city"), campaign_location.get("state")) if x)


def fact_check(content: dict, facts: dict) -> list[str]:
    problems: list[str] = []
    corpus = " ".join(f"{f['value']} {f['quote']}" for f in facts["list"]).lower()
    texts = [content.get(k) or "" for k in ("headline", "subheadline", "eyebrow", "meta_title", "meta_description",
                                            "cta_heading", "cta_body", "service_area", "about_heading", "services_heading")]
    texts += content.get("about", []) + [s["description"] for s in content.get("services", [])]
    texts += [q["q"] + " " + q["a"] for q in content.get("faq", [])]
    for t in texts:
        low = t.lower()
        for pat in UNSUPPORTED:
            m = re.search(pat, low)
            if m and m.group(0) not in corpus:
                problems.append(f"unsupported claim '{m.group(0)}' in: {t[:80]}")
        for num in re.findall(r"\d[\d,.]*", t):
            if num not in corpus:
                problems.append(f"number '{num}' not in verified facts: {t[:80]}")
    for s in content.get("services", []):
        if facts["by_id"].get(s.get("fact_id")) != "service":
            problems.append(f"service '{s.get('name')}' does not cite a verified service fact")
    if len(content.get("meta_title", "")) > 70:
        problems.append("meta_title too long")
    return problems


def assemble(content: dict, facts: dict) -> dict:
    c = dict(content)
    c["business_name"] = facts["business_name"]
    p = facts.get("phone")
    c["phone_display"] = f"({p[:3]}) {p[3:6]}-{p[6:]}" if p and len(p) == 10 else None
    c["phone_tel"] = f"+1{p}" if p and len(p) == 10 else None
    c["email"] = facts.get("email")
    c["address"], c["hours"] = None, next((f["value"] for f in facts["list"] if f["key"] == "hours"), None)
    c["about"] = [a for a in c.get("about", []) if a.strip()][:3]
    return c


async def generate_spec(deps: Deps, ctx: CallContext, *, facts: dict, niche: str, location: str, model: str,
                        feedback: str | None = None) -> dict:
    fact_lines = "\n".join(f"- fact_id={f['id']} [{f['key']}] {f['value']}" for f in facts["list"])
    user = (f"Business niche: {niche}\nCampaign location: {location}\nBusiness name (verified): {facts['business_name']}\n"
            f"Verified facts:\n{fact_lines}\n")
    if feedback:
        user += f"\nThe previous draft was rejected for these reasons; fix them without adding new claims:\n{feedback}\n"
    res = await deps.claude.structured(ctx, model=model, system=SITESPEC_SYSTEM, user=user, schema=SITESPEC_SCHEMA,
                                       max_tokens=6000, operation="site.strategy")
    return {"content": res.data, "model": res.model}


async def recent_designs(db, workspace_id: UUID, exclude_site: UUID, limit: int = 12) -> list[Design]:
    rows = await db.fetch(
        """select ss.design from site_versions v join site_specs ss on ss.id = v.spec_id join generated_sites g on g.id = v.site_id
           where g.workspace_id = $1 and g.id <> $2 order by v.created_at desc limit $3""", workspace_id, exclude_site, limit)
    return [design_from_dict(r["design"]) for r in rows]


def repair_plan(report: dict, cycle: int) -> tuple[str, list[dict]]:
    blocking = [f for f in report["findings"] if f["severity"] in ("critical", "serious")]
    if any(f["check"] == "axe:color-contrast" for f in blocking):
        return "design_contrast", blocking
    if any(f["check"] in ("text_overflow", "horizontal_overflow", "meta_title", "meta_description") for f in report["findings"]):
        return ("copy_opus" if cycle >= 3 else "copy_sonnet"), blocking
    if blocking:
        return "unrepairable", blocking
    return "none", []


async def build_site(deps: Deps, ctx: CallContext, site_id: UUID, checkpoints: dict, save) -> dict:
    """`save(step, state)` persists a checkpoint. Returns final summary."""
    db, s = deps.db, deps.settings
    site = await db.fetchrow(
        """select g.*, c.niche, c.location, p.workspace_id from generated_sites g join campaigns c on c.id = g.campaign_id
           join prospects p on p.id = g.prospect_id where g.id = $1""", site_id)
    facts = await load_facts(db, site["prospect_id"])
    if not facts["business_name"]:
        await _set_state(db, ctx, site_id, "requires_human_review", "no independently verified business name")
        return {"state": "requires_human_review", "reason": "identity_unverified"}
    # The business's own verified location (a nationwide or state-wide campaign has no single city of its own).
    location = business_location(facts, site["location"] or {})
    agency = await db.fetchval("select coalesce(agency_name, 'our studio') from system_settings where workspace_id = $1",
                               ctx.workspace_id) or "our studio"

    # Strategy / spec (checkpointed)
    if "spec" not in checkpoints:
        await _set_state(db, ctx, site_id, "strategy")
        feedback, spec = None, None
        for attempt in range(2):
            gen = await generate_spec(deps, ctx, facts=facts, niche=site["niche"], location=location,
                                      model=s.claude_model_build, feedback=feedback)
            problems = fact_check(gen["content"], facts)
            if not problems:
                spec = gen
                break
            feedback = "\n".join(problems[:15])
            await emit_ev(db, ctx, f"Site draft {attempt + 1} rejected by factual guard ({len(problems)} issues)")
        if spec is None:
            gen = await generate_spec(deps, ctx, facts=facts, niche=site["niche"], location=location,
                                      model=s.claude_model_escalation, feedback=feedback)
            if fact_check(gen["content"], facts):
                await _set_state(db, ctx, site_id, "requires_human_review", "copy failed factual guard after escalation")
                return {"state": "requires_human_review", "reason": "factual_guard"}
            spec = gen
        design = choose_design(str(site_id), site["niche"], await recent_designs(db, ctx.workspace_id, site_id))
        checkpoints["spec"] = {"content": spec["content"], "model": spec["model"], "design": design.__dict__}
        await save("spec", checkpoints["spec"])

    content, design = checkpoints["spec"]["content"], design_from_dict(checkpoints["spec"]["design"])
    cycle = int(checkpoints.get("repair", {}).get("cycle", 0))
    if "repair" in checkpoints:
        content = checkpoints["repair"].get("content", content)
        design = design_from_dict(checkpoints["repair"].get("design", design.__dict__))

    while True:
        await _set_state(db, ctx, site_id, "building" if cycle == 0 else "repair")
        version = await _new_version(db, ctx, site_id, content, design, checkpoints["spec"]["model"], facts,
                                     "initial" if cycle == 0 else ("escalation" if cycle >= 3 else "repair"))
        out_dir = s.sites_artifact_dir / str(site_id) / f"v{version['version']}"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        manifest = render_site(out_dir / "dist", content=assemble(content, facts), design=design, agency=agency,
                               seed=str(site_id))
        await db.execute("""update site_versions set artifact_path = $2, artifact_sha256 = $3, file_count = $4, total_bytes = $5
                            where id = $1""", version["id"], str(out_dir / "dist"), manifest["sha256"],
                         manifest["file_count"], manifest["total_bytes"])
        await _set_state(db, ctx, site_id, "qa")
        qa_id = await db.fetchval("insert into qa_runs(workspace_id, site_version_id, status, runner) values ($1,$2,'running','pending') returning id",
                                  ctx.workspace_id, version["id"])
        qa_facts = {"business_name": facts["business_name"], "phones": [facts["phone"]] if facts.get("phone") else []}
        try:
            report, runner = await run_qa(s, out_dir / "dist", qa_facts, out_dir)
        except QaRunnerError as exc:
            await db.execute("update qa_runs set status='error', finished_at=now(), report=$2 where id=$1", qa_id, {"error": str(exc)})
            raise
        async with db.transaction() as conn:
            await conn.execute("update qa_runs set status=$2, runner=$3, report=$4, screenshots=$5, finished_at=now() where id=$1",
                               qa_id, "passed" if report["passed"] else "failed", runner, report,
                               [str(out_dir / "qa" / n) for n in report["screenshots"]])
            for f in report["findings"]:
                await conn.execute("""insert into qa_findings(qa_run_id, check_name, severity, viewport, message, details)
                                      values ($1,$2,$3,$4,$5,$6)""", qa_id, f["check"][:120], f["severity"], f["viewport"],
                                   f["message"][:1000], f.get("details") or {})
        await emit_ev(db, ctx, f"QA {'passed' if report['passed'] else 'failed'} for site v{version['version']} "
                               f"({len(report['findings'])} findings, runner={runner})")
        if report["passed"]:
            await db.execute("update generated_sites set current_version_id = $2, repair_cycles = $3 where id = $1",
                             site_id, version["id"], cycle)
            await _set_state(db, ctx, site_id, "passed")
            return {"state": "passed", "version_id": str(version["id"]), "cycles": cycle, "runner": runner}
        cycle += 1
        if cycle > MAX_REPAIR_CYCLES:
            await _set_state(db, ctx, site_id, "requires_human_review", "QA still failing after bounded repairs")
            await db.execute("update generated_sites set last_error=$2, last_error_at=now() where id=$1", site_id,
                             "QA still failing after bounded repairs: " + ", ".join(sorted({f["check"] for f in report["findings"]
                                                                                            if f["severity"] in ("critical", "serious")}))[:800])
            return {"state": "requires_human_review", "cycles": cycle - 1}
        action, blocking = repair_plan(report, cycle)
        if action == "design_contrast":
            design = Design(**{**design.__dict__, "contrast_boost": design.contrast_boost + 1})
        elif action in ("copy_sonnet", "copy_opus"):
            model = s.claude_model_escalation if action == "copy_opus" else s.claude_model_build
            fb = "Shorten and tighten copy to fix these QA findings:\n" + "\n".join(
                f"- {f['check']}: {f['message']}" for f in report["findings"][:10])
            gen = await generate_spec(deps, ctx, facts=facts, niche=site["niche"], location=location, model=model, feedback=fb)
            if not fact_check(gen["content"], facts):
                content = gen["content"]
        else:
            await _set_state(db, ctx, site_id, "requires_human_review", f"QA failures not auto-repairable: "
                             f"{', '.join(sorted({f['check'] for f in blocking}))[:300]}")
            return {"state": "requires_human_review", "cycles": cycle}
        checkpoints["repair"] = {"cycle": cycle, "content": content, "design": design.__dict__}
        await save("repair", checkpoints["repair"])


async def _new_version(db, ctx, site_id, content, design, model, facts, reason) -> dict:
    async with db.transaction() as conn:
        spec_id = await conn.fetchval(
            """insert into site_specs(workspace_id, prospect_id, spec, design, facts_used, model, prompt_version)
               select $1, prospect_id, $3, $4, $5::uuid[], $6, $7 from generated_sites where id = $2 returning id""",
            ctx.workspace_id, site_id, content, design.__dict__, [f["id"] for f in facts["list"]], model, PROMPT_VERSION)
        v = await conn.fetchval("select coalesce(max(version),0)+1 from site_versions where site_id = $1", site_id)
        vid = await conn.fetchval("""insert into site_versions(workspace_id, site_id, version, spec_id, reason)
                                     values ($1,$2,$3,$4,$5) returning id""", ctx.workspace_id, site_id, v, spec_id, reason)
    return {"id": vid, "version": v}


async def _set_state(db, ctx, site_id, state, reason: str | None = None) -> None:
    async with db.transaction() as conn:
        prev = await conn.fetchval("select state from generated_sites where id = $1 for update", site_id)
        await conn.execute("update generated_sites set state = $2 where id = $1", site_id, state)
        await audit(conn, workspace_id=ctx.workspace_id, actor_type="agent", actor_id="website_builder", action="site.state",
                    entity_type="generated_site", entity_id=site_id, from_state=prev, to_state=state,
                    data={"reason": reason} if reason else {})


async def emit_ev(db, ctx, msg: str) -> None:
    async with db.acquire() as conn:
        await emit(conn, workspace_id=ctx.workspace_id, task_id=ctx.task_id, campaign_id=ctx.campaign_id, message=msg)


_ = Path
