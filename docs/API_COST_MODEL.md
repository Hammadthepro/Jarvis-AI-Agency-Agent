# JARVIS — API Cost Model

Status: **Proposed.** Prices researched 2026-11-18. Pricing pages change often, and several
figures below come from third-party summaries that disagree with each other. Every price is
tagged:

- **[V]** confirmed from the provider's own documentation or our maintained API reference
- **[S]** consistent across several secondary sources, but not checked on the live official page
- **[E]** an estimate or assumption of ours

**Re-check every [S] item on the official pricing page before enabling paid usage.** The
application stores unit prices in configuration (`PRICING_*` settings), not in code, so a
correction doesn't need a deploy.

---

## 1. Per-service reference

### 1.1 Google Maps Places API (New)

Purpose: business discovery (Agent 3). Endpoints: `places:searchText`, `places/{id}` (Details).

Since 1 March 2025 Google gives a **free monthly allowance per SKU** instead of the old $200
credit **[V]**. The most expensive field in the `X-Goog-FieldMask` sets the SKU for the whole call.

| SKU | Free calls / month | List price per 1,000 after free tier | Fields we need it for |
|---|---|---|---|
| Text Search Essentials (IDs only) | unlimited **[S]** | $0 | place IDs only |
| Text Search Pro | 5,000 **[V]** | ≈ $32 **[S]** | name, address, location, types, business status |
| Text Search Enterprise | 1,000 **[V]** | ≈ $35 **[S]** | phone, website (`websiteUri` tier is inconsistent in Google's docs, so we assume Enterprise) |
| Place Details Enterprise | 1,000 **[V]** | ≈ $20 **[S/E]** | phone, website, hours for prospects that survive dedupe |

Strategy that keeps us in the free tier:
1. Text Search with a **Pro** mask (no phone/website). 20 results per page, up to 3 pages per query.
2. Dedupe by place_id and filter by `businessStatus = OPERATIONAL`.
3. Place Details with the **Enterprise** mask only for the survivors.

Expected usage per 100 prospects: ~8–15 Text Search Pro calls plus ~100 Details Enterprise calls.
**Free-tier capacity: about 1,000 qualified prospects per month at $0** (bounded by the 1,000
Enterprise Details calls).

Budget guard: hard monthly cap on Details Enterprise calls (`PLACES_MAX_ENTERPRISE_CALLS_MONTHLY=950`)
and a Google Cloud billing budget alert at $1.

### 1.2 SerpApi (Google Search engine)

Purpose: finding official websites and doing research (Agents 4 and 5). Endpoint: `GET https://serpapi.com/search?engine=google`.

| Plan | Price / month | Searches / month | Throughput |
|---|---|---|---|
| Free | $0 | 250 **[S]** (one source says 100, so check) | 50 / hour |
| Starter | $25 | 1,000 **[S]** | 200 / hour |
| Developer | $75 | 5,000 **[S]** | — |
| Production | $150 | 15,000 **[S]** | — |

Cached repeat searches don't count against quota **[S]**. Plans are monthly, with no overage
billing: once you hit the cap, requests fail, which our adapter maps to `QUOTA_EXCEEDED`.

Expected usage per 100 prospects:
- Verification: only for prospects where the Maps website is missing or doesn't match identity. Assume 60% × 1.5 queries ≈ **90**.
- Research on the top 20: 2–3 queries each ≈ **50**.
- **≈ 140 searches per 100 prospects.** The free tier covers roughly 175 prospects a month.

### 1.3 Firecrawl

Purpose: extracting content from verified official websites (Agents 4 and 5). Endpoints: `/v2/scrape`, `/v2/crawl`.

| Plan | Price / month | Credits | Notes |
|---|---|---|---|
| Free | $0 | 1,000 / month **[S]** | ~1 credit per page for a basic scrape |
| Hobby | $19 ($16 annual) | 5,000 **[S]** | |
| Standard | $99 | 100,000 **[S]** | |

When credits run out the API returns HTTP 402, which we map to `QUOTA_EXCEEDED`. Features like
JSON extraction, stealth proxies or browser actions can cost more than 1 credit per page, so we
use plain markdown scrapes and do extraction with Claude Haiku.

Expected usage per 100 prospects: ~40 have a functioning site × (1 identity page + up to 5 research
pages for those in the top 20) ≈ **40 + 60 = 100 credits**. The free tier covers roughly 1,000 prospects a month.

### 1.4 Claude API

Purpose: command parsing, fact extraction, reply classification (Haiku 5.5); site strategy, code
generation, repair and outreach copy (Opus 5.5).

| Model | Input $/MTok | Output $/MTok | Cache read $/MTok | Source |
|---|---|---|---|---|
| `claude-opus-5-5` | 4.00 | 20.00 | 0.20 | **[V]** |
| `claude-sonnet-5-5` | 2.00 | 10.00 | 0.10 | **[V]** |
| `claude-haiku-5-5` | 0.10 | 0.50 (≤100K-token prompts) | — | **[V]** |

The Message Batches API costs 50% of standard prices, with asynchronous results **[V]**. There's no
free tier, and you buy prepaid credits. Set a Console spend limit as well as our own caps.

Per-site generation estimate (Opus 5.5) **[E]**:

| Step | Input tokens | Output tokens | Cost |
|---|---|---|---|
| Strategy and site spec | 15K | 6K | $0.18 |
| Code generation (5–6 Astro pages and components) | 25K (scaffold cached after first call) | 35K | $0.80 |
| Repair cycles (expect ~1.5 × 40K in / 8K out) | 60K | 12K | $0.48 |
| Factual-claim check | 15K | 2K | $0.10 |
| **Total per site** | | | **≈ $1.55 (range $1.00–$3.00)** |

On Sonnet 5.5 the same work costs ≈ $0.80. With Batches (non-urgent builds) it's ≈ $0.78 on Opus.

Other Claude usage per 100 prospects **[E]**:
- Command parsing: negligible (< $0.01).
- Fact extraction with Haiku on ~100 pages × 6K in / 800 out: ≈ $0.10.
- Outreach drafts with Opus, 20 × (6K in / 600 out): ≈ $0.72 (≈ $0.36 on Sonnet).

### 1.5 Groq (deferred)

Llama 3.1 8B costs ≈ $0.05 / $0.08 per MTok and Llama 3.3 70B ≈ $0.59 / $0.79 **[S]**. The free tier
has per-model RPM and TPD limits, which sources report inconsistently. **Recommendation: don't
integrate in v1.** The Haiku 5.5 tasks above cost about $0.10 per 100 prospects, so Groq would save
pennies and cost another integration. We'll revisit if monthly classification spend goes over $5.

### 1.6 LiveKit (Phase 7)

| Plan | Price | Included |
|---|---|---|
| Build | $0 | 1,000 agent session minutes / month, 5 concurrent agent sessions, ≈ $2.50 inference credits **[S]** |
| Ship | from $50 / month | more minutes **[S]** |

STT and TTS either come out of LiveKit Inference credits or use a separate provider (for example
Deepgram or Cartesia free credits). Expected usage is ~100–300 voice minutes a month, so **$0**,
but STT/TTS beyond the included credits is ≈ $0.01–0.02 per minute **[E]**.

### 1.7 Cloudflare

| Product | Free allowance | Use |
|---|---|---|
| Pages | Unlimited static requests and bandwidth; 500 builds/month (Git-integrated); 20,000 files per deployment; 25 MiB per file **[S]** | Dashboard and previews |
| Workers | 100,000 requests/day, 10 ms CPU per invocation **[S]** | Optional webhook relay |
| Queues | Not needed in v1 | — |

Direct Upload deployments of pre-built previews don't use Git build minutes. Expected cost is **$0**.

### 1.8 Supabase

Free plan **[S]**: 500 MB database, 1 GB file storage, 5 GB egress, 50,000 MAU, 2 projects. Projects
**pause after 7 days of inactivity**, which the always-on worker prevents. Daily backups, no PITR.

Expected storage: ~50 KB of rows per prospect plus ~1–3 MB of site sources and artefacts per
generated site. 1 GB of Storage covers ~300–500 sites, so we enforce preview expiry and artefact
pruning. Expected cost is **$0**. Pro ($25/month) becomes necessary at about 5–10k prospects or
when PITR is needed.

### 1.9 Container host

A small VPS with 2 vCPU and 4 GB RAM costs ≈ **$5–8 / month [E]** (check current Hetzner or other
provider prices). Cloud Run is the alternative: free tier first, but an always-on worker isn't free.

### 1.10 Email sending (blocked on a domain)

| Option | Free allowance | Paid |
|---|---|---|
| Resend | 3,000/month, 100/day **[S/E]** | $20/month for 50k |
| Amazon SES | — | $0.10 per 1,000 emails **[S/E]** |

Both need a verified sending domain with SPF, DKIM and DMARC. A domain costs ≈ $10–12/year **[E]**,
which needs your approval.

---

## 2. Unit economics

### 2.1 Cost per 100 prospects (discovery → verification → research of the top 20)

| Item | Within free tier | If paid (no free tier left) |
|---|---|---|
| Places (≈12 Text Search Pro + 100 Details Enterprise) | $0 | ≈ $0.38 + $2.00 = $2.38 |
| SerpApi (≈140 searches) | $0 (if ≤ 250/month) | ≈ $3.50 at Starter's effective $0.025 |
| Firecrawl (≈100 credits) | $0 | ≈ $0.38 at Hobby rate |
| Claude Haiku (extraction) | $0.10 | $0.10 |
| **Total** | **≈ $0.10** | **≈ $6.40** |

### 2.2 Cost per generated website (build → QA → deploy)

| Item | Cost |
|---|---|
| Claude Opus 5.5 (strategy, code, repair, fact check) | ≈ $1.55 (range $1.00–3.00) |
| Sandbox build and Playwright QA (our host) | $0 marginal |
| Cloudflare Pages deploy | $0 |
| Outreach draft (Opus) | ≈ $0.04 |
| **Total** | **≈ $1.60 per site** (≈ $0.85 on Sonnet 5.5) |

The per-site cap is enforced at `SITE_MAX_COST_MICROS=4_000_000` ($4). Past that, the site moves to
`requires_human_review`.

---

## 3. Monthly scenarios

| Scenario | Volume | Places | SerpApi | Firecrawl | Claude | Host | Email | **Total / month** |
|---|---|---|---|---|---|---|---|---|
| A. Pilot | 150 prospects, 5 sites | $0 | $0 (free) | $0 | ≈ $10 | $6 | $0 (drafts only) | **≈ $16** |
| B. Working | 500 prospects, 25 sites | $0 | $25 (Starter) | $0 | ≈ $48 | $6 | $0 (Resend free) | **≈ $79** |
| C. Growth | 2,000 prospects, 100 sites | ≈ $20 | $75 (Developer) | $19 (Hobby) | ≈ $195 | $10 | $0–20 | **≈ $320–340** |

Claude estimates include ≈ 20% for retries and development testing. Scenario C would justify
switching generation to Sonnet 5.5 or Batches (≈ −$90/month) after we measure quality.

---

## 4. Allocating the $100 starting budget

Assumption: month 1 is development plus one real pilot campaign (Scenario A), with Phase-level
testing against real APIs at low caps.

| Bucket | Allocation | Notes |
|---|---|---|
| Claude API credits | $40 | ≈ $15 of development/integration testing, then ≈ 15 pilot sites. Console spend limit set to $40. |
| Container host | $8 | Month 1 |
| SerpApi | $0 | Stay on the free 250 searches. Upgrade to Starter ($25) only when a real campaign is blocked by quota. |
| Firecrawl, Places, Cloudflare, Supabase, LiveKit | $0 | Free tiers, with hard caps in config |
| Domain for outreach (if approved) | $12 | Annual |
| **Unallocated reserve** | **$40** | Covers SerpApi Starter in month 2, or more Claude credits, after review |

Application-level controls that enforce this:
- `MONTHLY_SPEND_CAP` = $50 and `DAILY_SPEND_CAP` = $5 globally.
- A per-campaign `budget_cap_micros`, defaulting to $10.
- Per-provider monthly unit caps (Places Enterprise calls, SERP searches, Firecrawl credits) set
  slightly below the free allowance, so we never cross into paid usage by accident.
- Every billable call reserves its worst-case cost before running.

No paid subscription will be activated without your explicit approval.
