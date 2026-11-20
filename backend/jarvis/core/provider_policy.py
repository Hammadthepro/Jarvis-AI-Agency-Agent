"""Provider-specific use policy. Each (provider, purpose) pair is decided on its own terms; a restriction on one provider
never spreads to another.

* Google Places API (New): prospect selection stays BLOCKED until the written approval described in
  jarvis.core.compliance is recorded (unchanged).
* SerpApi: prospect discovery (its Google Maps engine) and search verification are permitted uses of a provider this
  project chose. SerpApi's terms make lawful use the customer's responsibility and, on its larger plans, SerpApi
  assumes liability for collecting public search data ("U.S. Legal Shield"; not on Free/Starter/Developer plans).
  JARVIS records what it used and where every fact came from. An administrator can disable SerpApi in Integrations.
* Firecrawl: retrieving and analysing a business's public website, within robots.txt and site-terms checks
  (jarvis.security.robots).
* Manual import: records the operator attests they have the right to use.
* Licensed business data: no provider integrated.

This module decides availability; it never records an approval."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from jarvis.core.compliance import gate_status


@dataclass(frozen=True)
class Rule:
    provider: str
    purpose: str
    label: str
    basis: str
    credential: str | None = None
    approval_scope: str | None = None     # compliance gate that must be open
    available: bool = True                # False: not integrated at all


RULES: dict[tuple[str, str], Rule] = {(r.provider, r.purpose): r for r in [
    Rule("google_places", "prospect_discovery", "Google Places API (New)",
         "Google Maps Platform terms for Places API (New); prospect selection for outreach needs written approval from "
         "Google or a written legal opinion covering the intended use.",
         credential="GOOGLE_PLACES_API_KEY", approval_scope="google_places_prospect_selection"),
    Rule("serp_api", "prospect_discovery", "SerpApi (Google Maps results)",
         "SerpApi Terms of Service: lawful use is the customer's responsibility; SerpApi's U.S. Legal Shield (larger "
         "plans only) covers the collection of public search data. JARVIS keeps the source and retrieval time of "
         "every listing it uses.", credential="SERPAPI_API_KEY"),
    Rule("serp_api", "website_verification", "SerpApi (Google search results)",
         "SerpApi Terms of Service; independent evidence for whether a business has a website.", credential="SERPAPI_API_KEY"),
    Rule("firecrawl", "website_retrieval", "Firecrawl",
         "Firecrawl terms; public business websites only, subject to robots.txt and site-terms checks.",
         credential="FIRECRAWL_API_KEY"),
    Rule("manual_import", "prospect_discovery", "Imported business list",
         "Records the operator attests they have the right to use."),
    Rule("licensed_business_data", "prospect_discovery", "Licensed business data",
         "No licensed business-data provider is integrated or approved.", available=False),
]}


def configured(deps, provider: str) -> bool:
    client = {"google_places": getattr(deps, "places", None), "serp_api": getattr(deps, "serp", None),
              "firecrawl": getattr(deps, "firecrawl", None)}.get(provider)
    return True if client is None else bool(client.configured)


async def evaluate(conn, deps, workspace_id: UUID, provider: str, purpose: str) -> dict:
    """{allowed, provider, purpose, label, reason, basis, fix}. `reason` is specific to this provider and purpose."""
    from jarvis.core.integrations import DISABLED
    rule = RULES.get((provider, purpose))
    if rule is None:
        return {"allowed": False, "provider": provider, "purpose": purpose, "label": provider,
                "reason": f"{provider} is not a permitted source for {purpose.replace('_', ' ')}.", "basis": None, "fix": None}
    out = {"provider": provider, "purpose": purpose, "label": rule.label, "basis": rule.basis}
    if not rule.available:
        return {**out, "allowed": False, "reason": rule.basis, "fix": None}
    if rule.approval_scope:
        st = await gate_status(conn, workspace_id, rule.approval_scope)
        if not st["open"]:
            return {**out, "allowed": False, "restricted": True,
                    "reason": f"{rule.label} is restricted for {purpose.replace('_', ' ')}: {st['reason']}",
                    "fix": "Record the written approval in Settings > Compliance, or use another permitted source."}
    if provider in DISABLED:
        return {**out, "allowed": False, "reason": f"{rule.label} is disabled in Integrations.",
                "fix": "An administrator can re-enable it in Integrations."}
    if rule.credential and not configured(deps, provider):
        return {**out, "allowed": False, "not_configured": True, "reason": f"{rule.label} is not configured ({rule.credential}).",
                "fix": f"Add the {rule.label} key in Integrations."}
    return {**out, "allowed": True, "reason": None, "fix": None}


async def discovery_options(conn, deps, workspace_id: UUID) -> list[dict]:
    """Every discovery source with its own status, for the conversation and the UI."""
    return [await evaluate(conn, deps, workspace_id, p, "prospect_discovery")
            for p in ("serp_api", "google_places", "manual_import", "licensed_business_data")]
