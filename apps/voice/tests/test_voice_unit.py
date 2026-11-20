"""Offline tests of the voice agent's turn handling, identity binding and LiveKit object construction."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent  # noqa: E402


def test_parse_identity_only_accepts_api_minted_users():
    assert agent.parse_identity("user-123", json.dumps({"workspace_id": "w1"})) == ("123", "w1")
    assert agent.parse_identity("agent-x", json.dumps({"workspace_id": "w1"})) is None
    assert agent.parse_identity("user-123", "not json") is None
    assert agent.parse_identity("user-123", "{}") is None


def test_last_user_text():
    items = [SimpleNamespace(role="user", text_content=" first "), SimpleNamespace(role="assistant", text_content="x"),
             SimpleNamespace(role="user", text_content=" Find 10 roofers in Denver ")]
    assert agent.last_user_text(items) == "Find 10 roofers in Denver"
    assert agent.last_user_text([]) == ""


@pytest.mark.asyncio
async def test_turn_executes_command_and_returns_spoken_reply():
    seen, published = [], []

    def handler(req):
        seen.append((req.headers["X-Voice-Agent-Key"], json.loads(req.content)))
        return httpx.Response(200, json={"say": "I'll look for 10 roofing businesses in Denver, CO. Say confirm to authorize."})

    async def publish(kind, text):
        published.append((kind, text))
    t = agent.TurnHandler("u1", "w1", publish, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                          api="http://api", secret="s3cret")
    say = await t.handle("Find 10 roofers in Denver")
    assert "roofing" in say and seen == [("s3cret", {"user_id": "u1", "workspace_id": "w1", "text": "Find 10 roofers in Denver"})]
    assert published == [("transcript", "Find 10 roofers in Denver"), ("status", say)]


@pytest.mark.asyncio
async def test_turn_reports_api_errors_and_outage_honestly():
    async def publish(kind, text):
        pass
    t = agent.TurnHandler("u1", "w1", publish, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "voice agent key invalid or voice NOT_CONFIGURED"}}))))
    assert "NOT_CONFIGURED" in await t.handle("hello")

    def down(req):
        raise httpx.ConnectError("refused", request=req)
    t2 = agent.TurnHandler("u1", "w1", publish, http=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    assert "couldn't reach" in await t2.handle("hello")
    assert await t2.handle("") == "I didn't catch that."


@pytest.mark.asyncio
async def test_worker_and_session_objects_construct_with_real_sdk(monkeypatch):
    """Construction only (no network): placeholder keys satisfy the SDK's config validation."""
    pytest.importorskip("livekit.agents")
    monkeypatch.setenv("LIVEKIT_API_KEY", "placeholder")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "placeholder")
    opts = agent.build_worker()
    assert callable(opts.entrypoint_fnc)
    from livekit.agents import AgentSession, RoomInputOptions
    AgentSession(stt=agent.STT, tts=agent.TTS, turn_handling=agent.TURN_HANDLING)
    RoomInputOptions(participant_identity="user-1", close_on_disconnect=True)


def test_control_messages_only_from_session_owner():
    msg = json.dumps({"type": "interrupt"}).encode()
    assert agent.parse_control(msg, "jarvis-control", "user-1", "user-1") == "interrupt"
    assert agent.parse_control(json.dumps({"type": "end"}).encode(), "jarvis-control", "user-1", "user-1") == "end"
    assert agent.parse_control(msg, "jarvis-control", "user-2", "user-1") is None      # another participant
    assert agent.parse_control(msg, "jarvis", "user-1", "user-1") is None              # wrong topic
    assert agent.parse_control(b"not json", "jarvis-control", "user-1", "user-1") is None
    assert agent.parse_control(json.dumps({"type": "run_shell"}).encode(), "jarvis-control", "user-1", "user-1") is None


@pytest.mark.asyncio
async def test_progress_is_spoken_only_on_real_changes_and_stops_at_terminal_state():
    states = iter([
        {"found": True, "state": "running", "stage": "verification", "counts": {"deployed": 0}, "say": "a"},
        {"found": True, "state": "running", "stage": "verification", "counts": {"deployed": 0}, "say": "a"},
        {"found": True, "state": "running", "stage": "build", "counts": {"deployed": 0}, "say": "Now building."},
        {"found": True, "state": "completed", "stage": "completed", "counts": {"deployed": 2}, "say": "Done: 2 deployed."},
    ])
    spoken, published = [], []

    async def speak(t):
        spoken.append(t)

    async def publish(k, t):
        published.append((k, t))
    t = agent.TurnHandler("u1", "w1", publish, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=next(states)))), api="http://api", secret="s", speak=speak, poll_s=0)
    await t.progress_loop("c1")
    assert spoken == ["Now building.", "Done: 2 deployed."] and ("progress", "Now building.") in published


@pytest.mark.asyncio
async def test_authorized_campaign_starts_progress_watch():
    async def publish(k, t):
        pass

    async def speak(t):
        pass
    t = agent.TurnHandler("u1", "w1", publish, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"kind": "confirmed", "campaign_id": "c9", "say": "Campaign authorized."}))),
        api="http://api", secret="s", speak=speak, poll_s=3600)
    assert await t.handle("confirm") == "Campaign authorized."
    assert "c9" in t.watchers
    await t.aclose()


def test_livekit_credentials_from_env_or_integrations_center(monkeypatch):
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    seen = []

    def handler(req):
        seen.append(req.headers.get("X-Voice-Agent-Key"))
        return httpx.Response(200, json={"state": "CONFIGURED", "url": "wss://x.livekit.cloud", "api_key": "k", "api_secret": "s"})
    assert agent.ensure_livekit_env("http://api", "sec", httpx.Client(transport=httpx.MockTransport(handler))) == "integrations_center"
    assert os.environ["LIVEKIT_URL"] == "wss://x.livekit.cloud" and seen == ["sec"]
    assert agent.ensure_livekit_env("http://api", "sec") == "environment"
    for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    nc = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"state": "NOT_CONFIGURED"})))
    assert agent.ensure_livekit_env("http://api", "sec", nc) == "missing"


def test_heartbeat_reports_failure_honestly():
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True})))
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert agent.heartbeat_once(ok, "http://api", "s") is True
    assert agent.heartbeat_once(bad, "http://api", "s") is False


def test_worker_uses_explicit_dispatch_and_prewarm(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    opts = agent.build_worker()
    assert opts.agent_name == "jarvis" == agent.AGENT_NAME            # must match the API's VOICE_AGENT_NAME
    assert opts.prewarm_fnc is agent.prewarm


def test_prewarm_loads_vad_before_any_job():
    proc = SimpleNamespace(userdata={})
    agent.prewarm(proc)
    assert proc.userdata["vad"] is not None


@pytest.mark.asyncio
async def test_plugins_outside_a_job_need_the_sdk_http_context():
    """The live test's speech synthesis runs outside a job; the SDK requires http_context.open() there."""
    from livekit.agents.utils import http_context
    with pytest.raises(RuntimeError, match="outside of a job context"):
        http_context.http_session()
    async with http_context.open():
        assert http_context.http_session() is not None
