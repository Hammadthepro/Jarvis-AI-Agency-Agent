"""SerpApi Google Search engine adapter (search research + independent website verification)."""

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

    async def account(self) -> dict:
        """Provider-reported usage (free endpoint)."""
        resp = await http_request(PROVIDER, self.http, "GET", f"{BASE}/account.json", params={"api_key": self._key()})
        return resp.json()
