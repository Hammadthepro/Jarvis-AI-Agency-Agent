# JARVIS — API Cost Model

Status: **Proposed, revision 2.** Prices researched 2026-11-18.

Revision 2: VPS is now a mandatory recurring cost. The domain was removed from the mandatory
budget. Claude routing changed (Haiku / Sonnet, with Opus for escalation only). Places costs were
recalculated with the rechecked field masks. Development and testing costs were added. Mandatory
and optional spend are now separated.

Source tags:
- **[V]**: provider's own documentation, or our maintained Claude API reference
- **[S]**: consistent across secondary sources, not confirmed on the live official pricing page
- **[E]**: our estimate

**Every [S] price must be checked on the official page before any paid usage is enabled.**
Unit prices live in configuration (`PRICING_*`, versioned as `pricing_version`), not in code.

**No paid subscription or plan upgrade is ever activated automatically.** JARVIS can recommend an
upgrade. Only you can purchase one, outside the app. Raising an in-app cap above a free allowance
needs an explicit, audited `allow_paid` change.

---

## 1. Provider reference

### 1.1 Google Maps Places API (New)

Free monthly allowance per SKU, in place of the old $200 credit **[V]**. The highest-tier field in
the field mask sets the SKU **[V]**. Field-to-SKU mapping confirmed from Google's Text Search (New)
and Place Details (New) documentation **[V]**.

| SKU | Free / month | Price per 1,000 beyond free | JARVIS mask |
|---|---|---|---|
| Text Search Essentials (IDs Only) | unlimited **[S]** | $0 | `IDS_ONLY` |
| Text Search Pro | 5,000 **[V]** | ≈ $32 **[S]** | `DISCOVERY_PRO` (fallback) |
| Text Search Enterprise | 1,000 **[V]** | ≈ $35 **[S]** | `DISCOVERY_ENTERPRISE` (primary) |
| Place Details Pro | 5,000 **[V/S]** | ≈ $17 **[S/E]** | `DETAILS_DISPLAY` (on demand) |
| Place Details Enterprise | 1,000 **[V]** | ≈ $20 **[S/E]** | `DETAILS_CONTACT` (on demand, rare) |

Text Search is billed **per request**, and each request returns up to 20 places **[V]**. Discovery
therefore uses one Enterprise request per page of 20 results, instead of a Details call per place.

Per 100 qualified-candidate prospects: about 200 raw results are needed after filtering for
non-operational listings, duplicates and off-niche results, plus overhead for splitting large
cities into tiles. That's **≈ 12 Text Search Enterprise requests [E]**.
**Free capacity: ≈ 8,000 prospects/month at $0.** Paid equivalent: ≈ $0.42 per 100 prospects.

Default caps (`usage_limits`): Text Search Enterprise 900/month, 60/day. Details Pro 300/month.
Details Enterprise 50/month. The Google Cloud per-day quota is set below these (§3).

### 1.2 SerpApi

| Plan | Price / month | Searches | Notes |
|---|---|---|---|
| Free | $0 | 250 **[S]** (one source says 100) | 50/hour |
| Starter | $25 | 1,000 **[S]** | optional |
| Developer | $75 | 5,000 **[S]** | optional |

There's no overage billing: requests fail at the cap. Cached repeats are free **[S]**.
Per 100 prospects: verification ≈ 100 searches (≈ 50–60 prospects without a confirmed Maps website,
≤ 2 queries each) + research of the top 20 at 2 each = 40. **≈ 140 searches [E].**
Free capacity ≈ 175 prospects/month. Default cap: 240/month, 45/hour.

### 1.3 Firecrawl

| Plan | Price / month | Credits |
|---|---|---|
| Free | $0 | 1,000/month **[S]** |
| Hobby | $19 | 5,000 **[S]** (optional) |

