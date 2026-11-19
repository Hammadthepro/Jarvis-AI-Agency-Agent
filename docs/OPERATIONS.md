# JARVIS — Operations

## Topology
- **VPS (required):** Docker Compose runs `migrate`, `api`, `worker`, `sandbox-runner`, `caddy`, `backup`, and `voice` (profile).
  **Only `sandbox-runner` has the Docker socket.** It exposes one fixed operation (`POST /qa`, token-authenticated,
  internal network only) and always starts the QA container with the same hardened flags. Those flags are:
  `--pull=never`, `--network none`, `--read-only`, uid 10001, `--cap-drop ALL`, `no-new-privileges`, and resource
  limits. Only the site directory is mounted (read-only), plus an output directory. The worker cannot pass images,
  flags or mounts. A generic socket proxy was removed: it allowed `POST /containers/create` with arbitrary host
  config, so a compromised worker could have started a privileged container.
- **Secrets are split per service** (`env/app.env`, `env/runner.env`, `env/voice.env`, `env/backup.env`, mode 600, never in git).
  The runner gets no provider or database credentials. The backup service holds only the age **public** key.
- **Supabase:** Postgres (`DATABASE_URL`, session pooler) and Auth (`SUPABASE_JWKS_URL` or `SUPABASE_JWT_SECRET`).
- **Cloudflare Pages:** the dashboard is built with `VITE_API_BASE_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.
  Generated previews use the `jarvis-previews` project, Direct Upload, one branch per site.

## First deployment
1. Provision a VPS. **This is a purchase, and you must authorise it.** Then, as root: `ADMIN_SSH_CIDR=<your ip>/32 scripts/vps-setup.sh`.
   Record the printed ed25519 host-key fingerprint.
2. Put `/opt/jarvis/env/{app,runner,backup,voice}.env` (mode 600) on the server, built from `env/*.env.example`. Export `API_HOSTNAME` and `CADDY_ACME_EMAIL` for Caddy.
3. From your machine or CI, run:
   `VPS_HOST=… VPS_USER=jarvis VPS_SSH_KEY_FILE=… VPS_HOST_KEY="<host> ssh-ed25519 AAAA…" scripts/deploy.sh`.
   SSH uses `StrictHostKeyChecking=yes` against the pinned key only.
4. The deploy builds the images, pins the sandbox image ID into `env/runner.env`, and starts the stack. `migrate` must succeed before `api` and `worker` start, and the sandbox runner must be healthy before the worker starts. It then checks `/ready` and the runner's `/health`. `rsync` never touches `env/*.env`.
5. Dashboard: create a Cloudflare Pages project, build `dashboard/` (`npx vite build`) and deploy `dist/`.

## Health and monitoring
- `GET /health` (liveness) and `GET /ready` (database). Readiness returns 503 when the DB is unreachable or paused.
- `GET /system/readiness` is the production checklist: providers, the compliance gate, workers, terms/privacy, backups and outreach.
- The worker heartbeats into `worker_heartbeats`. The dashboard Overview shows live and stale workers, queue depth and dead letters.
- Logs are structured JSON on stdout (`docker compose logs -f worker`), with secrets redacted.

## Database recovery
- The API and worker reconnect with exponential backoff. While the DB is down the worker stops claiming tasks, and leases are recovered afterwards.
- **Supabase Free may pause an inactive project.** Worker traffic is not assumed to prevent this. `/ready` shows `paused_suspected`; resume the project in the Supabase dashboard.

## Sandbox image (controlled provisioning)
- The QA sandbox image is built only in CI or during `scripts/deploy.sh`, from a base image pinned by digest.
  The deploy writes the resulting image ID into `env/runner.env` as `SANDBOX_IMAGE=sha256:...`.
- In production the runner refuses to start with an unpinned image, and refuses to run if the image is missing. It never pulls.

## Rate limiting
Shared token buckets live in Postgres (`rate_limit_buckets`), so limits hold across every API instance and worker.
- API: per IP, per user and per command (`resources/default_limits.json` → `api_rate_limits`).
- Providers: per provider account (`rate_limits`), checked before any reservation or request.

## Scheduled jobs
The worker runs these under a Postgres advisory lock held on one dedicated connection, so only one worker runs them at a time.
Timing is tracked in `scheduled_jobs`.
- Lease recovery: every 60 s.
- Retention: hourly.
- Provider usage sync: hourly.
- Preview cleanup: hourly.

## Retention and legal holds
Every rule is listed in `backend/jarvis/core/retention.py`:
- Task events: 90 days.
- Raw scraped pages: 90 days.
- SERP results: 180 days.
- Evidence of rejected prospects: 30 days.
- Places coordinates: 30 days.

Evidence cited by a verified fact or contact is kept. Audit logs, suppressions, outreach records, the cost ledger,
usage events and backup runs are **never** deleted by retention. Purged evidence leaves a content-free tombstone
(`evidence_tombstones`: source type, host, hashes, dates, rule), so verification records keep resolvable references;
`dangling_evidence_refs` must be 0 and is recorded on every run. A legal hold (`legal_holds`, at workspace, campaign
or prospect scope) exempts all covered rows until it is released. Each run is recorded in `retention_runs` and `audit_logs`.

## Usage reconciliation
The hourly sync writes account-level `provider_usage_reports` with an explicit status:
- `ok`: the provider reported usage.
- `unavailable`: no usage API is integrated for that provider. Usage is **unknown, not zero**.
- `not_configured`: no credential.
- `error`: the usage API call failed.

Anthropic is recorded as `unavailable` at account level: no usage/cost Admin API is integrated. JARVIS's own
per-response token counts and their locally priced cost are kept in the report's `raw` and labelled `local_estimate`.

Every reported cost has a `cost_basis` (`provider_reported`, `provider_invoice`, `local_estimate`, `manual_entry`).
Database constraints prevent a response-derived cost from being labelled as provider-reported or invoiced.
Settings → Usage shows estimated, reserved, committed (local estimate), ambiguous and released spend, the
account-level provider-reported figure, and an invoice status (`UNAVAILABLE` unless a billing export is imported).
`POST /usage/sync` runs the sync on demand.

## Backups
- The `backup` service runs `python -m jarvis.ops.backup run` daily. Each run: `pg_dump`, encrypted with `age` to
  `BACKUP_AGE_RECIPIENT` (keep the private key offline); 7 local copies; upload to `BACKUP_S3_*` (for example Cloudflare R2);
  verify size and sha256 of the uploaded object.
- **Restore drill** (monthly, required for `PROTECTED`): `scripts/restore-drill.sh`. It downloads the newest *off-site*
  object, verifies its checksum, decrypts it, restores it into `RESTORE_DRILL_DATABASE_URL` (a scratch database) and
  compares row counts with production.
- Status is `PROTECTED` only when two things hold. The latest backup was uploaded to a destination that is **not**
  on this host or a private network. And a restore drill from that destination succeeded within 35 days.
  Otherwise the status is `BACKUP_NOT_PROTECTED` (or `NO_BACKUP_RECORDED`, `BACKUP_FAILED`, `BACKUP_STALE`).
- Manual restore of a specific file: `scripts/restore.sh <file.dump.age> <age-identity> <target-db-url>`.

## Graceful restart
`docker compose up -d worker` sends SIGTERM. The worker stops claiming, gives in-flight tasks up to 60–90 s to finish, then hands their leases back.
Delivery is at-least-once: handlers are idempotent (idempotency keys, checkpoints, outbox).

## Kill switches
Settings → Global kill switch (per workspace), per-provider switches, and the per-campaign pause/cancel/kill switch.
They are enforced in the task-claim query and before every provider call.
