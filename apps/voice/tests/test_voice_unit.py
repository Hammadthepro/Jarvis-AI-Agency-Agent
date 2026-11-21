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


class Pub:
    def __init__(self):
        self.msgs = []

    async def __call__(self, kind, text, turn=None, stage=None):
        self.msgs.append((kind, text, turn, stage))

    def kinds(self):
        return [m[0] for m in self.msgs]


async def said(reply):
    """What JARVIS would speak for a `respond` result (an async text stream, or None for silence)."""
    return None if reply is None else "".join([c async for c in reply]).strip()


def item(role, text, id=None, interrupted=False):
    return SimpleNamespace(role=role, text_content=text, id=id or f"{role}-{text[:8]}", interrupted=interrupted)


@pytest.mark.asyncio
async def test_turn_executes_command_and_returns_spoken_reply():
    seen, pub = [], Pub()

    def handler(req):
        seen.append((req.headers["X-Voice-Agent-Key"], json.loads(req.content)))
        return httpx.Response(200, json={"say": "I'll look for 10 roofing businesses in Denver, CO. Say confirm to authorize.",
                                         "kind": "proposal", "agent": "claude"})
    trace = agent.PipelineTrace("RM_room")
    t = agent.TurnHandler("u1", "w1", pub, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                          api="http://api", secret="s3cret", trace=trace)
    say = await said(await t.respond([item("user", "Find 10 roofers in Denver", "i1")]))
    assert "roofing" in say
    assert seen == [("s3cret", {"user_id": "u1", "workspace_id": "w1", "text": "Find 10 roofers in Denver", "turn_id": "RM_room:t1"})]
    assert pub.msgs[0] == ("transcript", "Find 10 roofers in Denver", "RM_room:t1", None)
    assert ("status", say, "RM_room:t1", None) in pub.msgs
    assert trace.stages("RM_room:t1") == ["turn_commit", "reply_generation_start", "claude_request_start",
                                         "claude_response", "tts_requested"]


@pytest.mark.asyncio
async def test_turn_reports_api_errors_and_outage_honestly():
    pub = Pub()
    t = agent.TurnHandler("u1", "w1", pub, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "voice agent key invalid or voice NOT_CONFIGURED"}}))))
    assert "NOT_CONFIGURED" in await t.handle("hello")
    assert ("error", "jarvis_api") in [(m[0], m[3]) for m in pub.msgs]

    def down(req):
        raise httpx.ConnectError("refused", request=req)
    t2 = agent.TurnHandler("u1", "w1", pub, http=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    assert "couldn't reach" in await t2.handle("hello")
    assert await t2.handle("") is None                     # nothing heard: nothing sent, nothing said


@pytest.mark.asyncio
async def test_claude_failure_is_surfaced_as_a_claude_error():
    pub = Pub()
    t = agent.TurnHandler("u1", "w1", pub, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"kind": "provider_error", "agent": "claude",
                                            "say": "Claude unavailable: PaymentRequiredError HTTP 402 billing_error."}))))
    say = await t.handle("Find roofers in Denver")
    assert say.startswith("Claude unavailable") and ("error", say, "local:t1", "claude") in pub.msgs


def test_one_turn_per_commit_and_interims_do_not_create_turns():
    tr = agent.PipelineTrace("R")
    assert tr.begin_turn() == "R:t1" and tr.begin_turn() == "R:t1"     # speech start + interim updates: same turn
    assert tr.commit() == "R:t1"
    assert tr.begin_turn() == "R:t2"                                    # next utterance
    assert tr.commit() == "R:t2" and tr.commit() == "R:t3"


def test_pending_turn_merges_unsent_fragments_and_reports_interrupted_reply():
    items = [item("user", "hello", "a"), item("assistant", "Hi, what should I work on?", "b"),
             item("user", "Find me the potential customers for", "c"), item("user", "web development in Denver.", "d")]
    text, ids, heard = agent.pending_user_turn(items, sent=set())
    assert text == "Find me the potential customers for web development in Denver." and ids == ["c", "d"] and heard is None
    text, ids, _ = agent.pending_user_turn(items, sent={"c"})
    assert text == "web development in Denver." and ids == ["d"]
    items2 = [item("assistant", "I'll look for roofers in", "e", interrupted=True), item("user", "No, make it Austin", "f")]
    assert agent.pending_user_turn(items2, set()) == ("No, make it Austin", ["f"], "I'll look for roofers in")


