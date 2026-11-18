# JARVIS — Security & Compliance

Status: **Proposed, revision 2.** This is an engineering interpretation of the relevant terms and
laws. It isn't legal advice. Items marked **⚖ Review** need your own check, or counsel's, against
the current official text.

Revision 2: Places content is never persisted (only Place IDs and optional 30-day coordinates).
Attribution rules are documented. Field masks are mapped to SKUs. Google billing protection is
layered. Outreach has a sending-readiness gate with legitimate-postal-address and
authenticated-domain requirements. Drafts are always available.

---

## 1. Google Maps Platform: Places API (New)

### 1.1 Storage rules (from Google's Places API policies page, checked 2026-11-18)

| Google rule | JARVIS enforcement |
|---|---|
| Cache or store Google Maps Content only as the Service Specific Terms expressly permit. No pre-fetching, indexing, storing or redistributing outside the Services. | No table stores Places content (DATABASE_SCHEMA §2). There's no `places_cache`. Places responses exist only in process memory during the task that requested them. |
| **Place IDs are exempt from caching limits and may be stored indefinitely.** | `place_refs.place_id` is stored. |
| Latitude/longitude may be cached for **up to 30 consecutive calendar days**, then deleted. | Off by default. If enabled, there's a DB check constraint for ≤ 30 days and a nightly purge. |
| Don't copy or save business names, addresses or user reviews. | No columns exist for them on Google-linked tables. A CI schema denylist test enforces this. Pydantic payload models for Places-calling tasks contain no Places fields. Logs redact Places response bodies. |
| Recommended: request only the fields you need, store the place ID, and fetch fresh details when a user interacts with the place. | Fixed field masks (§1.3). The lead detail "Show Google Maps listing" action makes a live Place Details Pro call, shown with attribution and never stored. |
| Don't create new content from Google Maps Content. | Scores, generated sites and outreach use only independently sourced facts. Ratings and reviews are never requested. The Website Builder, Outreach and Qualification agents can't call the Places adapter (ToolGateway allow-list). |

**Independent sourcing.** Business details that JARVIS keeps (name, phone, website, services) must
come from non-Google-Maps sources: the business's own website, other pages fetched by Firecrawl,
SerpApi results (treated as evidence, not fact), licence registers, or the operator. Places values
may be used in memory as *search keys* to find those sources, but they aren't written down. SERP
query strings built from Places values are stored only as a template ID plus a hash.

**⚖ Review: open questions**
1. Whether using Places search results to choose businesses for commercial outreach is consistent with the current Maps Platform Terms and Service Specific Terms. This is the biggest compliance dependency in the product. The Discovery adapter is swappable: alternatives are SerpApi's local engines (subject to SerpApi's terms) or a licensed business-data provider.
2. Whether sending a Places-derived business name to SerpApi as a search query counts as redistribution. We minimise this (name + city only, no addresses) and store only a hash.
3. Whether persisting a website URL that we independently confirm by fetching the site (first hinted by Places `websiteUri`) is acceptable. We store it only after our own probe confirms identity, with `source_type = http_probe`, and record only that a Maps hint was used.

### 1.2 Attribution and display requirements

These apply whenever the dashboard shows Places content (the live "Show Google Maps listing"
panel; Places content is never shown anywhere else):

| Requirement | Implementation |
|---|---|
| Attribution whenever Places content is displayed, unless it's on a Google Map where attribution is already visible | We don't show a Google Map, so the panel always shows attribution |
| Prefer the Google Maps logo. Use the text "Google Maps" where space is limited | Logo inside the panel container. Text fallback on narrow layouts |
| Logo not modified or distorted. Height 16–19 dp with the required clear space. Accessibility label "Google Maps" | `<GoogleMapsAttribution>` component using Google's asset unmodified. Snapshot test on size and aspect ratio |
| Text attribution: exact capitalisation, not wrapped, not translated, `translate="no"`, Roboto or sans-serif fallback, weight 400, colour white, #1F1F1F or #5E5E5E, 12–16 sp | Component styles fixed to these values. Visual regression test |
| Never remove, hide, obscure or alter attribution | The panel can't render without the attribution component (a type-level requirement) |
| Third-party attributions supplied in the response must be shown, and "Google Maps" alone isn't enough when they exist | The panel renders any `attributions` returned |
| Visually separate Places content from other content (border, background, whitespace), with attribution in the same container near the top or bottom | A distinct bordered card labelled "From Google Maps", separate from the independently verified data |
| Reviews/photos need author attribution and links | Not applicable: we never request reviews or photos |
| `googleMapsUri` link lets the user view the place on Google Maps | Included in the Pro mask and shown as "View on Google Maps" |

