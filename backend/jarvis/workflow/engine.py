"""Campaign workflow: durable, conditionally routed stages built on the Postgres task queue.

COMMAND_RECEIVED -> INTENT_PARSED -> VALIDATION (API) -> DISCOVERY(+DEDUP +WEBSITE_VERIFICATION per page)
-> RESEARCH -> QUALIFICATION -> SELECTION -> SITE_STRATEGY/GENERATION/BUILD/BROWSER_QA/REPAIR (site.build)
-> DEPLOYMENT/DEPLOYMENT_VERIFICATION (site.deploy) -> OUTREACH_DRAFT -> COMPLETED

Every stage is a task with an idempotency key; fan-in (`advance`) runs after each task finishes and
enqueues the next stage exactly once (idempotent keys). Stages absent from campaign.stages are skipped."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from decimal import Decimal
from uuid import UUID

import structlog

from jarvis.agents import builder, deployment, outreach, qualification
from jarvis.agents.command import NICHES, SERVICE_AREA_NICHES
from jarvis.agents.deps import Deps
from jarvis.agents.research import Researcher
from jarvis.agents.verification import Candidate, WebsiteVerifier, persist_outcome, satisfies_filter
from jarvis.core.errors import ComplianceBlocked
from jarvis.db.audit import audit, emit
from jarvis.db.queue import Task, enqueue, load_checkpoints, save_checkpoint
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
        prev = await conn.fetchrow("select state, workspace_id from campaigns where id = $1 for update", campaign_id)
        if prev is None or prev["state"] in ("cancelled",) and state != "cancelled":
            return
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
        if c["discovery_provider"] == "serp_api":
            await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.serp",
                          idempotency_key=f"c:{c['id']}:serp:0:0", payload={"target_index": 0, "page": 0})
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
# SerpApi discovery (Google Maps engine through SerpApi). Separate from Google Places API (New), which stays gated.
# ---------------------------------------------------------------------------------------------------------------------
STATE_NAMES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
               "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
               "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
               "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
               "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
               "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
               "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
               "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
               "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
               "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia"}
# "Anywhere in the US": large metros across every region, searched one page each so results are spread out.
US_METROS = [("New York", "NY"), ("Los Angeles", "CA"), ("Chicago", "IL"), ("Houston", "TX"), ("Phoenix", "AZ"),
             ("Philadelphia", "PA"), ("San Antonio", "TX"), ("San Diego", "CA"), ("Dallas", "TX"), ("Jacksonville", "FL"),
             ("Austin", "TX"), ("Columbus", "OH"), ("Charlotte", "NC"), ("Indianapolis", "IN"), ("Seattle", "WA"),
             ("Denver", "CO"), ("Nashville", "TN"), ("Oklahoma City", "OK"), ("Boston", "MA"), ("Portland", "OR"),
             ("Las Vegas", "NV"), ("Detroit", "MI"), ("Memphis", "TN"), ("Louisville", "KY"), ("Baltimore", "MD"),
             ("Milwaukee", "WI"), ("Albuquerque", "NM"), ("Atlanta", "GA"), ("Kansas City", "MO"), ("Omaha", "NE"),
             ("Miami", "FL"), ("Minneapolis", "MN"), ("Tampa", "FL"), ("New Orleans", "LA"), ("Salt Lake City", "UT"),
             ("Birmingham", "AL"), ("Boise", "ID"), ("Richmond", "VA"), ("Des Moines", "IA"), ("Little Rock", "AR")]
SERP_MAX_PAGES = 3


def serp_targets(location: dict) -> list[tuple[str, int, str | None, str | None]]:
    """(SerpApi location, zoom, city, state) to search, in order."""
    if location.get("nationwide"):
        return [(f"{c}, {STATE_NAMES[s]}, United States", 11, c, s) for c, s in US_METROS]
    city, st = location.get("city"), location.get("state")
    if city:
        return [(", ".join(x for x in (city, STATE_NAMES.get(st or "", st), "United States") if x), 11, city, st)]
    return [(f"{STATE_NAMES.get(st, st)}, United States", 7, None, st)]


_ADDR = re.compile(r",\s*([^,]+?),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*(?:,\s*United States)?\s*$")


def address_city_state(address: str | None) -> tuple[str | None, str | None]:
    m = _ADDR.search(address or "")
    return (m.group(1).strip(), m.group(2)) if m and m.group(2) in STATE_NAMES else (None, None)


@handler("discovery.serp")
async def discovery_serp(deps: Deps, task: Task) -> dict:
    c = await _campaign(deps, task.campaign_id)
    ctx = ctx_for(deps, task)
    ti, page_no = task.payload["target_index"], task.payload["page"]
    targets = serp_targets(c["location"] or {})
    if ti >= len(targets) or await accepted_count(deps, c["id"]) >= c["target_count"]:
        return {"done": True}
    loc, zoom, t_city, t_state = targets[ti]
    nationwide = bool((c["location"] or {}).get("nationwide"))
    per_target_cap = max(2, -(-c["target_count"] // 5)) if nationwide else c["target_count"]
    page = await deps.serp.maps_search(ctx, c["niche"], location=loc, zoom=zoom, start=page_no * 20)
    min_rating = validate_min_rating(c["minimum_google_rating"])
    policy = c["missing_rating_policy"]
    stats = {"target": loc, "returned": len(page.listings), "duplicate": 0, "not_operational": 0, "below_rating": 0,
             "unrated_dropped": 0, "unrated_review": 0, "accepted": 0, "filtered_website": 0, "out_of_area": 0}
    verifier = WebsiteVerifier(deps)
    exclusions = [str(e).lower() for e in ((c["objective"] or {}).get("exclusions") or []) if str(e).strip()]
    stats["excluded"] = 0
    taken = 0
    for li in page.listings:
        if await accepted_count(deps, c["id"]) >= c["target_count"] or taken >= per_target_cap:
            break
        if li.closed:
            stats["not_operational"] += 1
            continue
        if any(e in f"{li.title} {li.type or ''}".lower() for e in exclusions):      # the owner's exclusions
            stats["excluded"] += 1
            continue
        city, st = address_city_state(li.address)
        if not nationwide and t_state and st and st != t_state:
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
        excerpt = json.dumps({"title": li.title, "address": li.address, "phone": li.phone, "website": li.website,
                              "rating": li.rating, "reviews": li.reviews, "type": li.type, "open_state": li.open_state})
        async with deps.db.transaction() as conn:
            pid = await conn.fetchval(
                """insert into prospects(workspace_id, campaign_id, discovery_provider, external_ref, business_category,
                                         review_reason, state) values ($1,$2,'serp_api',$3,$4,$5,'discovered')
                   on conflict (campaign_id, external_ref) where external_ref is not null do nothing returning id""",
                c["workspace_id"], c["id"], f"serp:{li.listing_id}", c["niche"], review_reason)
            if pid is None:
                stats["duplicate"] += 1
                continue
            ev = await conn.fetchval(
                """insert into evidence(workspace_id, prospect_id, source_type, source_ref, retention_policy, content_excerpt,
                                        content_sha256) values ($1,$2,'serp_result',$3,'days_180',$4,$5) returning id""",
                c["workspace_id"], pid, f"serpapi:google_maps:{page.search_id or 'n/a'}:{li.listing_id}", excerpt,
                hashlib.sha256(excerpt.encode()).hexdigest())
            facts = [("business_name", li.title, li.title), ("phone", li.phone, li.phone),
                     ("city", city or t_city, li.address), ("state", st or t_state, li.address),
                     ("listing_rating", li.rating, f"{li.rating} stars" if li.rating is not None else None),
                     ("listing_review_count", li.reviews, f"{li.reviews} reviews" if li.reviews is not None else None)]
            for key, val, quote in facts:
                if val is not None and quote:
                    await conn.execute(
                        """insert into research_facts(workspace_id, prospect_id, fact_key, value, status, evidence_id, quote,
                                                      confidence) values ($1,$2,$3,$4,'verified',$5,$6,0.8)""",
                        c["workspace_id"], pid, key, val, ev, str(quote)[:300])
        taken += 1
        cand = Candidate(li.title, li.phone, city or t_city, st or t_state, li.website, "serp_api")
        await _verify_and_record(deps, task, ctx, c, pid, cand, verifier, stats)
    async with deps.db.transaction() as conn:
        await emit(conn, workspace_id=c["workspace_id"], task_id=task.id, campaign_id=c["id"],
                   message=f"SerpApi listings for {loc} (page {page_no + 1}): {stats['returned']} returned, "
                           f"{stats['accepted']} accepted", data=stats)
        if await accepted_count(deps, c["id"]) < c["target_count"]:
            if page.has_more and page_no + 1 < (1 if nationwide else SERP_MAX_PAGES):
                await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.serp",
                              idempotency_key=f"c:{c['id']}:serp:{ti}:{page_no + 1}",
                              payload={"target_index": ti, "page": page_no + 1})
            elif ti + 1 < len(targets):
                await enqueue(conn, workspace_id=c["workspace_id"], campaign_id=c["id"], task_type="discovery.serp",
                              idempotency_key=f"c:{c['id']}:serp:{ti + 1}:0", payload={"target_index": ti + 1, "page": 0})
    return stats


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
            """insert into qualification_scores(workspace_id, prospect_id, rule_set_version, total, components, inputs_hash)
               values ($1,$2,$3,$4,$5,$6) on conflict do nothing""",
            c["workspace_id"], r["id"], sc["rule_set_version"], sc["total"], sc["components"], sc["inputs_hash"])
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

    return await builder.build_site(deps, ctx_for(deps, task, task.subject_id), task.subject_id, cps, save)


@handler("site.deploy")
async def site_deploy(deps: Deps, task: Task) -> dict:
    return await deployment.deploy_site(deps, ctx_for(deps, task, task.subject_id), task.subject_id)


@handler("outreach.draft")
async def outreach_draft(deps: Deps, task: Task) -> dict:
    return await outreach.create_draft(deps, ctx_for(deps, task), task.campaign_id, task.subject_id)


@handler("campaign.finalize")
async def campaign_finalize(deps: Deps, task: Task) -> dict:
    await set_campaign_state(deps, task.campaign_id, "completed", stage="completed")
    return {}


STAGE_TASKS = {"discovery": ("discovery.page", "discovery.serp", "verify.prospect", "campaign.start"), "research": ("research.prospect",),
               "qualification": ("qualify.campaign",), "build": ("site.build",), "deploy": ("site.deploy",),
               "outreach_draft": ("outreach.draft",)}


async def advance(deps: Deps, campaign_id: UUID) -> str | None:
    """Fan-in: when no work is in flight for the campaign, enqueue the next stage (idempotently)."""
    db = deps.db
    c = await _campaign(deps, campaign_id)
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
