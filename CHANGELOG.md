# JARVIS Changelog

## Revision 14: website preview pipeline repair, Cartesia voice (2026-11-21)
- **QA runner install failure (root cause):**
  - The Codespaces image is Ubuntu 20.04, where Playwright 1.63 cannot install Chromium (`ERROR: Playwright does not support chromium on ubuntu20.04-x64`, reproduced). QA ran Chromium on the host (`QA_RUNNER_MODE=subprocess`), which was also not isolated.
  - `dev-up.sh` / `dev-setup.sh` now build the digest-pinned Docker QA sandbox (Ubuntu 24.04, `--network none`, read-only, non-root, no capabilities, no secrets) and prove QA executes with a real self-check (`jarvis.agents.qa_selfcheck`).
  - The worker then runs with `QA_RUNNER_MODE=docker`. `.env` is never edited. `dev-status.sh` shows the result.
- **Reproducible QA dependencies:** the lockfile carried a second `playwright-core` (1.65.1, pulled in by `@axe-core/playwright`'s peer range) next to Playwright 1.63.0. It is now pinned to a single 1.63.0, matching the sandbox's browsers.
- **Pipeline honesty:**
  - A site whose build/QA or deployment ultimately fails is marked `failed`, with the reason and retry position (`generated_sites.last_error`, migration 0012). It is no longer left looking "in progress".
  - A deployment whose `pages.dev` URL does not verify is now a retried failure, not a successful task.
  - A campaign that asked for previews but completed none ends as `requires_human_review` with the reasons, never `completed`.
- **Preview correctness:**
  - The same phone number in two formats ("(615) 555-0121" vs "+16155550121") was flagged as conflicting, which dropped the phone from previews. Phones now compare as the 10-digit number.
  - Site copy uses the business's own verified city (a nationwide campaign has no city).
  - Cloudflare branch names always keep the site's unique suffix, so one prospect's preview can no longer replace another's.
- **Dashboard:** each preview shows the business, QA and deployment status, the preview link, screenshots, and the failure reason with retry information. JARVIS's `work_status` returns the same per-preview detail.
- **Cartesia voice** through the official `livekit-plugins-cartesia==1.8.12`:
  - `sonic-3`, voice "Katie" by default; voice, model, speed and emotion configurable.
  - The WebSocket is prewarmed while the browser joins, and text pacing is on.
  - New Integrations card: the key is stored encrypted, and Test connection is a free voice lookup.
  - Without a key, voice is NOT_CONFIGURED and the agent says so; no other speech provider or LiveKit Inference route is used. The live-test speech generators also moved off the retired `cartesia/sonic-2` Inference route.
- **No unapproved billable voice features:** the turn detector is now the local `v1-mini` model and interruption detection is local VAD. The LiveKit Inference cloud versions are opt-in. Endpointing is dynamic (0.5–3.0 s).
- **Measured latency** per turn (`voice.latency`): endpointing, first Claude text, first audio, Cartesia TTFB, browser-audible (reported by the dashboard), total, and interrupt-to-stop. `verify-live.sh` prints it.
- Spoken replies never include ids, URLs or markdown.

## Revision 13: JARVIS V2: objectives, provider-specific discovery, complete workflows, natural streaming voice (2026-11-20)
- **Root causes found:**
  - **Rigid questions and forgotten objective:** Claude handed the owner's raw words to `start_prospect_search`, which ran the old regex parser. The parser asked its own canned questions ("Which type of business and which city or area should I search?") and knew nothing of earlier turns.
  - **Places blocking everything:** every search defaulted to Google Places, so its restriction blocked all discovery.
  - **"I couldn't finish that in one go":** the tool loop ran out after 5 steps.
- **Structured objective** shared by voice and text, stored in `conversation_state.objective` and on the campaign (migration 0011).
  - Claude records details with `set_objective`. The server applies defaults and returns at most one essential question.
  - A changed objective withdraws the old proposal.
  - `propose_objective` produces the plan. After the owner's yes, `confirm_pending_action` creates the campaign, confirms its first task is queued, and returns the real campaign and task ids.
  - The whole objective survives, including "build their websites and show me".
  - The identity question is answered truthfully: JARVIS, powered by the configured Claude model.
- **Provider-specific policy** (`jarvis/core/provider_policy.py`).
  - Google Places API (New) stays blocked until approval is recorded, and is used only when asked for explicitly.
  - SerpApi is the default discovery source (Google Maps engine). Its status and basis are its own, as are Firecrawl's and imported lists'.
  - A blocked source is explained with that source's own reason. The Commands console and system readiness show each source separately.
- **SerpApi discovery stage** (`discovery.serp`):
  - city, state or nationwide (40 metros, one page each)
  - closed listings skipped
  - minimum rating applied, with unrated businesses kept for review and not built
  - owner exclusions enforced
  - each listing stored as evidence with verified facts (name, phone, city/state, rating, review count)
  - then the existing verification, research, qualification, build, browser QA with repair, and private Cloudflare deploy
- **Real progress, proactively.** The worker posts a conversation message from database counts whenever the stage or state changes, a preview passes QA, or a preview is deployed. It never posts duplicates. Preview links appear with the business name and are clickable.
- **Streaming replies.**
  - Claude's text is streamed (Messages API streaming, budget-gated) to `/internal/voice/command/stream`, and the voice agent hands it to TTS as it arrives.
  - The turn runs as a server task, so barge-in stops speech but the turn is still completed and stored.
  - The dashboard shows JARVIS's words as they are spoken.
- **Voice quality.** The audit found `cartesia/sonic-2`, which is retired in LiveKit Inference, with no voice selected. JARVIS now uses `cartesia/sonic-3` through LiveKit Inference with a pinned voice; voice, speed, emotion and language are configurable. Links are not read aloud. ElevenLabs was evaluated (plugin-only, separate paid account) and not enabled.
- **Tests (simulated providers):**
  - the owner's exact veterinary conversation
  - a nationwide veterinary campaign through the real worker to verified previews, with the progress messages
  - provider-specific restrictions
  - streaming through the real Anthropic SDK (SSE) and through a real LiveKit AgentSession
  - barge-in during a streamed reply
  - TTS configuration
- **Live checks** in `scripts/verify-live.sh`:
  - the browser voice test now speaks the veterinary conversation and fails if JARVIS re-asks
  - `--vet-workflow` runs the whole objective for real

## Revision 12: natural, real-time voice conversation (2026-11-20)
- **Root cause of "JARVIS hears me but never answers":** the voice `AgentSession` had STT, TTS and VAD but no LLM object. In LiveKit Agents 1.8.12, `AgentActivity` returns early after every completed user turn when `session.llm is None` (`agent_activity.py`: "skip response if no llm is set"), so the JARVIS bridge in `llm_node` never ran. Reproduced with a real `AgentSession`: without an LLM it refuses to generate (`trying to generate reply without an LLM model`) and makes 0 calls to JARVIS. With `JarvisBridgeLLM` it makes 1 call, and the reply is the assistant message. This is now a regression test. `JarvisBridgeLLM` never calls a model; replies still come only from the shared JARVIS conversation (the same Claude agent, history, tools, approvals and budget as text).
- **Repeated partial bubbles:** LiveKit publishes every interim transcript as a new text stream with the same `lk.segment_id`. The dashboard keyed bubbles by stream id, so each interim update became its own bubble. Now one live caption is built from segments (`lk.segment_id`, `lk.transcription_final`) and committed once, when the agent commits the turn. Late updates cannot create a second message.
- **Turn detection:** LiveKit's end-of-turn model (`inference.TurnDetector`, v1 via LiveKit Inference with local fallback) plus VAD, and endpointing of 0.6 s minimum and 4.0 s maximum. A mid-sentence pause no longer ends the turn. `VOICE_TURN_DETECTION=vad` selects silence-only turns.
- **Never on partial transcripts:** preemptive generation is explicitly off. It is on by default in this SDK, but a JARVIS turn is stored and can propose or start work.
- **Natural behaviour:** barge-in, with false-interruption resume. Fragments cut off by an interruption are merged into the next turn. Claude is told when its previous reply was interrupted and what the owner heard. Voice guidance (follow-ups, corrections, references) is in the shared prompt. Echo protection: browser echo cancellation, and the agent drops transcripts that repeat its own recent speech. Each turn is sent once, and the API replays a retried `turn_id` instead of running it twice.
- **Diagnostics:** every stage is logged as `voice.pipeline turn=<room>:t<n> stage=… t_ms=…`. Stages: agent joined, mic audio subscribed, speech start/end, first interim, final transcript, turn commit, reply start, Claude request/response with latency, TTS requested, playout start, reply played or interrupted, browser mic published, browser audio playing or blocked. The API logs the same turn id and stores it with both messages.
- **Errors by stage:** STT, TTS, Claude and JARVIS-service failures are sent to the browser, named and shown, with "Retrying" when the SDK will retry. The browser unlocks audio on Start and offers "enable sound" if autoplay is blocked.
- **Live tests:** the real-room test is now one continuous conversation: a paused long request as ONE turn, a follow-up, barge-in, and a reconnect that remembers the city. There is a new browser test (`dashboard/e2e/voice.e2e.mjs`) where Chromium's microphone is real synthesized speech; it checks one stored message per spoken turn, Claude replies, and audio actually playing. Both run from `scripts/verify-live.sh`.

## Revision 11: the voice agent can actually start sessions (2026-11-20)
- **Root cause of "agent did not join":** `build_worker()` defined the LiveKit entrypoint inside itself. On Linux, LiveKit Agents (1.8.12) starts job processes with `forkserver`, which pickles the entrypoint by reference, so every job process failed with `Can't pickle local object 'build_worker.<locals>.entrypoint'`. No room could be served while the parent kept heart-beating. The entrypoint, prewarm and the agent class are now module-level.
- **Honest voice health:** OPERATIONAL now requires the worker to be registered with LiveKit (`worker_registered`) and a job process to have actually started (reported from inside it after prewarm). Otherwise the status lists the gap with the worker's last SDK error, sanitized. Migration 0010.
- `dev-up.sh` waits for a ready job process, not a heartbeat.
- Regression test: a real LiveKit job process is started exactly as the SDK does on Linux (forkserver with the SDK's preloads). The pre-fix agent fails it with the pickling error; the fixed agent passes.

## Revision 10: Claude failures reported honestly, reliable voice dispatch (2026-11-20)
- **Anthropic errors keep their meaning.** Authentication, billing (402 and spend-limit classes, "credit balance"), permission, model access, invalid request, rate limit, overload and timeout each carry the class, HTTP status, Anthropic error type, sanitized message and request id. They are logged without secrets.
- **Test connection sends one tiny real message**, so CONNECTED is no longer based on a model lookup alone.
- **No silent Basic Mode.** Claude failures produce an explicit "Claude unavailable: reason" reply, and nothing runs. The basic parser is opt-in only and labelled.
- **Voice:** explicit agent dispatch through the token's room configuration, VAD prewarm, a distinct "JARVIS is joining" state, a 45-second join window with a clean failure, and handling of the agent leaving. The live test uses the SDK's `http_context.open()` for its standalone speech synthesis, can drive the running agent, and asserts that the reply came from Claude and that the reply's audio was received.
- `dev-up.sh` restarts services that were started from an older commit.

## Revision 9: one JARVIS conversation, managed voice agent, voice-first screen (2026-11-19)
- **One conversation brain for voice and text:** Claude with fixed tools over existing operations, persistent memory and focus (migration 0009). Work starts only after an explicit yes (checked server-side). Budget-gated. Website text is fenced as untrusted. Deterministic fallback, labelled.
- **Work on a named business:** verify, research, qualify, build, browser-QA and deploy a preview for one business through the normal durable pipeline.
- **Credential consistency fix:** the worker applies dashboard credentials before its first job, so the "saved but X_API_KEY not set" usage message no longer appears; usage refreshes after a successful test; stale reports are hidden.
- **Managed voice agent:** `dev-up.sh` generates the voice secret once, installs and supervises the agent (backoff, stops after repeated crashes) and waits for its heartbeat.
- **Voice-first JARVIS screen:** a central orb that follows the real voice and task state, a live shared transcript, confirm and cancel controls, a secondary text field, and compact work, attention and system panels. The old console moved to Commands.
- **`scripts/verify-live.sh`:** real provider tests, a Claude turn, LiveKit token scope, a spoken round trip, and an optional full build-and-deploy of one business.

## Revision 8: reliable development startup, Codespaces, no sign-in during development (2026-11-19)
- **One database configuration:** every process reads the repository-root `.env`; the default `DATABASE_URL` matches `docker-compose.dev.yml` (previously the code defaulted to a different database with no password, causing password failures and missing tables when a terminal lacked the export). Empty `.env` values mean "not set". `postgresql+asyncpg://` URLs are accepted. Production requires an explicit `DATABASE_URL`.
- **Migrations gate startup:** `/ready` reports pending or modified migrations (503) and the worker refuses to start until they are applied. The migrator has a `status` command and clear errors for wrong passwords, missing databases and failed migrations.
- **One-command startup:** `scripts/dev-up.sh`, `dev-status.sh`, `dev-down.sh` (PID and log tracking, health checks, no duplicate processes, lock against concurrent runs, never deletes data). `.devcontainer/` starts everything on every Codespace start and fast-forwards to `origin/main` when safe. Postgres health check in `docker-compose.dev.yml`.
- **No sign-in during development:** the login page is removed. An explicit local development access mode (`JARVIS_ENV=local` + `JARVIS_LOCAL_DEV_ACCESS=true`) acts as one local operator; settings refuse it in any other environment.
- **Dashboard to API:** the browser calls `/api` on the dashboard's own origin (Vite proxy), so Codespaces never needs the API's address or a public API port.
- Tests: `test_dev_environment.py` and `scripts/tests/dev-startup-test.sh` (real Docker startup workflow).

## Revision 7: Integrations Center, voice agent and Command Center (2026-11-19)
- **Integrations Center:** provider credentials can be saved, tested, disabled and removed from the dashboard by deployment administrators. Stored AES-256-GCM encrypted (`JARVIS_ENCRYPTION_KEY`, previously unused); fails closed without a key; never returned, logged or echoed. Explicit states including NOT_TESTED, PERMISSION_DENIED and CONNECTION_FAILED. Changes reach every API instance and worker. Migration 0007.
- **Voice:** one conversation router for voice and text (clarification answers, confirm, cancel, status, new commands). The agent heartbeats, narrates real progress, accepts interrupt and end controls from the session owner only, and can fetch LiveKit credentials saved in the dashboard. Voice is OPERATIONAL only after LiveKit validation and a live agent heartbeat. Tokens: 10 minutes, microphone only. Migration 0008.
- **Dashboard:** new navigation, Command Center (attention items, active work, integration health, spend), Integrations, Voice Agent and Usage & Costs pages.
- **Fixes found by live checks:** Resend rejects keys with HTTP 400, now reported as INVALID_CREDENTIALS; provider errors show the provider's own message.
- **Parser:** "prepare two redesign previews" and similar phrasings now enable the build, QA and deploy stages.

## Revision 6: consistency and data-integrity audit (2026-11-19)
- **Docker architecture docs:** every document now states that only `sandbox-runner` mounts the Docker socket. A static regression test fails if docs, scripts, CI or config describe the removed socket proxy as current.
- **Evidence retention integrity:** migration 0005 adds content-free `evidence_tombstones` (trigger on every evidence delete), a `dangling_evidence_refs` integrity view (recorded on each retention run) and a backfill. Verification references resolve as live or expired.
- **Usage accounting:** migration 0006 adds `cost_basis`. Anthropic account-level usage is `unavailable`; JARVIS costs are labelled `local_estimate`; `/usage` reports `invoice_status` (`UNAVAILABLE` unless a billing export is imported). Constraints refuse mislabelled costs.
- **Backups:** the restore drill fails on a missing checksum or inconsistent row counts; a bucket on the server's own public IP is not independent. R2 is documented as unverified; status stays `BACKUP_NOT_PROTECTED`.
- **Bug fix:** concurrent first requests from a new user could create two workspaces (commands then 404'd). Now exactly one.
- **Status accuracy:** acceptance criteria split into implemented / test-verified / live-verified / deployed. Criteria previously shown as fully green without live verification (for example 19) corrected.

## Revision 5: hardening before live provider testing (2026-11-19)
- **Shared rate limiting:** Postgres token buckets for the API (IP, user, commands) and provider throughput, across all instances. The in-process limiter was removed.
- **Sandbox:** a dedicated sandbox runner (the only Docker-socket holder, with one fixed operation) replaces the generic socket proxy, which allowed privileged container creation. Images are pinned by digest; `--pull=never`; production requires the runner mode and a pinned image. Base image pinned by digest.
- **Retention:** one scheduled module covering all rules, including 180-day SERP cleanup, legal holds, protected record types, and run logs.
- **Provider usage reconciliation:** scheduled hourly, account-level reports with explicit statuses. Unknown is never shown as zero. `/usage` separates estimated, reserved, committed, ambiguous, released and provider-reported.
- **Scheduler fix:** the advisory lock is now taken and released on the same connection (previously it could leak across pooled connections). Per-job timing is stored in `scheduled_jobs`.
- **Repair loop:** dedicated tests for all stages and the hard stop.
- **Voice:** sessions bound to the authenticated participant, explicit close-on-disconnect, closed HTTP client, non-deprecated interruption config, offline tests, and a credentialed live test.
- **Backups:** boto3 off-site upload with checksum verification, a restore drill from the off-site copy, and `PROTECTED` only for an independent destination with a recent drill.
- **Production config:** least-privilege per-service env files, read-only containers with dropped capabilities, migrate-first startup, worker heartbeat healthcheck, and deploy retry that never re-uploads.
- **Deploy script:** `rsync --delete` no longer deletes the server's `env/*.env` secrets.
- Migrations 0003 and 0004 (additive; existing records preserved).

## Revision 4: implementation (2026-11-18)
- Implemented the full platform. See docs/IMPLEMENTATION_STATUS.md for verified status, test results, blockers and decisions that differ from revision 3 (structured-spec renderer, Postgres-checkpointed engine, VERIFIED_* website statuses, API-served unsubscribe, and a generic Docker socket proxy for sandbox access, which revision 5 removed and replaced with the dedicated sandbox runner).
- New docs: docs/IMPLEMENTATION_STATUS.md, docs/OPERATIONS.md. README rewritten with run instructions.

## Revision 3 (2026-11-18)

**Product requirements (new document):** `docs/PRODUCT_REQUIREMENTS.md`
- PR-2, dynamic Google rating filter: optional `minimum_google_rating`, no default, 0.5 steps with no silent rounding, mapped to Text Search `minRating`, re-checked in memory, explicit missing-rating policy (`exclude` / `include_for_review`). Ratings and review counts are never persisted and are shown only in the attributed Google Places listing panel.
- PR-3, evidence-based website availability: `WEBSITE_LISTED`, `WEBSITE_WORKING`, `WEBSITE_BROKEN`, `NO_WEBSITE`, `WEBSITE_UNKNOWN`. A missing `websiteUri` never means `NO_WEBSITE`. `NO_WEBSITE` requires completed independent checks.
- PR-4, Google Places compliance gate: the intended use is stated precisely. Places discovery and qualification stay disabled until Google or qualified counsel confirms that use in writing. `DiscoveryProvider` is replaceable (`manual_import` now, licensed data later). SerpApi's Maps engines are no longer offered as a workaround.

**API contracts (new document):** `docs/API_CONTRACTS.md`. Error codes (`COMPLIANCE_BLOCKED`, …), `CampaignParameters`, command-parsing fixtures, `WebsiteAssessment`, the listing-panel response, compliance-approval endpoints, the Places adapter, `PlaceInMemory`, and `DiscoveryProvider`.

**Google API scope and naming:**
- Google Places API (New) is the only Google Maps Platform API used. Text Search (`places:searchText`) is primary; Place Details serves only the listing panel.
- Maps JavaScript, Embed, Static, Routes, Geocoding, Autocomplete and Nearby Search are explicitly excluded (ARCHITECTURE §5.0).
- Terminology standardised: "Google Maps Platform" for terms, billing and quotas; "Google Places API (New)" for the integration; "Google Maps Content" for the data. The listing panel was renamed "Google Places listing" (its attribution text stays "Google Maps", as Google requires).

**Field masks and costs:**
- Re-verified per-field SKUs: `rating`, `userRatingCount` and `websiteUri` are Enterprise; `displayName` and `businessStatus` are Pro; `attributions` is Essentials (IDs Only).
- `attributions` added to every mask.
- `DETAILS_DISPLAY` and `DETAILS_CONTACT` replaced by `LISTING_DISPLAY` (Place Details Enterprise).
- Cost model now uses a yield model based on confirmed pagination (≤ 20 per page, ≤ 60 per query, short pages, inconsistent results).
- `NO_WEBSITE` campaigns need about 80–106 requests and about 375 SERP searches per 100 prospects. The SerpApi free tier covers about 65 confirmed no-website prospects a month.
- Scenarios recalculated. The mandatory $45 budget is unchanged.

**Files changed:** ARCHITECTURE.md, IMPLEMENTATION_PLAN.md, ENVIRONMENT_VARIABLES.md, README.md, .env.example, docs/API_COST_MODEL.md, docs/DATABASE_SCHEMA.md, docs/SECURITY_AND_COMPLIANCE.md, docs/SYSTEM_DIAGRAM.md. New: docs/PRODUCT_REQUIREMENTS.md, docs/API_CONTRACTS.md, docs/VERIFICATION_REGISTER.md, CHANGELOG.md.

## Revision 2 (2026-11-18)
- Places content no longer persisted (`places_cache` removed; Place IDs only).
- VPS made a required recurring cost.
- Model routing changed to Haiku / Sonnet, with Opus for escalation only.
- App-level billing hard stops added, with estimated vs reported usage.
- Supabase resilience and own backups added.
- Outreach sending-readiness gate added.
- Costs split into mandatory and optional.
- Capped live tests and a multi-campaign diversity dataset added.

## Revision 1 (2026-11-18)
Initial architecture documents.
