"""Voice backend: scoped short-lived LiveKit tokens, honest operational status, agent heartbeat/config auth,
session termination, and the shared conversation router (same command pipeline for voice and text)."""
import jwt
import pytest
from pydantic import SecretStr

from tests.conftest import login

SECRET = "voice-secret-0123456789abcdef"
LK = {"livekit_url": "wss://jarvis-test.livekit.cloud", "livekit_api_key": SecretStr("APIabc12345"),
      "livekit_api_secret": SecretStr("s" * 40), "voice_agent_shared_secret": SecretStr(SECRET)}


@pytest.fixture
def voice(client, deps):
    s = deps.settings.model_copy(update=LK)
    client.app.state.settings = s
    client.app.state.base_settings = s
    client.app.state.livekit.settings = s
    return client


def agent_h():
    return {"X-Voice-Agent-Key": SECRET}


async def test_token_is_scoped_and_short_lived(voice, db):
    a = await login(voice, "tok@voice.dev")
    r = (await voice.post("/voice/token", headers=a["headers"])).json()
    claims = jwt.decode(r["token"], "s" * 40, algorithms=["HS256"])
    v = claims["video"]
    assert claims["sub"] == f"user-{a['me']['user_id']}" and v["room"] == r["room"] and v["roomJoin"] is True
    assert v["canPublishSources"] == ["microphone"] and not v.get("roomAdmin") and not v.get("roomCreate") and not v.get("roomList")
    assert 0 < claims["exp"] - claims["nbf"] <= 610 and r["expires_in_s"] == 600
    assert a["me"]["workspace_id"] in r["room"]


async def test_voice_status_is_never_ready_on_configuration_alone(voice, db):
    await db.execute("delete from integration_status where provider='livekit'; delete from voice_agent_heartbeats")
    a = await login(voice, "status@voice.dev")
    s = (await voice.get("/voice/status", headers=a["headers"])).json()
    assert s["state"] == "NOT_READY" and s["checks"]["livekit_configured"] and not s["checks"]["livekit_validated"]
    assert any("Test connection" in m for m in s["missing"]) and any("voice agent" in m for m in s["missing"])
    await db.execute("insert into integration_status(provider, state, last_success_at) values ('livekit','CONNECTED',now())")
    assert (await voice.post("/internal/voice/heartbeat", json={"agent_id": "a1", "version": "2"})).status_code == 401
    # Parent alive but NOT registered with LiveKit: not operational.
    assert (await voice.post("/internal/voice/heartbeat", json={"agent_id": "a1", "version": "2"}, headers=agent_h())).status_code == 200
    s = (await voice.get("/voice/status", headers=a["headers"])).json()
    assert s["state"] == "NOT_READY" and s["checks"]["agent_online"] and not s["checks"]["agent_registered"]
    # Registered, but every job process fails to start (the real pickling bug): still NOT operational, reason shown.
    err = "error spawning process: AttributeError: Can't pickle local object 'build_worker.<locals>.entrypoint'"
    await voice.post("/internal/voice/heartbeat", headers=agent_h(),
                     json={"agent_id": "a1", "version": "2", "registered": True, "worker_id": "AW_1", "last_error": err})
    s = (await voice.get("/voice/status", headers=a["headers"])).json()
    assert s["state"] == "NOT_READY" and s["checks"]["agent_registered"] and not s["checks"]["agent_job_process_ready"]
    assert any("no job process has started" in m and "Can't pickle" in m for m in s["missing"])
    # A job process really started (reported from inside it after prewarm): operational.
    await voice.post("/internal/voice/heartbeat", headers=agent_h(), json={"agent_id": "a1", "event": "process_ready"})
    await voice.post("/internal/voice/heartbeat", headers=agent_h(),
                     json={"agent_id": "a1", "version": "2", "registered": True, "worker_id": "AW_1"})
    s = (await voice.get("/voice/status", headers=a["headers"])).json()
    assert s["state"] == "OPERATIONAL" and all(s["checks"].values()) and not s["missing"]
    await db.execute("update voice_agent_heartbeats set last_seen_at = now() - interval '5 minutes'")
    assert (await voice.get("/voice/status", headers=a["headers"])).json()["state"] == "NOT_READY"
    await db.execute("delete from integration_status where provider='livekit'; delete from voice_agent_heartbeats")


async def test_voice_status_not_configured_without_livekit(client):
    a = await login(client, "nolk@voice.dev")
    s = (await client.get("/voice/status", headers=a["headers"])).json()
    assert s["state"] == "NOT_CONFIGURED" and "Integrations > LiveKit" in s["missing"][0]


