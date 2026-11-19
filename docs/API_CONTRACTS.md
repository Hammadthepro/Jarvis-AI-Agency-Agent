# JARVIS — API Contracts

Status: **Revision 3.** Design-level contracts for the FastAPI service and internal adapter
interfaces. Field names are normative. Exact OpenAPI is generated from Pydantic models in Phase 1.

Conventions: JSON over HTTPS. `Authorization: Bearer <Supabase JWT>`. Errors use
`{"error": {"code": str, "message": str, "details": {}}}`. Writes accept an `Idempotency-Key` header.

## 1. Error codes

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `VALIDATION_ERROR` | schema or value invalid (e.g. `minimum_google_rating` not a 0.5 multiple) |
| 409 | `INVALID_TRANSITION` | state change not allowed |
| 409 | `NEEDS_CLARIFICATION` | command missing or ambiguous fields; the response includes questions |
| 403 | `COMPLIANCE_BLOCKED` | the action needs an approval that doesn't exist (Places gate, sending readiness) |
| 402 | `BUDGET_BLOCKED` | a reservation would exceed a cap; nothing was sent to the provider |
| 424 | `NOT_CONFIGURED` | the required provider has no credentials |
| 429 | `QUOTA_EXCEEDED` / `RATE_LIMITED` | provider or app quota |
| 502 | `PROVIDER_ERROR` | upstream failure after retries |
| 503 | `DATABASE_UNAVAILABLE` | DB health check failing |

## 2. Commands and campaign parameters

### `POST /commands`
Request: `{"channel": "text"|"voice", "text": str}`
Response 200:
```json
{
  "command_id": "uuid",
  "status": "awaiting_confirmation" | "needs_clarification" | "rejected",
  "parameters": CampaignParameters | null,
  "questions": [ {"field": "minimum_google_rating", "prompt": "Google filters in 0.5 steps. Use 4.0 or 3.5?", "options": [4.0, 3.5]} ],
  "plan": PlanEstimate | null,
  "compliance": {"discovery_provider": "google_places", "gate": "approved"|"blocked", "reason": str|null}
}
```

### `CampaignParameters` (Pydantic, strict)
| Field | Type | Rules |
|---|---|---|
| `niche` | str | required, 2–80 chars |
| `location` | `{city?: str, state?: str (USPS code), country: "US", radius_km?: number}` | required. At least city or state |
| `target_count` | int | required, 1–1000 |
| `discovery_provider` | `"google_places" \| "manual_import" \| "licensed_business_data"` | default from settings |
| `minimum_google_rating` | number \| null | **optional, no default.** If set: 0.5 ≤ x ≤ 5.0 and `x * 2` is an integer. Otherwise `VALIDATION_ERROR`. Allowed only with `discovery_provider = google_places` |
| `missing_rating_policy` | `"exclude" \| "include_for_review"` \| null | **required if** `minimum_google_rating` is set, and must be null otherwise |
| `website_filter` | array of `"NO_WEBSITE" \| "WEBSITE_BROKEN" \| "WEBSITE_WORKING_OUTDATED" \| "ANY"` | default `["ANY"]`. `WEBSITE_UNKNOWN` isn't selectable |
| `include_service_area_businesses` | bool | default true for service niches (sent as `includePureServiceAreaBusinesses`) |
| `scope` | array of stages | default discovery → qualification |
| `budget_cap_usd` | number | default from settings. Can't exceed the global cap |

Command parsing examples (normative test fixtures):

| Command | Result |
|---|---|
| "Find 100 plumbers in Houston with at least a 3.5 Google rating and no website." | niche=plumbing, Houston TX, 100, min=3.5, policy=null → card asks for the policy (`exclude` pre-selected), website_filter=[NO_WEBSITE] |
| "Find dentists in Dallas with a minimum Google rating of 4.0." | niche=dentist, Dallas TX, **target_count missing → clarification**, min=4.0 |
| "Find 50 businesses rated 4.5 or higher." | min=4.5, 50. **Location and niche missing → clarification** |
| "Find 20 roofers in Denver rated at least 3.7" | clarification: 3.5 or 4.0 (never auto-rounded) |
| "…rating above 5" / "…rating of 6" / "…rating of -1" / "…rating of four-ish" | `VALIDATION_ERROR` or clarification. Never accepted |
| "Find 30 electricians in Austin" | min=null, policy=null. No rating filter, no rating used |

### `POST /commands/{id}/confirm`
Body: `{"parameters": CampaignParameters}` (the operator can edit). Response: `{"campaign_id": uuid, "state": "authorized"}` or an error (`COMPLIANCE_BLOCKED`, `BUDGET_BLOCKED`, `VALIDATION_ERROR`).

### `PlanEstimate`
`{"places_requests_max": int, "serp_searches_max": int, "firecrawl_credits_max": int, "claude_usd_max": number, "estimated_usd_range": [min, max], "within_free_allowances": bool, "assumptions": {"results_per_request": 15, "no_website_listing_rate": 0.2, …}}`

## 3. Campaigns, prospects, websites

