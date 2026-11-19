# JARVIS — Development Guide

One command starts everything. In a GitHub Codespace, it runs automatically every time the Codespace starts.

## 1. Starting JARVIS

```bash
scripts/dev-up.sh
```

It runs these steps in order, and stops at the first failure with the real error and the relevant log:

1. **Configuration.** Creates `.env` from `.env.example` if it does not exist. An existing `.env` is never modified,
   except to add `JARVIS_ENCRYPTION_KEY` once if it is missing (section 6).
2. **Dependencies.** Installs backend, dashboard and QA-runner packages only when they are missing or their lock
   files changed.
3. **Database.** Starts PostgreSQL from `docker-compose.dev.yml` and waits until its health check passes. It then
   connects with exactly the `DATABASE_URL` the API and worker use, so a wrong password or a missing database fails
   here with a clear message.
4. **Migrations.** Applies pending migrations (`python -m jarvis.db.migrate`). If one fails, nothing else starts.
5. **API.** Starts on port 8000 and waits for `/ready`, which requires the database **and** all migrations to be
   applied.
6. **Worker.** Starts and waits for its heartbeat. It refuses to run if any migration is pending.
7. **Dashboard.** Starts Vite on port 5173 and checks that it can reach the API through `/api`.
8. **URLs.** Prints the dashboard and API URLs. In Codespaces these are the forwarded `https://<codespace>-5173…` addresses.

Running it again is safe. Services that are already running are left alone and nothing is started twice. A lock stops
two runs overlapping. Processes are tracked in `.jarvis/run/*.pid`, and logs go to `.jarvis/logs/<service>.log`.

## 2. Automatic start in Codespaces

`.devcontainer/devcontainer.json` defines two hooks:

- **`postCreateCommand`** runs `scripts/dev-setup.sh` once, when the Codespace is created or rebuilt. It installs
  dependencies.
- **`postStartCommand`** runs `scripts/dev-up.sh --sync` every time the Codespace starts or resumes. Output goes to
  `.jarvis-startup.log`.
  - `--sync` first fast-forwards to `origin/main`, but only when you are on `main` with no uncommitted changes and the
    update is a pure fast-forward. Otherwise it changes nothing and says why.
  - It then starts the database, applies migrations, and starts the API, worker and dashboard.
- Port 5173 (the dashboard) opens in the browser automatically. Port 8000 (the API) stays private: the dashboard
  reaches it through its own `/api` proxy.

**Container user and image.** The devcontainer uses `mcr.microsoft.com/devcontainers/universal:2` as is. Its existing
non-root user is `codespace` (uid 1000, in the `docker` group), which `devcontainer.json` names explicitly. Docker-in-Docker
is already part of that image, so no Features are added: a Feature would make Codespaces rebuild the ~15 GB image at
creation time, and a failure there puts the Codespace into recovery mode.

**Known limitation:** `universal:2` is Ubuntu 20.04, and Playwright 1.63 does not ship Chromium for Ubuntu 20.04. In the
Codespace, the QA runner install therefore fails (startup warns and continues), and website QA cannot run there until
the QA runner uses a supported environment, for example the Docker QA sandbox (`QA_RUNNER_MODE=docker`).

**First time only:** after this configuration is added to a Codespace that already exists, run **Codespaces: Rebuild
Container** from the Command Palette once. Until then the hooks are not active, but `scripts/dev-up.sh` still works
when run by hand.

If startup failed, read `.jarvis-startup.log` and run `scripts/dev-status.sh`.

## 3. Stopping JARVIS

```bash
scripts/dev-down.sh
```

This stops the dashboard, worker and API gracefully (the worker finishes or returns its in-flight tasks). The database
keeps running.

```bash
scripts/dev-down.sh --db
```

This also stops the database container. The data volume is always kept; no script ever deletes it.

## 4. Checking health

```bash
scripts/dev-status.sh
```

It reports:

- the database container and connection;
- migrations (applied, pending or modified);
- API process and `/ready`;
- worker process and heartbeat;
- dashboard, and whether it reaches the API through `/api`;
- missing configuration;
- each provider's integration state.

It exits 0 only when everything is healthy.

## 5. Migrations

- **Files.** SQL files live in `database/migrations/NNNN_name.sql`. Each is applied once, in order, inside a
  transaction, and recorded with its checksum in `schema_migrations`.
- **Re-running.** Running the migrator again does nothing for files that are already applied. An advisory lock stops
  two runners at once.
- **Status.**
  ```bash
  backend/.venv/bin/python -m jarvis.db.migrate status
  ```
  Exit code 3 means migrations are pending.
- **Apply.**
  ```bash
  backend/.venv/bin/python -m jarvis.db.migrate
  ```
  `dev-up.sh` does this for you.
- **A failed migration is rolled back.** Startup stops and shows the database error. Fix the migration, or add a new
  one, and run `dev-up.sh` again.
- **Never edit an applied migration.** The runner refuses to continue if an applied file's checksum changed; add a new
  file instead.
- **Old databases.** Databases created before migrations 0007 (`integration_credentials`) and 0008
  (`voice_agent_heartbeats`) get them automatically on the next `dev-up.sh`, and existing data is kept.

## 6. Configuration

- **One `.env` for everything.** All processes read one file: the repository-root `.env`. That covers the API, the
  worker, migrations, the scripts and the dashboard (`VITE_*` only), regardless of the folder they start in. Real
  environment variables, such as Codespaces secrets, override `.env`.
