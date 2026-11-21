"""Controlled comparison of discovery providers on the SAME cities, categories and result limit.

Only aggregate metrics are kept (counts, percentages, latency, failures, estimated cost): no business record from any
provider is stored by an evaluation. Google Places runs under the "google_places_technical_testing" compliance scope,
whose recorded intended use is "capped technical verification ... no results are retained beyond test assertions";
without that approval it is reported BLOCKED. A provider without credentials is reported NOT_CONFIGURED. Nothing is
estimated for a provider that did not run.

Bounded by design: at most 3 cities x 3 categories x 20 results per provider, one request per cell (plus one geocoding
request per city for Geoapify)."""

from __future__ import annotations

import time
from statistics import mean
from uuid import UUID

from jarvis.core import budget, provider_policy
from jarvis.core.compliance import gate_status
from jarvis.core.errors import JarvisError, ProviderError
from jarvis.discovery.providers import Target, address_parts, domain_key, name_key, phone_key
from jarvis.integrations.base import CallContext

MAX_CITIES, MAX_CATEGORIES, MAX_LIMIT = 3, 3, 20
PROVIDERS = ("google_places", "geoapify", "serp_api")


def validate_plan(plan: dict) -> dict:
    cities = [c for c in plan.get("cities") or [] if isinstance(c, dict) and c.get("city") and c.get("state")][:MAX_CITIES]
    cats = [str(c).strip().lower() for c in plan.get("categories") or [] if str(c).strip()][:MAX_CATEGORIES]
    if not cities or not cats:
        raise JarvisError("an evaluation needs 1-3 cities ({city, state}) and 1-3 business categories")
    return {"cities": [{"city": c["city"].strip()[:60], "state": c["state"].strip().upper()[:2]} for c in cities],
            "categories": cats, "limit": max(1, min(int(plan.get("limit") or MAX_LIMIT), MAX_LIMIT))}


async def _google(deps, ctx, cat: str, city: dict, limit: int) -> list[dict]:
    from jarvis.integrations.google_places import DISCOVERY_ENTERPRISE
    page = await deps.places.text_search(ctx, text_query=f"{cat} in {city['city']}, {city['state']}", min_rating=None,
                                         include_service_area=True, page_size=limit, mask=DISCOVERY_ENTERPRISE,
                                         scope="google_places_technical_testing")
    out = []
    for p in page.places:      # held in memory only for this computation; discarded with this function
        _c, _s, pc = address_parts(p.formatted_address)
        out.append({"phone": p.national_phone_number, "website": p.website_uri, "name": p.display_name,
                    "address": p.formatted_address, "postcode": pc, "category": p.primary_type})
    return out


async def _geoapify(deps, ctx, cat: str, city: dict, limit: int, cache: dict) -> list[dict]:
    from jarvis.integrations.geoapify import category_for
    gcat = category_for(cat)
    if not gcat:
        raise JarvisError(f"Geoapify has no category for '{cat}'")
    key = (city["city"], city["state"])
    if key not in cache:
        cache[key] = await deps.geoapify.city_place_id(ctx, city["city"], city["state"])
    if not cache[key]:
        raise JarvisError(f"Geoapify could not locate {city['city']}, {city['state']}")
    rows = await deps.geoapify.places(ctx, category=gcat, place_id=cache[key], limit=limit)
    return [{"phone": g.phone, "website": g.website, "name": g.name, "address": g.address, "postcode": g.postcode,
             "category": ",".join(g.categories)} for g in rows]


async def _serp(deps, ctx, cat: str, city: dict, limit: int) -> list[dict]:
    from jarvis.discovery.providers import STATE_NAMES, SerpApiDiscovery
    t = Target(f"{city['city']}, {STATE_NAMES.get(city['state'], city['state'])}, United States", city["city"], city["state"])
    page = await SerpApiDiscovery(deps).page(ctx, cat, t, 0, {})
    return [{"phone": li.phone, "website": li.website, "name": li.name, "address": li.address, "postcode": li.postcode,
             "category": li.category} for li in page.listings[:limit]]


