"""Discovery provider abstraction. A provider turns (business category, area) into pages of normalized listings with
provenance; the generic discovery stage (jarvis.workflow.engine: discovery.listings) does de-duplication, rating and
exclusion rules, storage and website verification the same way for every provider.

Providers: serp_api (Google Maps results via SerpApi), geoapify (OpenStreetMap via Geoapify). Google Places API (New)
keeps its own, compliance-gated stage (discovery.page): its content may not be stored."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from jarvis.core.errors import IntegrationState, ProviderError
from jarvis.integrations.base import CallContext
from jarvis.security.robots import registrable_domain

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
# "Anywhere in the US": large metros across every region, one page each so results are spread out.
US_METROS = [("New York", "NY"), ("Los Angeles", "CA"), ("Chicago", "IL"), ("Houston", "TX"), ("Phoenix", "AZ"),
             ("Philadelphia", "PA"), ("San Antonio", "TX"), ("San Diego", "CA"), ("Dallas", "TX"), ("Jacksonville", "FL"),
             ("Austin", "TX"), ("Columbus", "OH"), ("Charlotte", "NC"), ("Indianapolis", "IN"), ("Seattle", "WA"),
             ("Denver", "CO"), ("Nashville", "TN"), ("Oklahoma City", "OK"), ("Boston", "MA"), ("Portland", "OR"),
             ("Las Vegas", "NV"), ("Detroit", "MI"), ("Memphis", "TN"), ("Louisville", "KY"), ("Baltimore", "MD"),
             ("Milwaukee", "WI"), ("Albuquerque", "NM"), ("Atlanta", "GA"), ("Kansas City", "MO"), ("Omaha", "NE"),
             ("Miami", "FL"), ("Minneapolis", "MN"), ("Tampa", "FL"), ("New Orleans", "LA"), ("Salt Lake City", "UT"),
             ("Birmingham", "AL"), ("Boise", "ID"), ("Richmond", "VA"), ("Des Moines", "IA"), ("Little Rock", "AR")]
_ADDR = re.compile(r",\s*([^,]+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*(?:,\s*United States)?\s*$")
_GENERIC = {"llc", "inc", "co", "company", "corp", "the", "and", "of", "pllc", "pc", "group", "ltd"}


@dataclass(frozen=True)
class Target:
    label: str                 # human-readable area, e.g. "Austin, Texas, United States"
    city: str | None
    state: str | None
    nationwide: bool = False


@dataclass(frozen=True, repr=False)
class Listing:
    provider: str
    external_id: str           # provider's own id (de-duplication within a campaign; never a Google Places id)
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postcode: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    reviews: int | None = None
    category: str | None = None
    closed: bool = False
    source_ref: str = ""       # where this listing came from (search id / request)
    excerpt: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"Listing({self.provider}:{self.external_id})"


@dataclass
class Page:
    listings: list[Listing]
    has_more: bool
    note: str | None = None


def targets_for(location: dict) -> list[Target]:
    if location.get("nationwide"):
        return [Target(f"{c}, {STATE_NAMES[s]}, United States", c, s, True) for c, s in US_METROS]
    city, st = location.get("city"), location.get("state")
    if city:
        return [Target(", ".join(x for x in (city, STATE_NAMES.get(st or "", st), "United States") if x), city, st)]
    return [Target(f"{STATE_NAMES.get(st, st)}, United States", None, st)]


def address_parts(address: str | None) -> tuple[str | None, str | None, str | None]:
    m = _ADDR.search(address or "")
    return (m.group(1).strip(), m.group(2), m.group(3)) if m and m.group(2) in STATE_NAMES else (None, None, None)


# ---------------------------------------------------------------- de-duplication keys
def phone_key(phone: str | None) -> str | None:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 10 else None


def domain_key(website: str | None) -> str | None:
    if not website:
        return None
    host = urlsplit(website if "://" in website else f"https://{website}").hostname or ""
    dom = registrable_domain(host)
    return dom or None


def name_words(name: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", "")) if t not in _GENERIC}


def name_key(name: str | None, postcode: str | None, street: str | None = None) -> str | None:
    """Exact normalized name + postcode (or street): two branches of a chain, or two similar names, never collide."""
    w = " ".join(sorted(name_words(name)))
    loc = postcode or (re.sub(r"\W+", " ", street.lower()).strip() if street else None)
    return f"{w}|{loc}" if w and loc else None


def same_business(a_name: str | None, b_name: str | None) -> bool:
    """Shared phone or domain only merges two listings when their names also share a distinctive word."""
    a, b = name_words(a_name), name_words(b_name)
    return bool(a and b and (a & b))


# ---------------------------------------------------------------- providers
class SerpApiDiscovery:
    key, label = "serp_api", "SerpApi (Google Maps results)"

    def __init__(self, deps) -> None:
        self.deps = deps

    async def page(self, ctx: CallContext, niche: str, target: Target, page_no: int, state: dict) -> Page:
        zoom = 11 if target.city else 7
        res = await self.deps.serp.maps_search(ctx, niche, location=target.label, zoom=zoom, start=page_no * 20)
        out = []
        for li in res.listings:
            city, st, pc = address_parts(li.address)
            out.append(Listing("serp_api", li.listing_id, li.title, li.address, city or target.city, st or target.state, pc,
                               li.phone, li.website, li.rating, li.reviews, li.type, li.closed,
                               f"serpapi:google_maps:{res.search_id or 'n/a'}:{li.listing_id}",
                               {"title": li.title, "address": li.address, "phone": li.phone, "website": li.website,
                                "rating": li.rating, "reviews": li.reviews, "type": li.type, "open_state": li.open_state}))
        return Page(out, res.has_more)


class GeoapifyDiscovery:
    key, label = "geoapify", "Geoapify Places (OpenStreetMap)"

    def __init__(self, deps) -> None:
        self.deps = deps

    async def page(self, ctx: CallContext, niche: str, target: Target, page_no: int, state: dict) -> Page:
        from jarvis.integrations.geoapify import category_for
        client = self.deps.geoapify
        if client is None:
            raise ProviderError("geoapify", IntegrationState.NOT_CONFIGURED, "missing credential: GEOAPIFY_API_KEY")
        cat = category_for(niche)
        if not cat:
            return Page([], False, note=f"Geoapify has no category for '{niche}'")
        pid = state.get("place_id") or await client.city_place_id(ctx, target.city, target.state)
        if not pid:
            return Page([], False, note=f"Geoapify could not locate {target.label}")
        state["place_id"] = pid
        rows = await client.places(ctx, category=cat, place_id=pid, limit=20, offset=page_no * 20)
        out = []
        for g in rows:
            out.append(Listing("geoapify", g.place_id, g.name, g.address, g.city or target.city,
                               (g.state if g.state and len(g.state) == 2 else target.state), g.postcode, g.phone, g.website,
                               None, None, ",".join(g.categories[:3]) or cat, False,
                               f"geoapify:places:{pid}:{g.place_id}",
                               {"title": g.name, "address": g.address, "phone": g.phone, "website": g.website,
                                "categories": list(g.categories[:5]), "lat": g.lat, "lon": g.lon}))
        return Page(out, len(rows) >= 20)


PROVIDERS = {"serp_api": SerpApiDiscovery, "geoapify": GeoapifyDiscovery}


def provider(deps, key: str):
    return PROVIDERS[key](deps)