@pytest.mark.asyncio
async def test_a_turn_is_sent_once_even_if_generation_runs_again():
    calls, pub = [], Pub()
    t = agent.TurnHandler("u1", "w1", pub, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: calls.append(1) or httpx.Response(200, json={"say": "OK.", "kind": "reply"}))))
    items = [item("user", "Find potential customers for web development in Denver", "u1")]
    assert await said(await t.respond(items)) == "OK."
    assert await t.respond(items) is None and len(calls) == 1     # already sent: no second request, no repeated reply


@pytest.mark.asyncio
async def test_echo_of_jarvis_own_speech_is_ignored():
    calls, pub = [], Pub()
    t = agent.TurnHandler("u1", "w1", pub, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: calls.append(1) or httpx.Response(200, json={"say": "x"}))))
    t.agent_said("JARVIS here. What should I work on?")
    assert await t.respond([item("user", "what should I work on", "e1")]) is None and calls == []
    assert agent.looks_like_echo("what should I work on", ["JARVIS here. What should I work on?"])
    assert not agent.looks_like_echo("work on roofing companies in Denver", ["JARVIS here. What should I work on?"])
    assert not agent.looks_like_echo("yes", ["Shall I start? Say yes."])          # short answers are never echo-dropped


def test_turn_handling_uses_turn_detector_endpointing_and_no_preemptive_generation():
    th = agent.turn_handling("DETECTOR")
    assert th["turn_detection"] == "DETECTOR" and th["preemptive_generation"] == {"enabled": False}
    assert th["interruption"]["enabled"] is True
    assert th["endpointing"]["min_delay"] >= 0.5 and th["endpointing"]["max_delay"] >= 3.0
    assert agent.turn_handling(None)["turn_detection"] == "vad"


def test_session_errors_name_the_failing_stage():
    err = SimpleNamespace(type="stt_error", error=RuntimeError("websocket closed"), recoverable=True)
    assert agent.describe_error(err) == ("stt", "Speech recognition error: RuntimeError: websocket closed. Retrying.")
    assert agent.describe_error(SimpleNamespace(type="tts_error", error=TimeoutError("t"), recoverable=False))[0] == "tts"
    assert agent.describe_error(SimpleNamespace(type="llm_error", error=ValueError("x"), recoverable=False))[0] == "claude"


def test_browser_pipeline_events_are_accepted_only_from_an_allowlist():
    ok = json.dumps({"type": "client", "event": "audio_playing"}).encode()
    assert agent.parse_control(ok, "jarvis-control", "user-1", "user-1") == "client:audio_playing"
    bad = json.dumps({"type": "client", "event": "rm -rf"}).encode()
    assert agent.parse_control(bad, "jarvis-control", "user-1", "user-1") is None


@pytest.mark.asyncio
async def test_worker_and_session_objects_construct_with_real_sdk(monkeypatch):
    """Construction only (no network): placeholder keys satisfy the SDK's config validation."""
    pytest.importorskip("livekit.agents")
    monkeypatch.setenv("LIVEKIT_API_KEY", "placeholder")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "placeholder")
    opts = agent.build_worker()
    assert callable(opts.entrypoint_fnc)
    from livekit.agents import AgentSession, RoomInputOptions
    detector = agent.make_turn_detector()
    session = AgentSession(stt=agent.STT, tts=agent.build_tts(CARTESIA_CFG), llm=agent.jarvis_bridge_llm_class()(),
                           turn_handling=agent.turn_handling(detector))
    # The root cause of "JARVIS hears but never answers": with no LLM, AgentActivity skips every reply.
    assert session.llm is not None and session.llm.model == "jarvis-conversation"
    from livekit.agents import inference
    assert isinstance(detector, inference.TurnDetector)
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

    async def publish(k, t, turn=None, stage=None):
        published.append((k, t))
    t = agent.TurnHandler("u1", "w1", publish, http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=next(states)))), api="http://api", secret="s", speak=speak, poll_s=0)
    await t.progress_loop("c1")
    assert spoken == ["Now building.", "Done: 2 deployed."] and ("progress", "Now building.") in published


@pytest.mark.asyncio
async def test_authorized_campaign_starts_progress_watch():
    async def publish(k, t, turn=None, stage=None):
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


