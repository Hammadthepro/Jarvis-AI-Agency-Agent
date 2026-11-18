"""Firecrawl adapter (v2 scrape). Callers MUST pass the URL through the SSRF guard and robots.txt
check first (jarvis.security); this adapter re-validates the URL shape as defence in depth."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from jarvis.config import Settings
from jarvis.core import budget
from jarvis.core.errors import IntegrationState, ProviderError, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call, http_request
from jarvis.security.ssrf import validate_url_shape

PROVIDER = "firecrawl"
BASE = "https://api.firecrawl.dev/v2"


@dataclass(frozen=True)
class ScrapedPage:
    url: str
    status_code: int | None
    title: str | None
    markdown: str


class FirecrawlClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=90)

    @property
    def configured(self) -> bool:
        return self.settings.firecrawl_api_key is not None

    def _headers(self) -> dict:
        if not self.settings.firecrawl_api_key:
            raise not_configured(PROVIDER, "FIRECRAWL_API_KEY")
        return {"Authorization": f"Bearer {self.settings.firecrawl_api_key.get_secret_value()}"}

    async def scrape(self, ctx: CallContext, url: str) -> ScrapedPage:
        validate_url_shape(url)
        headers = self._headers()
        body = {"url": url, "formats": ["markdown"], "onlyMainContent": True, "timeout": 60000}

        async def do() -> CallOutcome:
            resp = await http_request(PROVIDER, self.http, "POST", f"{BASE}/scrape", json=body, headers=headers)
            data = resp.json()
            if not data.get("success"):
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, str(data.get("error"))[:300])
            d = data.get("data") or {}
            meta = d.get("metadata") or {}
            page = ScrapedPage(meta.get("sourceURL") or url, meta.get("statusCode"), meta.get("title"),
                               (d.get("markdown") or "")[:200_000])
            credits = int(d.get("creditsUsed") or meta.get("creditsUsed") or 1)
            return CallOutcome(page, credits, credits * budget.unit_price(PROVIDER, "credit"),
                               http_status=resp.status_code)

        return await gateway_call(ctx, provider=PROVIDER, operation="scrape", unit="credit", units=1,
                                  estimated_micros=budget.unit_price(PROVIDER, "credit"), fn=do)

    async def credit_usage(self) -> dict:
        resp = await http_request(PROVIDER, self.http, "GET", f"{BASE}/team/credit-usage", headers=self._headers())
        return resp.json()
