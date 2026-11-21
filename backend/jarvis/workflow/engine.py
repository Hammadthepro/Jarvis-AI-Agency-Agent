"""Campaign workflow: durable, conditionally routed stages built on the Postgres task queue.

COMMAND_RECEIVED -> INTENT_PARSED -> VALIDATION (API) -> DISCOVERY(+DEDUP +WEBSITE_VERIFICATION per page)
-> RESEARCH -> QUALIFICATION -> SELECTION -> SITE_STRATEGY/GENERATION/BUILD/BROWSER_QA/REPAIR (site.build)
-> DEPLOYMENT/DEPLOYMENT_VERIFICATION (site.deploy) -> OUTREACH_DRAFT -> COMPLETED

Every stage is a task with an idempotency key; fan-in (`advance`) runs after each task finishes and
enqueues the next stage exactly once (idempotent keys). Stages absent from campaign.stages are skipped."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from uuid import UUID

import structlog

from jarvis.agents import builder, deployment, outreach, qualification
from jarvis.agents.command import NICHES, SERVICE_AREA_NICHES
from jarvis.agents.deps import Deps
from jarvis.agents.research import Researcher
from jarvis.agents.verification import Candidate, WebsiteVerifier, persist_outcome, satisfies_filter
from jarvis.core.errors import ComplianceBlocked, IntegrationState, ProviderError, ValidationFailed
from jarvis.db.audit import audit, emit
from jarvis.db.queue import Task, enqueue, load_checkpoints, save_checkpoint
from jarvis.discovery.providers import domain_key, name_key, phone_key, same_business, targets_for
from jarvis.discovery.providers import provider as discovery_provider
from jarvis.integrations.base import CallContext
from jarvis.integrations.google_places import validate_min_rating

log = structlog.get_logger(__name__)
Handler = Callable[[Deps, Task], Awaitable[dict]]
HANDLERS: dict[str, Handler] = {}
MAX_PAGES_PER_QUERY = 3
MAX_QUERIES = 40


def handler(name: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return deco


def ctx_for(deps: Deps, task: Task, site_id: UUID | None = None) -> CallContext:
    return CallContext(deps.db, task.workspace_id, task.id, task.campaign_id, site_id)


async def _campaign(deps: Deps, campaign_id: UUID):
    return await deps.db.fetchrow("select * from campaigns where id = $1", campaign_id)


async def set_campaign_state(deps: Deps, campaign_id: UUID, state: str, *, stage: str | None = None,
                             reason: str | None = None) -> None:
    async with deps.db.transaction() as conn:
        prev = await conn.fetchrow("select state::text state, workspace_id from campaigns where id = $1 for update", campaign_id)
        if prev is None or prev["state"] in ("cancelled", "cancel_requested") and state != "cancelled":
            return                       # a cancelled (or cancelling) task never moves on to another state
        await conn.execute("update campaigns set state = $2, current_stage = coalesce($3, current_stage), blocked_reason = $4 where id = $1",
                           campaign_id, state, stage, reason)
        await audit(conn, workspace_id=prev["workspace_id"], actor_type="agent", actor_id="orchestrator", action="campaign.state",
                    entity_type="campaign", entity_id=campaign_id, from_state=prev["state"], to_state=state,
                    data={"stage": stage, "reason": reason})
        await emit(conn, workspace_id=prev["workspace_id"], campaign_id=campaign_id, event_type="state",
                   message=f"Campaign {state}" + (f" ({stage})" if stage else "") + (f": {reason}" if reason else ""))
        if state in ("blocked", "failed", "requires_human_review", "completed"):
            await conn.execute("""insert into notifications(workspace_id, level, title, body) values ($1,$2,$3,$4)""",
                               prev["workspace_id"], "info" if state == "completed" else "warning",
                               f"Campaign {state}", reason or f"Campaign reached state {state}.")


def discovery_queries(niche: str, location: dict) -> list[str]:
    syn = [niche] + [k for k, v in NICHES.items() if v == niche and k != niche][:3]
    loc = ", ".join(x for x in (location.get("city"), location.get("state")) if x)
    seen, out = set(), []
    for s in syn:
        q = f"{s} in {loc}"
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:MAX_QUERIES]


async def accepted_count(deps: Deps, campaign_id: UUID) -> int:
    return await deps.db.fetchval(
        "select count(*) from prospects where campaign_id = $1 and state not in ('rejected')", campaign_id)


@handler("campaign.start")
async def campaign_start(deps: Deps, task: Task) -> dict:
    c = await _campaign(deps, task.campaign_id)
    await set_campaign_state(deps, c["id"], "running", stage="discovery")
    async with deps.db.transaction() as conn:
        if c["discovery_provider"] in ("serp_api", "geoapify"):
            await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.listings",
                          idempotency_key=f"c:{c['id']}:disc:{c['discovery_provider']}:0:0",
                          payload={"provider": c["discovery_provider"], "target_index": 0, "page": 0})
        elif c["discovery_provider"] == "google_places":
            await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.page",
                          idempotency_key=f"c:{c['id']}:disc:0:0", payload={"query_index": 0, "page": 0})
        elif c["discovery_provider"] == "manual_import":
            for p in await conn.fetch("select id from prospects where campaign_id = $1 and state = 'discovered'", c["id"]):
                await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="verify.prospect",
                              subject_id=p["id"], idempotency_key=f"c:{c['id']}:verify:{p['id']}")
        else:
            raise ComplianceBlocked("licensed_business_data provider is not configured; no licensed provider has been "
                                    "integrated or approved")
    return {"provider": c["discovery_provider"]}


async def _verify_and_record(deps, task, ctx, c, pid, cand, verifier, stats) -> None:
    out = await verifier.verify(ctx, cand)
    await persist_outcome(deps.db, workspace_id=c["workspace_id"], prospect_id=pid, task_id=task.id, out=out)
    ok = satisfies_filter(out.status, out.site_health, c["website_filter"])
    identity = await deps.db.fetchval(
        "select count(*) from research_facts where prospect_id=$1 and fact_key='business_name' and status='verified'", pid)
    if not ok:
        await deps.db.execute("update prospects set state='rejected', rejection_reason=$2 where id=$1", pid,
                              f"website filter not satisfied ({out.status}{'/' + out.site_health if out.site_health else ''})")
        stats["filtered_website"] += 1
    else:
        new_state = "website_verified" if identity else "requires_human_review"
        await deps.db.execute("""update prospects set state=$2, review_reason=coalesce(review_reason, $3) where id=$1""",
                              pid, new_state, None if identity else "identity_unconfirmed")
        stats["accepted"] += 1


def _install_discovery_page() -> None:
    """Registers discovery.page. Places objects are scoped to this handler and discarded with it."""

    @handler("discovery.page")
    async def discovery_page_impl(deps: Deps, task: Task) -> dict:
        c = await _campaign(deps, task.campaign_id)
        ctx = ctx_for(deps, task)
        qi, page_no = task.payload["query_index"], task.payload["page"]
        queries = discovery_queries(c["niche"], c["location"])
        if qi >= len(queries) or await accepted_count(deps, c["id"]) >= c["target_count"]:
            return {"done": True}
        from jarvis.integrations.google_places import DISCOVERY_ENTERPRISE, DISCOVERY_ENTERPRISE_WITH_RATING
        min_rating = validate_min_rating(c["minimum_google_rating"])
        policy = c["missing_rating_policy"]
        send_min = min_rating if (min_rating is not None and policy == "exclude") else None
        mask = DISCOVERY_ENTERPRISE_WITH_RATING if min_rating is not None else DISCOVERY_ENTERPRISE
        page = await deps.places.text_search(ctx, text_query=queries[qi], min_rating=send_min,
                                                 include_service_area=c["niche"] in SERVICE_AREA_NICHES or c["niche"] not in NICHES.values(),
                                                 page_token=task.payload.get("page_token"), mask=mask)
        stats = {"returned": len(page.places), "duplicate": 0, "not_operational": 0, "below_rating": 0,
                 "unrated_dropped": 0, "unrated_review": 0, "accepted": 0, "filtered_website": 0}
        verifier = WebsiteVerifier(deps)
        for place in page.places:
            if await accepted_count(deps, c["id"]) >= c["target_count"]:
                break
            if place.business_status not in (None, "OPERATIONAL"):
                stats["not_operational"] += 1
                continue
            review_reason = None
            if min_rating is not None:
                if place.rating is None:
                    if policy == "exclude":
                        stats["unrated_dropped"] += 1
                        continue
                    review_reason, stats["unrated_review"] = "discovery_filter_unconfirmed", stats["unrated_review"] + 1
                elif Decimal(str(place.rating)) < min_rating:
                    stats["below_rating"] += 1
                    continue
            async with deps.db.transaction() as conn:
                await conn.execute("""insert into place_refs(workspace_id, place_id) values ($1,$2)
                                      on conflict (workspace_id, place_id) do update set last_seen_at = now()""",
                                   c["workspace_id"], place.place_id)
                pid = await conn.fetchval(
                    """insert into prospects(workspace_id, campaign_id, discovery_provider, place_id, business_category,
                                             review_reason, state) values ($1,$2,'google_places',$3,$4,$5,'discovered')
                       on conflict (campaign_id, place_id) where place_id is not null do nothing returning id""",
                    c["workspace_id"], c["id"], place.place_id, c["niche"], review_reason)
            if pid is None:
                stats["duplicate"] += 1
                continue
            cand = Candidate(place.display_name, place.national_phone_number, (c["location"] or {}).get("city"),
                             (c["location"] or {}).get("state"), place.website_uri, "google_places")
            await _verify_and_record(deps, task, ctx, c, pid, cand, verifier, stats)
        next_token = page.next_page_token
        del page  # Places content is discarded with this task; only counts are recorded
        async with deps.db.transaction() as conn:
            await emit(conn, workspace_id=c["workspace_id"], task_id=task.id, campaign_id=c["id"],
                       message=f"Discovery page {page_no + 1} of query {qi + 1}: {stats['returned']} returned, "
                               f"{stats['accepted']} accepted", data=stats)
            if await accepted_count(deps, c["id"]) < c["target_count"]:
                if next_token and page_no + 1 < MAX_PAGES_PER_QUERY:
                    await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.page",
                                  idempotency_key=f"c:{c['id']}:disc:{qi}:{page_no + 1}",
                                  payload={"query_index": qi, "page": page_no + 1, "page_token": next_token})
                elif qi + 1 < len(queries):
                    await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.page",
                                  idempotency_key=f"c:{c['id']}:disc:{qi + 1}:0", payload={"query_index": qi + 1, "page": 0})
        return stats


_install_discovery_page()


# ---------------------------------------------------------------------------------------------------------------------
# Generic listing discovery (serp_api, geoapify; jarvis.discovery.providers). Google Places API (New) keeps its own,
# compliance-gated stage above (its content may not be stored).
# ---------------------------------------------------------------------------------------------------------------------
MAX_PAGES_PER_TARGET = 3
VERIFY_CONCURRENCY = 4          # website verifications of one page run in parallel (each makes several network calls)


async def _find_duplicate(conn, campaign_id: UUID, li, pk, dk, nk) -> UUID | None:
    """Same business already in this campaign (from any provider)? Phone or domain must agree AND the names must share a
    distinctive word; or the exact normalized name + postcode/street matches. Similar names alone never merge."""
    rows = await conn.fetch(
        """select k.prospect_id id, k.kind, (select f.value #>> '{}' from research_facts f where f.prospect_id=k.prospect_id
                          and f.fact_key='business_name' order by f.created_at limit 1) name
           from prospect_keys k where k.campaign_id=$1 and ((k.kind='phone' and k.key=$2) or (k.kind='domain' and k.key=$3)
             or (k.kind='name' and k.key=$4))""", campaign_id, pk, dk, nk)
    for r in rows:
        if r["kind"] == "name" or same_business(li.name, r["name"]):
            return r["id"]
    return None


async def _store_listing(conn, c, li, review_reason) -> tuple[UUID | None, bool]:
    """Inserts one listing as a prospect with its evidence and facts; or, if the business is already there from another
    provider, adds this listing as corroborating evidence. Returns (prospect id, is_new)."""
    pk, dk, nk = phone_key(li.phone), domain_key(li.website), name_key(li.name, li.postcode, (li.address or "").split(",")[0])
    excerpt = json.dumps(li.excerpt)
    dup = await _find_duplicate(conn, c["id"], li, pk, dk, nk)
    if dup is not None:
        await conn.execute(
            """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt,
                                    content_sha256, source_provider, independence_key)
               values ($1,$2,'serp_result',$3,'days_180',$4,$5,$6,$6)""",
            c["workspace_id"], dup, li.source_ref, excerpt, hashlib.sha256(excerpt.encode()).hexdigest(), li.provider)
        return dup, False
    pid = await conn.fetchval(
        """insert into prospects(workspace_id, campaign_id, discovery_provider, external_ref, business_category,
                                 review_reason, state)
           values ($1,$2,$3,$4,$5,$6,'discovered')
           on conflict (campaign_id, external_ref) where external_ref is not null do nothing returning id""",
        c["workspace_id"], c["id"], li.provider, f"{'serp' if li.provider == 'serp_api' else li.provider}:{li.external_id}",
        c["niche"], review_reason)
    if pid is None:
        return None, False
    for kind, key in (("phone", pk), ("domain", dk), ("name", nk)):
        if key:
            await conn.execute("""insert into prospect_keys(prospect_id, campaign_id, workspace_id, kind, key)
                                  values ($1,$2,$3,$4,$5)""", pid, c["id"], c["workspace_id"], kind, key[:200])
    ev = await conn.fetchval(
        """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt,
                                content_sha256, source_provider, independence_key)
           values ($1,$2,'serp_result',$3,'days_180',$4,$5,$6,$6) returning id""",
        c["workspace_id"], pid, li.source_ref, excerpt, hashlib.sha256(excerpt.encode()).hexdigest(), li.provider)
    facts = [("business_name", li.name, li.name), ("phone", li.phone, li.phone), ("city", li.city, li.address),
             ("state", li.state, li.address),
             ("listing_rating", li.rating, f"{li.rating} stars" if li.rating is not None else None),
             ("listing_review_count", li.reviews, f"{li.reviews} reviews" if li.reviews is not None else None),
             ("listing_category", li.category, li.category)]
    for key, val, quote in facts:
        if val is not None and quote:
            # All of these come from ONE listing: they share its independence key and count as one source.
            await conn.execute(
                """insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote,
                                              confidence) values ($1,$2,$3,$4,'verified',$5,$6,0.8)""",
                c["workspace_id"], pid, key, val, ev, str(quote)[:300])
    return pid, True


@handler("discovery.listings")
async def discovery_listings(deps: Deps, task: Task) -> dict:
    c = await _campaign(deps, task.campaign_id)
    ctx = ctx_for(deps, task)
    prov_key = task.payload.get("provider") or c["discovery_provider"]
    ti, page_no = task.payload["target_index"], task.payload["page"]
    targets = targets_for(c["location"] or {})
    if ti >= len(targets) or await accepted_count(deps, c["id"]) >= c["target_count"]:
        return {"done": True}
    target = targets[ti]
    nationwide = target.nationwide
    per_target_cap = max(2, -(-c["target_count"] // 5)) if nationwide else c["target_count"]
    state = dict(task.payload.get("state") or {})
    t0 = time.monotonic()
    page = await discovery_provider(deps, prov_key).page(ctx, c["niche"], target, page_no, state)
    search_ms = int((time.monotonic() - t0) * 1000)
    min_rating = validate_min_rating(c["minimum_google_rating"])
    policy = c["missing_rating_policy"]
    exclusions = [str(e).lower() for e in ((c["objective"] or {}).get("exclusions") or []) if str(e).strip()]
    stats = {"provider": prov_key, "target": target.label, "returned": len(page.listings), "duplicate": 0,
             "duplicate_cross_provider": 0, "not_operational": 0, "below_rating": 0, "unrated_dropped": 0,
             "unrated_review": 0, "excluded": 0, "out_of_area": 0, "accepted": 0, "filtered_website": 0,
             "search_ms": search_ms, "note": page.note}
    to_verify: list[tuple[UUID, Candidate]] = []
    for li in page.listings:
        if await accepted_count(deps, c["id"]) >= c["target_count"] or len(to_verify) >= per_target_cap:
            break
        if li.closed:
            stats["not_operational"] += 1
            continue
        if any(e in f"{li.name} {li.category or ''}".lower() for e in exclusions):
            stats["excluded"] += 1
            continue
        if not nationwide and target.state and li.state and li.state != target.state:
            stats["out_of_area"] += 1
            continue
        review_reason = None
        if min_rating is not None:
            if li.rating is None:
                if policy == "exclude":
                    stats["unrated_dropped"] += 1
                    continue
                review_reason, stats["unrated_review"] = "discovery_filter_unconfirmed", stats["unrated_review"] + 1
            elif Decimal(str(li.rating)) < min_rating:
                stats["below_rating"] += 1
                continue
        async with deps.db.transaction() as conn:
            pid, new = await _store_listing(conn, c, li, review_reason)
        if not new:
            stats["duplicate"] += 1
            if pid is not None:
                stats["duplicate_cross_provider"] += 1
            continue
        to_verify.append((pid, Candidate(li.name, li.phone, li.city, li.state, li.website, li.provider)))
    # Verify this page's businesses in parallel (bounded): each verification is several independent network calls.
    verifier, sem = WebsiteVerifier(deps), asyncio.Semaphore(VERIFY_CONCURRENCY)
    t1 = time.monotonic()

    async def one(pid: UUID, cand: Candidate) -> None:
        async with sem:
            await _verify_and_record(deps, task, ctx, c, pid, cand, verifier, stats)
    await asyncio.gather(*(one(p, cd) for p, cd in to_verify))
    stats["verify_ms"] = int((time.monotonic() - t1) * 1000)
    async with deps.db.transaction() as conn:
        await emit(conn, workspace_id=c["workspace_id"], task_id=task.id, campaign_id=c["id"],
                   message=f"{discovery_provider(deps, prov_key).label}: {target.label} (page {page_no + 1}): "
                           f"{stats['returned']} returned, {stats['accepted']} accepted"
                           + (f", {stats['duplicate']} duplicates" if stats["duplicate"] else "")
                           + (f" ({page.note})" if page.note else ""), data=stats)
        if await accepted_count(deps, c["id"]) < c["target_count"]:
            nxt = None
            if page.has_more and page_no + 1 < (1 if nationwide else MAX_PAGES_PER_TARGET):
                nxt = {"provider": prov_key, "target_index": ti, "page": page_no + 1, "state": state}
            elif ti + 1 < len(targets):
                nxt = {"provider": prov_key, "target_index": ti + 1, "page": 0}
            elif c["discovery_secondary"] and prov_key != c["discovery_secondary"]:
                nxt = {"provider": c["discovery_secondary"], "target_index": 0, "page": 0}    # primary ran out
            if nxt:
                await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.listings",
                              idempotency_key=f"c:{c['id']}:disc:{nxt['provider']}:{nxt['target_index']}:{nxt['page']}",
                              payload=nxt)
    return stats


@handler("discovery.serp")
async def discovery_serp(deps: Deps, task: Task) -> dict:
    """Earlier name of the SerpApi stage: tasks already queued under it still run (same implementation)."""
    task.payload.setdefault("provider", "serp_api")
    return await discovery_listings(deps, task)


@handler("provider.evaluate")
async def provider_evaluate(deps: Deps, task: Task) -> dict:
    from jarvis.discovery.evaluation import run_evaluation
    try:
        res = await run_evaluation(deps, task.workspace_id, task.subject_id, task.payload["providers"], task.payload["plan"])
    except Exception as exc:
        await deps.db.execute("""update provider_evaluations set status='failed', finished_at=now(), results=$2 where id=$1""",
                              task.subject_id, {"error": f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"[:500]})
        raise
    return {"providers": {k: v.get("status") for k, v in res["providers"].items()}}


@handler("verify.prospect")
async def verify_prospect(deps: Deps, task: Task) -> dict:
    c = await _campaign(deps, task.campaign_id)
    ctx = ctx_for(deps, task)
    p = await deps.db.fetchrow("select * from prospects where id = $1 and workspace_id = $2", task.subject_id, task.workspace_id)
    if p is None or p["state"] != "discovered":
        return {"skipped": True}
    facts = {r["fact_key"]: r["value"] for r in await deps.db.fetch(
        "select fact_key, value #>> '{}' value from research_facts where prospect_id = $1 and status = 'verified'", p["id"])}
    site = await deps.db.fetchval("""select source_ref from evidence where prospect_id = $1 and source_type = 'manual_import'
                                     and source_ref like 'website:%' limit 1""", p["id"])
    cand = Candidate(facts.get("business_name"), facts.get("phone"), facts.get("city") or c["location"].get("city"),
                     facts.get("state") or c["location"].get("state"), site.removeprefix("website:") if site else None,
                     "manual_import")
    stats: dict = {"accepted": 0, "filtered_website": 0}
    await _verify_and_record(deps, task, ctx, c, p["id"], cand, WebsiteVerifier(deps), stats)
    return stats


@handler("research.prospect")
async def research_prospect(deps: Deps, task: Task) -> dict:
    p = await deps.db.fetchrow("select state from prospects where id = $1 and workspace_id = $2", task.subject_id, task.workspace_id)
    if p is None or p["state"] not in ("website_verified", "requires_human_review", "identity_verified"):
        return {"skipped": True}
    summary = await Researcher(deps).research(ctx_for(deps, task), task.subject_id)
    if p["state"] != "requires_human_review":
        await deps.db.execute("update prospects set state='researched' where id=$1", task.subject_id)
    return summary


@handler("qualify.campaign")
async def qualify_campaign(deps: Deps, task: Task) -> dict:
    c = await _campaign(deps, task.campaign_id)
    rules = await deps.db.fetchval("select rules from scoring_rule_sets where workspace_id=$1 and is_active", c["workspace_id"]) \
        or qualification.DEFAULT_RULES
    rows = await deps.db.fetch("""select id, state, review_reason from prospects where campaign_id = $1
                                  and state in ('researched','website_verified','requires_human_review')""", c["id"])
    results = []
    for r in rows:
        inp = await qualification.gather_inputs(deps.db, r["id"], c["niche"])
        sc = qualification.score(inp, rules)
        await deps.db.execute(
            """insert into qualification_scores(workspace_id, prospect_id, rule_set_version, total, components, inputs_hash,
                                                readiness, evidence_confidence, missing, reasons)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) on conflict do nothing""",
            c["workspace_id"], r["id"], sc["rule_set_version"], sc["total"], sc["components"], sc["inputs_hash"],
            sc["status"], sc["evidence_confidence"], sc["missing"], sc["reasons"])
        if r["state"] != "requires_human_review":
            await deps.db.execute("update prospects set state=$2 where id=$1", r["id"], "qualified" if sc["qualified"] else "researched")
        # Kept for the owner's review (identity unconfirmed, or no rating when a minimum rating was asked for):
        # scored and shown, but no preview is built until the owner decides.
        results.append((sc["total"], sc["qualified"] and r["state"] != "requires_human_review" and not r["review_reason"], r["id"]))
    results.sort(key=lambda x: (-x[0], str(x[2])))
    selected = [pid for total, q, pid in results if q][: c["sites_to_build"]]
    async with deps.db.transaction() as conn:
        for pid in selected:
            slug = f"{c['niche'].replace(' ', '-')}-{secrets.token_hex(4)}"
            await conn.execute("""insert into generated_sites(workspace_id, prospect_id, campaign_id, slug) values ($1,$2,$3,$4)
                                  on conflict (prospect_id) do nothing""", c["workspace_id"], pid, c["id"], slug)
    return {"scored": len(results), "qualified": sum(1 for r in results if r[1]), "selected": [str(s) for s in selected],
            "kept_for_review": sum(1 for r in rows if r["review_reason"])}


@handler("site.build")
async def site_build(deps: Deps, task: Task) -> dict:
    cps = await load_checkpoints(deps.db, task.id)

    async def save(step: str, state: dict) -> None:
        async with deps.db.acquire() as conn:
            await save_checkpoint(conn, task.id, step, state)

    try:
        out = await builder.build_site(deps, ctx_for(deps, task, task.subject_id), task.subject_id, cps, save)
    except Exception as exc:
        await record_site_failure(deps, task, exc, stage="build/QA")
        raise
    if task.payload.get("then_deploy") and out.get("state") == "passed":
        # A manual rebuild of a finished task: deploy the new passing version (the campaign's own fan-in has ended).
        async with deps.db.transaction() as conn:
            await enqueue(conn, workspace_id=task.workspace_id, campaign_id=task.campaign_id, task_type="site.deploy",
                          subject_id=task.subject_id, idempotency_key=f"{task.idempotency_key}:deploy", max_attempts=3)
    return out


@handler("site.qa")
async def site_qa(deps: Deps, task: Task) -> dict:
    """QA the current version again in the configured isolated runner, and record the run (no rebuild)."""
    from pathlib import Path

    from jarvis.agents.qa import run_qa
    v = await deps.db.fetchrow("""select v.id, v.version, v.artifact_path from site_versions v where v.site_id=$1
                                  order by version desc limit 1""", task.subject_id)
    if v is None or not v["artifact_path"]:
        raise ValidationFailed("this website has no built version to QA")
    facts = await builder.load_facts(deps.db, await deps.db.fetchval("select prospect_id from generated_sites where id=$1",
                                                                     task.subject_id))
    out_dir = Path(v["artifact_path"]).parent
    qa_id = await deps.db.fetchval("insert into qa_runs(workspace_id, site_version_id, status, runner) values ($1,$2,'running','pending') returning id",
                                   task.workspace_id, v["id"])
    try:
        report, runner = await run_qa(deps.settings, Path(v["artifact_path"]), {"business_name": facts["business_name"],
                                      "phones": [facts["phone"]] if facts.get("phone") else []}, out_dir)
    except Exception as exc:
        await deps.db.execute("update qa_runs set status='error', finished_at=now(), report=$2 where id=$1", qa_id, {"error": str(exc)[:500]})
        raise
    async with deps.db.transaction() as conn:
        await conn.execute("update qa_runs set status=$2, runner=$3, report=$4, screenshots=$5, finished_at=now() where id=$1",
                           qa_id, "passed" if report["passed"] else "failed", runner, report,
                           [str(out_dir / "qa" / n) for n in report["screenshots"]])
        for f in report["findings"]:
            await conn.execute("""insert into qa_findings(qa_run_id, check_name, severity, viewport, message, details)
                                  values ($1,$2,$3,$4,$5,$6)""", qa_id, f["check"][:120], f["severity"], f["viewport"],
                               f["message"][:1000], f.get("details") or {})
        await emit(conn, workspace_id=task.workspace_id, task_id=task.id, campaign_id=task.campaign_id,
                   message=f"QA {'passed' if report['passed'] else 'failed'} for v{v['version']} ({len(report['findings'])} findings, runner={runner})")
    return {"passed": report["passed"], "findings": len(report["findings"]), "runner": runner}


@handler("site.deploy")
async def site_deploy(deps: Deps, task: Task) -> dict:
    try:
        out = await deployment.deploy_site(deps, ctx_for(deps, task, task.subject_id), task.subject_id)
        if out.get("status") == "failed":
            # The upload happened but the pages.dev URL did not serve the expected page: a failure, retried within limits.
            raise ProviderError("cloudflare", IntegrationState.TEMPORARILY_UNAVAILABLE,
                                f"deployed URL {out.get('url')} did not verify: {out.get('checks', [])[-1:] }", retryable=True)
        return out
    except Exception as exc:
        await record_site_failure(deps, task, exc, stage="deployment")
        raise


def _final_attempt(task: Task, exc: Exception) -> bool:
    from jarvis.agents.verification import BLOCKING_STATES
    from jarvis.core.errors import BudgetBlocked
    if isinstance(exc, (ComplianceBlocked, BudgetBlocked)):
        return True
    if isinstance(exc, ProviderError):
        return exc.state in BLOCKING_STATES or not exc.retryable or task.attempts >= task.max_attempts
    return task.attempts >= task.max_attempts


async def record_site_failure(deps: Deps, task: Task, exc: Exception, *, stage: str) -> None:
    """Keeps the reason and retry position on the site; on the last attempt the site is marked failed (never left
    looking as if it were still in progress)."""
    final = _final_attempt(task, exc)
    msg = getattr(exc, "message", None) or str(exc)
    reason = f"{stage} failed (attempt {task.attempts} of {task.max_attempts}{', final' if final else ', will retry'}): " \
             f"{type(exc).__name__}: {msg}"[:1000]
    await deps.db.execute(
        """update generated_sites set last_error=$2, last_error_at=now(),
               state = case when $3 and state <> 'deployed' then 'failed'::site_state else state end
           where id=$1""", task.subject_id, reason, final)
    async with deps.db.transaction() as conn:
        await emit(conn, workspace_id=task.workspace_id, task_id=task.id, campaign_id=task.campaign_id, message=reason[:300])


@handler("outreach.draft")
async def outreach_draft(deps: Deps, task: Task) -> dict:
    return await outreach.create_draft(deps, ctx_for(deps, task), task.campaign_id, task.subject_id)


@handler("campaign.finalize")
async def campaign_finalize(deps: Deps, task: Task) -> dict:
    """Completed only if what was asked for exists. Previews were requested and sites were selected, but none was
    deployed and verified: that is not a success, it needs the owner's attention, with the actual reasons."""
    c = await _campaign(deps, task.campaign_id)
    n = await deps.db.fetchrow(
        """select count(*) sites, count(*) filter (where state='deployed') deployed,
                  string_agg(distinct left(last_error, 200), ' | ') filter (where state in ('failed','requires_human_review')) errors
           from generated_sites where campaign_id=$1""", task.campaign_id)
    if c["sites_to_build"] and "build" in c["stages"] and n["sites"] and not n["deployed"]:
        reason = f"No preview website was completed ({n['sites']} attempted). {n['errors'] or 'See each site for details.'}"
        await set_campaign_state(deps, task.campaign_id, "requires_human_review", stage="completed", reason=reason[:500])
        return {"state": "requires_human_review"}
    await set_campaign_state(deps, task.campaign_id, "completed", stage="completed")
    return {"state": "completed"}


