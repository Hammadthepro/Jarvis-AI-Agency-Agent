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
the same command, approval, budget and workflow system as text. The agent does not use a general chatbot: each final
utterance is routed to the existing handlers (answer a clarification, confirm, cancel, status, or new command), and the
exact reply is spoken. After a campaign is authorized, the agent speaks real stage changes read from the database.

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

### Conversation brain (voice and text)
Voice and the text field on the JARVIS screen share one conversation per operator: the same history (stored in
`conversation_messages`), the same focus ("this company") and the same pending action.

- **With Anthropic configured**, Claude (the fast model) answers using a fixed set of tools over existing operations:
  prospect search, working on one named business (verify, research, qualify, and optionally build, browser-test and
  deploy a preview), status, findings, verified preview links and today's work.
- **Starting anything needs your explicit yes.** The server checks that your latest message is a yes before it starts
  any work. Every Claude call is budget-gated.
- **Without Claude**, or when it is unavailable or over budget, the deterministic command router answers, and the reply
  is marked "basic mode".

### Using it
Open **Voice Agent** (or "Talk to JARVIS" in the Command Center), allow the microphone, and speak. You can interrupt
JARVIS by talking, or press **Stop speaking**. Examples: "Find 10 independent roofing companies in Houston with outdated
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
  `JARVIS_LIVE_USER_ID`, `JARVIS_LIVE_WORKSPACE_ID`).
- Real-stack E2E: start the API, worker and built dashboard, then `node dashboard/e2e/stack.e2e.mjs <url> <dir>`.
  With `E2E_ADMIN_EMAIL` set (and listed in `JARVIS_ADMIN_EMAILS`), it also saves deliberately invalid keys and confirms
  the real providers reject them (requires network access to the providers).