### 1.3 Field masks and billing SKUs

Confirmed against Google's Text Search (New) and Place Details (New) field tables. The
highest-tier field sets the SKU for the whole request.

| Mask | Endpoint | Fields | SKU |
|---|---|---|---|
| `DISCOVERY_ENTERPRISE` | Text Search | id, displayName, formattedAddress, businessStatus, primaryType, types, websiteUri, nationalPhoneNumber, nextPageToken | **Enterprise** (websiteUri, nationalPhoneNumber) |
| `DISCOVERY_PRO` | Text Search | id, displayName, formattedAddress, businessStatus, primaryType, types, nextPageToken | **Pro** |
| `IDS_ONLY` | Text Search | id, nextPageToken | **Essentials (IDs Only)** |
| `DETAILS_DISPLAY` | Place Details | id, displayName, formattedAddress, businessStatus, googleMapsUri | **Pro** (displayName, businessStatus, googleMapsUri. formattedAddress alone would be Essentials) |
| `DETAILS_CONTACT` | Place Details | DETAILS_DISPLAY + websiteUri, nationalPhoneNumber | **Enterprise** |

Never requested: rating, userRatingCount, regularOpeningHours (Enterprise), reviews/photos
(Enterprise + Atmosphere). A unit test asserts mask → SKU for every constant.

### 1.4 Billing protection

**Google Cloud budget alerts don't cap spending. They only notify.** Controls:
1. **App-level hard stop (primary):** before every Places request, the ToolGateway reserves one unit on the per-SKU daily and monthly counters and the campaign counter, under a row lock. If any cap would be exceeded, the request is **not sent** (`BUDGET_BLOCKED`). Default caps sit below the free allowances.
2. **Provider-side hard stop:** Cloud Console → Google Maps Platform → Quotas → per-day request limits on the Text Search and Place Details methods, set *below* the app caps, because quota enforcement has some latency.
3. **Warnings:** Cloud Billing budget alerts at $1 and $5, and an in-app alert at 80% of any cap.
4. **Key restrictions:** API restriction to Places API (New) only, IP restriction to the VPS.
5. Estimated usage (our ledger) and provider-reported usage (manually imported from Cloud Billing reports in v1) are shown separately.

---

## 2. SerpApi
- Stay within plan quota and throughput. Our token bucket stays below the plan limit, and nothing is retried past `QUOTA_EXCEEDED`.
- Results and snippets are stored as **evidence**, never as verified facts.
- The API key goes in the query string, so it's redacted from logs, errors and stored URLs.
- Usage is synced from the SerpApi account endpoint and shown as provider-reported.

## 3. Firecrawl and direct HTTP probes

| Rule | Enforcement |
|---|---|
| Crawl only verified official sites. Other pages are single-page scrapes for identity or evidence | The Research agent checks `website_audits` before crawling |
| Bounded | `limit` ≤ 5 pages, `maxDepth` 2, same domain, 60 s timeout, per-prospect and monthly credit caps |
| robots.txt | Checked by our probe before every Firecrawl call. Disallowed URLs are skipped |
| Third-party site terms | Directory and social pages: one page per source, no login walls, no bulk extraction. Sources whose terms forbid automated access are on a blocklist **⚖ Review the list** |
| No personal data beyond public business contacts | Extraction prompt and schema are limited to business information |

**SSRF guard** (all direct fetches): http/https only on ports 80/443. Resolve DNS ourselves and
reject private, loopback, link-local, CGNAT, multicast, IPv6 ULA and metadata addresses. Connect
to the vetted IP (anti-rebinding). Re-validate each redirect, with at most 5. 2 MB / 10 s limits.
Generic User-Agent `JarvisSiteCheck/1.0`, with no personal or agency data.

## 4. Website build sandboxing