# ---------------------------------------------------------------- job process start (regression: "Can't pickle local object")
def test_entrypoint_and_prewarm_are_module_level_and_picklable():
    import pickle
    for fn in (agent.entrypoint, agent.prewarm):
        assert "<locals>" not in fn.__qualname__
        assert pickle.loads(pickle.dumps(fn)) is fn                       # pickled by reference, as forkserver does
    opts = agent.build_worker()
    assert opts.entrypoint_fnc is agent.entrypoint and opts.prewarm_fnc is agent.prewarm


def test_a_real_job_process_starts_and_runs_prewarm():
    """Spawns a real LiveKit job process exactly as AgentServer does on Linux (forkserver + SDK preloads)."""
    import subprocess
    here = Path(__file__).resolve().parent
    r = subprocess.run([sys.executable, str(here / "spawn_job_process.py"), str(here.parent)], capture_output=True, text=True,
                       timeout=180, env={**os.environ, "VOICE_AGENT_SHARED_SECRET": ""})
    out = r.stdout + r.stderr
    assert "Can't pickle" not in out, out[-2000:]
    assert "RESULT: job process started and prewarm completed" in out, out[-2000:]
    assert "Traceback" not in out, out[-2000:]


def test_prewarm_reports_a_ready_job_process(monkeypatch):
    sent = []
    monkeypatch.setattr(agent, "SECRET", "s")
    monkeypatch.setattr(agent, "heartbeat_once", lambda c, **kw: sent.append(kw.get("event")) or True)
    proc = SimpleNamespace(userdata={})
    agent.prewarm(proc)
    assert proc.userdata["vad"] is not None and sent == ["process_ready"]


def test_worker_errors_are_captured_and_sanitized(monkeypatch):
    import logging
    monkeypatch.setitem(agent.WORKER_STATE, "last_error", None)
    h = agent.LiveKitErrorCapture()
    try:
        raise AttributeError("Can't pickle local object 'build_worker.<locals>.entrypoint' api_secret=abc123SECRET")
    except AttributeError:
        rec = logging.LogRecord("livekit.agents", logging.ERROR, __file__, 1, "error spawning process", None, sys.exc_info())
    h.emit(rec)
    err = agent.WORKER_STATE["last_error"]
    assert "Can't pickle" in err and "abc123SECRET" not in err
    h.emit(logging.LogRecord("livekit.agents", logging.INFO, __file__, 1, "fine", None, None))   # handler level filters info


def test_heartbeat_carries_registration_and_last_error(monkeypatch):
    seen = []
    monkeypatch.setitem(agent.WORKER_STATE, "registered", True)
    monkeypatch.setitem(agent.WORKER_STATE, "worker_id", "AW_1")
    monkeypatch.setitem(agent.WORKER_STATE, "last_error", "boom")
    c = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(json.loads(r.content)) or httpx.Response(200, json={})))
    assert agent.heartbeat_once(c, "http://api", "s")
    assert seen[0]["registered"] is True and seen[0]["worker_id"] == "AW_1" and seen[0]["last_error"] == "boom"
    assert seen[0]["event"] == "heartbeat" and seen[0]["agent_id"] == agent.AGENT_ID


def test_server_marks_registration_when_livekit_registers_the_worker(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    monkeypatch.setitem(agent.WORKER_STATE, "registered", False)
    server = agent.build_server()
    server.emit("worker_registered", "AW_42", None)
    assert agent.WORKER_STATE["registered"] is True and agent.WORKER_STATE["worker_id"] == "AW_42"


@pytest.mark.asyncio
async def test_real_agent_session_routes_a_committed_turn_to_jarvis_and_replies():
    """Regression for "JARVIS hears but never answers": a real LiveKit AgentSession (text mode, no network) must send the
    committed user turn to the JARVIS API once and turn its reply into the assistant message. Without an LLM object on
    the session, LiveKit refuses to generate any reply, which is what the voice session silently did before."""
    pytest.importorskip("livekit.agents")
    from livekit.agents import AgentSession
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        lines = [{"delta": "Denver web development prospects. "}, {"delta": "Which industry first?"},
                 {"final": {"say": "Denver web development prospects. Which industry first?", "kind": "reply"}}]
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"},
                              content="".join(json.dumps(x) + "\n" for x in lines).encode())
    turns = agent.TurnHandler("u1", "w1", Pub(), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                              trace=agent.PipelineTrace("R"))
    async with AgentSession(llm=agent.jarvis_bridge_llm_class()()) as s:
        await s.start(agent.jarvis_agent_class()(turns))
        res = await s.run(user_input="Find potential customers for web development in Denver")
    replies = [e.item.text_content for e in res.events if getattr(e.item, "role", None) == "assistant"]
    assert replies == ["Denver web development prospects. Which industry first?"]
    assert len(calls) == 1 and calls[0]["text"] == "Find potential customers for web development in Denver"

    async with AgentSession() as s:                         # the old configuration: no LLM -> no reply at all
        await s.start(agent.jarvis_agent_class()(turns))
        with pytest.raises(RuntimeError, match="without an LLM"):
            await s.run(user_input="hello")