async def test_agent_config_requires_secret(voice):
    assert (await voice.get("/internal/voice/config")).status_code == 401
    r = (await voice.get("/internal/voice/config", headers=agent_h())).json()
    assert r["state"] == "CONFIGURED" and r["url"].startswith("wss://")


async def test_end_session_only_for_own_room(voice, deps):
    import httpx
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={})
    voice.app.state.livekit.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    a = await login(voice, "end@voice.dev")
    b = await login(voice, "other@voice.dev")
    room = (await voice.post("/voice/token", headers=a["headers"])).json()["room"]
    assert (await voice.post("/voice/end", headers=b["headers"], json={"room": room})).status_code == 404
    r = await voice.post("/voice/end", headers=a["headers"], json={"room": room})
    assert r.json()["ok"] is True and calls == ["/twirp/livekit.RoomService/DeleteRoom"]


async def test_conversation_multi_turn_clarify_status_cancel(voice):
    a = await login(voice, "conv@voice.dev")
    me = a["me"]

    async def say(text):
        r = await voice.post("/internal/voice/command", headers=agent_h(),
                             json={"user_id": me["user_id"], "workspace_id": me["workspace_id"], "text": text})
        assert r.status_code == 200, r.text
        return r.json()
    r = await say("Find 10 roofers in Houston with at least a 3.5 Google rating using Google Places")
    assert r["kind"] == "clarify" and "rating" in r["say"].lower()
    r = await say("hmm, purple")                                       # unclear answer: asks again, nothing created
    assert r["kind"] == "clarify" and r["say"].startswith("Sorry")
    r = await say("exclude them")                                       # answers the pending question
    assert r["kind"] == "blocked" and "Google Places" in r["say"] and "Import" in r["say"]
    r = await say("confirm")
    assert r["kind"] == "blocked" or "nothing waiting" in r["say"].lower()
    r = await say("what's the status?")
    assert r["kind"] == "status" and "Nothing is running" in r["say"]
    r = await say("Find 5 dentists in Austin")
    r = await say("cancel")
    assert r["kind"] == "cancelled"
    cmds = (await voice.get("/commands", headers=a["headers"])).json()
    assert cmds[0]["status"] == "rejected" and all(c["channel"] == "voice" for c in cmds)


async def test_example_agency_command_is_understood_and_blocker_explained(voice):
    a = await login(voice, "example@voice.dev")
    r = (await voice.post("/conversation/turn", headers=a["headers"], json={
        "text": "Find 10 independent roofing companies in Houston with outdated websites and prepare two redesign previews."})).json()
    # SerpApi discovery is not covered by the Google Places restriction: the plan is offered for confirmation
    assert r["kind"] == "confirm_needed", r
    assert "10 roofing businesses in Houston, TX" in r["say"] and "outdated websites" in r["say"]
    assert "2 preview websites" in r["say"] and "blocked" not in r["say"]
    cmds = (await voice.get("/commands", headers=a["headers"])).json()
    assert cmds[0]["channel"] == "text"                                 # same router, text channel


async def test_viewer_cannot_start_work_by_voice(voice, db):
    from tests.test_core_db import new_ws
    ws, owner = await new_ws(db)
    import uuid
    viewer = uuid.uuid4()
    await db.execute("insert into app_users(id, email) values ($1,'viewer@voice.dev')", viewer)
    await db.execute("insert into memberships(workspace_id, user_id, role) values ($1,$2,'viewer')", ws, viewer)
    r = (await voice.post("/internal/voice/command", headers=agent_h(),
                          json={"user_id": str(viewer), "workspace_id": str(ws), "text": "Find 10 roofers in Denver"})).json()
    assert r["kind"] == "error" and "viewer" in r["say"]
    assert await db.fetchval("select count(*) from commands where workspace_id=$1", ws) == 0
    r = await voice.post("/internal/voice/command", headers=agent_h(),
                         json={"user_id": str(uuid.uuid4()), "workspace_id": str(ws), "text": "status"})
    assert r.status_code == 403


async def test_token_dispatches_the_named_jarvis_agent(voice):
    """Explicit dispatch: the browser token itself asks LiveKit to send the 'jarvis' agent into this new room."""
    a = await login(voice, "dispatch@voice.dev")
    r = (await voice.post("/voice/token", headers=a["headers"])).json()
    claims = jwt.decode(r["token"], "s" * 40, algorithms=["HS256"])
    agents = claims["roomConfig"]["agents"]
    assert agents == [{"agentName": "jarvis", "metadata": claims["metadata"]}]
    assert a["me"]["workspace_id"] in claims["metadata"]