Generated code is untrusted and is never executed in the API or worker process.

| Control | Setting |
|---|---|
| Isolation | Fresh container per build from a pinned image digest on the VPS |
| Network | `--network none`. Dependencies pre-installed. `package.json` is owned by the scaffold |
| Privileges | non-root UID, `--cap-drop ALL`, `no-new-privileges`, default seccomp profile |
| Filesystem | read-only root, 512 MB tmpfs `/work` |
| Resources | `--cpus 1`, `--memory 1g`, `--pids-limit 256`. 180 s build, 300 s QA |
| Docker socket | never mounted into the sandbox. A restricted runner service starts sandboxes. gVisor considered for Phase 9 |
| Output | static files only (allow-listed extensions), ≤ 20 MB, no `_worker.js` or `functions/` |
| Content | no external scripts by default, no trackers. Forms disabled with a preview notice |
| Paths | writes only to allowed scaffold paths. Traversal rejected |

## 5. Preview sites (pages.dev)
- `X-Robots-Tag: noindex, nofollow`, meta robots noindex, and `robots.txt` set to disallow everything.
- A persistent banner: "Concept preview prepared by *[agency]*. Not affiliated with or endorsed by *[business]*."
- No logos, photos or copyrighted text copied from the prospect without permission. No Google Maps content (no Places names, addresses, ratings or photos). Only independently verified facts and licensed imagery.
- No fabricated reviews, testimonials, awards, licences or prices (enforced by the factual-claim QA check).
- Unguessable slug, 30-day expiry, automatic deletion.
- Publishing to a client's live domain isn't a capability of the system in v1.

## 6. Secrets and API security

| Rule | Implementation |
|---|---|
| Server-side only | Keys only on the VPS (`.env`, mode 600, or Docker secrets) and in GitHub Actions secrets. The SPA gets only public Supabase values and the API URL |
| Encrypted at rest | Keys entered through Settings are AES-256-GCM encrypted with `JARVIS_ENCRYPTION_KEY`. The UI shows the last 4 characters |
| Least privilege | Cloudflare token: Pages:Edit + Workers KV on one account. Google key: Places API (New) only, IP-restricted. Claude: a dedicated workspace with a spend limit. SES IAM: `ses:SendEmail` only |
| Redaction | Log processor redacts `*key*`, `*token*`, `*secret*`, `authorization`, `api_key=`, and Places response bodies |
| Webhooks | Provider signature verified at the edge Worker, re-signed with HMAC to the API, 5-minute replay window, provider event ID used as the idempotency key |
| Auth | Supabase Auth with TOTP MFA recommended. JWT checked against JWKS (`aud`, `iss`, `exp`) |
| VPS hardening | SSH keys only, firewall (80/443 open, SSH restricted), unattended security updates, Docker daemon not exposed |
| Rate limiting | per user and IP on the API |

## 7. US outreach compliance

### 7.1 CAN-SPAM (15 U.S.C. 7701 et seq.; 16 CFR Part 316)
This applies to commercial email, including B2B. The FTC guide lists penalties of up to $53,088
per violating email (inflation-adjusted). **⚖ Review.**

| Requirement | Enforcement |
|---|---|
| Accurate header information | From/Reply-To on the agency's authenticated domain |
| Non-deceptive subject lines | Template rules: no "Re:"/"Fwd:" on a first touch, no false urgency. Lint on Claude output |
| Identify the message as an advertisement | Footer disclosure |
| **Valid physical postal address** | Must be the sender's current street address, a PO Box registered with the USPS, or a private mailbox registered with a Commercial Mail Receiving Agency under USPS regulations (FTC guidance). The owner records the address, its type and a dated attestation that it's current and registered. Placeholder or invalid patterns are rejected (e.g. missing ZIP, "123 Main St", "N/A"). Re-attestation every 12 months |
| Clear opt-out | Signed one-click unsubscribe link served by the edge Worker (with `List-Unsubscribe` and `List-Unsubscribe-Post` headers), plus "reply STOP" |
| Honour opt-outs within 10 business days, with no fee or extra steps, and never transfer the address | Immediate suppression. The suppression check and the send are in one transaction |
| Responsibility for agents acting on your behalf | Every send is logged and audited |

