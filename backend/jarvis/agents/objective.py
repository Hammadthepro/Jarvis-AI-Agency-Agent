"""The owner's objective, as structured conversation state (voice and text alike).

Claude records what the owner says with `set_objective`; this module merges updates, applies sensible defaults, names
the single most important missing detail, and turns a complete objective into validated CampaignParameters. Nothing
here calls a model: the rules are deterministic and tested."""

from __future__ import annotations

from decimal import Decimal

from jarvis.agents.command import ALL_STAGES, CITY_STATE, US_STATES, CampaignParameters, Location

DEFAULT_TARGET = 10
DEFAULT_PREVIEWS = 3
# Website preference -> discovery filter. "prefer_*" keeps everyone verified and lets qualification rank businesses
# without a website (or with a broken/outdated one) first; "*_only" excludes the rest.
WEBSITE_FILTER = {"prefer_no_or_poor_website": ["ANY"], "any": ["ANY"], "no_website_only": ["NO_WEBSITE"],
                  "poor_or_no_website_only": ["NO_WEBSITE", "WEBSITE_BROKEN", "WEBSITE_WORKING_OUTDATED"],
                  "outdated_or_broken_only": ["WEBSITE_BROKEN", "WEBSITE_WORKING_OUTDATED"]}
# Building previews stops at a private preview deployment. Outreach drafting is a separate, later decision.
BUILD_STAGES = [s for s in ALL_STAGES if s != "outreach_draft"]
RESEARCH_STAGES = ["discovery", "verification", "research", "qualification"]
FIELDS = ("original_request", "task", "business_category", "area", "target_count", "minimum_rating", "unrated_policy",
          "website_preference", "previews_to_build", "exclusions", "discovery_source", "notes")


def merge(current: dict, update: dict) -> dict:
    """Earlier details are kept; only the fields given change. `original_request` is set once."""
    out = {} if update.get("new_objective") else {k: v for k, v in (current or {}).items()}
    if update.get("new_objective"):
        out["status"] = "gathering"
    for k in FIELDS:
        if k not in update:
            continue
        v = update[k]
        if k == "original_request" and out.get("original_request"):
            continue
        if k == "area" and isinstance(v, dict):
            v = {**(out.get("area") or {}), **{a: b for a, b in v.items() if b not in (None, "")}}
            if v.get("scope") == "nationwide":
                v = {"scope": "nationwide"}
            elif v.get("scope") == "state":
                v.pop("city", None)
        if k == "exclusions" and isinstance(v, list):
            v = sorted({*(out.get("exclusions") or []), *[str(x)[:120] for x in v]})
        out[k] = v
    if out.get("status") in ("proposed", "running", "done") and any(k in update for k in FIELDS if k != "original_request"):
        out["status"] = "gathering"           # changed after a proposal: it must be proposed (and approved) again
        out.pop("campaign_id", None)
    return out


def _location(area: dict | None) -> tuple[Location | None, dict | None]:
    area = area or {}
    if area.get("scope") == "nationwide":
        return Location(nationwide=True), None
    city = (area.get("city") or "").strip() or None
    st = (area.get("state") or "").strip().upper() or None
    if st and st not in US_STATES:
        return None, {"field": "area.state", "question": f"Which US state did you mean by {area.get('state')}?"}
    if city and not st:
        st = CITY_STATE.get(city.lower())
        if not st:
            return None, {"field": "area.state", "question": f"Which state is {city} in?"}
    if not (city or st):
        return None, {"field": "area", "question": "Where should I look: a city, a state, or anywhere in the US?"}
    return Location(city=city.title() if city else None, state=st), None


def resolve(obj: dict) -> dict:
    """{parameters (or None), defaults_applied, missing (the ONE next question, or None), summary}."""
    defaults: list[str] = []
    obj = obj or {}
    if not (obj.get("business_category") or "").strip():
        return {"parameters": None, "defaults_applied": [], "summary": None,
                "missing": {"field": "business_category", "question": "What kind of businesses should I focus on?"}}
    loc, q = _location(obj.get("area"))
    if q:
        return {"parameters": None, "defaults_applied": [], "missing": q, "summary": None}
    target = obj.get("target_count")
    if not target:
        target, _ = DEFAULT_TARGET, defaults.append(f"{DEFAULT_TARGET} businesses (no number given)")
    target = max(1, min(int(target), 1000))
    build = obj.get("task", "find_and_build_previews") != "find_prospects"
    previews = obj.get("previews_to_build")
    if build and not previews:
        previews = min(DEFAULT_PREVIEWS, target)
        defaults.append(f"preview websites for the top {previews} qualified prospects")
    previews = max(0, min(int(previews or 0), 50, target)) if build else 0
    rating = obj.get("minimum_rating")
    policy = None
    if rating is not None:
        d = Decimal(str(rating))
        if not (Decimal("0.5") <= d <= Decimal("5.0")):
            return {"parameters": None, "defaults_applied": defaults, "summary": None,
                    "missing": {"field": "minimum_rating", "question": f"{rating} isn't a rating I can filter by. Use 0.5 to 5?"}}
        rating = (d * 2).to_integral_value(rounding="ROUND_CEILING") / 2      # 3.7 -> 4.0 is stricter: never looser
        if rating != d:
            defaults.append(f"minimum rating {rating} (ratings filter in half-star steps)")
        policy = obj.get("unrated_policy")
        if not policy:
            policy = "include_for_review"
            defaults.append("businesses without a rating kept aside for your review")
    pref = obj.get("website_preference") or "prefer_no_or_poor_website"
    if not obj.get("website_preference"):
        defaults.append("businesses without a website, or with a poor one, ranked first")
    source = obj.get("discovery_source") or "serp_api"
    params = CampaignParameters(niche=obj["business_category"].strip().lower()[:80], location=loc, target_count=target,
                                discovery_provider=source, minimum_google_rating=rating, missing_rating_policy=policy,
                                website_filter=WEBSITE_FILTER.get(pref, ["ANY"]),
                                stages=BUILD_STAGES if previews else RESEARCH_STAGES, sites_to_build=previews)
    return {"parameters": params, "defaults_applied": defaults, "missing": None, "summary": summary(params, obj)}


def summary(p: CampaignParameters, obj: dict) -> str:
    where = "across the US" if p.location.nationwide else f"in {p.location.label()}"
    s = f"find {p.target_count} {p.niche} businesses {where}"
    if p.minimum_google_rating is not None:
        s += f" rated at least {p.minimum_google_rating}"
        s += (", keeping unrated ones for your review" if p.missing_rating_policy == "include_for_review"
              else ", leaving out unrated ones")
    pref = obj.get("website_preference") or "prefer_no_or_poor_website"
    s += {"prefer_no_or_poor_website": ", prioritising those without a website or with a poor one",
          "no_website_only": ", only businesses without a website",
          "poor_or_no_website_only": ", only businesses without a website or with a poor one",
          "outdated_or_broken_only": ", only businesses with an outdated or broken website"}.get(pref, "")
    if p.sites_to_build:
        s += (f", then build, browser-test and privately deploy {p.sites_to_build} tailored preview "
              f"website{'s' if p.sites_to_build != 1 else ''} and show them to you")
    if obj.get("exclusions"):
        s += f" (excluding: {', '.join(obj['exclusions'])})"
    return s
