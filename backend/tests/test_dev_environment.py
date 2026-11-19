"""Development startup and access-mode guarantees: one database configuration, tracked idempotent migrations,
readiness gated on migrations, clear connection errors, and a local-only access mode that production refuses."""
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

from jarvis.config import DEV_DATABASE_URL, Settings, normalize_dsn
from jarvis.db.migrate import MIGRATIONS_DIR, _describe_connect_error, migrate, status
from tests.conftest import PG_ADMIN

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"


async def fresh_db() -> str:
    name = f"jarvis_dev_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(PG_ADMIN)
    await admin.execute(f'create database "{name}"')
    await admin.close()
    return PG_ADMIN.rsplit("/", 1)[0] + f"/{name}"


async def drop_db(url: str) -> None:
    admin = await asyncpg.connect(PG_ADMIN)
    await admin.execute(f'drop database if exists "{url.rsplit("/", 1)[1]}" with (force)')
    await admin.close()


# ------------------------------------------------------------------ one configuration
def test_single_env_file_and_dev_defaults_match_compose():
    from jarvis import config
    assert config.ENV_FILE is None                                   # tests never read a developer .env
    compose = (ROOT / "docker-compose.dev.yml").read_text()
    assert "POSTGRES_USER: postgres" in compose and "POSTGRES_PASSWORD: jarvis" in compose and "POSTGRES_DB: jarvis" in compose
    assert DEV_DATABASE_URL == "postgresql://postgres:jarvis@localhost:5432/jarvis"
    example = (ROOT / ".env.example").read_text()
    assert f"DATABASE_URL={DEV_DATABASE_URL}" in example
    assert normalize_dsn("postgresql+asyncpg://u:p@h/d") == "postgresql://u:p@h/d"
    assert Settings(database_url="postgresql+asyncpg://u:p@h:5432/d").database_url == "postgresql://u:p@h:5432/d"


# ------------------------------------------------------------------ migrations
async def test_pending_detected_applied_once_and_rerun_is_noop():
    url = await fresh_db()
    try:
        st = await status(url)
        names = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
        assert st.pending == names and not st.ok
        assert {"0007_integration_credentials.sql", "0008_voice_agent.sql"} <= set(names)
        assert await migrate(url) == names
        assert await migrate(url) == []                               # re-running startup never re-applies
        st = await status(url)
        assert st.ok and st.applied == names
        conn = await asyncpg.connect(url)
        try:
            assert await conn.fetchval("select to_regclass('public.integration_credentials') is not null")
            assert await conn.fetchval("select to_regclass('public.voice_agent_heartbeats') is not null")
            assert await conn.fetchval("select count(*) from schema_migrations") == len(names)
        finally:
            await conn.close()
    finally:
        await drop_db(url)


async def test_existing_database_missing_0007_0008_gets_them(tmp_path):
    """An existing database migrated before 0007/0008 existed: only the new files run, existing rows survive."""
    url = await fresh_db()
    try:
        old = tmp_path / "old"
        old.mkdir()
        for p in sorted(MIGRATIONS_DIR.glob("*.sql"))[:6]:
            (old / p.name).write_text(p.read_text())
        await migrate(url, old)
        conn = await asyncpg.connect(url)
        await conn.execute("insert into app_users(id, email) values ($1, 'keep@me.dev')", uuid.uuid4())
        assert not await conn.fetchval("select to_regclass('public.integration_credentials') is not null")
        await conn.close()
        assert await migrate(url) == ["0007_integration_credentials.sql", "0008_voice_agent.sql"]
        conn = await asyncpg.connect(url)
        assert await conn.fetchval("select count(*) from app_users where email='keep@me.dev'") == 1
        assert await conn.fetchval("select to_regclass('public.integration_credentials') is not null")
        await conn.close()
    finally:
        await drop_db(url)


async def test_failed_migration_rolls_back_and_reports(tmp_path):
    url = await fresh_db()
    try:
        d = tmp_path / "m"
        d.mkdir()
        (d / "0001_ok.sql").write_text("create table a(x int);")
        (d / "0002_bad.sql").write_text("create table b(x int); select * from no_such_table;")
        with pytest.raises(RuntimeError, match="0002_bad.sql failed and was rolled back"):
            await migrate(url, d)
        conn = await asyncpg.connect(url)
        try:
            assert await conn.fetchval("select to_regclass('public.a') is not null")
            assert not await conn.fetchval("select to_regclass('public.b') is not null")        # rolled back
            assert await conn.fetchval("select array_agg(filename) from schema_migrations") == ["0001_ok.sql"]
        finally:
            await conn.close()
        (d / "0001_ok.sql").write_text("create table a(x int); -- edited")
        st = await status(url, d)
        assert st.modified == ["0001_ok.sql"] and "modified after being applied" in st.describe()
    finally:
        await drop_db(url)


def run_cli(*args, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "jarvis.db.migrate", *args], cwd=BACKEND, capture_output=True, text=True,
                          env={**os.environ, "JARVIS_ENV_FILE": "", "JARVIS_ENV": "local", **env}, timeout=60)


