"""JARVIS LiveKit voice agent.

Speech-to-text and text-to-speech run through LiveKit Inference (STT: deepgram/nova-3, TTS: cartesia/sonic-2;
configurable). Recognised speech is NOT answered by a general LLM: every final user turn is sent to the same
JARVIS command pipeline as typed text (POST /internal/voice/command), and JARVIS's structured reply is spoken
back. Barge-in/interruptions are handled by AgentSession (allow_interruptions).

Identity: the browser joins with a token minted by the JARVIS API (POST /voice/token) whose participant identity
is `user-<uuid>` and whose metadata carries the workspace id. The agent forwards those, authenticated with
VOICE_AGENT_SHARED_SECRET; the API re-checks workspace membership and role.

Run: python apps/voice/agent.py start   (needs LIVEKIT_URL/API_KEY/API_SECRET, JARVIS_API_URL, VOICE_AGENT_SHARED_SECRET)
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.plugins import silero

log = logging.getLogger("jarvis.voice")
API = os.environ.get("JARVIS_API_URL", "http://api:8000")
SECRET = os.environ.get("VOICE_AGENT_SHARED_SECRET", "")
STT = os.environ.get("VOICE_STT_MODEL", "deepgram/nova-3")
TTS = os.environ.get("VOICE_TTS_MODEL", "cartesia/sonic-2")


class JarvisVoice(Agent):
    def __init__(self, ctx: JobContext, user_id: str, workspace_id: str) -> None:
        super().__init__(instructions="You relay the operator's spoken commands to JARVIS and speak JARVIS's replies.",
                         allow_interruptions=True)
        self.ctx, self.user_id, self.workspace_id = ctx, user_id, workspace_id
        self.http = httpx.AsyncClient(timeout=60)

    async def _publish(self, kind: str, text: str, final: bool = True) -> None:
        payload = json.dumps({"type": kind, "text": text, "final": final}).encode()
        await self.ctx.room.local_participant.publish_data(payload, topic="jarvis", reliable=True)

    async def llm_node(self, chat_ctx, tools, model_settings):  # noqa: ARG002 - signature defined by livekit-agents
        user_text = ""
        for item in reversed(chat_ctx.items):
            if getattr(item, "role", None) == "user":
                user_text = (getattr(item, "text_content", None) or "").strip()
                break
        if not user_text:
            return "I didn't catch that."
        await self._publish("transcript", user_text)
        try:
            r = await self.http.post(f"{API}/internal/voice/command", headers={"X-Voice-Agent-Key": SECRET},
                                     json={"user_id": self.user_id, "workspace_id": self.workspace_id, "text": user_text})
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("voice.api_error %s", exc)
            return "I couldn't reach the JARVIS service. Please use the text console."
        say = body.get("say") or body.get("error", {}).get("message") or "Done."
        await self._publish("status", say)
        return say


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    if not participant.identity.startswith("user-"):
        log.warning("voice.unexpected_participant %s", participant.identity)
        return
    meta = json.loads(participant.metadata or "{}")
    session = AgentSession(stt=STT, tts=TTS, vad=silero.VAD.load(), allow_interruptions=True)
    await session.start(room=ctx.room, agent=JarvisVoice(ctx, participant.identity.removeprefix("user-"), meta.get("workspace_id", "")))
    await session.say("JARVIS is listening. What should I work on?", allow_interruptions=True)


if __name__ == "__main__":
    if not SECRET:
        raise SystemExit("VOICE_AGENT_SHARED_SECRET is not set; refusing to start (NOT_CONFIGURED)")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
