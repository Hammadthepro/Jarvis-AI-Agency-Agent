"""Offline tests of the voice agent's turn handling, identity binding and LiveKit object construction."""
import json
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
