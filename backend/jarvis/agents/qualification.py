"""Agent 6: Qualification (rule set v2). Deterministic Python: no model decides a score.

Seven separate dimensions, each 0-100 with its own explanation:
  website_opportunity  what the website check established (no website found, confirmed broken, outdated, working, unknown)
  identity             business name and phone confirmed, and by how many INDEPENDENT sources
  commercial_potential verified services and service area (evidence that there is a business to sell a website to)
  relevance            the business really is the requested kind AND offers services a website would present
  contactability       a public business email or a verified phone
  evidence_confidence  how many independent sources stand behind the facts, and how sure the website check is
  readiness            the overall prospect readiness (weighted), CAPPED unless every gate passes

Gates (a strong website opportunity can never compensate for missing evidence):
  qualified       opportunity >= 60, identity >= 60 (two independent sources), relevance >= 50, commercial >= 30, contactability >= 60,
                  evidence_confidence >= 50
  needs_research  the website status is inconclusive, or identity / evidence / commercial evidence is insufficient
  not_a_fit       the business already has a working, modern website, or is not the requested kind of business
Anything not `qualified` has readiness capped at 59. Google ratings and review counts are NEVER inputs (they may be
shown as information)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_RULES = {
    "version": 2,
    "weights": {"website_opportunity": 25, "identity": 20, "commercial_potential": 15, "relevance": 15,
                "contactability": 10, "evidence_confidence": 15},
    "gates": {"website_opportunity": 60, "identity": 60, "relevance": 50, "commercial_potential": 30, "contactability": 60,
              "evidence_confidence": 50},
    "cap_when_not_qualified": 59,
}
OPPORTUNITY = {"NO_WEBSITE_FOUND": (100, "Independent checks found no website: full build opportunity"),
               "WEBSITE_BROKEN_CONFIRMED": (90, "Website confirmed broken on two checks: rebuild opportunity"),
               "outdated": (60, "Website works but is outdated: redesign opportunity"),
               "working": (0, "Modern working website: little opportunity")}
INCONCLUSIVE = {"WEBSITE_BLOCKED": "the site blocked our check", "WEBSITE_RATE_LIMITED": "the site rate-limited our check",
                "WEBSITE_AUTH_REQUIRED": "the site requires a login", "WEBSITE_FOUND": "a likely site was found but not confirmed",
                "NO_WEBSITE_LISTED": "no website was listed and the search was not conclusive",
                "WEBSITE_UNKNOWN": "the website check was inconclusive"}


@dataclass(frozen=True)
class Inputs:
    website_check: str | None            # WEBSITE_CHECKS value (derived for older verifications)
    site_health: str | None
    website_confidence: float
    name_sources: int                    # independent sources confirming the business name
    phone_sources: int                   # independent sources confirming the phone number
    independent_sources: int             # distinct independent sources behind all verified facts
    has_public_email: bool
    has_verified_phone: bool
    category_matches: bool
    service_count: int
    has_service_area: bool
    conflicting_facts: int

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()


def legacy_check(status: str | None, health: str | None) -> str | None:
    """website_check for verifications recorded before it existed."""
    if status == "VERIFIED_NO_WEBSITE":
        return "NO_WEBSITE_FOUND"
    if status == "VERIFIED_HAS_WEBSITE":
        return "WEBSITE_BROKEN_CONFIRMED" if health == "broken" else "WEBSITE_WORKING"
    return "WEBSITE_UNKNOWN" if status else None


def score(inp: Inputs, rules: dict = DEFAULT_RULES) -> dict:
    if (rules or {}).get("version", 1) < 2:
        rules = DEFAULT_RULES                  # v1 rule sets are superseded (they let opportunity compensate)
    dims: dict[str, tuple[int, str]] = {}
    missing: list[str] = []
    reasons: list[str] = []

    # 1. Website opportunity
    chk = inp.website_check
    if chk in ("NO_WEBSITE_FOUND", "WEBSITE_BROKEN_CONFIRMED"):
        dims["website_opportunity"] = OPPORTUNITY[chk]
    elif chk in ("WEBSITE_WORKING", "WEBSITE_REDIRECTED"):
        dims["website_opportunity"] = OPPORTUNITY["outdated" if inp.site_health == "outdated" else "working"]
    else:
        why = INCONCLUSIVE.get(chk or "WEBSITE_UNKNOWN", "the website has not been checked")
        dims["website_opportunity"] = (0, f"Unknown: {why}; no points for unknowns")
        missing.append(f"a conclusive website check ({why})")

    # 2. Identity
    if inp.name_sources >= 2 and inp.phone_sources >= 1:
        dims["identity"] = (100, f"Name confirmed by {inp.name_sources} independent sources, phone confirmed")
    elif inp.name_sources >= 1 and inp.phone_sources >= 1:
        dims["identity"] = (50, "Name and phone come from a single source; not independently confirmed")
        missing.append("a second, independent source confirming the business")
    elif inp.name_sources >= 1:
        dims["identity"] = (25, "Only the business name is known")
        missing.append("a verified phone number")
    else:
        dims["identity"] = (0, "Business identity not verified")
        missing.append("a verified business name")
    if inp.conflicting_facts:
        dims["identity"] = (max(0, dims["identity"][0] - 40), dims["identity"][1] + f"; {inp.conflicting_facts} conflicting fact(s)")
        missing.append("resolution of conflicting business information")

    # 3. Commercial potential (no ratings, no reviews)
    pts = min(60, 30 * inp.service_count) + (20 if inp.has_service_area else 0) + (20 if inp.service_count >= 3 else 0)
    dims["commercial_potential"] = (min(pts, 100), f"{inp.service_count} verified service(s)"
                                    + ("; verified service area" if inp.has_service_area else ""))
    if inp.service_count == 0:
        missing.append("verified services (what the business sells)")

    # 4. Relevance to website-development work
    if inp.category_matches and inp.service_count > 0:
        dims["relevance"] = (100, "The requested kind of business, with services a website would present")
    elif inp.category_matches:
        dims["relevance"] = (50, "Category matches the request; services not verified yet")
    else:
        dims["relevance"] = (0, "Not confirmed to be the requested kind of business")

    # 5. Contactability
    if inp.has_public_email:
        dims["contactability"] = (100, "Public business email (collection not prohibited)")
    elif inp.has_verified_phone:
        dims["contactability"] = (60, "Verified phone number only")
    else:
        dims["contactability"] = (0, "No verified way to contact the business")
        missing.append("a verified phone number or public business email")

    # 6. Evidence confidence: independent sources, and how sure the website check is
    src = {0: 0, 1: 30, 2: 65}.get(inp.independent_sources, 90)
    conf = round(0.75 * src + 0.25 * 100 * max(0.0, min(1.0, inp.website_confidence)))
    dims["evidence_confidence"] = (conf, f"{inp.independent_sources} independent source(s); website check confidence "
                                         f"{inp.website_confidence:.2f}")
    if inp.independent_sources < 2:
        missing.append("evidence from more than one independent source")

    w = rules["weights"]
    readiness = round(sum(dims[k][0] * w[k] for k in w) / sum(w.values()))
    gates = rules["gates"]
    failed = [k for k, g in gates.items() if dims[k][0] < g]
    if chk in ("WEBSITE_WORKING", "WEBSITE_REDIRECTED") and inp.site_health != "outdated":
        status = "not_a_fit"
        reasons.append("Already has a working, modern website")
    elif dims["relevance"][0] == 0:
        status = "not_a_fit"
        reasons.append("Not confirmed to be the requested kind of business")
    elif failed:
        status = "needs_research"
        reasons += [f"{k.replace('_', ' ')} {dims[k][0]} below {gates[k]}" for k in failed]
    else:
        status = "qualified"
        reasons.append("Every dimension meets its threshold")
    if status != "qualified":
        readiness = min(readiness, rules["cap_when_not_qualified"])
    comps = [{"rule": k, "max": 100, "awarded": v[0], "explanation": v[1]} for k, v in dims.items()]
    comps.append({"rule": "readiness", "max": 100, "awarded": readiness, "explanation": f"{status.replace('_', ' ')}"})
    return {"total": readiness, "components": comps, "qualified": status == "qualified", "status": status,
            "evidence_confidence": dims["evidence_confidence"][0], "missing": list(dict.fromkeys(missing)),
            "reasons": reasons, "rule_set_version": rules["version"], "inputs_hash": inp.digest()}


def _independence(ind: str | None, source_type: str, source_ref: str) -> str:
    if ind:
        return ind
    host = (urlsplit(source_ref).hostname or "") if "://" in (source_ref or "") else ""
    parts = host.lower().split(".")
    return f"web:{'.'.join(parts[-2:])}" if len(parts) >= 2 else f"{source_type}:{(source_ref or '')[:60]}"


async def gather_inputs(db, prospect_id, niche: str) -> Inputs:
    wv = await db.fetchrow("""select status::text status, site_health, website_check, confidence from website_verifications
                              where prospect_id = $1 order by checked_at desc limit 1""", prospect_id)
    facts = await db.fetch(
        """select f.fact_key, f.status::text status, e.independence_key, e.source_type::text source_type, e.source_ref
           from research_facts f left join evidence e on e.id = f.evidence_id where f.prospect_id = $1""", prospect_id)
    verified = [f for f in facts if f["status"] == "verified"]
    src = lambda key: len({_independence(f["independence_key"], f["source_type"] or "", f["source_ref"] or "")  # noqa: E731
                           for f in verified if f["fact_key"] == key})
    independent = {_independence(f["independence_key"], f["source_type"] or "", f["source_ref"] or "") for f in verified
                   if f["fact_key"] in ("business_name", "phone", "service", "service_area", "city", "state")}
    keys = [f["fact_key"] for f in verified]
    email = await db.fetchval("""select count(*) from contacts where prospect_id = $1 and contact_type = 'email'
                                 and is_public_business_contact and not harvesting_prohibited and verification = 'verified'""",
                              prospect_id)
    cat = await db.fetchval("select business_category from prospects where id = $1", prospect_id)
    chk = (wv["website_check"] or legacy_check(wv["status"], wv["site_health"])) if wv else None
    return Inputs(chk, wv["site_health"] if wv else None, float(wv["confidence"]) if wv else 0.0,
                  src("business_name"), src("phone"), len(independent), bool(email), "phone" in keys,
                  (cat or "").lower() == niche.lower(), keys.count("service"), "service_area" in keys,
                  sum(1 for f in facts if f["status"] == "conflicting"))