@pytest.mark.asyncio
async def test_reply_is_streamed_to_speech_as_claude_writes_it():
    """NDJSON deltas from the API are yielded one by one (TTS starts on the first sentence); the final reply is
    published once; the turn's stages are traced."""
    lines = [{"delta": "Veterinary businesses it is. "}, {"delta": "I'll search across the US."},
             {"final": {"say": "Veterinary businesses it is. I'll search across the US.", "kind": "reply", "agent": "claude"}}]
    pub, seen = Pub(), []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"},
                              content="".join(json.dumps(x) + "\n" for x in lines).encode())
    t = agent.TurnHandler("u1", "w1", pub, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                          trace=agent.PipelineTrace("R"))
    stream = await t.respond([item("user", "Veterinary businesses.", "v1")])
    chunks = [c async for c in stream]
    assert chunks == ["Veterinary businesses it is. ", "I'll search across the US."]
    assert seen == ["/internal/voice/command/stream"]
    assert [m for m in pub.msgs if m[0] == "status"] == [("status", "Veterinary businesses it is. I'll search across the US.", "R:t1", None)]
    assert "claude_first_text" in t.trace.stages("R:t1")


@pytest.mark.asyncio
async def test_interrupting_a_streamed_reply_stops_speech_without_losing_the_turn():
    lines = [{"delta": "Here is a long explanation "}, {"delta": "that the owner talks over. "}, {"final": {"say": "x"}}]
    t = agent.TurnHandler("u1", "w1", Pub(), http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "application/x-ndjson"},
                                 content="".join(json.dumps(x) + "\n" for x in lines).encode()))), trace=agent.PipelineTrace("R"))
    stream = await t.respond([item("user", "Explain how you qualify prospects", "q1")])
    assert await stream.__anext__() == "Here is a long explanation "
    await stream.aclose()                                        # LiveKit closes the reply when the owner barges in
    assert "reply_interrupted" in t.trace.stages("R:t1")
    assert t.recent_agent[-1] == "Here is a long explanation "  # what was actually said, for echo detection


@pytest.mark.asyncio
async def test_links_are_not_read_out():
    async def chunks():
        for c in ["Your preview is at https://vet-", "ab12.jarvis-previews.pages.dev ", "if you want a look."]:
            yield c
    out = "".join([c async for c in agent.speakable_stream(chunks())])
    assert out == "Your preview is at the link in the chat if you want a look."


CARTESIA_CFG = {"state": "CONFIGURED", "api_key": "cartesia-test-key", "model": "sonic-3",
                "voice_id": "f786b574-daa5-4673-aa0c-cbe3e8534c02", "speed": "1.0", "emotion": None, "language": "en"}


def test_cartesia_options_are_validated_and_configurable():
    o = agent.tts_options(CARTESIA_CFG)
    assert o == {"api_key": "cartesia-test-key", "model": "sonic-3", "voice": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
                 "language": "en", "text_pacing": True}                    # natural speed (1.0) is not overridden
    o = agent.tts_options({**CARTESIA_CFG, "speed": "1.1", "emotion": "positivity, curiosity"})
    assert o["speed"] == 1.1 and o["emotion"] == ["positivity", "curiosity"]
    assert agent.tts_options({**CARTESIA_CFG, "speed": "slow"})["speed"] == "slow"
    for bad in ({"speed": "3.5"}, {"speed": "warp"}, {"model": "sonic-9"}):
        with pytest.raises(ValueError):
            agent.tts_options({**CARTESIA_CFG, **bad})


