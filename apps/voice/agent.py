"""JARVIS LiveKit voice agent.

Speech-to-text and text-to-speech run through LiveKit Inference (STT: deepgram/nova-3, TTS: cartesia/sonic-2;
configurable). Recognised speech is NOT answered by a general LLM: every final user turn is sent to the same
JARVIS conversation router as typed text (POST /internal/voice/command), which creates, clarifies, confirms or
cancels commands and reports real campaign status. JARVIS's reply is spoken back. After a campaign is
authorized, the agent polls its real progress and speaks stage changes (it never invents progress).

Identity: the browser joins with a token minted by the JARVIS API (POST /voice/token): identity `user-<uuid>`,
metadata {"workspace_id": ...}. Only the API can mint such tokens (it holds the LiveKit secret). The session is
bound to that one participant and closes when they disconnect.

Controls (data messages on topic "jarvis-control" from that participant only):
  {"type": "interrupt"}  stop speaking now (barge-in also works by simply talking)
  {"type": "end"}        end the session

Credentials: LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET from the environment, or, when they were saved
in the Integrations Center, fetched once at startup from the API's internal endpoint (internal network +
VOICE_AGENT_SHARED_SECRET). The agent heartbeats to the API so the dashboard can report voice as OPERATIONAL.

Dispatch: the agent registers as VOICE_AGENT_NAME (default "jarvis") with explicit dispatch; the API puts that name in
each browser token's room configuration, so LiveKit dispatches exactly one JARVIS agent to each new room on join.

Run: python apps/voice/agent.py start   (JARVIS_API_URL, VOICE_AGENT_SHARED_SECRET, optionally LIVEKIT_*)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import threading
import uuid

import httpx

log = logging.getLogger("jarvis.voice")
API = os.environ.get("JARVIS_API_URL", "http://api:8000")
SECRET = os.environ.get("VOICE_AGENT_SHARED_SECRET", "")
STT = os.environ.get("VOICE_STT_MODEL", "deepgram/nova-3")
TTS = os.environ.get("VOICE_TTS_MODEL", "cartesia/sonic-2")
# Explicit dispatch: the API names this agent in each browser token (roomConfig.agents), so every new JARVIS room gets
# exactly one agent as soon as the browser joins. Automatic dispatch is not used.
AGENT_NAME = os.environ.get("VOICE_AGENT_NAME", "jarvis")
GREETING = "JARVIS here. What should I work on?"
TURN_HANDLING = {"interruption": {"enabled": True}}   # barge-in: the operator can interrupt JARVIS mid-sentence
VERSION = "2"
# Shared by the worker and its job processes (they inherit the environment), so job processes report under it.
AGENT_ID = os.environ.get("JARVIS_VOICE_AGENT_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
os.environ["JARVIS_VOICE_AGENT_ID"] = AGENT_ID
TERMINAL = {"completed", "failed", "cancelled", "blocked", "requires_human_review"}


def parse_identity(identity: str, metadata: str | None) -> tuple[str, str] | None:
    """Returns (user_id, workspace_id) for a JARVIS-minted participant, else None."""
    if not identity.startswith("user-"):
        return None
    try:
        ws = json.loads(metadata or "{}").get("workspace_id")
    except json.JSONDecodeError:
        return None
    return (identity.removeprefix("user-"), ws) if ws else None


def last_user_text(items) -> str:
    for item in reversed(list(items)):
        if getattr(item, "role", None) == "user":
            return (getattr(item, "text_content", None) or "").strip()
    return ""


def parse_control(payload: bytes, topic: str | None, sender: str | None, owner: str) -> str | None:
    """Accepts control messages only from the session owner on the control topic."""
    if topic != "jarvis-control" or sender != owner:
        return None
    try:
        kind = json.loads(payload.decode()).get("type")
    except (ValueError, AttributeError):
        return None
    return kind if kind in ("interrupt", "end") else None


class TurnHandler:
    """Transport-independent logic for one voice session (unit-tested without LiveKit)."""

    def __init__(self, user_id: str, workspace_id: str, publish, http: httpx.AsyncClient | None = None,
                 api: str = API, secret: str = SECRET, speak=None, poll_s: float = 20.0) -> None:
        self.user_id, self.workspace_id, self.publish = user_id, workspace_id, publish
        self.http = http or httpx.AsyncClient(timeout=60)
        self.api, self.secret = api, secret
        self.speak, self.poll_s = speak, poll_s
        self.watchers: dict[str, asyncio.Task] = {}

    def _h(self) -> dict:
        return {"X-Voice-Agent-Key": self.secret}

    async def handle(self, text: str) -> str:
        if not text:
            return "I didn't catch that."
        await self.publish("transcript", text)
        try:
            r = await self.http.post(f"{self.api}/internal/voice/command", headers=self._h(),
                                     json={"user_id": self.user_id, "workspace_id": self.workspace_id, "text": text})
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("voice.api_error %s", exc)
            say = "I couldn't reach the JARVIS service. Nothing was started. Please try again or use the text console."
            await self.publish("status", say)
            return say
        say = body.get("say") or (body.get("error") or {}).get("message") or "I didn't get a reply from JARVIS."
        await self.publish("status", say)
        if body.get("kind"):
            await self.publish("reply", json.dumps({"kind": body["kind"], "command_id": body.get("command_id"),
                                                    "campaign_id": body.get("campaign_id")}))
        if body.get("campaign_id") and self.speak is not None:
            self.watch(body["campaign_id"])
        return say

    def watch(self, campaign_id: str) -> None:
        if campaign_id not in self.watchers:
            self.watchers[campaign_id] = asyncio.create_task(self.progress_loop(campaign_id))

    async def progress_loop(self, campaign_id: str, max_polls: int = 360) -> None:
        """Speaks real stage changes for an authorized campaign until it reaches a terminal state."""
        last = None
        for _ in range(max_polls):
            await asyncio.sleep(self.poll_s)
            try:
                r = await self.http.get(f"{self.api}/internal/voice/progress", headers=self._h(),
                                        params={"campaign_id": campaign_id, "user_id": self.user_id,
                                                "workspace_id": self.workspace_id})
                p = r.json()
            except (httpx.HTTPError, ValueError):
                continue                      # transient: try again next poll, never invent progress
            if not p.get("found", True):
                return
            key = (p.get("state"), p.get("stage"), (p.get("counts") or {}).get("deployed"))
            if key != last:
                if last is not None:
                    await self.publish("progress", p["say"])
                    await self.speak(p["say"])
                last = key
            if p.get("state") in TERMINAL:
                return

    async def aclose(self) -> None:
        for t in self.watchers.values():
            t.cancel()
        await self.http.aclose()


def fetch_livekit_config(api: str = API, secret: str = SECRET, http: httpx.Client | None = None) -> dict | None:
    """LiveKit credentials saved in the Integrations Center (internal endpoint). None when unavailable."""
    c = http or httpx.Client(timeout=10)
    try:
        r = c.get(f"{api}/internal/voice/config", headers={"X-Voice-Agent-Key": secret})
        body = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("voice.config_unavailable %s", exc)
        return None
    return body if body.get("state") == "CONFIGURED" else None


def ensure_livekit_env(api: str = API, secret: str = SECRET, http: httpx.Client | None = None) -> str:
    """Returns where LiveKit credentials came from; sets LIVEKIT_* for the SDK when fetched from the API."""
    if all(os.environ.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")):
        return "environment"
    cfg = fetch_livekit_config(api, secret, http)
    if not cfg:
        return "missing"
    os.environ.update({"LIVEKIT_URL": cfg["url"], "LIVEKIT_API_KEY": cfg["api_key"], "LIVEKIT_API_SECRET": cfg["api_secret"]})
    return "integrations_center"


# Worker (parent process) health, reported with every heartbeat. A running supervisor or parent is NOT enough: the API
# reports voice as operational only when the worker is registered with LiveKit and a job process has started.
WORKER_STATE: dict = {"registered": False, "worker_id": None, "last_error": None}


def _sanitize(text: str) -> str:
    return re.sub(r"(api[_-]?(key|secret)|token|secret)\S*\s*[:=]\s*\S+", r"\1=***", text, flags=re.IGNORECASE)[:300]


class LiveKitErrorCapture(logging.Handler):
    """Keeps the most recent error the LiveKit SDK logged in the worker (e.g. a job process that cannot start)."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            msg = f"{msg}: {type(exc).__name__}: {exc}"
        WORKER_STATE["last_error"] = _sanitize(msg)


