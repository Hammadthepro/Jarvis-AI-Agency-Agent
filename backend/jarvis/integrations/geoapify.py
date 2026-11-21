"""Geoapify Places API (OpenStreetMap data) for prospect discovery.

* Forward geocoding turns "Anaheim, CA" into a Geoapify place id; Places then lists businesses of a category inside it.
* Geoapify's Places results often lack a website or phone (OSM data): a missing website field is ONLY "not listed".
* Free plan: 3,000 credits/day, commercial use allowed with Geoapify and data-source attribution ("Powered by Geoapify",
  "(c) OpenStreetMap contributors", ODbL). JARVIS never activates a paid plan; each request is recorded as a request."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from jarvis.config import Settings
from jarvis.core import budget
from jarvis.core.errors import IntegrationState, ProviderError, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call, http_request

PROVIDER = "geoapify"
BASE = "https://api.geoapify.com"
ATTRIBUTION = "Powered by Geoapify · © OpenStreetMap contributors (ODbL)"
# JARVIS niche -> Geoapify Places category keys (from the Geoapify Places category list). Niches without a Geoapify
# category (e.g. plumbing, roofing) cannot be discovered through Geoapify; the provider says so instead of guessing.
CATEGORIES = {
    "dentist": "healthcare.dentist", "dental": "healthcare.dentist", "veterinarian": "pet.veterinary",
    "veterinary": "pet.veterinary", "veterinary clinic": "pet.veterinary", "vet": "pet.veterinary",
    "electrician": "service.electrician", "hair salon": "service.beauty.hairdresser", "barber": "service.beauty.hairdresser",
    "beauty salon": "service.beauty", "restaurant": "catering.restaurant", "cafe": "catering.cafe",
    "lawyer": "office.lawyer", "attorney": "office.lawyer", "auto repair": "service.vehicle.repair.car",
    "florist": "commercial.florist", "bakery": "commercial.food_and_drink.bakery",
}


def category_for(niche: str) -> str | None:
    n = (niche or "").strip().lower()
    if n in CATEGORIES:
        return CATEGORIES[n]
    return next((v for k, v in CATEGORIES.items() if k in n), None)


@dataclass(frozen=True)
class GeoPlace:
    place_id: str
    name: str
    address: str | None
    street: str | None
    city: str | None
    state: str | None
    postcode: str | None
    phone: str | None
    website: str | None
    lat: float | None
    lon: float | None
    categories: tuple[str, ...]


class GeoapifyClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=20)

    @property
    def configured(self) -> bool:
        return self.settings.geoapify_api_key is not None

    def _key(self) -> str:
        if not self.settings.geoapify_api_key:
            raise not_configured(PROVIDER, "GEOAPIFY_API_KEY")
        return self.settings.geoapify_api_key.get_secret_value()

    async def _get(self, ctx: CallContext, path: str, params: dict, operation: str):
        params = {**params, "apiKey": self._key()}

        async def do() -> CallOutcome:
            resp = await http_request(PROVIDER, self.http, "GET", f"{BASE}{path}", params=params)
            data = resp.json()
            if isinstance(data, dict) and data.get("error") and not data.get("features") and not data.get("results"):
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, str(data.get("message") or data["error"])[:300])
            return CallOutcome(data, 1, budget.unit_price(PROVIDER, "request"), http_status=resp.status_code)

        return await gateway_call(ctx, provider=PROVIDER, operation=operation, unit="request", units=1,
                                  estimated_micros=budget.unit_price(PROVIDER, "request"), fn=do)

    async def city_place_id(self, ctx: CallContext, city: str | None, state: str | None) -> str | None:
        text = ", ".join(x for x in (city, state, "United States") if x)
        data = await self._get(ctx, "/v1/geocode/search", {"text": text, "type": "city" if city else "state",
                                                            "filter": "countrycode:us", "format": "json", "limit": 1},
                               "geocode.search")
        res = (data or {}).get("results") or []
        return res[0].get("place_id") if res else None

    async def places(self, ctx: CallContext, *, category: str, place_id: str, limit: int = 20, offset: int = 0) -> list[GeoPlace]:
        data = await self._get(ctx, "/v2/places", {"categories": category, "filter": f"place:{place_id}",
                                                   "limit": max(1, min(limit, 100)), "offset": max(0, offset), "lang": "en"},
                               "places.search")
        out = []
        for f in (data or {}).get("features") or []:
            p = f.get("properties") or {}
            raw = (p.get("datasource") or {}).get("raw") or {}
            contact = p.get("contact") or {}
            if not p.get("place_id") or not p.get("name"):
                continue
            out.append(GeoPlace(str(p["place_id"]), str(p["name"])[:200], p.get("formatted"),
                                " ".join(x for x in (p.get("housenumber"), p.get("street")) if x) or None,
                                p.get("city"), (p.get("state_code") or p.get("state") or "")[:20] or None, p.get("postcode"),
                                contact.get("phone") or raw.get("phone") or raw.get("contact:phone"),
                                p.get("website") or raw.get("website") or raw.get("contact:website"),
                                p.get("lat"), p.get("lon"), tuple(p.get("categories") or ())))
        return out