### 7.2 Authenticated sending domain
- **SPF**: includes the email provider. **DKIM**: provider-signed, aligned with the From domain. **DMARC**: a published record (`p=none` at minimum for monitoring; `quarantine` recommended after warm-up).
- The provider must report the domain as verified.
- DNS is checked automatically at configuration time and again if the last check is older than 24 hours before any send.
- Forbidden sender domains: `pages.dev`, `workers.dev`, and free-mail providers.
- Gmail/Yahoo bulk-sender practice: one-click unsubscribe, complaint rate < 0.3%, low volume (default 20/day per outreach campaign), warm-up.

### 7.3 Sending-readiness gate
Drafting is **always available**: drafts need no email provider, domain or address. They are
generated, edited and stored as `outreach_messages` in `draft`.

**Sending is blocked** (`NOT_CONFIGURED` / `BLOCKED_COMPLIANCE`) until **all** of these pass, as
recorded in `sending_readiness` and re-checked inside the send transaction:
1. Legitimate postal address recorded, typed and attested (≤ 12 months old).
2. Sending domain authenticated: SPF, DKIM and DMARC pass, and the provider reports it verified (check ≤ 24 h old).
3. From and Reply-To on that domain. Reply-To mailbox confirmed (test message received).
4. Email provider configured, and its webhook signature verified end to end.
5. Unsubscribe endpoint reachable over public HTTPS, with a round-trip test that writes a test suppression.
6. Outreach campaign authorised by the owner, with daily and total limits and a send window.
7. Recipient checks per message: verified public business contact, not suppressed (email, domain, place_id, phone), preview deployment verified live, at least one verified observation referenced.

The Outreach page shows the checklist with each item's status. No item can be bypassed from the UI or the API.

### 7.4 Other channels
No SMS or automated calls (TCPA risk). Click-to-call exists only on generated sites.

### 7.5 Sequence rules
Stop on `opt_out`, `not_interested`, `bounce` or a complaint. Domain-level suppression on hard
bounce or complaint. At most 1 follow-up after ≥ 4 days. Send only inside the recipient's local
business hours. State laws may add requirements. **⚖ Review.**

## 8. Autonomous action permissions

| Action | Autonomous? | Condition |
|---|---|---|
| Discovery, verification, research, scoring | Yes | within authorised scope and caps |
| Build, QA, deploy previews | Yes | within `max_sites` and the budget |
| Draft outreach | Yes | always (no email configuration needed) |
| Send outreach | Within limits | only when the sending-readiness gate passes and the outreach campaign is authorised |
| Live client-domain publishing | **No** | not in v1. Later only with recorded client authorisation |
| Purchases, paid plans, raising caps above free allowances | **No** | human action only. Audited |
| Contracts, pricing commitments | **No** | interested replies are flagged for a human |
| Anything outside the campaign authorisation | **No** | blocked |

Kill switches: global, per provider and per campaign. Checked before every claim and every external call.

## 9. Database availability and data protection
- Supabase Free may pause an inactive project, and we don't assume worker traffic prevents it. Detection, alerting, a manual-resume runbook and automatic reconnection are in ARCHITECTURE §11–12.
- Supabase Free has no managed backups or PITR. We take daily encrypted `pg_dump`s off-site and test a restore monthly.
- While the DB is unavailable, no billable or outward-facing action runs, because nothing can reserve budget or record usage.

## 10. Threat model summary

| Threat | Mitigation |
|---|---|
| Prompt injection from crawled pages | Content passed as quoted data. Extraction calls have no dangerous tools. Schema-validated outputs. Recipients come from the DB only |
| Fabricated business facts | Quote-verified facts, plus the factual-claim QA check |
| Accidental persistence of Places content | No columns, payload schemas without Places fields, CI denylist test, log redaction |
| Malicious generated code | Sandbox and static-only output |
| Leaked keys | Server-only storage, redaction, scoped keys, provider spend limits |
| Runaway spend | Pre-call reservations, provider quotas, caps below free allowances, kill switch, bounded retries |
| Duplicate sends | Idempotency keys, send state committed with the provider ID |
| Webhook spoofing | Signature verification and replay window |
| DB outage causing unrecorded spend | Ledger write required before any billable call. Worker pauses when the DB is unhealthy |