def heartbeat_once(http: httpx.Client, api: str = API, secret: str = SECRET, detail: str | None = None,
                   event: str = "heartbeat") -> bool:
    try:
        r = http.post(f"{api}/internal/voice/heartbeat", headers={"X-Voice-Agent-Key": secret},
                      json={"agent_id": AGENT_ID, "version": VERSION, "detail": detail, "event": event,
                            "registered": WORKER_STATE["registered"], "worker_id": WORKER_STATE["worker_id"],
                            "last_error": WORKER_STATE["last_error"]})
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def start_heartbeat(interval_s: float = 30.0) -> threading.Event:
    stop = threading.Event()

    def run() -> None:
        with httpx.Client(timeout=10) as c:
            while not stop.is_set():
                if not heartbeat_once(c):
                    log.warning("voice.heartbeat_failed")
                stop.wait(interval_s)
    threading.Thread(target=run, name="jarvis-voice-heartbeat", daemon=True).start()
    return stop


# ---------------------------------------------------------------------------------------------------------------------
# LiveKit worker. Everything LiveKit hands to a job process (prewarm, entrypoint) is MODULE-LEVEL: on Linux the SDK starts
# job processes with multiprocessing "forkserver", which pickles these callables by reference. A function defined inside
# another function cannot be pickled ("Can't pickle local object ..."), so no job process could start.
# ---------------------------------------------------------------------------------------------------------------------
def prewarm(proc) -> None:
    """Runs once in each job process before it takes a job: load the VAD model, then report that a job process really
    started (this is the signal that the worker can serve rooms)."""
    from livekit.plugins import silero
    proc.userdata["vad"] = silero.VAD.load()
    if SECRET:
        with httpx.Client(timeout=5) as c:
            heartbeat_once(c, event="process_ready")