STAGE_TASKS = {"discovery": ("discovery.page", "discovery.listings", "discovery.serp", "verify.prospect", "campaign.start"), "research": ("research.prospect",),
               "qualification": ("qualify.campaign",), "build": ("site.build",), "deploy": ("site.deploy",),
               "outreach_draft": ("outreach.draft",)}


async def advance(deps: Deps, campaign_id: UUID) -> str | None:
    """Fan-in: when no work is in flight for the campaign, enqueue the next stage (idempotently)."""
    db = deps.db
    c = await _campaign(deps, campaign_id)
    if c is not None and c["state"] == "cancel_requested":
        from jarvis.workflow.work import finish_cancellation
        await finish_cancellation(db, campaign_id)
        return None
    if c is None or c["state"] != "running":
        return None
    inflight = await db.fetchval("""select count(*) from tasks where campaign_id = $1
                                    and status in ('pending','leased','running','retry_wait')""", campaign_id)
    if inflight:
        return None
    blocked = await db.fetchrow("""select task_type, error_code, error_detail from tasks where campaign_id = $1 and status = 'blocked'
                                   order by finished_at desc limit 1""", campaign_id)
    done = {r["task_type"] for r in await db.fetch("select distinct task_type from tasks where campaign_id = $1", campaign_id)}
    stages = c["stages"]
    ws = c["workspace_id"]
    verified = await db.fetchval("""select count(*) from prospects where campaign_id = $1
                                    and state in ('website_verified','researched','qualified','requires_human_review')""", campaign_id)
    if blocked and blocked["task_type"] in STAGE_TASKS["discovery"] and not verified:
        await set_campaign_state(deps, campaign_id, "blocked", stage="discovery",
                                 reason=f"{blocked['error_code']}: {blocked['error_detail']}"[:500])
        return "blocked"
    async with db.transaction() as conn:
        if "research.prospect" not in done and "research" in stages:
            rows = await conn.fetch("""select id from prospects where campaign_id = $1 and state in ('website_verified','requires_human_review')
                                       order by created_at limit $2""", campaign_id, c["target_count"])
            for r in rows:
                await enqueue(conn, workspace_id=ws, campaign_id=campaign_id, task_type="research.prospect", subject_id=r["id"],
                              idempotency_key=f"c:{campaign_id}:research:{r['id']}")
            if rows:
                await conn.execute("update campaigns set current_stage='research' where id=$1", campaign_id)
                return "research"
        if "qualify.campaign" not in done and "qualification" in stages:
            await enqueue(conn, workspace_id=ws, campaign_id=campaign_id, task_type="qualify.campaign",
                          idempotency_key=f"c:{campaign_id}:qualify")
            await conn.execute("update campaigns set current_stage='qualification' where id=$1", campaign_id)
            return "qualification"
        if "site.build" not in done and "build" in stages:
            sites = await conn.fetch("select id from generated_sites where campaign_id = $1 and state = 'queued'", campaign_id)
            for s in sites:
                await enqueue(conn, workspace_id=ws, campaign_id=campaign_id, task_type="site.build", subject_id=s["id"],
                              idempotency_key=f"c:{campaign_id}:build:{s['id']}", max_attempts=3)
            if sites:
                await conn.execute("update campaigns set current_stage='build' where id=$1", campaign_id)
                return "build"
        if "site.deploy" not in done and "deploy" in stages:
            sites = await conn.fetch("select id from generated_sites where campaign_id = $1 and state = 'passed'", campaign_id)
            for s in sites:
                await enqueue(conn, workspace_id=ws, campaign_id=campaign_id, task_type="site.deploy", subject_id=s["id"],
                              idempotency_key=f"c:{campaign_id}:deploy:{s['id']}", max_attempts=3)
            if sites:
                await conn.execute("update campaigns set current_stage='deploy' where id=$1", campaign_id)
                return "deploy"
        if "outreach.draft" not in done and "outreach_draft" in stages:
            rows = await conn.fetch("""select distinct g.prospect_id from generated_sites g where g.campaign_id = $1
                                       and g.state = 'deployed'""", campaign_id)
            for r in rows:
                await enqueue(conn, workspace_id=ws, campaign_id=campaign_id, task_type="outreach.draft", subject_id=r["prospect_id"],
                              idempotency_key=f"c:{campaign_id}:draft:{r['prospect_id']}")
            if rows:
                await conn.execute("update campaigns set current_stage='outreach_draft' where id=$1", campaign_id)
                return "outreach_draft"
        if "campaign.finalize" not in done:
            await enqueue(conn, workspace_id=ws, campaign_id=campaign_id, task_type="campaign.finalize",
                          idempotency_key=f"c:{campaign_id}:finalize")
            return "finalize"
    return None
