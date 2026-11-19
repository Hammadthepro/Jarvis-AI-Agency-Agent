# JARVIS — Verification Register

Status: **Revision 3** (2026-11-18). Lists what the documentation depends on, and classifies
each item as a confirmed fact, an estimate or an unresolved legal question. Update an entry
whenever it's checked.

This register covers provider facts the design relies on. It does **not** record whether JARVIS's own
integrations work: that is tracked per criterion (implemented / test-verified / live-verified / deployed) in
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). As of 2026-11-19, no integration is live-verified.

## A. Confirmed facts (primary source checked 2026-11-18)

| # | Fact | Source |
|---|---|---|
| A1 | `rating`, `userRatingCount`, `websiteUri` and `nationalPhoneNumber` are Text Search Enterprise and Place Details Enterprise fields | Google Places API (New) Place Data Fields and Text Search pages |
| A2 | `displayName`, `businessStatus`, `googleMapsUri` and `primaryType` are Pro. `formattedAddress` and `types` are Pro in Text Search and Essentials in Place Details | same |
| A3 | `attributions`, `id` and `nextPageToken` are Essentials (IDs Only) | same |
| A4 | The highest-tier field in the field mask sets the SKU | Google Text Search / Place Details pages |
| A5 | `minRating`: 0.0–5.0 in 0.5 increments, rounded up | Text Search (New) page |
| A6 | Text Search: `pageSize` 1–20 (default 20); ≤ 60 results across pages; identical requests may return inconsistent results; follow-up pages must repeat the other parameters | Text Search (New) page |
| A7 | `includePureServiceAreaBusinesses` returns businesses without storefronts, which omit `formattedAddress` | Text Search (New) page |
| A8 | Place IDs may be stored indefinitely. Lat/lng may be cached for ≤ 30 consecutive days. Names, addresses and reviews may not be saved | Places API policies page |
| A9 | Attribution rules (logo/text, sizes, colours, third-party attributions, visual separation) | Places API policies page |
| A10 | Per-SKU free monthly allowances replaced the $200 credit (Pro 5,000; Enterprise 1,000) | Google Maps Platform March 2025 billing change page |
| A11 | Budget alerts don't cap spend. Per-API quotas cap requests, with enforcement latency | Google Maps Platform cost management page; Cloud "Capping API usage" page |
| A12 | CAN-SPAM postal address may be a street address, a USPS-registered PO Box, or a CMRA private mailbox | FTC CAN-SPAM compliance guide |
| A13 | Claude prices (Haiku 5.5 $0.10/$0.50; Sonnet 5.5 $2/$10; Opus 5.5 $4/$20 per MTok; Batches 50% off) | maintained Claude API reference |

## B. Secondary-source facts (verify on the official page before any paid use)

| # | Item | Check where |
|---|---|---|
| B1 | Text Search Enterprise ≈ $35/1,000; Pro ≈ $32/1,000; Details Enterprise ≈ $20/1,000 | Google Maps Platform pricing page |
| B2 | SerpApi free 250/month (one source says 100); Starter $25 for 1,000 | serpapi.com/pricing |
| B3 | Firecrawl free 1,000 credits/month; Hobby $19 for 5,000 | firecrawl.dev/pricing |
| B4 | Supabase Free: 500 MB DB, 1 GB storage, pause after ~7 days of inactivity, no managed backups or PITR | supabase.com/pricing and docs |
| B5 | Hetzner CX23-class ≈ €5.49 + €0.50 IPv4 excluding VAT | Hetzner console |
| B6 | LiveKit Build: 1,000 agent minutes/month | livekit.io/pricing |
| B7 | Cloudflare Pages, Workers, KV and R2 free allowances | Cloudflare docs |
| B8 | Resend free 3,000/month and 100/day | resend.com/pricing |
| B9 | Nearby Search (New) has no `minRating` parameter (one reason to use Text Search) | Nearby Search (New) docs |

## C. Estimates (to be replaced with measurements)

| # | Estimate | Value | Measured in |
|---|---|---|---|
| C1 | Average places returned per Text Search request | 15 (12 with `minRating`) | Phase 3 technical tests / first approved campaign |
| C2 | Share operational, in-niche, non-duplicate | 0.70 | Phase 3 |
| C3 | Share with no listed website (`NO_WEBSITE` candidates) | 0.20, highly uncertain | Phase 4 labelled dataset |
| C4 | Share of no-listing candidates confirmed `NO_WEBSITE` | 0.60 | Phase 4 |
| C5 | Whether zero-result Text Search requests are billed | assumed yes | Google billing report after technical tests |
| C6 | How `minRating` treats unrated places | unknown; design doesn't depend on it | technical test (if the scope is opened) |
| C7 | SERP searches per 100 prospects | 140 (any status) / 375 (`NO_WEBSITE`) | Phase 4 |
| C8 | Firecrawl credits per 100 prospects | 100 / 290 | Phase 5 |
| C9 | Claude cost per site | $0.80 expected, $1.56 worst case | Phase 6 diversity run |
| C10 | Development Claude spend | ≈ $29 | running ledger |

## D. Unresolved legal and compliance questions (need Google or qualified counsel)

| # | Question | Blocks |
|---|---|---|
| D1 | **Is the intended use permitted:** using Places API (New) Text Search, with `minRating` and website-listing data, to find and filter US businesses by Google rating and website availability, in order to select website-development prospects for commercial email outreach? | Places discovery, rating filter, Places-based qualification (gate scope `google_places_prospect_selection`) |
| D2 | Is showing the live listing panel (including rating) to the operator a permitted display? | `google_places_listing_display` |
| D3 | Does campaign membership (place_id kept in a campaign with a rating parameter) count as a derived record of rating data? | D1 approval wording |
| D4 | Is the `discovery_filter_unconfirmed` review flag acceptable? If not, remove `include_for_review` | missing-rating policy |
| D5 | Is sending a Places-derived business name to SerpApi as a search query acceptable? | verification of Places-sourced candidates |
| D6 | Is persisting a website URL that we independently confirmed, first hinted by Places `websiteUri`, acceptable? Is recording a `maps_hint` listing source acceptable? | website availability records |
| D7 | Is building a concept preview site for a named business without its consent acceptable (non-affiliation banner, noindex, no copied assets)? | outreach use of previews |
| D8 | State anti-spam and privacy law requirements beyond CAN-SPAM | outreach sending |
| D9 | Terms of directory and social sites used as independent sources (which may be fetched automatically) | independent identity sourcing |
| D10 | Terms of any future licensed business-data provider | `licensed_business_data` provider |
