"""JARVIS LiveKit voice agent.

Speech-to-text and text-to-speech run through LiveKit Inference (STT: deepgram/nova-3, TTS: cartesia/sonic-2;
configurable). Recognised speech is NOT answered by a general LLM: every final user turn is sent to the same
JARVIS command pipeline as typed text (POST /internal/voice/command) and JARVIS's reply is spoken back.

Identity: the browser joins with a token minted by the JARVIS API (POST /voice/token): identity `user-<uuid>`,
metadata {"workspace_id": ...}. Only the API can mint such tokens (it holds the LiveKit secret). The session is
bound to that one participant (RoomInputOptions.participant_identity) and closes when they disconnect; a
reconnect from the dashboard creates a new room and a new session. Transient network drops are handled by
the LiveKit SDK's own reconnection without ending the session.

Run: python apps/voice/agent.py start   (LIVEKIT_URL/API_KEY/API_SECRET, JARVIS_API_URL, VOICE_AGENT_SHARED_SECRET)
"""

from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger("jarvis.voice")
API = os.environ.get("JARVIS_API_URL", "http://api:8000")
SECRET = os.environ.get("VOICE_AGENT_SHARED_SECRET", "")
STT = os.environ.get("VOICE_STT_MODEL", "deepgram/nova-3")
TTS = os.environ.get("VOICE_TTS_MODEL", "cartesia/sonic-2")
GREETING = "JARVIS is listening. What should I work on?"
TURN_HANDLING = {"interruption": {"enabled": True}}   # barge-in: the operator can interrupt JARVIS mid-sentence


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


class TurnHandler:
    """Transport-independent logic for one voice turn (unit-tested without LiveKit)."""

    def __init__(self, user_id: str, workspace_id: str, publish, http: httpx.AsyncClient | None = None,
                 api: str = API, secret: str = SECRET) -> None:
        self.user_id, self.workspace_id, self.publish = user_id, workspace_id, publish
        self.http = http or httpx.AsyncClient(timeout=60)
        self.api, self.secret = api, secret

    async def handle(self, text: str) -> str:
        if not text:
            return "I didn't catch that."
        await self.publish("transcript", text)
        try:
            r = await self.http.post(f"{self.api}/internal/voice/command", headers={"X-Voice-Agent-Key": self.secret},
                                     json={"user_id": self.user_id, "workspace_id": self.workspace_id, "text": text})
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("voice.api_error %s", exc)
            say = "I couldn't reach the JARVIS service. Please use the text console."
            await self.publish("status", say)
            return say
        say = body.get("say") or (body.get("error") or {}).get("message") or "Done."
        await self.publish("status", say)
        return say

    async def aclose(self) -> None:
        await self.http.aclose()


def build_worker():
    """Imports LiveKit only when running the agent (keeps TurnHandler testable without it)."""
    from livekit.agents import Agent, AgentSession, JobContext, RoomInputOptions, WorkerOptions
    from livekit.plugins import silero

    class JarvisVoice(Agent):
        def __init__(self, turns: TurnHandler) -> None:
            super().__init__(instructions="Relay the operator's spoken commands to JARVIS and speak JARVIS's replies.")
            self.turns = turns

        async def llm_node(self, chat_ctx, tools, model_settings):  # noqa: ARG002 - signature defined by livekit-agents
            return await self.turns.handle(last_user_text(chat_ctx.items))

        async def on_exit(self) -> None:
            await self.turns.aclose()

    async def entrypoint(ctx: JobContext) -> None:
        await ctx.connect()
        participant = await ctx.wait_for_participant()
        ident = parse_identity(participant.identity, participant.metadata)
        if ident is None:
            log.warning("voice.rejected_participant %s", participant.identity)
            return

        async def publish(kind: str, text: str) -> None:
            await ctx.room.local_participant.publish_data(json.dumps({"type": kind, "text": text, "final": True}).encode(),
                                                          reliable=True, topic="jarvis")

        session = AgentSession(stt=STT, tts=TTS, vad=silero.VAD.load(), turn_handling=TURN_HANDLING)
        await session.start(room=ctx.room, agent=JarvisVoice(TurnHandler(*ident, publish)),
                            room_input_options=RoomInputOptions(participant_identity=participant.identity, close_on_disconnect=True))
        await session.say(GREETING, allow_interruptions=True)

    return WorkerOptions(entrypoint_fnc=entrypoint)


if __name__ == "__main__":
    if not SECRET:
        raise SystemExit("VOICE_AGENT_SHARED_SECRET is not set; refusing to start (NOT_CONFIGURED)")
    from livekit.agents import cli
    cli.run_app(build_worker())
