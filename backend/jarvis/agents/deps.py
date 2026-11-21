"""Dependency container (constructor-injected providers). Tests substitute HTTP transports or
clients here; production wires real adapters. There is no global mock switch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from jarvis.config import Settings
from jarvis.db.pool import Database
from jarvis.integrations.cartesia import CartesiaClient
from jarvis.integrations.claude import ClaudeClient
from jarvis.integrations.cloudflare import CloudflarePagesClient
from jarvis.integrations.firecrawl import FirecrawlClient
from jarvis.integrations.google_places import GooglePlacesClient
from jarvis.integrations.resend import ResendClient
from jarvis.integrations.serpapi import SerpApiClient
from jarvis.security.ssrf import FetchResult, resolve_public, safe_fetch


@dataclass
class Deps:
    settings: Settings
    db: Database
    places: GooglePlacesClient
    serp: SerpApiClient
    firecrawl: FirecrawlClient
    claude: ClaudeClient
    cloudflare: CloudflarePagesClient
    email: ResendClient
    fetch: Callable[..., Awaitable[FetchResult]] = field(default=safe_fetch)
    resolver: Callable[[str, int], Awaitable[str]] = field(default=resolve_public)
    second_probe_delay_s: float | None = None   # None => settings.broken_second_probe_minutes
    cartesia: CartesiaClient | None = None

    @classmethod
    def build(cls, settings: Settings, db: Database) -> Deps:
        return cls(settings=settings, db=db, places=GooglePlacesClient(settings), serp=SerpApiClient(settings),
                   firecrawl=FirecrawlClient(settings), claude=ClaudeClient(settings),
                   cloudflare=CloudflarePagesClient(settings), email=ResendClient(settings),
                   cartesia=CartesiaClient(settings))

    async def fetch_url(self, url: str, **kw) -> FetchResult:
        if self.fetch is safe_fetch:
            return await safe_fetch(url, resolver=self.resolver, **kw)
        return await self.fetch(url, **kw)