| Method | Path | Notes |
|---|---|---|
| GET | `/campaigns` / `/campaigns/{id}` | includes `parameters`, `state`, counts by website state, cost (estimated and reported) |
| POST | `/campaigns/{id}/pause` · `/resume` · `/cancel` | state transitions |
| GET | `/campaigns/{id}/events` | SSE, supports `Last-Event-ID` |
| GET | `/prospects?campaign_id=&website_state=&state=` | **No Google fields in the response.** Name and contact come from verified independent facts, else `null` with `identity_status: "unverified"` |
| GET | `/prospects/{id}` | facts, website assessment, score breakdown |
| GET | `/prospects/{id}/google-listing` | **Live** Place Details call (see §5). Not cached, not stored. Response carries `attribution` and `display_rules`. Returns `COMPLIANCE_BLOCKED` if the display scope isn't approved, and `BUDGET_BLOCKED` at the cap |
| POST | `/prospects/{id}/confirm-identity` | operator confirmation (`source_type=manual`) |
| POST | `/prospects/{id}/reverify-website` | re-runs verification (for `WEBSITE_UNKNOWN`) |
| GET | `/sites`, `/sites/{id}`, `/sites/{id}/versions`, `/deployments` | |

### `WebsiteAssessment`
```json
{
  "state": "WEBSITE_WORKING" | "WEBSITE_BROKEN" | "NO_WEBSITE" | "WEBSITE_UNKNOWN",
  "reason": "nxdomain" | "tls_error" | "http_5xx" | "http_4xx" | "parked" | "identity_mismatch" | "insufficient_evidence" | "conflicting_evidence" | "verification_incomplete" | null,
  "quality": "modern" | "outdated" | "unassessed" | null,
  "url": "https://…" | null,
  "listing_sources": ["directory_page", "serp_result", "maps_hint"],
  "checks": [ {"check": "serp_name_city", "status": "completed"|"failed"|"skipped", "result": "no_candidate"} ],
  "confidence": 0.86,
  "evidence": [ {"source_id": "uuid", "observation": "…"} ],
  "checked_at": "…"
}
```
`WEBSITE_LISTED` never appears in persisted responses when its only source is Google, because it's resolved within the task. `maps_hint` records that a Google listing hint existed, without its value. That's an open legal question (SECURITY_AND_COMPLIANCE §1.1).

### `GET /prospects/{id}/google-listing` response
```json
{
  "place_id": "…",
  "display_name": "…", "formatted_address": "…", "business_status": "OPERATIONAL",
  "rating": 4.3, "user_rating_count": 127,
  "google_maps_uri": "https://maps.google.com/?cid=…",
  "attributions": [ {"provider": "…", "providerUri": "…"} ],
  "attribution_required": {"google_maps": true, "third_party": true},
  "fetched_at": "…", "cache": "none"
}
```
The response has `Cache-Control: no-store`. The dashboard renders it only inside `<GooglePlacesListingPanel>`, which enforces the attribution rules.

## 4. Settings, compliance, outreach

| Method | Path | Notes |
|---|---|---|
| GET | `/compliance/approvals` | gate status per scope |
| POST | `/compliance/approvals` | owner only. `{scope, approver_type: "google"|"legal_counsel", approver_name, document_ref, intended_use_text, approved_at, review_by}`. Audited. `intended_use_text` must match the PR-4.1 text version |
| DELETE | `/compliance/approvals/{id}` | revokes approval. The gate closes immediately |
| GET/PUT | `/settings/limits` | caps. `allow_paid` changes audited |
| GET | `/usage?period=` | `{provider, estimated: {...}, reported: {...} \| "not_available", variance, synced_at}` |
| GET | `/outreach/readiness` | the checklist items with status |
| POST | `/outreach/drafts` | always allowed |
| POST | `/outreach/campaigns/{id}/authorize` | `COMPLIANCE_BLOCKED` unless readiness passes |
| GET | `/health` · `/ready` · `/providers/health` | `/providers/health` includes `compliance_gate` per provider |

## 5. Internal adapter contract: Google Places

```python
class PlacesTextSearchRequest(BaseModel):
    text_query: str
    included_type: str | None
    location_bias: Circle | Rectangle | None      # or location_restriction (rectangle), never both
    min_rating: Decimal | None                     # validated 0.5 steps; sent as minRating
    include_pure_service_area_businesses: bool
    page_size: int = 20                            # 1..20
    page_token: str | None

class PlaceInMemory(BaseModel):                    # never serialised to DB, logs or task payloads
    place_id: str
    display_name: str | None
    formatted_address: str | None                  # absent for service-area businesses
    business_status: str | None
    primary_type: str | None
    website_uri: str | None
    national_phone_number: str | None
    rating: float | None
    user_rating_count: int | None
    attributions: list[Attribution]
    model_config = ConfigDict(frozen=True)         # plus a custom __repr__ that redacts all but place_id

async def text_search(req, *, mask: FieldMask) -> TextSearchPage    # TextSearchPage.places: list[PlaceInMemory]
```
Gate check: `text_search` raises `ComplianceBlocked` unless an approval exists for the calling scope (`google_places_prospect_selection`, or `google_places_technical_testing` in `live-test`).

## 6. Internal contract: DiscoveryProvider

```python
class DiscoveryProvider(Protocol):
    name: str
    supports_google_rating: bool
    async def discover(self, params: CampaignParameters, page: PageCursor) -> DiscoveryPage
    # DiscoveryPage yields candidates for in-task verification; only stable IDs are persisted
```
`google_places` (gated), `manual_import` (CSV upload with operator-attested rights to use), `licensed_business_data` (future).
