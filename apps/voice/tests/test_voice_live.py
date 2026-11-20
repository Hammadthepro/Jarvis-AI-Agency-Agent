"""LIVE voice test against a real LiveKit project and a running JARVIS API. Skips (with the reason) unless all
of these are set:
  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   - LiveKit Cloud project (Build plan is enough)
  JARVIS_API_URL                                     - reachable JARVIS API configured with the same LiveKit keys
  VOICE_AGENT_SHARED_SECRET                          - same value as the API
  JARVIS_LIVE_USER_ID, JARVIS_LIVE_WORKSPACE_ID      - an operator member of a workspace in that API's database

What it verifies end to end (real room, real STT/TTS, real Claude through the JARVIS API; no simulated transcripts):
  1. authentication: a participant without a JARVIS identity is ignored; a JARVIS-minted identity is served
  2. a long request spoken with a mid-sentence pause becomes ONE turn (not cut off), with exactly one transcript
  3. the reply comes from Claude (not provider_error) and is spoken (audio frames after the reply)
  4. a follow-up ("focus on roofing...") is one more turn with its own reply
  5. barge-in: speaking over JARVIS's reply interrupts it and the new instruction gets its own reply
  6. reconnect: a new room continues the same conversation (JARVIS can say what was discussed)
Cost: a few seconds of STT/TTS and agent minutes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REQUIRED = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "JARVIS_API_URL", "VOICE_AGENT_SHARED_SECRET",
            "JARVIS_LIVE_USER_ID", "JARVIS_LIVE_WORKSPACE_ID"]
MISSING = [k for k in REQUIRED if not os.environ.get(k)]
pytestmark = pytest.mark.skipif(bool(MISSING), reason=f"voice NOT_CONFIGURED for live test; missing: {', '.join(MISSING)}")
AGENT = Path(__file__).resolve().parents[1] / "agent.py"


def token(identity: str, room: str, metadata: dict) -> str:
    from livekit import api
    return (api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"]).with_identity(identity)
            .with_metadata(json.dumps(metadata)).with_grants(api.VideoGrants(room_join=True, room=room))
            # explicit dispatch, exactly like the JARVIS API's browser tokens
            .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=os.environ.get("VOICE_AGENT_NAME", "jarvis"))]))
            .to_jwt())


async def speak_into(room, text: str | list[str], pause_s: float = 0.0, source=None):
    """Plays synthesized speech into the room as the user's microphone; a list is spoken with `pause_s` of silence
    between the parts (a natural mid-sentence pause). Runs in the TEST process, outside any agent job, so the plugin's
    HTTP session is opened explicitly with the SDK's supported context manager."""
    from livekit import rtc
    from livekit.agents import inference
    from livekit.agents.utils import http_context
    if source is None:
        source = rtc.AudioSource(24000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    silence = rtc.AudioFrame.create(24000, 1, 2400)                # 100 ms
    async with http_context.open():
        tts = inference.TTS(model=os.environ.get("VOICE_TTS_MODEL", "cartesia/sonic-2"), sample_rate=24000)
        parts = [text] if isinstance(text, str) else text
        for i, part in enumerate(parts):
            if i:
                for _ in range(int(pause_s * 10)):
                    await source.capture_frame(silence)
            async for chunk in tts.synthesize(part):
                await source.capture_frame(chunk.frame)
    for _ in range(20):                                            # trailing silence so the turn can end
        await source.capture_frame(silence)
    return source


class Conversation:
    """One browser-like participant in a real JARVIS room."""

    def __init__(self, identity: str, room_name: str):
        from livekit import rtc
        self.rtc, self.identity, self.room_name = rtc, identity, room_name
        self.room = rtc.Room()
        self.messages: list[dict] = []
        self.frames = 0
        self.agent_joined_s = None
        self.source = None
        self.t0 = time.monotonic()

    def of(self, kind: str) -> list[dict]:
        return [m for m in self.messages if m.get("type") == kind]

    async def __aenter__(self):
        rtc = self.rtc

        def on_data(pkt) -> None:
            if pkt.topic == "jarvis":
                self.messages.append(json.loads(pkt.data.decode()))

        def on_join(p) -> None:
            if self.agent_joined_s is None and not p.identity.startswith("user-"):
                self.agent_joined_s = round(time.monotonic() - self.t0, 1)

        def on_track(track, _pub, _p) -> None:
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                async def pump():
                    async for ev in rtc.AudioStream(track):
                        if any(ev.frame.data):
                            self.frames += 1
                asyncio.ensure_future(pump())
        self.room.on("data_received", on_data)
        self.room.on("track_subscribed", on_track)
        self.room.on("participant_connected", on_join)
        await self.room.connect(os.environ["LIVEKIT_URL"],
                                token(self.identity, self.room_name, {"workspace_id": os.environ["JARVIS_LIVE_WORKSPACE_ID"]}))
        return self

    async def __aexit__(self, *exc):
        await self.room.disconnect()

    async def wait(self, cond, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            await asyncio.sleep(0.25)
        return cond()

    async def say(self, text, pause_s: float = 0.0) -> None:
        self.source = await speak_into(self.room, text, pause_s, self.source)

    async def turn(self, text, pause_s: float = 0.0, timeout: float = 60) -> dict:
        """Speaks one turn; waits for its reply and for the reply to be heard. Returns {transcript, reply, frames}."""
        n = len(self.of("status"))
        await self.say(text, pause_s)
        assert await self.wait(lambda: len(self.of("status")) > n, timeout), f"no reply to {text!r}: {self.messages}"
        f0 = self.frames
        await self.wait(lambda: self.frames - f0 > 50, 20)
        return {"transcript": self.of("transcript")[-1]["text"], "reply": self.of("status")[-1]["text"],
                "frames": self.frames - f0, "errors": self.of("error")}


@pytest.fixture(scope="module")
def agent_worker():
    if os.environ.get("JARVIS_LIVE_USE_RUNNING_AGENT") == "1":   # verify the managed production agent itself
        yield None
        return
    proc = subprocess.Popen([sys.executable, str(AGENT), "dev"], env=os.environ.copy(), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    time.sleep(8)
    assert proc.poll() is None, proc.stdout.read()
    yield proc
    proc.terminate()
    proc.wait(timeout=30)


@pytest.mark.asyncio
async def test_unauthenticated_identity_is_not_served(agent_worker):
    async with Conversation(f"guest-{uuid.uuid4().hex[:6]}", f"jarvis-live-{uuid.uuid4().hex[:8]}") as c:
        await asyncio.sleep(12)
    assert c.messages == [] and c.frames == 0


@pytest.mark.asyncio
async def test_natural_conversation_follow_up_interruption_and_reconnect(agent_worker):
    ident = f"user-{os.environ['JARVIS_LIVE_USER_ID']}"
    async with Conversation(ident, f"jarvis-live-{uuid.uuid4().hex[:8]}") as c:
        assert await c.wait(lambda: c.frames > 0, 60), "the JARVIS agent never joined/greeted"     # greeting heard
        print("agent joined after", c.agent_joined_s, "s")

        # 1. a long request with a natural mid-sentence pause is ONE turn, not cut off
        t1 = await c.turn(["Find potential customers for", "web development in Denver"], pause_s=0.7)
        print("turn 1:", t1)
        assert len(c.of("transcript")) == 1, f"one sentence must give one turn: {c.of('transcript')}"
        assert "customers" in t1["transcript"].lower() and "denver" in t1["transcript"].lower()
        assert not t1["errors"], t1["errors"]
        assert t1["frames"] > 0, "JARVIS's reply was not heard"

        # 2. follow-up in the same conversation
        t2 = await c.turn("Focus on roofing companies, and prioritize businesses with outdated websites.")
        print("turn 2:", t2)
        assert len(c.of("transcript")) == 2 and "roof" in t2["transcript"].lower()
        assert not t2["errors"], t2["errors"]

        # 3. barge-in: ask for something long, talk over the reply, and the correction is answered
        n = len(c.of("status"))
        await c.say("Explain in detail how you will qualify these roofing prospects.")
        assert await c.wait(lambda: len(c.of("status")) > n, 60)
        f0 = c.frames
        assert await c.wait(lambda: c.frames - f0 > 20, 20), "JARVIS did not start speaking"
        t3 = await c.turn("Stop. Actually, do the same for Austin instead.")
        print("turn 3 (interruption):", t3)
        assert "austin" in t3["transcript"].lower()
        statuses, transcripts = c.of("status"), c.of("transcript")
        assert len(transcripts) == 4 and len(statuses) == 4, (transcripts, statuses)      # no duplicate turns or replies
        assert len({m["turn"] for m in transcripts}) == 4

    # 4. reconnect: a new room, the same conversation
    async with Conversation(ident, f"jarvis-live-{uuid.uuid4().hex[:8]}") as c2:
        assert await c2.wait(lambda: c2.frames > 0, 60)
        t4 = await c2.turn("Which city did I just switch to?")
        print("after reconnect:", t4)
        assert "austin" in t4["reply"].lower(), "the conversation was not restored after reconnect"
