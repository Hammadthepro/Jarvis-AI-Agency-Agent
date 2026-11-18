# JARVIS

An autonomous AI employee for a website-development agency serving US businesses. JARVIS finds
prospects, checks whether they have a website, researches them, scores them, builds a
personalised preview site, tests it, deploys it to a temporary URL and handles outreach within
the limits you authorise.

**Status: architecture review, revision 3. No application code yet.** See [CHANGELOG.md](CHANGELOG.md).

| Document | Contents |
|---|---|
| [docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md) | Product requirements: commands, rating filter, website availability states, compliance gate |
| [docs/API_CONTRACTS.md](docs/API_CONTRACTS.md) | REST and internal adapter contracts, error codes, command-parsing fixtures |
| [docs/VERIFICATION_REGISTER.md](docs/VERIFICATION_REGISTER.md) | Confirmed facts, estimates and open legal questions |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, agents, data flow, state machines, topology, failure recovery |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Phases, tasks, acceptance criteria, tests, deployment, risks |
| [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) | Every variable, with purpose, source, consumer, classification and validation |
| [docs/API_COST_MODEL.md](docs/API_COST_MODEL.md) | Provider pricing, unit economics, monthly scenarios, $100 allocation |
| [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) | Tables, constraints, indexes, RLS, retention and provenance |
| [docs/SECURITY_AND_COMPLIANCE.md](docs/SECURITY_AND_COMPLIANCE.md) | Google Maps Platform terms for the Places API, crawling, sandboxing, secrets, CAN-SPAM, autonomy limits |
| [docs/SYSTEM_DIAGRAM.md](docs/SYSTEM_DIAGRAM.md) | Mermaid diagrams |

Planned stack: React/Vite/TS dashboard and pages.dev previews on Cloudflare Pages · FastAPI,
Python workers, Playwright and the build sandbox on a required VPS · Supabase (Postgres, Auth, Storage) · Google Places API (New) Text Search (the only Google Maps Platform API used; gated) · SerpApi ·
Firecrawl · Claude API (Haiku 5.5 / Sonnet 5.5, Opus 5.5 for escalation) · Playwright · Cloudflare Pages previews · LiveKit (voice, later).
