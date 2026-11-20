# JARVIS — Integrations Center and Voice Agent

How to connect providers from the dashboard, how credentials are protected, and how to run and verify voice.
Status of each item (implemented / tested / live-verified) is in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## 1. One-time server setup (environment, not the browser)

These stay in the server environment (`env/app.env`); they cannot be set from the dashboard:

| Variable | Why |
|---|---|
| `JARVIS_ENCRYPTION_KEY` | Encrypts credentials saved in the dashboard. Generate: `openssl rand -base64 32`. Without it, saving fails closed with `ENCRYPTION_NOT_CONFIGURED`; nothing is ever stored in plaintext. |
| `JARVIS_ADMIN_EMAILS` (or `JARVIS_ADMIN_USER_IDS`) | Who may save, remove, enable or disable provider credentials. Provider accounts are shared by the whole deployment, so workspace owners alone cannot change them. |
| `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_JWKS_URL` | Database and authentication (shown read-only as the Supabase card). |
| `VOICE_AGENT_SHARED_SECRET` | Authenticates the voice agent process to the API (same value in `env/voice.env`). |

Then run migrations (`python -m jarvis.db.migrate`; 0007 and 0008 add the credential and voice tables).

## 2. Connecting a provider (dashboard)

1. Open **Integrations**.
2. Select a provider and click **Configure** (administrators only).
3. Enter the credentials and any settings, then **Save**. Secrets are sent once over HTTPS to the API, encrypted, and never returned: the card shows only "saved (dashboard)".
4. Click **Test connection**. JARVIS calls the real provider using a free endpoint and shows the actual result.
5. The card shows the state, last validation, last error, capabilities and any provider-reported quota.

| Provider | Fields | What Test connection calls (free) | Notes |
|---|---|---|---|
| Anthropic (Claude) | API key; fast/build/escalation models | `GET /v1/models/{model}` for each configured model | A Claude.ai or Claude Code subscription is not API access |
| SerpApi | API key | `GET /account.json` (also reports searches left) | |
| Firecrawl | API key | `GET /v1/team/credit-usage` (credits left) | |
| Cloudflare Pages | API token (Pages: Edit), account ID, preview project | token verify + `GET /accounts/{id}/pages/projects` | Reports whether the preview project exists (deploy creates it if missing) |
| LiveKit | server URL, API key, API secret | `RoomService/ListRooms` | Voice also needs the agent process (section 4) |
| Resend | API key, webhook signing secret (optional) | `GET /domains` (lists verified sending domains) | Sending also needs a verified domain and Outreach > Sender readiness |
| Google Places API (New) | API key | Text Search, IDs-only field mask | **Restricted.** Blocked until written approval is recorded (Settings > compliance gate) |
| Supabase | environment only | database health + JWKS fetch | Infrastructure; not editable from the browser |

**States** (never collapsed into a generic error):
`CONNECTED` (validated against the provider) · `NOT_TESTED` (credentials present, not validated) · `NOT_CONFIGURED` ·
`INVALID_CREDENTIALS` · `PERMISSION_DENIED` (valid key, missing permission) · `CONNECTION_FAILED` (unreachable or provider
error) · `RATE_LIMITED` · `QUOTA_EXCEEDED` · `DISABLED` (an admin turned it off; no calls are made) · `BLOCKED_PENDING_APPROVAL`.

**Precedence:** a value saved in the dashboard overrides the same environment variable. **Remove** deletes the dashboard
value and falls back to the environment (if set). Every API instance and worker applies changes within about 15–30 seconds.

**Disable** stops all calls to that provider immediately (the gateway refuses them before any request or budget reservation).

## 3. How credentials are protected

- AES-256-GCM with a random 96-bit nonce per value; each ciphertext is bound to its provider and field (authenticated
  data) and tagged with a key id. A database constraint rejects any non-encrypted value.
- Secrets are write-only: no API response, log line or audit entry contains them. Request-validation errors never echo
  submitted values. The browser keeps typed secrets only in the form until submitted; nothing goes to local or session storage.
- Every save, removal, enable, disable and test is recorded in `audit_logs` (provider and field names only).
- **Key rotation:** set the new key as `JARVIS_ENCRYPTION_KEY` and the old one as `JARVIS_ENCRYPTION_KEY_PREVIOUS`, restart,
  run `python -m jarvis.security.vault rotate`, then remove the previous key.