It returns HTTP 402 when credits run out. We use plain scrapes (≈ 1 credit/page) and run
extraction with Haiku.
Per 100 prospects: identity scrapes ≈ 20, independent identity pages for no-website prospects in
the top-20 ≈ 30, research crawls of functioning sites ≈ 50. **≈ 100 credits [E].** Free capacity
≈ 1,000 prospects/month. Default cap: 950/month.

### 1.4 Claude API

| Model | Role in JARVIS | Input $/MTok | Output $/MTok | Cache read $/MTok |
|---|---|---|---|---|
| `claude-haiku-5-5` | classification, extraction, routing, fact checks | 0.10 | 0.50 (prompts ≤ 100K) | — |
| `claude-sonnet-5-5` | strategy, generation, ordinary repair, outreach copy | 2.00 | 10.00 | 0.10 |
| `claude-opus-5-5` | escalation only | 4.00 | 20.00 | 0.20 |
All **[V]**. Message Batches: 50% off **[V]**. No free tier. Credits are prepaid. A Console spend limit is set as well as JARVIS caps.

#### Per-site cost (recalculated) [E]

| Step | Model | Tokens in / out | Expected cost | Worst case |
|---|---|---|---|---|
| Strategy → `SiteSpec` | Sonnet | 15K / 6K | $0.09 | $0.18 (re-plan once) |
| Strategy re-plan after 2 diversity rejections | Opus | 15K / 6K | 10% × $0.18 = $0.02 | $0.18 |
| Page/component generation | Sonnet | 25K / 35K | $0.40 | $0.40 |
| Ordinary repair (expected 1.2 cycles, max 2) | Sonnet | 40K / 8K per cycle | $0.19 | $0.32 |
| Repair escalation (cycle 3) | Opus | 50K / 12K | 15% × $0.44 = $0.07 | $0.44 |
| Factual-claim check, diversity text check (≈ 3 runs) | Haiku | 15K / 2K per run | $0.01 | $0.02 |
| Outreach draft | Sonnet | 6K / 0.6K | $0.02 | $0.02 |
| **Total per site** | | | **≈ $0.80** | **≈ $1.56** |

Hard cap `SITE_MAX_COST_MICROS = 2,000,000` ($2.00). A site that hits it goes to `requires_human_review`.
Optional levers: Batches for non-urgent builds (≈ $0.42/site); prompt caching of the scaffold
and system prompt (included in the estimate).
For comparison, revision 1 (Opus for everything) was ≈ $1.60/site expected.

#### Other Claude usage [E]
- Command parsing and routing (Haiku): < $0.001 per command.
- Fact extraction (Haiku): ≈ $0.001 per page. ≈ $0.20 per 100 prospects including the top-20 research.
- Reply classification (Haiku): < $0.001 per reply.

### 1.5 Groq (deferred)
Llama 3.1 8B ≈ $0.05/$0.08 per MTok **[S]**. Not integrated in v1: Haiku covers this work for
≈ $0.20 per 100 prospects. We'll revisit if classification spend goes over $5/month.

### 1.6 LiveKit (Phase 7)
Build plan: $0, 1,000 agent session minutes/month, 5 concurrent sessions, ≈ $2.50 inference
credits **[S]**. Expected 100–300 minutes/month → $0. STT/TTS beyond the included credits ≈ $0.01–0.02/min **[E]**.

### 1.7 Cloudflare (free plan)
| Product | Allowance | Use |
|---|---|---|
| Pages | unlimited static requests and bandwidth. Git builds 500/month. 20,000 files/deployment. 25 MiB/file **[S]** | dashboard, previews (Direct Upload uses no build minutes) |
| Workers | 100,000 requests/day, short CPU time per request **[S]** | unsubscribe endpoint, webhook relay only (not long-running work) |
| Workers KV | free tier with daily write limits **[S]** | holds unsubscribe events while the API is down |
| R2 | 10 GB-month storage free **[S]** | off-site encrypted DB backups |
Expected: **$0**.

