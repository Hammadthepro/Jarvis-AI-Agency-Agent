# JARVIS — System Diagrams

Revision 3. Mermaid diagrams, which render on GitHub. Revision 3 adds: the Google Places API (New) Text Search as the only Google API, the compliance gate, the rating filter, and evidence-based website states.

Revision 2 changes:
- the VPS is a required component;
- Workers handle edge handlers only;
- Places content stays in memory only;
- model routing is Haiku, Sonnet, then Opus;
- a sending-readiness gate is added.

## 1. Architecture

```mermaid
flowchart LR
  U[Operator<br/>browser / mic]

  subgraph CF[Cloudflare · free]
    D[Dashboard SPA<br/>Pages]
    P[jarvis-previews<br/>Pages · branch per site]
    E[Edge Worker · workers.dev<br/>unsubscribe + webhook relay<br/>short handlers only]
    KV[(Workers KV<br/>unsubscribe buffer)]
  end

  subgraph VPS[VPS · REQUIRED · Docker Compose]
    API[FastAPI<br/>REST · SSE · auth · policy]
    W[Python workers<br/>agents · scheduler · LangGraph<br/>DB health monitor]
    SB[[Build sandbox<br/>no network · Astro · Playwright]]
    BK[Backup job<br/>pg_dump · encrypted]
    V[Voice agent · Phase 7]
  end

  subgraph SUPA[Supabase Free]
    DB[(Postgres<br/>place_refs = place_id only<br/>independent evidence · ledger)]
    AU[Auth]
    ST[(Storage)]
  end

  subgraph EXT[External APIs]
    GP[Google Places API New<br/>Text Search · Place Details<br/>gated]
    SE[SerpApi]
    FC[Firecrawl]
    CL[Claude<br/>Haiku · Sonnet · Opus]
    LK[LiveKit]
    EM[Email provider<br/>when ready]
  end

  R2[(Off-site backup<br/>R2)]

  U --> D
  D -- JWT --> API
  D -- login --> AU
  API <--> DB
  W <--> DB
  W <--> ST
  W -- spawn --> SB
  W -- compliance gate --> GP
  W --> SE
  W --> FC
  W --> CL
  W --> EM
  W -- direct upload --> P
  BK --> DB
  BK --> R2
  EM -- webhooks --> E
  U -- unsubscribe link --> E
  E <--> KV
  E -- HMAC forward --> API
  V <--> LK
  V -- /commands --> API
```

## 2. Agent interaction

```mermaid
flowchart TB
  CMD[1 · Command<br/>Haiku 5.5] -->|CommandIntent| ORC
  ORC{{2 · Orchestrator & Policy<br/>deterministic Python}}
  GW[[ToolGateway<br/>kill switch · scope · reserve caps BEFORE call · usage]]

  ORC --> DIS[3 · Discovery]
  DIS -->|in-memory candidates| VER[4 · Verification]
  ORC --> RES[5 · Research<br/>Haiku extraction]
  ORC --> QUA[6 · Qualification<br/>pure Python]
  ORC --> WB[7 · Website Builder<br/>Sonnet 5.5]
  WB <-->|repair loop: Sonnet x2 then Opus x1| QA[8 · QA<br/>sandbox · Haiku fact check]
  ORC --> DEP[9 · Deployment]
  ORC --> OUT[10 · Outreach<br/>drafts always · sends gated]

  DIS --> GW
  VER --> GW
  RES --> GW
  WB --> GW
  QA --> GW
  DEP --> GW
  OUT --> GW
  GW --> EXT[(Providers)]
  GW -. BUDGET_BLOCKED: request not sent .-> ORC
```

## 3. Lead generation flow