- **Lost key:** stored credentials cannot be decrypted; the Integrations Center reports this, and an admin re-enters them.

## 4. Voice agent

### Architecture
Browser (microphone) ⇄ LiveKit room ⇄ JARVIS voice agent (`apps/voice/agent.py`) → JARVIS API conversation router →
the same Claude agent, history, tools, approvals and budget as text. After a campaign is authorized, the agent speaks
real stage changes read from the database.

**Pipeline, one continuous session (no button per sentence):**

| Stage | How | Streaming? |
|---|---|---|
| Speech to text | LiveKit Inference `deepgram/nova-3` | Yes: interim and final transcripts |
| End of turn | LiveKit turn detector model (`inference.TurnDetector`, v1 with local fallback) + Silero VAD; endpointing 0.6 s min / 4.0 s max | Decides from the audio whether you finished; a pause mid-sentence doesn't end the turn |
| Reply | `POST /internal/voice/command/stream` → the shared Claude conversation | **Yes.** Claude's text is streamed from the Messages API as it is written (NDJSON deltas); speech starts on the first sentence. While Claude runs a tool (a search, a proposal), nothing is spoken until that step's text arrives |
| Text to speech | LiveKit Inference `cartesia/sonic-3`, voice pinned (`VOICE_TTS_VOICE`), 24 kHz PCM | Yes: sentence by sentence as text streams in |

- **Exactly one message per spoken turn.** Interim transcripts only update the live caption. Preemptive generation is off,
  so Claude never runs on words you haven't finished.
- **Interrupting:** talk over JARVIS (or press Stop speaking) and it stops. Your new words are the next turn, and Claude
  is told what you heard of the interrupted reply.
- **Echo:** the browser captures with echo cancellation, and the agent ignores a "user" transcript that repeats its own
  recent speech. Headphones still give the best results.
- Settings: `VOICE_TURN_DETECTION` (`model` or `vad`), `VOICE_ENDPOINT_MIN_DELAY`, `VOICE_ENDPOINT_MAX_DELAY`,
  `VOICE_LOG_TRANSCRIPTS=1` (include transcript text in logs; off by default).
- If the owner talks over a reply, the speech stops at once; the turn still completes on the server and is stored, so
  JARVIS remembers it. Links in replies are not read out ("the link in the chat"); they are clickable in the feed.

### The voice JARVIS speaks with
Audit of the previous setup: `cartesia/sonic-2` (now **retired** in LiveKit Inference), **no voice selected** (the
gateway default), and synthesis started only after the whole reply had been generated. That is what sounded robotic
and slow.

| Option | Through | Price (LiveKit Build/Ship tier) | Notes |
|---|---|---|---|
| **Cartesia Sonic 3** (chosen) | LiveKit Inference: billed by the existing LiveKit account | $50 per million characters (Scale: $37.50) | Current Cartesia generation; WebSocket streaming; voice, speed, emotion and language options; documented voices |
| Cartesia Sonic 3.5 / 3.6 | LiveKit Inference | same | Newer; 3.6 is not in the installed SDK's model list yet, 3.5 is (`VOICE_TTS_MODEL=cartesia/sonic-3.5`) |
| Inworld TTS 2.0 / 2.0 Flash | LiveKit Inference | $25 / $15 per million characters | Cheaper; worth an A/B listen |
| ElevenLabs Flash / Turbo | **Plugin only**: needs its own ElevenLabs account, API key and paid plan | $0.04 per 1,000 characters ($40 per million), ~75 ms | Excellent naturalness; not enabled because it needs a new subscription (not purchased) |

A typical spoken reply is 150–300 characters, so at $50 per million characters a reply costs about 1–1.5 cents to
synthesize. Settings (agent environment): `VOICE_TTS_MODEL` (default `cartesia/sonic-3`), `VOICE_TTS_VOICE` (default
`9626c31c-bec5-4cca-baa8-f8ba9e84c8bc`, LiveKit's documented en-US Cartesia voice "Jacqueline"; for example
`a167e0f3-df7e-4d52-a9c3-f949145efdab` is "Blake", en-US male), `VOICE_TTS_SPEED` (`slow`, `normal`, `fast` or a number),
`VOICE_TTS_EMOTION` (optional, Cartesia), `VOICE_TTS_LANGUAGE` (default `en`). Restart the agent after changing them.