### 1.8 Supabase (Free)
500 MB DB, 1 GB Storage, 5 GB egress, 50k MAU, 2 projects **[S]**. **No managed backups and no
PITR on Free [S]. Projects pause after ≈ 7 days of low activity and need a manual resume [S].**
We don't assume worker traffic prevents the pause (ARCHITECTURE §12). Expected: **$0**.
Optional: Pro at $25/month **[S]**, which removes the pause and adds daily backups. Recommended once JARVIS is in regular production use.

### 1.9 VPS (required)
Hosts FastAPI, workers, Playwright/Chromium and the build sandbox. Spec: 2 vCPU, 4 GB RAM, ≥ 40 GB
disk (for example Hetzner CX23-class, ≈ €5.49 + €0.50 IPv4 excluding VAT after June 2026 pricing **[S]**).
**Budget: $10/month, recurring and mandatory** (the instance plus a margin for VAT and currency;
add provider snapshots at about +20% if wanted). Scale-up to 4 vCPU / 8 GB at growth volume ≈ $15–20/month **[E]**.

### 1.10 Email sending (optional; sending blocked until ready)
| Option | Free | Paid |
|---|---|---|
| Resend | 3,000/month, 100/day **[S]** | $20/month |
| Amazon SES | — | ≈ $0.10 per 1,000 **[S]** |
Requires an authenticated sending domain. **A domain purchase is not part of the mandatory budget.**
Drafts work without any email setup.

---

## 2. Unit economics

### 2.1 Per 100 prospects (discover → verify → research top 20 → score)

| Item | Within free tiers | At paid rates |
|---|---|---|
| Places (≈ 12 Text Search Enterprise) | $0 | ≈ $0.42 |
| SerpApi (≈ 140) | $0 (≤ 250/month) | ≈ $3.50 (Starter effective $0.025) |
| Firecrawl (≈ 100 credits) | $0 | ≈ $0.38 |
| Claude Haiku | ≈ $0.20 | ≈ $0.20 |
| **Total** | **≈ $0.20** | **≈ $4.50** |

### 2.2 Per generated website
| Item | Cost |
|---|---|
| Claude (Sonnet main, Haiku checks, Opus escalation) | ≈ $0.80 expected, $1.56 worst case, $2.00 hard cap |
| Sandbox build and QA | $0 marginal (VPS) |
| Cloudflare Pages preview | $0 |
| **Total** | **≈ $0.80** |

---

## 3. Billing protection

| Provider | App-level hard stop (before the request) | Provider-side hard stop | Warning only |
|---|---|---|---|
| Google Places | `usage_limits` per SKU/day/month (below the free allowance) + per-campaign caps | **Cloud Console quota:** per-day request limits on `SearchText` (e.g. 50/day) and `GetPlace` (e.g. 20/day), below the app caps to allow for enforcement lag | Cloud Billing budget alerts at $1 / $5. **Alerts don't cap spend** |
| SerpApi | monthly and hourly caps below the plan limit | plan limit (no overage) | 80% alert |
| Firecrawl | monthly credit cap | plan credits (auto-recharge **off**) | 80% alert |
| Claude | per-call `max_tokens` → worst-case reservation, per-site cap, daily and monthly caps, Opus escalation caps | Console workspace spend limit | 80% alert |
| Cloudflare / Supabase / LiveKit | free plan, no paid plan attached | no billing method, or paid features off | — |

Usage display: the Activity → Costs view shows **Estimated (JARVIS ledger)** and
**Provider-reported** in separate columns, with the variance and the sync time:
- SerpApi: account API (searches used this month), synced hourly.
- Firecrawl: credit-usage API, synced hourly.
- Claude: exact token `usage` from each response (so the ledger is exact for tokens), plus the Usage and Cost API if an admin key is configured.
- Google: no simple usage API is used in v1. Usage is entered manually or imported from Cloud Billing reports, labelled "manual". If nothing is imported, the column reads "not available".

---

## 4. Development and testing costs (one-off, month 1) [E]