def test_cli_missing_database_and_unreachable_server_fail_clearly():
    missing = PG_ADMIN.rsplit("/", 1)[0] + "/no_such_database_xyz"
    r = run_cli(env={"DATABASE_URL": missing})
    assert r.returncode == 1 and "database does not exist" in r.stderr
    r = run_cli(env={"DATABASE_URL": "postgresql://postgres:jarvis@127.0.0.1:1/jarvis"})
    assert r.returncode == 1 and "is the database running" in r.stderr
    r = run_cli("status", env={"DATABASE_URL": missing})
    assert r.returncode == 1


def test_wrong_password_message():
    msg = _describe_connect_error(asyncpg.InvalidPasswordError("password authentication failed for user \"postgres\""),
                                  "postgresql://postgres:wrong@localhost:5432/jarvis")
    assert "password authentication failed" in msg and "localhost:5432/jarvis as postgres" in msg and "wrong" not in msg


# ------------------------------------------------------------------ readiness gated on migrations
async def test_api_not_ready_and_worker_refuses_until_migrated(deps, db):
    from jarvis.api.app import create_app
    from jarvis.worker.main import SchemaNotReady, Worker
    url = await fresh_db()
    try:
        await migrate(url, MIGRATIONS_DIR)
        conn = await asyncpg.connect(url)
        await conn.execute("delete from schema_migrations where filename in ('0007_integration_credentials.sql','0008_voice_agent.sql')")
        await conn.close()
        from jarvis.db.pool import Database
        pdb = Database(url, pool_size=2)
        await pdb.connect()
        d2 = type(deps)(**{**deps.__dict__, "db": pdb, "settings": deps.settings.model_copy(update={"database_url": url})})
        app = create_app(d2.settings, d2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/ready")
        assert r.status_code == 503 and r.json()["migrations"]["pending"] == ["0007_integration_credentials.sql", "0008_voice_agent.sql"]
        with pytest.raises(SchemaNotReady, match="0007_integration_credentials.sql"):
            await Worker(d2, worker_id="schema-check").serve()
        await pdb.close()
    finally:
        await drop_db(url)


async def test_ready_when_migrated(client):
    r = await client.get("/ready")
    assert r.status_code == 200 and r.json()["migrations"]["ok"] is True


# ------------------------------------------------------------------ local access mode
def test_local_access_cannot_be_enabled_outside_local():
    for env in ("production", "live-test", "test"):
        with pytest.raises(ValueError, match="only permitted when JARVIS_ENV=local"):
            Settings(jarvis_env=env, jarvis_local_dev_access=True)
    assert Settings(jarvis_env="local", jarvis_local_dev_access=True).local_dev_access_active
    assert not Settings(jarvis_env="local").local_dev_access_active


def test_production_requires_explicit_database_url():
    from tests.test_production_config import PROD
    base = {k: v for k, v in PROD.items() if k != "database_url"}
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(**base)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(**{**base, "database_url": DEV_DATABASE_URL})


async def test_local_mode_serves_dashboard_without_token(deps):
    from jarvis.api.app import create_app
    s = deps.settings.model_copy(update={"jarvis_env": "local", "jarvis_local_dev_access": True})
    app = create_app(s, deps)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        me = (await c.get("/me")).json()
        assert me["auth_mode"] == "local_development" and me["role"] == "owner"
        assert (await c.get("/me")).json()["workspace_id"] == me["workspace_id"]          # stable single operator
        assert (await c.get("/integrations")).json()["can_manage"] is True
        assert (await c.get("/me", headers={"Authorization": "Bearer forged"})).status_code == 401   # tokens still verified
    off = create_app(deps.settings.model_copy(update={"jarvis_env": "local", "jarvis_local_dev_access": False}), deps)  # mode not enabled
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=off), base_url="http://t") as c:
        assert (await c.get("/me")).status_code == 401


def test_startup_scripts_exist_and_are_executable():
    for name in ("dev-up.sh", "dev-down.sh", "dev-status.sh"):
        p = ROOT / "scripts" / name
        assert p.exists() and os.access(p, os.X_OK), name
    assert (ROOT / ".devcontainer" / "devcontainer.json").exists()


_ = asyncio


def test_env_example_loads_and_empty_values_mean_not_configured(tmp_path, monkeypatch):
    """The shipped .env.example must load as-is: empty provider fields are 'not set' (NOT_CONFIGURED), not errors."""
    env = tmp_path / ".env"
    env.write_text((ROOT / ".env.example").read_text())
    for k in list(os.environ):
        if k.startswith(("JARVIS_", "DATABASE_URL", "EMAIL_", "RESEND_", "LIVEKIT_", "SUPABASE_")) and k != "JARVIS_ENV_FILE":
            monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=str(env))
    assert s.jarvis_env == "local" and s.local_dev_access_active and s.database_url == DEV_DATABASE_URL
    assert s.email_provider is None and s.resend_api_key is None and s.livekit_url is None and s.anthropic_api_key is None
    assert str(s.sites_artifact_dir).startswith(str(ROOT))
