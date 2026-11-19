"""LIVE provider tests: real credentials, real network, tightly capped (one call each).
Run only with JARVIS_LIVE_TESTS=1. Each test SKIPS with an explicit reason when its credential is missing;
skipped live tests are never reported as passed. Google Places is not exercised here unless the
`google_places_technical_testing` compliance scope is approved (see docs)."""

from __future__ import annotations

import os
import uuid

import pytest

from jarvis.config import Settings
from jarvis.integrations.base import CallContext

LIVE = os.environ.get("JARVIS_LIVE_TESTS") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="live tests disabled (set JARVIS_LIVE_TESTS=1)")


def need(var: str) -> None:
    if not os.environ.get(var):
        pytest.skip(f"{var} not set: integration NOT_CONFIGURED, live test not run")


async def _ctx(db):
    from jarvis.api.auth import create_workspace
    uid = uuid.uuid4()
    await db.execute("insert into app_users(id) values ($1)", uid)
    ws = await create_workspace(db, uid, "live-test")
    return CallContext(db, ws)


async def test_live_claude_models_and_one_structured_call(db):
    need("ANTHROPIC_API_KEY")
    from jarvis.integrations.claude import ClaudeClient
    s = Settings(jarvis_env="live-test")
    c = ClaudeClient(s)
    for m in (s.claude_model_fast, s.claude_model_build, s.claude_model_escalation):
        assert (await c.validate_model(m))["id"]
    res = await c.structured(await _ctx(db), model=s.claude_model_fast, system="Return the JSON asked for.",
                             user="Return {\"ok\": true}", schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                                  "required": ["ok"], "additionalProperties": False},
                             max_tokens=50, operation="live.test")
    assert res.data == {"ok": True} and res.tokens_in > 0


async def test_live_serpapi_one_search(db):
    need("SERPAPI_API_KEY")
    from jarvis.integrations.serpapi import SerpApiClient
    s = Settings(jarvis_env="live-test")
    r = await SerpApiClient(s).search(await _ctx(db), "plumber Houston TX", location="Houston, Texas")
    assert r.search_id and len(r.results) > 0


async def test_live_firecrawl_one_scrape(db):
    need("FIRECRAWL_API_KEY")
    from jarvis.integrations.firecrawl import FirecrawlClient
    page = await FirecrawlClient(Settings(jarvis_env="live-test")).scrape(await _ctx(db), "https://example.com")
    assert "Example Domain" in page.markdown


async def test_live_cloudflare_token():
    need("CLOUDFLARE_API_TOKEN")
    need("CLOUDFLARE_ACCOUNT_ID")
    from jarvis.integrations.cloudflare import CloudflarePagesClient
    r = await CloudflarePagesClient(Settings(jarvis_env="live-test")).verify_token()
    assert r.get("success") is True