| Activity | Provider usage | Cost |
|---|---|---|
| Command-parsing eval (40 commands × ~5 runs) | Haiku | ≈ $0.05 |
| Extraction faithfulness eval (~200 pages) | Haiku + Firecrawl 200 credits (free) | ≈ $0.20 |
| Reply-classification eval | Haiku | ≈ $0.05 |
| Website builder prompt development (~20 iterative builds) | Sonnet/Opus | ≈ $16 |
| Cross-niche diversity dataset run (12 sites, plan §Testing) | Sonnet/Opus | ≈ $10 |
| Capped live integration tests (all phases) | Places ≤ 30 req, SerpApi ≤ 60, Firecrawl ≤ 60, Claude ≤ $3, Cloudflare ≤ 20 deploys | ≈ $3 (Places/SerpApi/Firecrawl within free) |
| **Development total** | | **≈ $29** |

---

## 5. Monthly operating scenarios (after development)

| Scenario | Volume | VPS (mand.) | Claude (mand.) | Places | SerpApi | Firecrawl | Supabase | Email | **Mandatory** | **Optional** |
|---|---|---|---|---|---|---|---|---|---|---|
| A. Pilot | 150 prospects, 5 sites | $10 | ≈ $5 | $0 | $0 (free) | $0 | $0 | $0 (drafts) | **≈ $15** | $0 |
| B. Working | 500 prospects, 25 sites | $10 | ≈ $25 | $0 | Starter $25 | $0 | $0 | $0 | **≈ $35** | ≈ $25 |
| C. Growth | 2,000 prospects, 100 sites | $15–20 | ≈ $98 | $0 | Developer $75 | Hobby $19 | Pro $25 (recommended) | $0 (Resend free) + domain ≈ $1/mo | **≈ $113–118** | ≈ $120 |

Claude figures include +20% for retries. Without the optional SerpApi plan, scenario B is limited
to about 175 prospects/month by the free 250 searches. The app blocks at the cap; it doesn't charge.

---

## 6. Final $100 budget breakdown (initial period)

### 6.1 Mandatory

| Item | Amount | Notes |
|---|---|---|
| VPS, month 1 | $10 | recurring $10/month after that |
| Claude API credits, development and testing | $29 | §4 |
| Claude API credits, first pilot campaign | $6 | Scenario A (5 sites + research) |
| Google Places / SerpApi / Firecrawl / Cloudflare / Supabase / LiveKit / GitHub | $0 | free tiers. App caps below each allowance. Google requires a billing account on file |
| **Mandatory total** | **$45** | |

### 6.2 Optional (only with your explicit approval, never automatic)

| Item | Amount | Trigger |
|---|---|---|
| SerpApi Starter | $25/month | a real campaign is blocked by the 250-search cap |
| Firecrawl Hobby | $19/month | 1,000 credits/month isn't enough |
| Supabase Pro | $25/month | regular production use (removes the pause, adds managed backups) |
| Sending domain | ≈ $10–12/year | you want outreach to send, and don't already have a domain |
| Email provider paid plan | $0–20/month | more than 100 emails/day |
| VPS snapshots | ≈ $2/month | provider-level backups as well as `pg_dump` |
| LiveKit STT/TTS overage | ≈ $1–3/month | Phase 7, beyond included credits |

### 6.3 Reserve

| Item | Amount |
|---|---|
| **Unallocated reserve** | **$55** |

The reserve covers month 2 (VPS $10 + pilot Claude ≈ $5) and leaves room for one optional item,
such as SerpApi Starter, once you approve it.

### 6.4 Matching app caps
- `BUDGET_MONTHLY_CAP_USD = 35` (metered API spend, Claude mainly). The Claude Console spend limit is set to $35.
- `BUDGET_DAILY_CAP_USD = 5`.
- `BUDGET_CAMPAIGN_DEFAULT_USD = 5`.
- Provider unit caps below each free allowance. `allow_paid = false` everywhere.
