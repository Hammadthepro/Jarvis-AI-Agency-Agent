"""SerpApi adapter: Google Search engine (research, independent website verification) and Google Maps engine
(prospect discovery: public listings with rating, review count, phone, address and website)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from jarvis.config import Settings
from jarvis.core import budget
from jarvis.core.errors import IntegrationState, ProviderError, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call, http_request

PROVIDER = "serp_api"
BASE = "https://serpapi.com"


@dataclass(frozen=True)
class SerpResult:
    position: int
    title: str
    link: str
    displayed_link: str | None
    snippet: str | None


@dataclass(frozen=True)
class SerpResponse:
    search_id: str | None
    results: tuple[SerpResult, ...]
    local_website: str | None  # knowledge-graph website, when present (independent evidence, not fact)


@dataclass(frozen=True, repr=False)
class MapsListing:
    listing_id: str              # SerpApi place_id (or data_id): used only to de-duplicate within a campaign
    title: str
    address: str | None
    phone: str | None
    website: str | None
    rating: float | None
    reviews: int | None
    type: str | None
    open_state: str | None

    @property
    def closed(self) -> bool:
        s = (self.open_state or "").lower()
        return "permanently closed" in s or "temporarily closed" in s

    def __repr__(self) -> str:
        return f"MapsListing({self.listing_id})"


@dataclass(frozen=True)
class MapsPage:
    search_id: str | None
    listings: tuple[MapsListing, ...]
    has_more: bool


class SerpApiClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=40)

    @property
    def configured(self) -> bool:
        return self.settings.serpapi_api_key is not None

    def _key(self) -> str:
        if not self.settings.serpapi_api_key:
            raise not_configured(PROVIDER, "SERPAPI_API_KEY")
        return self.settings.serpapi_api_key.get_secret_value()

    async def search(self, ctx: CallContext, query: str, *, location: str | None = None, num: int = 10) -> SerpResponse:
        params = {"engine": "google", "q": query, "gl": "us", "hl": "en", "num": num, "api_key": self._key()}
        if location:
            params["location"] = location

        async def do() -> CallOutcome:
            resp = await http_request(PROVIDER, self.http, "GET", f"{BASE}/search.json", params=params)
            data = resp.json()
            if "error" in data and not data.get("organic_results"):
                err = str(data["error"])
                if "hasn't returned any results" in err:
                    return CallOutcome(SerpResponse(data.get("search_metadata", {}).get("id"), (), None), 1,
                                       budget.unit_price(PROVIDER, "search"), http_status=resp.status_code)
                state = IntegrationState.QUOTA_EXCEEDED if "run out" in err.lower() else IntegrationState.PROVIDER_ERROR
                raise ProviderError(PROVIDER, state, err)
            results = tuple(SerpResult(r.get("position", i + 1), r.get("title", ""), r.get("link", ""),
                                       r.get("displayed_link"), r.get("snippet"))
                            for i, r in enumerate(data.get("organic_results", [])) if r.get("link"))
            kg = data.get("knowledge_graph") or {}
            cached = data.get("search_metadata", {}).get("status") == "Cached"
            return CallOutcome(SerpResponse(data.get("search_metadata", {}).get("id"), results, kg.get("website")),
                               0 if cached else 1, 0 if cached else budget.unit_price(PROVIDER, "search"),
                               http_status=resp.status_code)

        return await gateway_call(ctx, provider=PROVIDER, operation="search", unit="search", units=1,
                                  estimated_micros=budget.unit_price(PROVIDER, "search"), fn=do)

    async def maps_search(self, ctx: CallContext, query: str, *, location: str, zoom: int = 11, start: int = 0) -> MapsPage:
        """One page (up to 20) of Google Maps listings for `query` around `location`. SerpApi google_maps engine:
        `location` must be combined with `z`; pages are `start` offsets in steps of 20."""
        params = {"engine": "google_maps", "type": "search", "q": query, "location": location, "z": zoom,
                  "hl": "en", "gl": "us", "start": start, "api_key": self._key()}

        async def do() -> CallOutcome:
            resp = await http_request(PROVIDER, self.http, "GET", f"{BASE}/search.json", params=params)
            data = resp.json()
            meta = data.get("search_metadata", {})
            if "error" in data and not data.get("local_results"):
                err = str(data["error"])
                if "hasn't returned any results" in err:
                    return CallOutcome(MapsPage(meta.get("id"), (), False), 1, budget.unit_price(PROVIDER, "search"),
                                       http_status=resp.status_code)
                state = IntegrationState.QUOTA_EXCEEDED if "run out" in err.lower() else IntegrationState.PROVIDER_ERROR
                raise ProviderError(PROVIDER, state, err)
            out = []
            for r in data.get("local_results") or []:
                lid = r.get("place_id") or r.get("data_id")
                if not lid or not r.get("title"):
                    continue
                rating = r.get("rating")
                out.append(MapsListing(str(lid), str(r["title"])[:200], r.get("address"), r.get("phone"), r.get("website"),
                                       float(rating) if isinstance(rating, (int, float)) else None,
                                       int(r["reviews"]) if isinstance(r.get("reviews"), int) else None,
                                       r.get("type"), r.get("open_state")))
            cached = meta.get("status") == "Cached"
            more = bool((data.get("serpapi_pagination") or {}).get("next")) or len(out) >= 20
            return CallOutcome(MapsPage(meta.get("id"), tuple(out), more), 0 if cached else 1,
                               0 if cached else budget.unit_price(PROVIDER, "search"), http_status=resp.status_code)

        return await gateway_call(ctx, provider=PROVIDER, operation="maps_search", unit="search", units=1,
                                  estimated_micros=budget.unit_price(PROVIDER, "search"), fn=do)

    async def account(self) -> dict:
        """Provider-reported usage (free endpoint)."""
        resp = await http_request(PROVIDER, self.http, "GET", f"{BASE}/account.json", params={"api_key": self._key()})
        return resp.json()
