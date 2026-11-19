"""Google Places API (New) adapter: Text Search (primary) and Place Details (listing panel only).

The ONLY Google Maps Platform API integrated. No Maps JavaScript, Embed, Static, Routes, Geocoding
or Autocomplete. Every call is gated by the compliance gate (see jarvis.core.compliance).

Places responses are parsed into `PlaceInMemory`, which is frozen, never serialised to the DB,
task payloads or logs (its repr redacts everything except place_id)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from jarvis.config import Settings
from jarvis.core import budget
from jarvis.core.errors import ValidationFailed, not_configured
from jarvis.integrations.base import CallContext, CallOutcome, gateway_call, http_request

PROVIDER = "google_places"

# Field-to-SKU mapping verified 2026-11-18 against Google's Place Data Fields (New) table.
TEXT_SEARCH_FIELD_SKU = {
    "places.id": "ids_only", "places.attributions": "ids_only", "nextPageToken": "ids_only",
    "places.displayName": "pro", "places.formattedAddress": "pro", "places.businessStatus": "pro",
    "places.primaryType": "pro", "places.types": "pro", "places.googleMapsUri": "pro",
    "places.websiteUri": "enterprise", "places.nationalPhoneNumber": "enterprise",
    "places.rating": "enterprise", "places.userRatingCount": "enterprise",
}
DETAILS_FIELD_SKU = {
    "id": "ids_only", "attributions": "ids_only", "formattedAddress": "essentials", "types": "essentials",
    "displayName": "pro", "businessStatus": "pro", "googleMapsUri": "pro", "primaryType": "pro",
    "websiteUri": "enterprise", "nationalPhoneNumber": "enterprise", "rating": "enterprise",
    "userRatingCount": "enterprise",
}
_TIER = ["ids_only", "essentials", "pro", "enterprise"]

DISCOVERY_ENTERPRISE = ("places.id", "places.attributions", "places.displayName", "places.formattedAddress",
                        "places.businessStatus", "places.primaryType", "places.websiteUri",
                        "places.nationalPhoneNumber", "nextPageToken")
DISCOVERY_ENTERPRISE_WITH_RATING = DISCOVERY_ENTERPRISE + ("places.rating",)
DISCOVERY_PRO = ("places.id", "places.attributions", "places.displayName", "places.formattedAddress",
                 "places.businessStatus", "places.primaryType", "nextPageToken")
IDS_ONLY = ("places.id", "places.attributions", "nextPageToken")
LISTING_DISPLAY = ("id", "attributions", "displayName", "formattedAddress", "businessStatus", "googleMapsUri",
                   "rating", "userRatingCount")


def mask_sku(mask: tuple[str, ...], endpoint: str = "text_search") -> str:
    table = TEXT_SEARCH_FIELD_SKU if endpoint == "text_search" else DETAILS_FIELD_SKU
    tier = max((table[f] for f in mask), key=_TIER.index)
    return f"{endpoint}_{tier}" if endpoint == "text_search" else f"place_details_{tier}"


def validate_min_rating(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except Exception as exc:
        raise ValidationFailed("minimum_google_rating must be a number") from exc
    if not (Decimal("0.5") <= d <= Decimal("5.0")) or (d * 2) % 1 != 0:
        raise ValidationFailed("minimum_google_rating must be between 0.5 and 5.0 in 0.5 increments (no rounding)",
                               details={"value": str(value)})
    return d


@dataclass(frozen=True, repr=False)
class PlaceInMemory:
    place_id: str
    display_name: str | None
    formatted_address: str | None
    business_status: str | None
    primary_type: str | None
    website_uri: str | None
    national_phone_number: str | None
    rating: float | None
    attributions: tuple = field(default_factory=tuple)

    def __repr__(self) -> str:  # never leak Places content into logs/tracebacks
        return f"PlaceInMemory(place_id={self.place_id!r}, <redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, repr=False)
class TextSearchPage:
    places: tuple[PlaceInMemory, ...]
    next_page_token: str | None
    sku: str

    def __repr__(self) -> str:
        return f"TextSearchPage(n={len(self.places)}, has_next={self.next_page_token is not None})"


def _parse_place(p: dict) -> PlaceInMemory:
    return PlaceInMemory(
        place_id=p["id"], display_name=(p.get("displayName") or {}).get("text"),
        formatted_address=p.get("formattedAddress"), business_status=p.get("businessStatus"),
        primary_type=p.get("primaryType"), website_uri=p.get("websiteUri"),
        national_phone_number=p.get("nationalPhoneNumber"), rating=p.get("rating"),
        attributions=tuple(p.get("attributions") or ()))


class GooglePlacesClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=30)

    @property
    def configured(self) -> bool:
        return self.settings.google_places_api_key is not None

    def _headers(self, mask: tuple[str, ...]) -> dict:
        if not self.configured:
            raise not_configured(PROVIDER, "GOOGLE_PLACES_API_KEY")
        assert self.settings.google_places_api_key
        return {"X-Goog-Api-Key": self.settings.google_places_api_key.get_secret_value(),
                "X-Goog-FieldMask": ",".join(mask), "Content-Type": "application/json"}

    async def text_search(self, ctx: CallContext, *, text_query: str, min_rating: Decimal | None,
                          include_service_area: bool, page_token: str | None = None,
                          location_bias: dict | None = None, page_size: int = 20,
                          scope: str = "google_places_prospect_selection",
                          mask: tuple[str, ...] | None = None) -> TextSearchPage:
        mask = mask or (DISCOVERY_ENTERPRISE_WITH_RATING if min_rating is not None else DISCOVERY_ENTERPRISE)
        sku = mask_sku(mask)
        body: dict[str, Any] = {"textQuery": text_query, "pageSize": max(1, min(20, page_size)),
                                "regionCode": "US", "includePureServiceAreaBusinesses": include_service_area}
        if min_rating is not None:
            body["minRating"] = float(min_rating)
        if page_token:
            body["pageToken"] = page_token
        if location_bias:
            body["locationBias"] = location_bias
        headers = self._headers(mask)

        async def do() -> CallOutcome:
            resp = await http_request(PROVIDER, self.http, "POST",
                                      f"{self.settings.places_api_base_url}/places:searchText",
                                      json=body, headers=headers)
            data = resp.json()
            page = TextSearchPage(tuple(_parse_place(p) for p in data.get("places", [])),
                                  data.get("nextPageToken"), sku)
            return CallOutcome(page, 1, budget.unit_price(PROVIDER, sku), sku=sku, http_status=resp.status_code,
                               extra={"returned": len(page.places)})

        return await gateway_call(ctx, provider=PROVIDER, operation="places:searchText", unit=sku, units=1,
                                  estimated_micros=budget.unit_price(PROVIDER, sku), fn=do, compliance_scope=scope)

    async def listing(self, ctx: CallContext, place_id: str) -> dict:
        """Live listing for the attributed Google Places listing panel. Never cached or stored."""
        sku = mask_sku(LISTING_DISPLAY, "details")
        headers = self._headers(LISTING_DISPLAY)

        async def do() -> CallOutcome:
            resp = await http_request(PROVIDER, self.http, "GET",
                                      f"{self.settings.places_api_base_url}/places/{place_id}", headers=headers)
            d = resp.json()
            value = {"place_id": d.get("id"), "display_name": (d.get("displayName") or {}).get("text"),
                     "formatted_address": d.get("formattedAddress"), "business_status": d.get("businessStatus"),
                     "rating": d.get("rating"), "user_rating_count": d.get("userRatingCount"),
                     "google_maps_uri": d.get("googleMapsUri"), "attributions": d.get("attributions") or [],
                     "attribution_required": {"google_maps": True, "third_party": bool(d.get("attributions"))}}
            return CallOutcome(value, 1, budget.unit_price(PROVIDER, sku), sku=sku, http_status=resp.status_code)

        return await gateway_call(ctx, provider=PROVIDER, operation="places.get", unit=sku, units=1,
                                  estimated_micros=budget.unit_price(PROVIDER, sku), fn=do,
                                  compliance_scope="google_places_listing_display")