_AGENT_CLASS = None


def jarvis_agent_class():
    """The LiveKit Agent subclass (created on first use so this module imports without LiveKit for unit tests)."""
    global _AGENT_CLASS
    if _AGENT_CLASS is None:
        from livekit.agents import Agent

        class JarvisVoice(Agent):
            def __init__(self, turns: TurnHandler) -> None:
                super().__init__(instructions="Relay the operator's spoken requests to JARVIS and speak JARVIS's replies.")
                self.turns = turns

            async def llm_node(self, chat_ctx, tools, model_settings):
                # Every turn goes to the shared JARVIS conversation (the same Claude agent and memory as text).
                return await self.turns.handle(last_user_text(chat_ctx.items))

            async def on_exit(self) -> None:
                await self.turns.aclose()

        _AGENT_CLASS = JarvisVoice
    return _AGENT_CLASS


async def entrypoint(ctx) -> None:
    """One JARVIS voice session in a dispatched room (runs inside a job process)."""
    from livekit.agents import AgentSession, RoomInputOptions
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    ident = parse_identity(participant.identity, participant.metadata)
    if ident is None:
        log.warning("voice.rejected_participant %s", participant.identity)
        return

    async def publish(kind: str, text: str) -> None:
        await ctx.room.local_participant.publish_data(json.dumps({"type": kind, "text": text, "final": True}).encode(),
                                                      reliable=True, topic="jarvis")

    vad = ctx.proc.userdata.get("vad")
    if vad is None:                                   # prewarm did not run (e.g. a custom launcher): load now
        from livekit.plugins import silero
        vad = silero.VAD.load()
    session = AgentSession(stt=STT, tts=TTS, vad=vad, turn_handling=TURN_HANDLING)

    async def speak(text: str) -> None:
        session.say(text, allow_interruptions=True)

    turns = TurnHandler(*ident, publish, speak=speak)

    def on_data(packet) -> None:
        sender = getattr(getattr(packet, "participant", None), "identity", None)
        kind = parse_control(packet.data, getattr(packet, "topic", None), sender, participant.identity)
        if kind == "interrupt":
            session.interrupt()
        elif kind == "end":
            ctx.shutdown(reason="ended by user")
    ctx.room.on("data_received", on_data)

    await session.start(room=ctx.room, agent=jarvis_agent_class()(turns),
                        room_input_options=RoomInputOptions(participant_identity=participant.identity, close_on_disconnect=True))
    await session.say(GREETING, allow_interruptions=True)


def build_worker():
    """Worker options: module-level (picklable) entrypoint and prewarm, explicit dispatch by name."""
    from livekit.agents import WorkerOptions
    return WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, agent_name=AGENT_NAME)


def build_server():
    """The LiveKit AgentServer with health tracking: registration with LiveKit and SDK errors feed the heartbeat."""
    from livekit.agents import AgentServer
    server = AgentServer.from_server_options(build_worker())

    def on_registered(worker_id, _info) -> None:
        WORKER_STATE.update(registered=True, worker_id=worker_id)
        log.info("voice.worker_registered id=%s", worker_id)
    server.on("worker_registered", on_registered)
    logging.getLogger("livekit").addHandler(LiveKitErrorCapture())
    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not SECRET:
        raise SystemExit("VOICE_AGENT_SHARED_SECRET is not set; refusing to start (NOT_CONFIGURED)")
    source = ensure_livekit_env()
    if source == "missing":
        raise SystemExit("LiveKit is NOT_CONFIGURED: set LIVEKIT_URL/LIVEKIT_API_KEY/LIVEKIT_API_SECRET or save them in "
                         "Integrations > LiveKit (the API must be reachable at JARVIS_API_URL)")
    log.info("voice.livekit_credentials source=%s", source)
    start_heartbeat()
    from livekit.agents import cli
    cli.run_app(build_server())