```mermaid
sequenceDiagram
  autonumber
  actor Op as Operator
  participant API
  participant Pol as Policy / ToolGateway
  participant Wk as Worker
  participant GP as Google Places
  participant SE as SerpApi
  participant FC as Firecrawl
  participant DB as Postgres

  Op->>API: Find 100 plumbers in Houston without websites
  API->>API: Command agent (Haiku) gives CommandIntent
  API->>Pol: plan and worst-case estimate
  Pol-->>Op: confirmation card (units, est. cost, caps)
  Op->>API: Authorise
  API->>DB: campaign authorized then queued
  Pol->>DB: compliance gate google_places_prospect_selection
  alt gate closed
    Pol-->>Op: COMPLIANCE_BLOCKED (campaign stays draft, use manual_import)
  else gate approved
  loop each discovery page task (up to 20 places, 60 per query)
    Wk->>Pol: reserve 1 Text Search Enterprise unit
    alt cap would be exceeded
      Pol-->>Wk: BUDGET_BLOCKED (no request sent)
    else reserved
      Wk->>GP: places:searchText, DISCOVERY_ENTERPRISE mask, minRating 3.5
      GP-->>Wk: places with rating, websiteUri, businessStatus (memory only)
      Wk->>Wk: dedupe, OPERATIONAL, rating check, missing-rating policy
      Wk->>Wk: probe listed site (SSRF guard, identity, second probe if failing)
      Wk->>SE: required independent checks when no site is listed
      Wk->>DB: place_id, availability state + checks + evidence
      Wk->>Wk: discard Places data incl. rating
    end
  end
  end
  loop top-N prospects
    Wk->>SE: research queries
    Wk->>FC: bounded scrape of official / directory pages
    Wk->>Wk: Haiku extracts facts with quotes, quote verifier
    Wk->>DB: research_facts (independent sources only)
  end
  Wk->>DB: lead_scores (deterministic)
  DB-->>Op: progress via SSE
```

## 4. Website availability states

```mermaid
stateDiagram-v2
  [*] --> WEBSITE_LISTED: a source lists a URL
  [*] --> no_listing: no URL from any source
  WEBSITE_LISTED --> WEBSITE_WORKING: responds, identity match
  WEBSITE_LISTED --> WEBSITE_BROKEN: fails twice, at least 10 min apart
  WEBSITE_LISTED --> no_listing: URL belongs to another business
  WEBSITE_LISTED --> WEBSITE_UNKNOWN: checks incomplete
  no_listing --> NO_WEBSITE: all required independent checks completed, confidence at threshold
  no_listing --> WEBSITE_UNKNOWN: incomplete, weak or conflicting evidence
  WEBSITE_UNKNOWN --> WEBSITE_LISTED: re-verify
  WEBSITE_UNKNOWN --> no_listing: re-verify
```
`no_listing` is an in-task step, not a persisted state. A Google-only `WEBSITE_LISTED` is also resolved within the task.

## 5. Website production flow

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> strategy
  strategy --> generating: SiteSpec by Sonnet, diversity guard passed
  strategy --> strategy: rejected once, re-plan by Sonnet
  strategy --> generating: rejected twice, re-plan by Opus
  generating --> building: source saved to Storage
  building --> qa: sandbox build OK
  building --> repair: build error
  qa --> passed: all checks pass
  qa --> repair: failures
  repair --> building: cycles 1-2 Sonnet, cycle 3 Opus
  repair --> requires_human_review: after cycle 3
  passed --> deployed: Pages direct upload and live check
  deployed --> expired: TTL cleanup
  generating --> failed: site cost cap or refusal
  requires_human_review --> generating: operator approves retry
```

## 6. Deployment and outreach flow

```mermaid
sequenceDiagram
  autonumber
  participant Wk as Worker
  participant SB as Sandbox
  participant CF as Cloudflare Pages
  participant RD as Sending readiness
  participant EM as Email provider
  participant Ed as Edge Worker
  participant DB as Postgres

  Wk->>SB: build and QA (no network)
  SB-->>Wk: static artefact and QA report
  Wk->>CF: direct upload, branch = slug
  CF-->>Wk: slug.jarvis-previews.pages.dev
  Wk->>CF: verify 200, title, noindex
  Wk->>DB: deployment verified, expires_at
  Wk->>DB: outreach DRAFT (always allowed, no email config needed)
  Wk->>RD: check postal address, SPF/DKIM/DMARC, provider, unsubscribe, authorisation
  alt any item failing
    RD-->>Wk: BLOCKED_COMPLIANCE (draft kept)
  else all pass
    Wk->>DB: suppression check and send record (one transaction)
    Wk->>EM: send with idempotency key and List-Unsubscribe
    EM-->>Ed: signed webhook (reply, bounce, complaint)
    Ed->>DB: forwarded via API, classify, suppress if needed
  end
```
