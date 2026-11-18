# JARVIS — Operations

## Topology
- **VPS (required):** Docker Compose runs `api`, `worker`, `docker-proxy`, `caddy`, `backup`, and `voice` (profile).
  The worker launches QA sandboxes through `docker-proxy`, which allows only container lifecycle calls.
  The worker never mounts the Docker socket, and sandboxes never see it.
- **Supabase:** Postgres (`DATABASE_URL`, session pooler) and Auth (`SUPABASE_JWKS_URL` or `SUPABASE_JWT_SECRET`).
- **Cloudflare Pages:** the dashboard is built with `VITE_API_BASE_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.
  Generated previews use the `jarvis-previews` project, Direct Upload, one branch per site.

## First deployment
1. Provision a VPS. **This is a purchase, and you must authorise it.** Then, as root: `ADMIN_SSH_CIDR=<your ip>/32 scripts/vps-setup.sh`.
   Record the printed ed25519 host-key fingerprint.
2. Put `/opt/jarvis/.env` (mode 600) on the server, built from `.env.example`. Set `API_HOSTNAME` and `CADDY_ACME_EMAIL`.
3. From your machine or CI, run:
   `VPS_HOST=… VPS_USER=jarvis VPS_SSH_KEY_FILE=… VPS_HOST_KEY="<host> ssh-ed25519 AAAA…" scripts/deploy.sh`.
   SSH uses `StrictHostKeyChecking=yes` against the pinned key only.
4. The deploy runs migrations (`docker compose run --rm migrate`), builds the QA sandbox image, starts the services and checks `/ready`.
5. Dashboard: create a Cloudflare Pages project, build `dashboard/` (`npx vite build`) and deploy `dist/`.

## Health and monitoring
- `GET /health` (liveness) and `GET /ready` (database). Readiness returns 503 when the DB is unreachable or paused.
- `GET /system/readiness` is the production checklist: providers, the compliance gate, workers, terms/privacy, backups and outreach.
- The worker heartbeats into `worker_heartbeats`. The dashboard Overview shows live and stale workers, queue depth and dead letters.
- Logs are structured JSON on stdout (`docker compose logs -f worker`), with secrets redacted.

## Database recovery
- The API and worker reconnect with exponential backoff. While the DB is down the worker stops claiming tasks, and leases are recovered afterwards.
- **Supabase Free may pause an inactive project.** Worker traffic is not assumed to prevent this. `/ready` shows `paused_suspected`; resume the project in the Supabase dashboard.

## Backups
- The `backup` service runs `scripts/backup.sh` daily: `pg_dump` encrypted with `age` to `BACKUP_AGE_RECIPIENT`.
  Keep the private key offline. Seven local copies are kept, plus an off-site copy to `BACKUP_S3_*` (for example Cloudflare R2).
- With no off-site target the run is recorded as `BACKUP_NOT_PROTECTED`, and Settings shows it.
- **Restore drill** (monthly): `scripts/restore.sh <file.dump.age> <age-identity> <scratch-db-url>`.

## Graceful restart
`docker compose up -d worker` sends SIGTERM. The worker stops claiming, gives in-flight tasks up to 60–90 s to finish, then hands their leases back.
Delivery is at-least-once: handlers are idempotent (idempotency keys, checkpoints, outbox).

## Kill switches
Settings → Global kill switch (per workspace), per-provider switches, and the per-campaign pause/cancel/kill switch.
They are enforced in the task-claim query and before every provider call.
