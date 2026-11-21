"""Cartesia (speech synthesis for JARVIS's voice). The voice agent synthesizes speech with the official LiveKit Cartesia
plugin; this adapter only VALIDATES the configuration: GET /voices/{id} is a free lookup (no synthesis, no credits
used) that proves the API key and that the configured voice exists."""

from __future__ import annotations

import re

import httpx

from jarvis.config import Settings
from jarvis.core.errors import IntegrationState, ProviderError, not_configured
from jarvis.integrations.base import http_request

PROVIDER = "cartesia"
BASE = "https://api.cartesia.ai"
API_VERSION = "2025-04-16"            # the version livekit-plugins-cartesia 1.8.12 sends (plugin constants.API_VERSION)
VOICE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class CartesiaClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=15)

    @property
    def configured(self) -> bool:
        return self.settings.cartesia_api_key is not None

    def _headers(self) -> dict:
        if not self.settings.cartesia_api_key:
            raise not_configured(PROVIDER, "CARTESIA_API_KEY")
        return {"X-API-Key": self.settings.cartesia_api_key.get_secret_value(), "Cartesia-Version": API_VERSION}

    async def voice(self, voice_id: str | None = None) -> dict:
        vid = voice_id or self.settings.cartesia_voice_id
        if not VOICE_ID.match(vid or ""):
            raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR, f"voice id {vid!r} is not a Cartesia voice id")
        try:
            r = await http_request(PROVIDER, self.http, "GET", f"{BASE}/voices/{vid}", headers=self._headers())
        except ProviderError as exc:
            if exc.upstream_status == 404:
                raise ProviderError(PROVIDER, IntegrationState.PROVIDER_ERROR,
                                    f"voice {vid} was not found for this Cartesia account; choose another voice id") from exc
            raise
        d = r.json()
        return {"voice_id": d.get("id", vid), "name": d.get("name"), "language": d.get("language")}
