"""Agent 6: Qualification. Pure deterministic Python, versioned rules, explained components.
Unknown evidence never earns full points. Google ratings/reviews are never inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

DEFAULT_RULES = {
    "version": 1,
    "min_qualified_total": 60,
    "website_opportunity": {"max": 35, "VERIFIED_NO_WEBSITE": 35, "broken": 30, "outdated": 20, "working": 0, "unknown": 0},
    "verified_active": {"max": 20, "name_and_phone": 20, "name_only": 10, "none": 0},
    "quality_opportunity": {"max": 15, "VERIFIED_NO_WEBSITE": 15, "broken": 15, "outdated": 10, "working": 0, "unknown": 0},
    "contactability": {"max": 10, "public_email": 10, "phone_only": 6, "none": 0},
    "business_relevance": {"max": 10, "category_and_services": 10, "category_only": 5},
    "commercial_potential": {"max": 10, "per_service": 2, "service_cap": 6, "service_area": 4},
}


@dataclass(frozen=True)
class Inputs:
    website_status: str | None
    site_health: str | None
    has_verified_name: bool
    has_verified_phone: bool
    has_public_email: bool
    category_matches: bool
    service_count: int
    has_service_area: bool

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()


def score(inp: Inputs, rules: dict = DEFAULT_RULES) -> dict:
    comps = []

    def add(name: str, awarded: int, why: str) -> None:
        comps.append({"rule": name, "max": rules[name]["max"], "awarded": awarded, "explanation": why})

    wo = rules["website_opportunity"]
    if inp.website_status == "VERIFIED_NO_WEBSITE":
        add("website_opportunity", wo["VERIFIED_NO_WEBSITE"], "Independent checks found no website")
    elif inp.website_status == "VERIFIED_HAS_WEBSITE" and inp.site_health in ("broken", "outdated", "working"):
        add("website_opportunity", wo[inp.site_health], f"Website verified as {inp.site_health}")
    else:
        add("website_opportunity", wo["unknown"], f"Website status not established ({inp.website_status or 'unverified'}): no points for unknowns")

    va = rules["verified_active"]
    if inp.has_verified_name and inp.has_verified_phone:
        add("verified_active", va["name_and_phone"], "Name and phone confirmed on independent sources")
    elif inp.has_verified_name:
        add("verified_active", va["name_only"], "Name confirmed independently; phone not confirmed")
    else:
        add("verified_active", va["none"], "Identity not independently confirmed")

    qo = rules["quality_opportunity"]
    key = "VERIFIED_NO_WEBSITE" if inp.website_status == "VERIFIED_NO_WEBSITE" else (
        inp.site_health if inp.website_status == "VERIFIED_HAS_WEBSITE" and inp.site_health else "unknown")
    add("quality_opportunity", qo[key], {"VERIFIED_NO_WEBSITE": "No site: full build opportunity",
                                         "broken": "Broken site: rebuild opportunity", "outdated": "Outdated site: redesign opportunity",
                                         "working": "Modern working site: low opportunity", "unknown": "Unknown: no points"}[key])

    co = rules["contactability"]
    if inp.has_public_email:
        add("contactability", co["public_email"], "Public business email found (collection not prohibited)")
    elif inp.has_verified_phone:
        add("contactability", co["phone_only"], "Only a verified phone number")
    else:
        add("contactability", co["none"], "No verified contact")

    br = rules["business_relevance"]
    if inp.category_matches and inp.service_count > 0:
        add("business_relevance", br["category_and_services"], "Matches campaign niche with verified services")
    elif inp.category_matches:
        add("business_relevance", br["category_only"], "Matches campaign niche; no verified services yet")
    else:
        add("business_relevance", 0, "Niche not confirmed")

    cp = rules["commercial_potential"]
    pts = min(cp["service_cap"], cp["per_service"] * inp.service_count) + (cp["service_area"] if inp.has_service_area else 0)
    add("commercial_potential", min(pts, cp["max"]), f"{inp.service_count} verified services"
        + ("; verified service area" if inp.has_service_area else ""))

    total = sum(c["awarded"] for c in comps)
    return {"total": total, "components": comps, "qualified": total >= rules["min_qualified_total"],
            "rule_set_version": rules["version"], "inputs_hash": inp.digest()}


async def gather_inputs(db, prospect_id, niche: str) -> Inputs:
    wv = await db.fetchrow("""select status, site_health from website_verifications where prospect_id = $1
                              order by checked_at desc limit 1""", prospect_id)
    facts = await db.fetch("select fact_key from research_facts where prospect_id = $1 and status = 'verified'", prospect_id)
    keys = [f["fact_key"] for f in facts]
    email = await db.fetchval("""select count(*) from contacts where prospect_id = $1 and contact_type = 'email'
                                 and is_public_business_contact and not harvesting_prohibited and verification = 'verified'""",
                              prospect_id)
    cat = await db.fetchval("select business_category from prospects where id = $1", prospect_id)
    return Inputs(wv["status"] if wv else None, wv["site_health"] if wv else None, "business_name" in keys,
                  "phone" in keys, bool(email), (cat or "").lower() == niche.lower(),
                  keys.count("service"), "service_area" in keys)
