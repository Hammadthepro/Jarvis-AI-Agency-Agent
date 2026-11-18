"""Test fixtures. A fresh Postgres database per session (real migrations), the real FastAPI app over
ASGI, and TEST DOUBLES for external providers (only importable under tests/, JARVIS_ENV=test)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import httpx
import pytest

os.environ.setdefault("JARVIS_ENV", "test")
PG_ADMIN = os.environ.get("TEST_PG_ADMIN_DSN", "postgresql://postgres@localhost:54329/postgres")

from jarvis.agents.deps import Deps  # noqa: E402
from jarvis.config import Settings  # noqa: E402
from jarvis.db.migrate import migrate  # noqa: E402
from jarvis.db.pool import Database  # noqa: E402
from tests.doubles import FakeAnthropic, FakeWeb  # noqa: E402

JWT_SECRET = "test-secret-at-least-32-bytes-long-123456"


@pytest.fixture(scope="session")
async def dsn():
    name = f"jarvis_test_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(PG_ADMIN)
    await admin.execute(f'create database "{name}"')
    await admin.close()
    url = PG_ADMIN.rsplit("/", 1)[0] + f"/{name}"
    await migrate(url)
    yield url
    admin = await asyncpg.connect(PG_ADMIN)
    await admin.execute(f'drop database "{name}" with (force)')
    await admin.close()


def make_settings(dsn: str, tmp: Path, **kw) -> Settings:
    base = dict(jarvis_env="test", jarvis_enable_test_doubles=True, database_url=dsn, supabase_jwt_secret=JWT_SECRET,
                sites_artifact_dir=tmp / "sites", qa_runner_mode="subprocess", jarvis_task_lease_seconds=30,
                no_website_min_confidence=0.8, unsubscribe_signing_key="unsub-key-0123456789abcdef0123456789")
    base.update(kw)
    return Settings(**base)


@pytest.fixture
async def db(dsn):
    d = Database(dsn, pool_size=5)
    await d.connect()
    yield d
    await d.close()


@pytest.fixture
def web():
    return FakeWeb()


@pytest.fixture
def fake_claude():
    return FakeAnthropic()


@pytest.fixture
async def deps(db, dsn, tmp_path, web, fake_claude):
    from jarvis.integrations.claude import ClaudeClient
    from jarvis.integrations.cloudflare import CloudflarePagesClient
    from jarvis.integrations.firecrawl import FirecrawlClient
    from jarvis.integrations.google_places import GooglePlacesClient
    from jarvis.integrations.resend import ResendClient
    from jarvis.integrations.serpapi import SerpApiClient
    s = make_settings(dsn, tmp_path, anthropic_api_key="sk-ant-test", serpapi_api_key="s" * 64, firecrawl_api_key="fc-test",
                      google_places_api_key="AIza" + "x" * 35, cloudflare_api_token="cf-test", cloudflare_account_id="a" * 32,
                      email_provider="resend", resend_api_key="re_test")
    http = httpx.AsyncClient(transport=web.transport())
    d = Deps(settings=s, db=db, places=GooglePlacesClient(s, http), serp=SerpApiClient(s, http), firecrawl=FirecrawlClient(s, http),
             claude=ClaudeClient(s, client=fake_claude), cloudflare=CloudflarePagesClient(s, http, runner=web.wrangler_runner),
             email=ResendClient(s, http), fetch=web.fetch, resolver=web.resolve, second_probe_delay_s=0)
    return d


@pytest.fixture
async def client(deps):
    from jarvis.api.app import create_app
    app = create_app(deps.settings, deps)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.app = app
        yield c


async def login(client, email: str) -> dict:
    r = await client.post("/auth/dev-token", json={"email": email})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/me", headers=h)).json()
    return {"headers": h, "me": me}