def _metrics(rows: list[dict], cat: str) -> dict:
    keys, dup = set(), 0
    for r in rows:
        k = phone_key(r["phone"]) or domain_key(r["website"]) or name_key(r["name"], r["postcode"])
        if k and k in keys:
            dup += 1
        if k:
            keys.add(k)
    n = len(rows) or 1
    word = cat.split()[0][:5]
    return {"returned": len(rows), "unique": len(keys), "duplicates": dup,
            "website_listed_pct": round(100 * sum(1 for r in rows if r["website"]) / n, 1),
            "phone_listed_pct": round(100 * sum(1 for r in rows if r["phone"]) / n, 1),
            # "complete" = has a street number and a postcode; correctness of the address is NOT verified here
            "address_complete_pct": round(100 * sum(1 for r in rows if r["postcode"] and any(ch.isdigit() for ch in (r["address"] or "").split(",")[0])) / n, 1),
            "category_match_pct": round(100 * sum(1 for r in rows if word in (r["category"] or "").lower()) / n, 1),
            "useful": sum(1 for r in rows if r["phone"] or r["website"])}


async def run_evaluation(deps, workspace_id: UUID, evaluation_id: UUID, providers: list[str], plan: dict) -> dict:
    db = deps.db
    results: dict = {"plan": plan, "providers": {}, "overlap": {}, "kind": "real provider requests (aggregates only)"}
    keysets: dict[str, set] = {}
    async with db.acquire() as conn:
        for p in providers:
            if p == "google_places":
                st = await gate_status(conn, workspace_id, "google_places_technical_testing")
                pol = {"allowed": st["open"] and deps.places.configured,
                       "reason": None if st["open"] and deps.places.configured else
                       (st["reason"] if not st["open"] else "Google Places API key NOT_CONFIGURED")}
                state = "BLOCKED" if not st["open"] else ("NOT_CONFIGURED" if not deps.places.configured else None)
            else:
                pol = await provider_policy.evaluate(conn, deps, workspace_id, p, "prospect_discovery")
                state = None if pol["allowed"] else ("NOT_CONFIGURED" if pol.get("not_configured") else "BLOCKED")
            if state:
                results["providers"][p] = {"status": state, "reason": pol["reason"]}
    geo_cache: dict = {}
    for p in providers:
        if p in results["providers"]:
            continue
        cells, lat, failures, all_rows = [], [], [], []
        ctx = CallContext(db, workspace_id)
        for city in plan["cities"]:
            for cat in plan["categories"]:
                t0 = time.monotonic()
                try:
                    rows = await (_google(deps, ctx, cat, city, plan["limit"]) if p == "google_places" else
                                  _geoapify(deps, ctx, cat, city, plan["limit"], geo_cache) if p == "geoapify" else
                                  _serp(deps, ctx, cat, city, plan["limit"]))
                except (ProviderError, JarvisError) as exc:
                    failures.append({"city": city["city"], "category": cat,
                                     "error": getattr(exc, "state", None) and exc.state.value or exc.code,
                                     "message": exc.message[:200]})
                    continue
                ms = int((time.monotonic() - t0) * 1000)
                lat.append(ms)
                cells.append({"city": city["city"], "category": cat, "latency_ms": ms, **_metrics(rows, cat)})
                all_rows += rows
        keysets[p] = {k for r in all_rows for k in (phone_key(r["phone"]), domain_key(r["website"])) if k}
        tot = _metrics(all_rows, plan["categories"][0]) if all_rows else {}
        unit = {"google_places": ("google_places", "text_search_enterprise"), "geoapify": ("geoapify", "request"),
                "serp_api": ("serp_api", "search")}[p]
        requests = len(cells) + len(failures) + (len(plan["cities"]) if p == "geoapify" else 0)
        cost = requests * budget.unit_price(*unit)
        results["providers"][p] = {
            "status": "COMPLETED" if cells else "FAILED", "requests": requests, "failures": failures,
            "latency_ms": {"avg": int(mean(lat)), "max": max(lat)} if lat else None, "cells": cells, "totals": tot,
            "estimated_cost_usd": round(cost / 1e6, 4),
            "cost_per_useful_usd": round(cost / 1e6 / tot["useful"], 4) if tot.get("useful") else None,
            "cost_basis": "local price table; Geoapify within its free plan is $0" if p == "geoapify" else "local price table"}
    done = [p for p in providers if p in keysets]
    for i, a in enumerate(done):
        for b in done[i + 1:]:
            results["overlap"][f"{a}~{b}"] = {"shared_phone_or_domain": len(keysets[a] & keysets[b]),
                                              f"only_{a}": len(keysets[a] - keysets[b]), f"only_{b}": len(keysets[b] - keysets[a])}
    await db.execute("""update provider_evaluations set status=$2, results=$3, finished_at=now() where id=$1""",
                     evaluation_id, "completed" if any(v.get("status") == "COMPLETED" for v in results["providers"].values())
                     else "failed", results)
    return results