- **Database.** `DATABASE_URL=postgresql://postgres:jarvis@localhost:5432/jarvis` matches `docker-compose.dev.yml`.
  Do not change one without the other. These are the credentials existing volumes were created with.
- **Empty values.** An empty value in `.env` means "not set": the provider shows `NOT_CONFIGURED`. Nothing is
  simulated.
- **`JARVIS_ENCRYPTION_KEY`.** It encrypts the provider keys you save in Integrations, so keep it stable.
  - If it is not provided, `dev-up.sh` generates one once and writes it to `.env`. It is never regenerated.
  - Files in the Codespace survive restarts and rebuilds, but **not deleting the Codespace**. To keep the key
    permanently, go to github.com > Settings > Codespaces > Secrets, add `JARVIS_ENCRYPTION_KEY` with the value from
    your `.env`, and give it access to this repository. The secret then overrides `.env` in every Codespace.
  - Credentials saved under a different key cannot be read. The Integrations page says so, and you re-enter them.
- **Provider keys.** Enter them in the dashboard (**Integrations**), or put them in `.env`. See
  [INTEGRATIONS_AND_VOICE.md](INTEGRATIONS_AND_VOICE.md).
- **Never commit `.env`.** It is git-ignored. Only `.env.example` is tracked.

## 7. No sign-in during development

The dashboard opens straight to the Command Center. This is an explicit **local development access mode**:

- `.env.example` sets `JARVIS_ENV=local` and `JARVIS_LOCAL_DEV_ACCESS=true`.
- Requests without a token then act as one local operator, `local-operator@jarvis.local`. That operator is the owner of
  a local workspace and administrator of the local deployment.
- The sidebar shows "Local development mode · no sign-in".

Safeguards:

- **Production refuses it.** Settings refuse to load if `JARVIS_LOCAL_DEV_ACCESS=true` with any `JARVIS_ENV` other
  than `local`, so the API and worker will not start. It is not a production feature.
- **Tokens are still checked.** A request that does send a token is verified normally.
- **Keep the ports private.** In Codespaces, forwarded ports are private by default, so only you can reach them. Do
  not make port 5173 or 8000 public while local access mode is on.
- **Production sign-in.** Supabase Auth (backend JWT verification) is kept for production. Sign-in screens will be added
  at the end of product development.

## 8. Opening the dashboard

- **Codespaces.** The Ports tab shows **JARVIS dashboard (5173)**; open it with the globe icon. It also opens
  automatically on start. The URL looks like `https://<codespace-name>-5173.app.github.dev`.
- **Local machine.** Open http://localhost:5173.
- **How it reaches the API.** The dashboard calls `/api/...` on its own origin, and Vite forwards that to the API on
  port 8000. So the browser never uses `localhost:8000` and needs no CORS setup.
  - For a deployed dashboard, set `VITE_API_BASE_URL` to the API's https URL instead.
  - If the API is down, the dashboard shows "The JARVIS API is not reachable at /api". It never silently uses another
    address.

## 9. After pulling new code

```bash
scripts/dev-up.sh
```

This installs changed dependencies, applies new migrations and starts anything that is not running. Running services
keep their old code until they are restarted. To restart them on new code:

```bash
scripts/dev-down.sh && scripts/dev-up.sh
```

The Vite dashboard reloads frontend changes automatically.

## 10. Recovering the database

| Symptom (from `dev-up.sh` or `dev-status.sh`) | Cause | Fix |
|---|---|---|
| `cannot connect to PostgreSQL ... is the database running?` | Container stopped, or Docker not running | `scripts/dev-up.sh` starts it; check `docker ps` and `docker compose -f docker-compose.dev.yml logs db` |
| `password authentication failed ...` | `DATABASE_URL` in `.env` (or an environment variable) does not match the volume's credentials | Set `DATABASE_URL=postgresql://postgres:jarvis@localhost:5432/jarvis`; check `env \| grep DATABASE_URL` for an override |
| `database does not exist` | Wrong database name in `DATABASE_URL` | Use `/jarvis`, or create the database |
| `migration NNNN failed and was rolled back` | The SQL failed | Fix it, or add a corrective migration; rerun `dev-up.sh` |
| `applied migration ... was modified` | An applied file was edited | Restore it from git and put the change in a new migration |
| `/ready` 503 with `pending` | Code is newer than the schema | `scripts/dev-up.sh` |
| `database did not become healthy in 60s` | Container crash, disk full or port 5432 taken | `docker compose -f docker-compose.dev.yml logs db`; free disk space |

**Data safety.** The `pgdata` volume is never removed by these scripts. Do not run
`docker compose -f docker-compose.dev.yml down -v`: the `-v` deletes the database.

## 11. Tests

| What | Command |
|---|---|
| Backend | `cd backend && .venv/bin/pytest -q`. Needs a PostgreSQL for test databases: set `TEST_PG_ADMIN_DSN`, for example `postgresql://postgres:jarvis@localhost:5432/postgres` |
| Dashboard | `cd dashboard && npm run typecheck && npm test && npx vite build` |
| Startup workflow | `scripts/tests/dev-startup-test.sh`. Real Docker database and real scripts, using a throwaway database |
| Real-stack E2E | With JARVIS running: `node dashboard/e2e/stack.e2e.mjs http://localhost:5173 /tmp`. Add `E2E_LIVE_PROVIDER_CHECKS=1` to also confirm the real providers reject invalid keys |
