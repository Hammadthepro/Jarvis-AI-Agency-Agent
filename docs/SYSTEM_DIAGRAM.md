# JARVIS — System Diagrams

All diagrams are Mermaid and render on GitHub.

## 1. Architecture

```mermaid
flowchart LR
  subgraph User
    U[Operator<br/>browser / mic]
  end

  subgraph CF[Cloudflare]
    D[Dashboard SPA<br/>Pages]
    P[jarvis-previews<br/>Pages · branch per site]
    R[Webhook relay<br/>Worker · optional]
  end

  subgraph Host[Container host · Docker Compose]
    API[FastAPI<br/>REST + SSE + auth + policy]
    W[Worker<br/>task runner · agents · scheduler · LangGraph]
    SB[[Build sandbox<br/>no network · Astro · Playwright]]
    V[Voice agent<br/>LiveKit · Phase 7]
  end

  subgraph SUPA[Supabase]
    DB[(Postgres<br/>tasks · prospects · evidence · ledger)]
    AU[Auth]
    ST[(Storage<br/>site sources · artefacts)]
  end

  subgraph EXT[External APIs]
    GP[Google Places New]
    SE[SerpApi]
    FC[Firecrawl]
    CL[Claude API]
    LK[LiveKit Cloud]
    EM[Email provider]
  end

  U --> D
  U <-.audio.-> LK
  D -- JWT --> API
  D -- login --> AU
  API <--> DB
  W <--> DB
  W <--> ST
  W -- spawn --> SB
  W --> GP & SE & FC & CL & EM
  W -- direct upload --> P
  API --> CL
  V <--> LK
  V -- /commands --> API
  EM -- webhooks --> R --> API
```

## 2. Agent interaction

```mermaid
flowchart TB
  CMD[1 · Command agent<br/>Haiku 5.5] -->|CommandIntent| ORC
  ORC{{2 · Orchestrator & Policy<br/>deterministic}}
  GW[[ToolGateway<br/>kill switch · scope · budget · usage]]

  ORC -->|discovery tasks| DIS[3 · Discovery]
  ORC -->|verify tasks| VER[4 · Verification]
  ORC -->|research tasks| RES[5 · Research]
  ORC -->|score| QUA[6 · Qualification<br/>pure function]
  ORC -->|build| WB[7 · Website Builder<br/>Opus 5.5]
  WB <-->|LangGraph repair loop ≤3| QA[8 · QA<br/>sandbox + Playwright]
  ORC -->|deploy| DEP[9 · Deployment]
  ORC -->|send within authorisation| OUT[10 · Outreach]

  DIS & VER & RES & WB & QA & DEP & OUT --> GW
  GW --> EXT[(Providers)]
  DIS & VER & RES & QUA & WB & QA & DEP & OUT -->|results + events| DB[(Postgres)]
  DB --> ORC
```

## 3. Lead generation flow

```mermaid
sequenceDiagram
  autonumber
  actor Op as Operator
  participant API
  participant Pol as Policy
  participant Wk as Worker
  participant GP as Google Places
  participant SE as SerpApi
  participant FC as Firecrawl
  participant DB as Postgres

  Op->>API: "Find 100 plumbers in Houston without websites"
  API->>API: Command agent → CommandIntent
  API->>Pol: plan + cost estimate
  Pol-->>Op: confirmation card (calls, est. cost, limits)
  Op->>API: Authorise
  API->>DB: campaign authorized → queued, root task
  Wk->>DB: claim discovery task (SKIP LOCKED)
  Wk->>GP: Text Search (Pro mask) × N pages
  Wk->>GP: Place Details (Enterprise mask) for survivors
  Wk->>DB: prospects (place_id) + places_cache (TTL)
  loop each prospect
    Wk->>Wk: DNS/HTTP probe of Maps website (SSRF guard)
    alt missing or mismatched
      Wk->>SE: ≤2 Google searches
      Wk->>Wk: probe candidates + identity match
    end
    Wk->>DB: website_audit (status, evidence, confidence)
  end
  loop top-N prospects
    Wk->>FC: bounded scrape/crawl of official site
    Wk->>SE: research queries
    Wk->>Wk: extract facts (Haiku) + quote verification
    Wk->>DB: research_facts (verified/inferred/unknown/conflicting)
  end
  Wk->>DB: lead_scores (deterministic, explained)
  DB-->>Op: live progress via SSE
```

## 4. Website production flow

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> strategy: Opus 5.5 → SiteSpec (verified facts only)
  strategy --> generating: diversity guard passed
  generating --> building: source → Storage
  building --> qa: sandbox build OK
  building --> repair: build error
  qa --> passed: all checks pass
  qa --> repair: failures
  repair --> building: cycle < 3
  repair --> requires_human_review: cycle = 3
  passed --> deployed: Pages direct upload + live verification
  deployed --> expired: TTL cleanup
  generating --> failed: budget / refusal
  requires_human_review --> generating: operator edits / approves retry
```

## 5. Deployment and outreach flow

```mermaid
sequenceDiagram
  autonumber
  participant Wk as Worker
  participant SB as Sandbox
  participant CF as Cloudflare Pages
  participant Pol as Policy
  participant EM as Email provider
  participant Pr as Prospect inbox
  participant DB as Postgres

  Wk->>SB: build + QA (no network)
  SB-->>Wk: static artefact + QA report
  Wk->>CF: direct upload (project jarvis-previews, branch=slug)
  CF-->>Wk: deployment id + https://slug.jarvis-previews.pages.dev
  Wk->>CF: GET URL (200, title, noindex header)
  Wk->>DB: deployment verified, expires_at
  Note over Wk,DB: Outreach requires outreach_campaign = authorized,<br/>sending domain, postal address
  Wk->>Wk: draft (observations → fact ids, preview link)
  Wk->>Pol: check limits, send window, suppression (same txn)
  Pol-->>Wk: allowed
  Wk->>EM: send (idempotency key, List-Unsubscribe)
  EM->>Pr: email
  Pr-->>EM: reply / unsubscribe / bounce
  EM-->>DB: signed webhook → reply classified
  alt opt-out / bounce / negative
    DB->>DB: suppression_list + stop sequence
  else interested / question
    DB-->>Wk: flag for human (no pricing commitments)
  end
```
