# JARVIS

An autonomous AI employee for a website-development agency serving US businesses. JARVIS finds
prospects, checks whether they have a website, researches them, scores them, builds a
personalised preview site, tests it, deploys it to a temporary URL and handles outreach within
the limits you authorise.

**Status: architecture review. No application code yet.**

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, agents, data flow, state machines, topology, failure recovery |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Phases, tasks, acceptance criteria, tests, deployment, risks |
| [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) | Every variable, with purpose, source, consumer, classification and validation |
| [docs/API_COST_MODEL.md](docs/API_COST_MODEL.md) | Provider pricing, unit economics, monthly scenarios, $100 allocation |
| [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) | Tables, constraints, indexes, RLS, retention and provenance |
| [docs/SECURITY_AND_COMPLIANCE.md](docs/SECURITY_AND_COMPLIANCE.md) | Google Maps terms, crawling, sandboxing, secrets, CAN-SPAM, autonomy limits |
| [docs/SYSTEM_DIAGRAM.md](docs/SYSTEM_DIAGRAM.md) | Mermaid diagrams |

Planned stack: React/Vite/TS dashboard on Cloudflare Pages · FastAPI + Python worker on a small
container host · Supabase (Postgres, Auth, Storage) · Google Places API (New) · SerpApi ·
Firecrawl · Claude API · Playwright · Cloudflare Pages previews · LiveKit (voice, later).