**Diagnosing a session:** every stage is logged in `.jarvis/logs/voice.log` as
`voice.pipeline turn=<room>:t<n> stage=<stage> t_ms=<ms since session start>`. The stages, in order:
`agent_joined`, `mic_audio_subscribed`, `browser_mic_published`, `speech_start`, `interim_transcript`, `final_transcript`,
`turn_commit`, `reply_generation_start`, `claude_request_start`, `claude_response` (with latency), `tts_requested`,
`agent_speaking`/`tts_playout_start`, `browser_audio_playing`, `reply_played`. Errors appear as `stt_error`, `tts_error`,
`claude_error` or `jarvis_api_error`. The API logs `conversation.turn_received` / `conversation.reply` with the same turn id,
and stores it in both messages' `meta.turn_id`. If a session stops at a stage, the last logged stage is where.

### Requirements
1. LiveKit configured and **validated** (Integrations > LiveKit > Test connection).
2. `VOICE_AGENT_SHARED_SECRET` set for the API and the agent.
3. The agent process running and heart-beating:
   ```
   cd apps/voice && pip install -r requirements.txt
   JARVIS_API_URL=http://localhost:8000 VOICE_AGENT_SHARED_SECRET=... python agent.py start
   ```
   In Docker Compose: `docker compose --profile voice up -d voice` (uses `env/voice.env`).
   LiveKit credentials come from `LIVEKIT_*` in its environment, or, if saved in the dashboard, are fetched once at
   startup from the API's internal endpoint (only on the internal network, only with the shared secret).

The Voice page reports **OPERATIONAL** only when all three hold; otherwise it lists exactly what is missing and links to
Integrations. STT/TTS run through LiveKit Inference (`deepgram/nova-3`, `cartesia/sonic-2`), billed by LiveKit.

### Session security
- `POST /voice/token` mints a token for one new room, valid 10 minutes, identity `user-<id>`, microphone-only publishing,
  no room admin, list or create rights. The LiveKit secret never reaches the browser.
- The agent serves only the participant whose identity the API minted, ignores control messages from anyone else, and
  ends when that participant leaves. `POST /voice/end` (or End session) deletes the room.
- Viewers can ask for status by voice but cannot start work. All commands go through the same authorization and
  compliance checks as text.

### Objectives, not commands
JARVIS keeps the owner's **objective** as structured state (in `conversation_state.objective`, and copied onto the
campaign when approved): the original request, the task (find and build previews, or find only), business category,
area (city, state or the whole US), number of businesses, minimum rating and what to do with unrated businesses,
website preference, number of previews, exclusions, status. Claude updates it with `set_objective` as the owner talks, so
nothing said earlier is asked again. Defaults instead of questions: 10 businesses; "build their websites and show me" =
previews for the top 3 qualified prospects; "preferably without a website" = rank those first; unrated businesses kept
for review when a minimum rating is set. Only the business type and the area are essential; JARVIS asks for one missing
thing at a time.

