# JARVIS — Product Requirements

Status: **Revision 3.** Architecture review stage. No application code.
Owner: operator (single-tenant). Related: [ARCHITECTURE.md](../ARCHITECTURE.md), [API_CONTRACTS.md](API_CONTRACTS.md).

Requirement IDs (`PR-x.y`) are referenced from the implementation plan and tests.

---

## PR-1 Natural-language campaign commands

| ID | Requirement |
|---|---|
| PR-1.1 | The operator can create a campaign by text (and by voice from Phase 7), for example: "Find 100 plumbers in Houston with at least a 3.5 Google rating and no website." |
| PR-1.2 | The Command agent extracts a validated `CampaignParameters` object (API_CONTRACTS §2): niche, location, target count, `minimum_google_rating` (optional), `missing_rating_policy` (only when a minimum is set), website-availability filter, and scope. |
| PR-1.3 | Missing required fields (niche, location, target count) produce a clarifying question, never a guess. "Find 50 businesses rated 4.5 or higher" asks for the location and the type of business. |
| PR-1.4 | Every campaign shows a confirmation card with the parsed parameters, the worst-case units and cost, and the compliance status before authorisation. |

## PR-2 Dynamic Google rating filter

| ID | Requirement |
|---|---|
| PR-2.1 | `minimum_google_rating` is an **optional** campaign parameter. **There is no default value.** If the operator doesn't state one, the campaign has no rating filter and no rating data is requested for filtering. |
| PR-2.2 | Accepted values: 0.5 to 5.0 in **0.5 increments** (0.5, 1.0, … 5.0). This matches Google's `minRating` constraint (0.0–5.0 in 0.5 steps). 0.0 is rejected because it means "no filter". |
| PR-2.3 | Values that aren't 0.5 multiples are **not silently rounded**. "At least 3.7" gives the clarification: "Google filters in 0.5 steps. Use 4.0 (stricter) or 3.5 (looser)?" Values outside the range, and non-numeric values, are rejected with a message. |
| PR-2.4 | Parsing covers phrasings such as "at least 3.5", "minimum rating of 4.0", "rated 4.5 or higher", "4+ stars" (→ 4.0), "above 4" (→ ambiguous: ask whether 4.0 inclusive or 4.5), "3.5-star and up". |
| PR-2.5 | When the Google Places provider is enabled (PR-4), the filter is sent as Text Search `minRating`. JARVIS also re-checks each returned place **in memory** (`rating ≥ minimum`) and applies the missing-rating policy itself, so behaviour doesn't depend on undocumented Google handling of unrated places. |
| PR-2.6 | **Missing-rating policy** (required whenever `minimum_google_rating` is set): `exclude` (unrated places are dropped) or `include_for_review` (unrated places are kept, flagged `rating_unknown`, can't reach `qualified` without operator review, and their count is shown separately). If the command doesn't specify a policy, the confirmation card asks, with `exclude` pre-selected as the safe choice. **Unrated businesses never silently pass a minimum-rating filter.** |
| PR-2.7 | Because Google's handling of unrated places under `minRating` isn't documented, `include_for_review` runs its queries **without** `minRating` and filters in memory. This costs more requests, and the confirmation card shows that cost. |
| PR-2.8 | Google `rating` and `userRatingCount` are **never persisted**: no columns, no caches, no derived per-prospect scores or flags, no logs. They're used in memory to filter, then discarded. Lead scoring doesn't use them. |
| PR-2.9 | The only places a rating is shown: (a) the live "Google Places listing" panel in lead detail, fetched on demand, with attribution; (b) the campaign's own parameter ("filter: rating ≥ 3.5"), which is the operator's input, not Google content. |
| PR-2.10 | A rating filter needs a provider that supplies Google ratings. While the Places provider is disabled by the compliance gate, a campaign with `minimum_google_rating` is rejected at confirmation with `COMPLIANCE_BLOCKED`, and the parsed request is kept as a draft. The feature stays in the product design. |

## PR-3 Evidence-based website availability

| State | Meaning | Persisted? |
|---|---|---|
| `WEBSITE_LISTED` | A source lists a website URL, not yet checked | Only when the listing source is independent (directory, social, SERP). When the only listing source is Google Places, the state exists **in memory only** during the verification task (the URL is Places content) |
| `WEBSITE_WORKING` | Our independent checks confirm the site resolves, responds with 2xx/3xx→2xx, serves real content, and matches the business's identity | Yes |
| `WEBSITE_BROKEN` | A listed website exists but fails verification: NXDOMAIN, connection or TLS failure, 4xx/5xx at the root, parked or for-sale page, or expired. Confirmed by a **second probe at least 10 minutes later** | Yes, with a reason code |
| `NO_WEBSITE` | **Independent evidence supports** that the business has no website (PR-3.3) | Yes, with evidence and confidence |
| `WEBSITE_UNKNOWN` | Not enough evidence, conflicting evidence, or verification incomplete (quota, errors, timeouts) | Yes, with the reason (`insufficient_evidence`, `conflicting_evidence`, `verification_incomplete`) |

| ID | Requirement |
|---|---|
| PR-3.1 | **A missing `websiteUri` in Google Places never on its own produces `NO_WEBSITE`.** It produces an in-memory "no listing" observation that starts the independent checks. |
| PR-3.2 | Website *quality* (`modern`, `outdated`, `unassessed`) is a separate attribute of `WEBSITE_WORKING`, not an availability state. |
| PR-3.3 | `NO_WEBSITE` requires **all** of the following, completed successfully: (1) ≥ 2 SERP queries (name + city/state; phone) with no candidate official domain after filtering out directories, social and aggregators; (2) every independent profile found (directory or social pages, ≤ 3) checked for a website link, with none found; (3) DNS check of up to 3 candidate domains derived from the business name, none resolving to a site that matches identity. If any check couldn't complete → `WEBSITE_UNKNOWN (verification_incomplete)`. |
| PR-3.4 | Confidence for `NO_WEBSITE` is computed deterministically from the checks completed. Below `NO_WEBSITE_MIN_CONFIDENCE` (default 0.8) → `WEBSITE_UNKNOWN (insufficient_evidence)`. |
| PR-3.5 | Website-availability filters in commands ("no website", "broken website", "no or broken website") map to the state sets `{NO_WEBSITE}`, `{WEBSITE_BROKEN}`, `{NO_WEBSITE, WEBSITE_BROKEN}`. `WEBSITE_UNKNOWN` never satisfies a filter. Unknown prospects are counted and can be re-verified. |
| PR-3.6 | Outreach may only claim "no website" or "broken website" when the persisted state is `NO_WEBSITE` or `WEBSITE_BROKEN`, and the message references the evidence. |
| PR-3.7 | Every assessment stores its state, evidence items, sources, timestamp, methods and confidence. |

## PR-4 Google Places compliance gate

| ID | Requirement |
|---|---|
| PR-4.1 | **Intended use, stated precisely:** use Google Places API (New) Text Search, with `minRating` and website-listing data, to find and filter US businesses by Google rating and website availability, in order to select potential website-development clients for commercial email outreach. |
| PR-4.2 | Not storing Places content is necessary but **not assumed sufficient** for compliance. The intended use itself needs approval. |
| PR-4.3 | Places-based discovery and prospect qualification stay **disabled** until a `compliance_approvals` record exists for scope `google_places_prospect_selection`, holding written confirmation from Google or a written opinion from qualified legal counsel that covers the exact use in PR-4.1, with the approver, date, document reference and review date. |
| PR-4.4 | While disabled, the Places adapter's discovery and qualification operations return `COMPLIANCE_BLOCKED`. The dashboard shows the gate status and the reason. |
| PR-4.5 | The discovery provider is replaceable (`DiscoveryProvider` interface). Planned providers: `google_places` (gated), `licensed_business_data` (future, independently licensed, TBD), `manual_import` (an operator-supplied list the operator has the right to use). SerpApi's Google Maps or local engines also return Google Maps content and **aren't** a way around the gate. They fall under the same question. |
| PR-4.6 | The rating-filter feature (PR-2) stays in the design. It becomes usable when the gate is approved, or with a licensed provider that supplies ratings under its own terms (the parameter name for that provider would differ, e.g. `minimum_provider_rating`). |
| PR-4.7 | Whether capped *technical* live tests of the Places adapter are allowed before approval (no prospect selection, nothing kept beyond test assertions) is an operator decision, recorded as a separate approval scope `google_places_technical_testing`. |

## PR-5 Research, qualification, building, outreach
Unchanged in substance from revision 2 (ARCHITECTURE §3–4). Lead scoring uses no Google rating or review data. The "website opportunity" component scores by availability state: `NO_WEBSITE` 35, `WEBSITE_BROKEN` 30, `WEBSITE_WORKING` + `outdated` 20, `WEBSITE_WORKING` + `modern` 0, `WEBSITE_UNKNOWN` 0.

## PR-6 Non-functional
Budgets, caps and kill switches (ARCHITECTURE §6), durability (§11), security (SECURITY_AND_COMPLIANCE), no fabricated data, and a live-test Definition of Done (IMPLEMENTATION_PLAN).
