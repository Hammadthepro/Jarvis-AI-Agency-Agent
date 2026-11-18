# JARVIS — System Diagrams

Revision 2. Mermaid diagrams, which render on GitHub. Changes in this revision:
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
    GP[Google Places New]
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
  W --> GP
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
  loop each discovery page task (20 places max)
    Wk->>Pol: reserve 1 Text Search Enterprise unit
    alt cap would be exceeded
      Pol-->>Wk: BUDGET_BLOCKED (no request sent)
    else reserved
      Wk->>GP: Text Search, DISCOVERY_ENTERPRISE mask
      GP-->>Wk: places held in memory only
      Wk->>Wk: dedupe by place_id, keep OPERATIONAL
      Wk->>Wk: probe websiteUri hint (SSRF guard, identity match)
      Wk->>SE: up to 2 searches when needed (hash-only query storage)
      Wk->>DB: place_refs.place_id, website_audit, independent evidence
      Wk->>Wk: discard Places data
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

## 4. Website production flow

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

## 5. Deployment and outreach flow

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