When the objective is complete, JARVIS proposes it. One yes approves every internal step (discovery, verification,
research, qualification, building, browser QA, repairs, private preview deployment); the API checks that the yes is the
owner's own latest message, then creates the campaign and confirms its first task is really in the durable queue
before saying it started. Production deployment and outreach are never part of it. Progress updates are posted into the
conversation by the worker from real database counts whenever the work changes stage, a preview passes QA or a preview
is deployed ("Found 10 candidate businesses. Researching them now", "3 qualified. Building 3 preview websites: 1 passed
browser QA so far", "Done. … 3 preview websites are ready").

### Discovery sources (each with its own rules)
| Source | Status | Basis |
|---|---|---|
| SerpApi (Google Maps results) | **Default.** Usable when configured and enabled | SerpApi Terms of Service: lawful use is the customer's responsibility; SerpApi's U.S. Legal Shield (larger plans only, not Free/Starter/Developer) covers collecting public search data. Listing data (name, address, phone, website, rating, review count) is stored with its source and retrieval time |
| Google Places API (New) | **Restricted** until written approval is recorded (Settings > Compliance) | Unchanged. Used only when the owner explicitly asks for Google Places |
| Imported list | Usable | Operator attests the right to use it |
| Licensed business data | Not integrated | |

A restriction on one source is never reported as a restriction on all of them. "Anywhere in the US" searches 40 large
metros across every region, one page each, so results are spread out.

### Conversation brain (voice and text)
Voice and the text field on the JARVIS screen share one conversation per operator: the same history (stored in
`conversation_messages`), the same focus ("this company") and the same pending action.

- **With Anthropic configured**, Claude (the fast model) answers using a fixed set of tools over existing operations:
  prospect search, working on one named business (verify, research, qualify, and optionally build, browser-test and
  deploy a preview), status, findings, verified preview links and today's work.
- **Starting anything needs your explicit yes.** The server checks that your latest message is a yes before it starts
  any work. Every Claude call is budget-gated.
- **If Claude is unavailable** (not configured, disabled, over budget, or failing), the reply says "Claude unavailable"
  with the real reason and does nothing else. The reason includes Anthropic's error type and request id, for example
  `PaymentRequiredError HTTP 402 billing_error: ...` or `BadRequestError HTTP 400 invalid_request_error: Your credit
  balance is too low...`. There is no silent fallback. The basic command parser runs only when you explicitly set
  `JARVIS_CONVERSATION_AGENT=deterministic`, and its replies are labelled "basic parser".
- **Test connection for Anthropic** checks every configured model, then sends one tiny real message with the fast model
  (8 tokens maximum, budget-gated). CONNECTED therefore means messages and billing work, not just that the key exists.

### Voice dispatch
The agent registers with LiveKit as `jarvis` (`VOICE_AGENT_NAME`) using explicit dispatch, and each browser token minted
by the API names that agent in its room configuration. LiveKit therefore sends exactly one JARVIS agent into each new
room as soon as the browser joins. The VAD model is loaded when a job process is prepared, not while you wait.

The browser shows "JARVIS is joining" until the agent has joined and published a live state. It waits up to 45 seconds;
if the agent doesn't start in that time, it ends the room and explains what to check.

### Using it
Open **Voice Agent** (or "Talk to JARVIS" in the Command Center), allow the microphone, and speak naturally; JARVIS
answers after each turn and keeps the conversation (follow-ups such as "focus on roofing companies" or "do the same for
Austin" refine the current request). You can interrupt JARVIS by talking, or press **Stop speaking**. Errors name the
failing stage (speech recognition, speech synthesis, Claude, JARVIS service). Examples: "Find 10 independent roofing companies in Houston with outdated
websites and prepare two redesign previews." → JARVIS explains the plan and any blocker; "confirm", "cancel",
"what's the status?".

## 5. Ports and services

| Service | Port | Exposure |
|---|---|---|
| api | 8000 | via Caddy 443 |
| dashboard | static (Cloudflare Pages) | public |
| worker | — | internal |
| sandbox-runner | 8090 | internal network only |
| voice agent | outbound only (to LiveKit and the API) | none |
| caddy | 80/443 | public |

## 6. Testing

- Backend: `cd backend && .venv/bin/pytest -q` (provider HTTP is simulated in these tests; they are not live checks).
- Dashboard: `cd dashboard && npm run typecheck && npm test && npx vite build`.
- Voice agent: `cd apps/voice && pytest -q tests` (live test needs `LIVEKIT_*`, `JARVIS_API_URL`, `VOICE_AGENT_SHARED_SECRET`,
  `JARVIS_LIVE_USER_ID`, `JARVIS_LIVE_WORKSPACE_ID`). The offline suite includes a real `AgentSession` turn.
- Live voice, room and browser: `scripts/verify-live.sh` runs the real-room conversation test (paused long request,
  follow-up, barge-in, reconnect) and `dashboard/e2e/voice.e2e.mjs` (Chromium whose microphone is real synthesized
  speech; checks one message per turn, Claude replies and audio playback).
- Real-stack E2E: start the API, worker and built dashboard, then `node dashboard/e2e/stack.e2e.mjs <url> <dir>`.
  With `E2E_ADMIN_EMAIL` set (and listed in `JARVIS_ADMIN_EMAILS`), it also saves deliberately invalid keys and confirms
  the real providers reject them (requires network access to the providers).
