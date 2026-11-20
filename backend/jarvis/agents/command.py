"""Agent 1: Command agent. Natural language -> validated CampaignParameters or clarification questions.

Two real parsers (recorded as `parser`):
* deterministic: grammar for the common "Find N <niche> in <City[, ST]> [criteria]" phrasing
* claude: Haiku structured output for everything else (NOT_CONFIGURED if no API key)
Both outputs pass the same deterministic validation. Ratings are never rounded or invented."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from jarvis.core.errors import ValidationFailed

WebsiteFilter = Literal["NO_WEBSITE", "WEBSITE_BROKEN", "WEBSITE_WORKING_OUTDATED", "ANY"]
Stage = Literal["discovery", "verification", "research", "qualification", "build", "qa", "deploy", "outreach_draft"]
ALL_STAGES: list[str] = ["discovery", "verification", "research", "qualification", "build", "qa", "deploy", "outreach_draft"]

US_STATES = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
             "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
             "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
CITY_STATE = {"houston": "TX", "dallas": "TX", "austin": "TX", "san antonio": "TX", "fort worth": "TX", "el paso": "TX",
              "phoenix": "AZ", "tucson": "AZ", "mesa": "AZ", "atlanta": "GA", "denver": "CO", "chicago": "IL",
              "los angeles": "CA", "san diego": "CA", "san jose": "CA", "san francisco": "CA", "sacramento": "CA",
              "new york": "NY", "brooklyn": "NY", "philadelphia": "PA", "pittsburgh": "PA", "miami": "FL",
              "orlando": "FL", "tampa": "FL", "jacksonville": "FL", "seattle": "WA", "portland": "OR",
              "las vegas": "NV", "boston": "MA", "nashville": "TN", "memphis": "TN", "charlotte": "NC",
              "raleigh": "NC", "columbus": "OH", "cleveland": "OH", "cincinnati": "OH", "detroit": "MI",
              "indianapolis": "IN", "minneapolis": "MN", "kansas city": "MO", "st. louis": "MO", "omaha": "NE",
              "oklahoma city": "OK", "tulsa": "OK", "albuquerque": "NM", "salt lake city": "UT", "boise": "ID",
              "louisville": "KY", "milwaukee": "WI", "baltimore": "MD", "washington": "DC", "new orleans": "LA",
              "birmingham": "AL", "richmond": "VA", "virginia beach": "VA", "honolulu": "HI", "anchorage": "AK"}
NICHES = {"plumber": "plumbing", "plumbers": "plumbing", "plumbing": "plumbing", "dentist": "dentist",
          "dentists": "dentist", "dental": "dentist", "roofer": "roofing", "roofers": "roofing", "roofing": "roofing",
          "electrician": "electrician", "electricians": "electrician", "hvac": "hvac", "landscaper": "landscaping",
          "landscapers": "landscaping", "landscaping": "landscaping", "bakery": "bakery", "bakeries": "bakery",
          "salon": "hair salon", "salons": "hair salon", "barber": "barber", "barbers": "barber",
          "auto repair": "auto repair", "mechanic": "auto repair", "mechanics": "auto repair",
          "pest control": "pest control", "lawyer": "lawyer", "lawyers": "lawyer", "attorney": "lawyer",
          "chiropractor": "chiropractor", "chiropractors": "chiropractor", "cleaning": "cleaning service",
          "cleaners": "cleaning service", "painter": "painting", "painters": "painting", "restaurant": "restaurant",
          "restaurants": "restaurant", "cafe": "cafe", "cafes": "cafe", "florist": "florist", "florists": "florist",
          "locksmith": "locksmith", "locksmiths": "locksmith", "contractor": "general contractor",
          "contractors": "general contractor", "handyman": "handyman", "veterinarian": "veterinarian", "vets": "veterinarian"}
SERVICE_AREA_NICHES = {"plumbing", "roofing", "electrician", "hvac", "landscaping", "pest control", "cleaning service",
                       "painting", "locksmith", "general contractor", "handyman"}
FILLER = {"businesses", "business", "companies", "company", "shops", "shop", "services", "service", "firms",
          "local", "small", "the"}


class Location(BaseModel):
    city: str | None = None
    state: str | None = None
    country: Literal["US"] = "US"
    nationwide: bool = False          # "anywhere in the US": discovery spreads across major US metros

    @field_validator("state")
    @classmethod
    def _state(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.upper()
        if v not in US_STATES:
            raise ValueError(f"unknown US state code {v}")
        return v

    @model_validator(mode="after")
    def _some(self) -> Location:
        if self.nationwide:
            self.city = self.state = None
        elif not (self.city or self.state):
            raise ValueError("location needs a city, a state, or nationwide")
        return self

    def label(self) -> str:
        return "United States" if self.nationwide else ", ".join(p for p in (self.city, self.state) if p)


class CampaignParameters(BaseModel):
    niche: str = Field(min_length=2, max_length=80)
    location: Location
    target_count: int = Field(ge=1, le=1000)
    # SerpApi is the default discovery source. Google Places API (New) is only used when asked for explicitly, and stays
    # blocked until its written approval is recorded (jarvis.core.provider_policy).
    discovery_provider: Literal["serp_api", "google_places", "manual_import", "licensed_business_data"] = "serp_api"
    minimum_google_rating: Decimal | None = None
    missing_rating_policy: Literal["exclude", "include_for_review"] | None = None
    website_filter: list[WebsiteFilter] = Field(default_factory=lambda: ["ANY"])
    include_service_area_businesses: bool = True
    stages: list[Stage] = Field(default_factory=lambda: ["discovery", "verification", "research", "qualification"])
    sites_to_build: int = Field(0, ge=0, le=50)

    @field_validator("minimum_google_rating", mode="before")
    @classmethod
    def _rating(cls, v):
        if v is None:
            return None
        d = Decimal(str(v))
        if not (Decimal("0.5") <= d <= Decimal("5.0")) or (d * 2) % 1 != 0:
            raise ValueError("minimum_google_rating must be 0.5-5.0 in 0.5 steps")
        return d

    @model_validator(mode="after")
    def _rules(self) -> CampaignParameters:
        if self.minimum_google_rating is not None and self.discovery_provider not in ("google_places", "serp_api"):
            raise ValueError("a minimum rating is only available with listing providers (serp_api, google_places)")
        if self.minimum_google_rating is None and self.missing_rating_policy is not None:
            raise ValueError("missing_rating_policy is only valid when minimum_google_rating is set")
        if "ANY" in self.website_filter and len(self.website_filter) > 1:
            raise ValueError("website_filter ANY cannot be combined with other values")
        return self

    def require_complete(self) -> None:
        if self.minimum_google_rating is not None and self.missing_rating_policy is None:
            raise ValidationFailed("missing_rating_policy is required when minimum_google_rating is set",
                                   details={"field": "missing_rating_policy", "options": ["exclude", "include_for_review"]})


class ParseResult(BaseModel):
    parser: Literal["deterministic", "claude"] | None
    draft: dict
    questions: list[dict]
    parameters: CampaignParameters | None = None


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "twenty": 20, "fifty": 50, "hundred": 100}


def _rating_from_text(t: str) -> tuple[str | None, str | None]:
    """Returns (rating_string, issue). issue in {'ambiguous_above', None}."""
    m = re.search(r"(?:rating|rated|stars?)\s+(?:of\s+)?(?:above|over|more than|greater than)\s+(\d(?:\.\d+)?)", t)
    if m:
        return m.group(1), "ambiguous_above"
    pats = [r"(?:at least|minimum(?: google)? rating of|min(?:imum)? rating(?: of)?|rating of at least)\s+(?:a\s+)?(-?\d(?:\.\d+)?)",
            r"rated\s+(?:at least\s+)?(-?\d(?:\.\d+)?)\s*(?:stars?\s*)?(?:or (?:higher|better|above|more)|\+|and up)",
            r"(-?\d(?:\.\d+)?)\s*\+\s*stars?", r"(-?\d(?:\.\d+)?)[- ]stars?\s+(?:and up|or higher|or better)",
            r"(?:google )?rating\s+(?:of\s+)?(-?\d(?:\.\d+)?)", r"rated\s+(-?\d(?:\.\d+)?)"]
    for p in pats:
        m = re.search(p, t)
        if m:
            return m.group(1), None
    if re.search(r"\brat(?:ing|ed)\b.*\b(?:ish|good|high|decent)\b", t):
        return "unparseable", None
    return None, None


def deterministic_parse(text: str) -> ParseResult:
    t = " ".join(text.strip().split())
    low = t.lower()
    draft: dict = {"discovery_provider": "serp_api"}
    questions: list[dict] = []

    if re.search(r"\b(?:google places|places api)\b", low):    # only when explicitly asked for (it is approval-gated)
        draft["discovery_provider"] = "google_places"
    m = re.search(r"\b(?:find|get|search for|look for|discover)\s+(?:me\s+)?(\d{1,5}|" + "|".join(_NUM_WORDS) + r")\b\s+(.+?)\s+in\s+", low)
    m2 = None if m else re.search(r"\b(?:find|get|search for|look for|discover)\s+(?:me\s+)?(.+?)\s+in\s+", low)
    if m:
        n = m.group(1)
        draft["target_count"] = int(n) if n.isdigit() else _NUM_WORDS[n]
        niche_phrase = m.group(2)
    elif m2:
        niche_phrase = m2.group(1)
    else:
        mm = re.search(r"\b(?:find|get)\s+(\d{1,5})\b\s+(.+?)(?:\s+(?:with|that|rated|who|which)\b|[.?!]|$)", low)
        niche_phrase = mm.group(2) if mm else None
        if mm:
            draft["target_count"] = int(mm.group(1))
    if niche_phrase:
        words = [w for w in re.split(r"\s+", niche_phrase) if w not in FILLER]
        phrase = " ".join(words).strip()
        niche = NICHES.get(phrase) or next((NICHES[w] for w in words if w in NICHES), None) or (phrase or None)
        if niche:
            draft["niche"] = niche
            draft["include_service_area_businesses"] = niche in SERVICE_AREA_NICHES or niche not in NICHES.values()

    loc = re.search(r"\bin\s+([a-z][a-z .'-]*?)(?:,\s*([a-z]{2}))?(?=\s+(?:with|that|who|which|rated|having|without|and|"
                    r"don't|do not|lacking|having|using|via|through|from|on)\b|[,.?!]\s|[.?!]|$)", low)
    if loc:
        city = loc.group(1).strip()
        state = (loc.group(2) or "").upper() or CITY_STATE.get(city)
        if city.upper() in US_STATES and not loc.group(2):
            draft["location"] = {"state": city.upper()}
        else:
            draft["location"] = {"city": city.title(), "state": state}
            if state is None:
                questions.append({"field": "location.state", "prompt": f"Which US state is {city.title()} in?"})

    r, issue = _rating_from_text(low)
    if r is not None:
        if r == "unparseable":
            questions.append({"field": "minimum_google_rating", "prompt": "What minimum Google rating should I use? "
                              "Google filters in 0.5 steps (e.g. 3.5, 4.0)."})
        elif issue == "ambiguous_above":
            d = Decimal(r)
            nxt = (d * 2 // 1 + 1) / 2
            questions.append({"field": "minimum_google_rating", "prompt": f"'Above {r}': use {d} inclusive or {nxt}?",
                              "options": [float(d), float(nxt)]})
        else:
            d = Decimal(r)
            if not (Decimal("0.5") <= d <= Decimal("5.0")):
                questions.append({"field": "minimum_google_rating",
                                  "prompt": f"{r} isn't a valid Google rating threshold. Use a value from 0.5 to 5.0 in 0.5 steps."})
            elif (d * 2) % 1 != 0:
                lo, hi = (d * 2 // 1) / 2, (d * 2 // 1 + 1) / 2
                questions.append({"field": "minimum_google_rating",
                                  "prompt": f"Google filters in 0.5 steps. Use {hi} (stricter) or {lo} (looser)?",
                                  "options": [float(hi), float(lo)]})
            else:
                draft["minimum_google_rating"] = str(d)

    wf: list[str] = []
    if re.search(r"(no|without(?: a)?|don't have|do not have|lacking|missing)( any)? (a )?websites?", low):
        wf.append("NO_WEBSITE")
    if re.search(r"broken websites?|websites? (that )?(are )?(down|broken)", low):
        wf.append("WEBSITE_BROKEN")
    if re.search(r"outdated websites?|old websites?", low):
        wf.append("WEBSITE_WORKING_OUTDATED")
    if wf:
        draft["website_filter"] = wf

    b = re.search(r"build (?:(?:personali[sz]ed )?(?:web)?sites?|websites) for (?:the )?(?:top )?(\d{1,2})", low)
    # "prepare two redesign previews", "build 3 preview websites", "create 2 personalised sites"
    b2 = None if b else re.search(r"\b(?:prepare|build|create|generate|make|design)\s+(\d{1,2}|" + "|".join(_NUM_WORDS) +
                                  r")\s+(?:(?:new|redesign(?:ed)?|personali[sz]ed|preview|website|web|homepage)\s+)*"
                                  r"(?:previews?|websites?|sites?|redesigns?|mock-?ups?)\b", low)
    if b or b2:
        n = (b or b2).group(1)
        draft["sites_to_build"] = int(n) if n.isdigit() else _NUM_WORDS[n]
        draft["stages"] = ALL_STAGES
    if "missing_rating_policy" not in draft and re.search(r"(include|keep) (unrated|businesses without (a )?rating)", low):
        draft["missing_rating_policy"] = "include_for_review"
    elif re.search(r"(exclude|skip|drop) unrated", low):
        draft["missing_rating_policy"] = "exclude"

    for f, prompt in (("niche", "What type of business should I look for?"),
                      ("location", "Which city or state should I search in?"),
                      ("target_count", "How many businesses should I find (1-1000)?")):
        if f not in draft and not any(q["field"].startswith(f) for q in questions):
            questions.append({"field": f, "prompt": prompt})
    if draft.get("minimum_google_rating") and "missing_rating_policy" not in draft:
        questions.append({"field": "missing_rating_policy",
                          "prompt": "Some businesses have no Google rating. Exclude them, or keep them for manual review?",
                          "options": ["exclude", "include_for_review"], "preselect": "exclude"})
    return _finalise("deterministic", draft, questions)


def _finalise(parser: str, draft: dict, questions: list[dict]) -> ParseResult:
    params = None
    if not questions:
        try:
            params = CampaignParameters.model_validate(draft)
        except ValueError as exc:
            questions.append({"field": "_", "prompt": f"I couldn't validate that request: {exc}"})
    return ParseResult(parser=parser, draft=draft, questions=questions, parameters=params)  # type: ignore[arg-type]


CLAUDE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["understood", "niche", "city", "state", "target_count", "minimum_google_rating",
                 "rating_phrase", "website_filter", "sites_to_build"],
    "properties": {
        "understood": {"type": "boolean"},
        "niche": {"type": ["string", "null"]}, "city": {"type": ["string", "null"]},
        "state": {"type": ["string", "null"]}, "target_count": {"type": ["integer", "null"]},
        "minimum_google_rating": {"type": ["number", "null"]},
        "rating_phrase": {"type": ["string", "null"], "description": "exact words the user used about rating, or null"},
        "website_filter": {"type": "array", "items": {"enum": ["NO_WEBSITE", "WEBSITE_BROKEN", "WEBSITE_WORKING_OUTDATED"]}},
        "sites_to_build": {"type": ["integer", "null"]},
    },
}
CLAUDE_SYSTEM = (
    "You extract structured campaign parameters for a website-development agency's prospecting tool. "
    "Extract ONLY what the user explicitly said. Use null for anything not stated. Never invent a rating, "
    "count, niche or location. state is a 2-letter US code only if stated or unambiguous from a well-known city. "
    "Prompt version: command-v1.")


def from_claude(data: dict) -> ParseResult:
    draft: dict = {"discovery_provider": "serp_api"}
    questions: list[dict] = []
    if data.get("niche"):
        draft["niche"] = data["niche"].strip().lower()
    if data.get("city") or data.get("state"):
        draft["location"] = {"city": data.get("city"), "state": data.get("state")}
    if data.get("target_count"):
        draft["target_count"] = data["target_count"]
    if data.get("minimum_google_rating") is not None:
        d = Decimal(str(data["minimum_google_rating"]))
        if not (Decimal("0.5") <= d <= Decimal("5.0")) or (d * 2) % 1 != 0:
            questions.append({"field": "minimum_google_rating",
                              "prompt": f"{d} isn't a supported threshold. Use 0.5-5.0 in 0.5 steps."})
        elif not data.get("rating_phrase"):
            questions.append({"field": "minimum_google_rating", "prompt": "Did you want a minimum Google rating?"})
        else:
            draft["minimum_google_rating"] = str(d)
            questions.append({"field": "missing_rating_policy", "prompt": "Exclude unrated businesses, or keep them for review?",
                              "options": ["exclude", "include_for_review"], "preselect": "exclude"})
    if data.get("website_filter"):
        draft["website_filter"] = data["website_filter"]
    if data.get("sites_to_build"):
        draft["sites_to_build"] = data["sites_to_build"]
        draft["stages"] = ALL_STAGES
    for f, prompt in (("niche", "What type of business should I look for?"),
                      ("location", "Which city or state should I search in?"),
                      ("target_count", "How many businesses should I find (1-1000)?")):
        if f not in draft:
            questions.append({"field": f, "prompt": prompt})
    return _finalise("claude", draft, questions)


def apply_answers(draft: dict, answers: dict) -> ParseResult:
    """Merge operator answers/edits into a draft and re-validate deterministically."""
    merged = {**draft}
    for k, v in answers.items():
        if k == "location.state":
            merged.setdefault("location", {})["state"] = v
        else:
            merged[k] = v
    questions: list[dict] = []
    if merged.get("minimum_google_rating") is not None and not merged.get("missing_rating_policy"):
        questions.append({"field": "missing_rating_policy", "prompt": "Exclude unrated businesses, or keep them for review?",
                          "options": ["exclude", "include_for_review"], "preselect": "exclude"})
    for f in ("niche", "location", "target_count"):
        if not merged.get(f):
            questions.append({"field": f, "prompt": f"Please provide {f.replace('_', ' ')}."})
    return _finalise("deterministic", merged, questions)
