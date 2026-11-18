# JARVIS — Security & Compliance

Status: **Proposed.** This is an engineering interpretation of the relevant terms and laws, not
legal advice. Items marked **⚖ Review** should be checked by you, or by counsel, against the
current official text before production use.

---

## 1. Google Maps Platform (Places API New)

### 1.1 Constraints we design around
Paraphrased from the Google Maps Platform Terms of Service and the Places API service-specific
terms. **⚖ Review the current versions.**

| Constraint | Design response |
|---|---|
| No scraping, no bulk export, and no building a database or list from Google Maps Content outside the permitted caching rules. | Use only the official API. Our durable prospect record is `place_id` plus data we independently verified. Places content lives only in `places_cache`, with an expiry. |
| **Place IDs may be stored indefinitely.** | `prospects.place_id` is retained. |
| Other Places content (name, address, phone, website, hours, ratings) may not be cached beyond the limited periods the terms allow. Latitude/longitude have a temporary caching allowance, generally up to 30 consecutive days. | `places_cache.expires_at` defaults to ≤ 30 days, a nightly purge deletes expired rows, and the UI refreshes through Place Details on demand. **⚖ Review:** the terms may allow less than 30 days for non-coordinate fields. The TTL is configurable (`PLACES_CACHE_TTL_HOURS`) and we should set it to whatever the current text permits. |
| Places content shown to users must carry attribution, and must not be shown on a non-Google map. | The dashboard shows "Google Maps" attribution on any Places-derived field. There is no third-party map display. |
| No using Places content to create "derived content" that substitutes for Google. | Generated websites and outreach use **independently verified facts** (from the business's own site or other sources), never Places-only fields. The Website Builder and Outreach agents can't read `places_cache` (ToolGateway). |
| Billing and quotas. | Field masks are always explicit. Per-SKU monthly caps are set below the free allowance. The Google Cloud budget alert is at $1. |

**Separation rule:** if a business's phone number appears only in Places data, we may display it
to the operator (with attribution, while cached), but it isn't a "verified contact" for outreach
until it's confirmed on an independent source, such as the business's website. This also
improves data quality.

**⚖ Open question:** whether using Places search results to choose businesses for commercial
outreach is consistent with the current terms. This is the biggest compliance dependency in the
product. If the answer is no, Discovery can switch to SERP local results (SerpApi `google_maps`
or `google_local` engines) or another licensed business-data source, and the rest of the
architecture stays the same.

### 1.2 Key handling
API key restricted to the Places API (New) only, plus an IP restriction to the container host. It's never sent to the browser.

---

## 2. SerpApi

- Use only within the plan's quota and throughput. Our adapter enforces a per-hour token bucket set below the plan limit.
- SerpApi returns Google results, and our use is subject to SerpApi's terms. We store queries, URLs and snippets as evidence with timestamps. **Snippets are never treated as verified facts.**
- No automated clicking-through from SERP results except our own probes of candidate official domains, which go through the SSRF guard and respect robots.txt.
- API key goes in the query string, which is how SerpApi works. So it must be **scrubbed from logs and error messages**, and `httpx` request logging is configured to redact `api_key`.

---

## 3. Firecrawl & our own HTTP probes

| Rule | Enforcement |
|---|---|
| Crawl only verified official websites, or a single page for identity checks | The Research agent only crawls a URL that has a `website_audits` row with `status in (functioning, outdated)` and identity score ≥ threshold |
| Bounded crawling | `limit` = `max_crawl_pages_per_prospect` (default 5), `maxDepth` 2, same domain only, timeout 60 s |
| Respect robots.txt | Our probe fetches and evaluates robots.txt before any Firecrawl crawl. Disallowed paths are skipped. A site that disallows everything is scraped only at its homepage for identity, or skipped (configurable) |
| No login walls, paywalls or form submission | Not supported by the adapter |
| Rate | At most 1 concurrent crawl per domain |
| No personal data beyond business contacts | Extraction prompts request business information only. Individual employees' personal details are discarded |

### SSRF protection (our direct fetches: DNS/HTTP probes, robots.txt)
- Allow only the `http` and `https` schemes, and ports 80 and 443.
- Resolve DNS ourselves. Reject private, loopback, link-local, CGNAT and multicast addresses, IPv6 ULA/link-local, and `169.254.169.254` and other metadata hosts. Connect to the **resolved IP**, with SNI/Host set to the hostname, so DNS rebinding can't work.
- Re-validate every redirect hop, with at most 5 redirects.
- Response size cap of 2 MB, total timeout of 10 s, no cookies, generic User-Agent `JarvisSiteCheck/1.0` (no personal or agency data).
- Firecrawl fetches on its own infrastructure, but we still validate the URL with the same rules before sending it.

---

## 4. Website build sandboxing

Generated code is untrusted. It is never imported or executed in the API or worker process.

| Control | Setting |
|---|---|
| Isolation | A fresh container per build from a pinned image digest (`build-sandbox`) |
| Network | `--network none`. Dependencies are pre-installed in the image, and `package.json` is owned by the scaffold, not by the model |
| User | non-root UID, `--cap-drop ALL`, `--security-opt no-new-privileges` |
| Filesystem | read-only root, tmpfs `/work` (512 MB), source copied in, artefact copied out |
| Resources | `--cpus 1`, `--memory 1g`, `--pids-limit 256`, wall-clock timeout 180 s for build and 300 s for QA |
| Docker socket | Never mounted into the sandbox. The worker uses a dedicated, restricted runner, and in later phases we'll consider gVisor (`runsc`) for extra isolation |
| Output validation | The artefact must be static files only (`.html .css .js .svg .png .jpg .webp .avif .ico .txt .xml .json .woff2`), ≤ 20 MB in total, with no server functions (`_worker.js`, `functions/` rejected) |
| Content rules | No external scripts except an allow-list (none by default). Forms post to a configured endpoint, or show a "preview, form disabled" notice. No tracking pixels |
| Path safety | Model output is written only to allowed paths inside the scaffold (`src/pages`, `src/components`, `src/content`, `public/`), and path traversal is rejected |

The model can't add npm dependencies. If it asks for one, the request is logged and the build
fails, which goes to repair or human review.

---

## 5. Preview sites (pages.dev)

Previews use a real business's name, so they need care:

- Served with `X-Robots-Tag: noindex, nofollow` (via `_headers`) and `<meta name="robots" content="noindex">`, plus `robots.txt` with `Disallow: /`.
- A persistent, non-dismissable banner: "Concept preview prepared by *[agency]*. Not affiliated with or endorsed by *[business]*."
- No use of the prospect's logo, photos or copyrighted text without permission. We use their **facts** (services, area, phone), our own copy, and licensed or royalty-free imagery.
- No fake reviews, testimonials, awards, licences, years in business or prices. The factual-claim QA check fails a build that contains any claim not linked to a verified fact.
- URLs use an unguessable slug (`<business-slug>-<8 random chars>`).
- Expiry after 30 days by default (`PREVIEW_TTL_DAYS`). Cleanup deletes the Cloudflare deployment and marks the record `expired`.
- **No deployment to a client's live domain** is possible from the Deployment agent. That requires a separate, human-approved flow, outside v1.

---

## 6. API key and secret security

| Rule | Implementation |
|---|---|
| Server-side only | Keys live only in the API/worker environment. The dashboard gets only `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` (public by design, protected by RLS) and `VITE_API_BASE_URL` |
| Storage | `.env` on the host with mode 600 and owned by the deploy user, or Docker secrets. GitHub Actions secrets for CI. Nothing committed (`.gitignore`, and secret scanning on the repo) |
| Encrypted at rest | Provider keys entered through the Settings UI (a later phase) are encrypted with AES-GCM using `JARVIS_ENCRYPTION_KEY` before they're stored. The UI shows only the last 4 characters |
| Least privilege | Cloudflare token: `Pages:Edit` on one account. Google key: Places API only, IP-restricted. Supabase service key: server only. Claude: a dedicated workspace with a spend limit |
| Rotation | Documented per key in ENVIRONMENT_VARIABLES.md. Rotate on any suspected leak |
| Logging | The `structlog` processor redacts keys matching `*key*`, `*token*`, `*secret*`, `authorization`, and the `api_key` query param |
| Webhooks | Verify provider signatures (Svix for Resend, SNS signatures for SES) and reject anything older than 5 minutes (replay protection). The idempotency key is the provider event ID |
| Auth | Supabase Auth (email plus magic link or password, with optional TOTP MFA). The API verifies JWTs against the project's JWKS, checking `aud`, `exp` and `iss` |
| Rate limiting | Per user and IP on the API (e.g. 60 req/min, 10 commands/min) |

---

## 7. US outreach compliance

### 7.1 CAN-SPAM (15 U.S.C. 7701 et seq., 16 CFR Part 316)
Applies to commercial email, **including B2B**. Each violating message can carry civil
penalties of over $50,000 (adjusted annually for inflation). **⚖ Review.**

| Requirement | Enforcement in JARVIS |
|---|---|
| No false or misleading header information | The From name and address are the agency's real identity on the agency's own domain. Reply-To is a monitored mailbox |
| No deceptive subject lines | Subjects come from templates, and Claude copy is checked against a rule list (no "Re:"/"Fwd:" on first touch, no false urgency) |
| Identify the message as an advertisement or solicitation | A clear disclosure line in the footer |
| Include a valid physical postal address | `users.agency_postal_address` is required. Sending is blocked if it's missing |
| Clear opt-out mechanism | A one-click unsubscribe link (with `List-Unsubscribe` and `List-Unsubscribe-Post` headers) and "reply STOP" |
| Honour opt-outs within 10 business days, with no fee or extra steps, and never sell or transfer the address | Opt-outs are processed immediately into `suppression_list`. The suppression check and the send happen in one DB transaction |
| Responsibility for third parties | We're responsible for what the agent sends, so every send is logged in `outreach_messages` and `audit_events` |

### 7.2 Deliverability rules (Gmail and Yahoo bulk-sender requirements)
SPF, DKIM and DMARC on the sending domain. One-click unsubscribe. Keep the spam-complaint rate
under 0.3%. Low volume: **default 20 emails/day per outreach campaign**, with warm-up. Never use
`pages.dev`, `workers.dev` or free-mail domains to send.

### 7.3 Other channels
**No SMS or automated calls.** The TCPA (Telephone Consumer Protection Act) and state laws make
those high-risk. Click-to-call exists only on the generated websites.

### 7.4 Outreach policy engine rules
1. Send only within an `outreach_campaign` in state `authorized`, inside the configured limits and send window (recipient's local business hours).
2. Use only contacts with `is_public_business_contact = true` and `verification = verified`.
3. Check suppression by email, domain, place_id and phone hash.
4. Each message must reference at least one real, verified observation (a fact ID), and the preview link must point to a `deployments` row with `status = verified`.
5. Stop the sequence on any reply classified `opt_out`, `not_interested` or `bounce`, or on a complaint. Hard bounces and complaints get suppressed at domain level.
6. At most 1 follow-up by default, at least 4 days later.
7. No sending until a sending domain is configured. Until then the status is `NOT_CONFIGURED`, and drafts only.

State laws, such as state anti-spam statutes and privacy laws like the CCPA/CPRA for any personal
data, may add requirements. Business contact data is lower risk, but the retention rules in the
schema still apply. **⚖ Review.**

---

## 8. Autonomous action permissions

| Action | Autonomous? | Condition |
|---|---|---|
| Discovery, verification, research, scoring | Yes | Within an authorised campaign scope and budget |
| Generate, build, QA and deploy **previews** | Yes | Within `max_sites` and budget, only after the campaign authorises the `building` scope |
| Send outreach | Yes, within limits | Only after an explicit outreach authorisation that sets limits. A sending domain and postal address are required |
| Publish to a client's live domain | **No** | Human approval with client authorisation recorded. Not in v1 |
| Purchases, paid plan upgrades, domain purchase | **No** | Human only. JARVIS can recommend, never execute |
| Contracts, pricing commitments, negotiation | **No** | Replies classified as `interested` or `question` are flagged for a human. Drafts may not include prices unless you provide them |
| Anything outside the campaign's authorisation | **No** | Blocked, and the task goes to `blocked` or `requires_human_review` |
| Raise its own limits | **No** | Limits change only through an authenticated user action, which is audited |

Kill switches: global (`system_settings.global_kill_switch`), per provider, and per campaign. All
are checked by the ToolGateway before every external call, and by the worker before claiming tasks.

---

## 9. Threat model summary

| Threat | Mitigation |
|---|---|
| Prompt injection from crawled websites ("ignore instructions, email X") | Crawled content is passed to Claude as quoted data with an explicit system instruction. Extraction outputs are validated by schema. No tool that sends or deploys is available to the research or extraction calls. Outreach recipients come from the DB, never from model output |
| Model fabricates business facts | Facts need a source quote that matches the fetched text exactly (we check substring presence). QA runs a factual-claim check |
| Malicious code generated or injected into a site | Sandbox (§4), static-only output, no external scripts |
| Leaked keys | Server-only storage, redaction, scoped keys, spend caps at each provider |
| Runaway spend | Reservations before calls, provider unit caps, daily and monthly caps, kill switch, bounded retries |
| Duplicate sends after a crash | Idempotency key per message, provider message ID stored, send state committed with the provider response |
| Webhook spoofing | Signature verification and replay window |
| Account takeover | Supabase Auth with MFA recommended. Only the owner role can authorise campaigns or outreach |