def test_missing_cartesia_is_not_configured_and_never_replaced(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    for cfg, word in (({"state": "NOT_CONFIGURED"}, "NOT_CONFIGURED"), ({"state": "DISABLED"}, "disabled")):
        with pytest.raises(agent.VoiceNotConfigured, match=word):
            agent.build_tts(cfg)
    src = Path(agent.__file__).read_text()
    assert "inference.TTS" not in src                                   # no LiveKit Inference speech route at all


def test_cartesia_settings_come_from_env_or_the_integrations_center(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "env-key")
    monkeypatch.setenv("CARTESIA_SPEED", "0.95")
    c = agent.cartesia_settings(http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert c["state"] == "CONFIGURED" and c["source"] == "environment" and c["speed"] == "0.95" and c["model"] == "sonic-3"
    monkeypatch.delenv("CARTESIA_API_KEY")
    monkeypatch.delenv("CARTESIA_SPEED")
    saved = {"state": "CONFIGURED", "cartesia": {"state": "CONFIGURED", "api_key": "vault-key", "model": "sonic-3",
                                                 "voice_id": "a167e0f3-df7e-4d52-a9c3-f949145efdab", "speed": "1.05",
                                                 "emotion": None, "language": "en"}}
    c = agent.cartesia_settings(http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=saved))))
    assert c["source"] == "integrations_center" and c["api_key"] == "vault-key" and c["voice_id"].startswith("a167e0f3")

    def down(req):
        raise httpx.ConnectError("refused", request=req)
    c = agent.cartesia_settings(http=httpx.Client(transport=httpx.MockTransport(down)))
    assert c["state"] == "NOT_CONFIGURED" and "could not be reached" in c["reason"]


def test_cartesia_tts_constructs_with_the_real_plugin():
    pytest.importorskip("livekit.plugins.cartesia")
    from livekit.plugins import cartesia
    tts = agent.build_tts(CARTESIA_CFG)
    assert isinstance(tts, cartesia.TTS) and tts.model == "sonic-3" and tts.capabilities.streaming is True
    assert tts.sample_rate == 24000 and tts._opts.voice == "f786b574-daa5-4673-aa0c-cbe3e8534c02"
    assert callable(tts.prewarm)                                        # the WebSocket pool can be opened before speaking


def test_turns_use_local_models_and_dynamic_endpointing():
    th = agent.turn_handling("DETECTOR")
    assert th["endpointing"]["mode"] == "dynamic" and th["interruption"]["mode"] == "vad"      # no billable cloud detector
    assert th["interruption"]["resume_false_interruption"] is True and th["preemptive_generation"] == {"enabled": False}
    det = agent.make_turn_detector()
    assert det.model == "turn-detector-v1-mini"                          # runs locally in the job process


def test_latency_is_measured_from_real_session_events():
    clock = iter([0.0, 1.0, 1.4, 1.9, 2.6, 3.0, 5.5, 9.0])
    tr = agent.PipelineTrace("R", clock=lambda: next(clock))
    turn = tr.begin_turn()
    tr.mark("speech_end", turn)                # 1.0
    tr.mark("turn_commit", turn)               # 1.4  -> endpointing 400 ms
    tr.mark("claude_first_text", turn)         # 1.9  -> +500
    tr.mark("tts_playout_start", turn)         # 2.6  -> +1200
    tr.mark("browser_reply_audible", turn)     # 3.0  -> +1600
    tr.mark("reply_played", turn)              # 5.5  -> +4100
    assert tr.latency(turn) == {"endpointing_ms": 400, "first_text_ms": 500, "first_audio_ms": 1200, "audible_ms": 1600,
                                "total_ms": 4100}


def test_internal_ids_and_markup_are_never_spoken():
    said = agent.speakable("Queued as **campaign** 0f3c2a1e-1111-4222-8333-944455556666, see https://x.pages.dev now")
    assert "0f3c2a1e" not in said and "*" not in said and "the link in the chat" in said


def test_browser_reports_when_the_reply_is_audible():
    msg = json.dumps({"type": "client", "event": "reply_audible"}).encode()
    assert agent.parse_control(msg, "jarvis-control", "user-1", "user-1") == "client:reply_audible"
