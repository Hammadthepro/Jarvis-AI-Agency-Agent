"""LIVE voice test against a real LiveKit project and a running JARVIS API. Skips (with the reason) unless all
of these are set:
  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   - LiveKit Cloud project (Build plan is enough)
  JARVIS_API_URL                                     - reachable JARVIS API configured with the same LiveKit keys
  VOICE_AGENT_SHARED_SECRET                          - same value as the API
  JARVIS_LIVE_USER_ID, JARVIS_LIVE_WORKSPACE_ID      - an operator member of a workspace in that API's database

What it verifies end to end:
  1. authentication: a participant without a JARVIS identity is ignored; a JARVIS-minted identity is served
  2. audio input: real speech (synthesised with LiveKit Inference TTS) is published as a microphone track
  3. transcription: the agent publishes a `transcript` data message containing the spoken command
  4. command execution: the agent publishes a `status` message produced by the JARVIS API
  5. spoken response: the agent publishes an audio track and non-silent frames arrive
  6. disconnect/reconnect: after leaving, a new session in a new room works again
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
            .with_metadata(json.dumps(metadata)).with_grants(api.VideoGrants(room_join=True, room=room)).to_jwt())


async def speak_into(room, text: str) -> None:
    from livekit import rtc
    from livekit.agents import inference
    source = rtc.AudioSource(24000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    tts = inference.TTS(model=os.environ.get("VOICE_TTS_MODEL", "cartesia/sonic-2"), sample_rate=24000)
    async for chunk in tts.synthesize(text):
        await source.capture_frame(chunk.frame)
    silence = rtc.AudioFrame.create(24000, 1, 2400)
    for _ in range(20):                       # trailing silence so endpointing closes the turn
        await source.capture_frame(silence)


async def session(identity: str, room_name: str, *, speak: str | None, expect_agent: bool, timeout: float = 60) -> dict:
    from livekit import rtc
    room = rtc.Room()
    got: dict = {"messages": [], "audio_frames": 0}

    def on_data(pkt: rtc.DataPacket) -> None:
        if pkt.topic == "jarvis":
            got["messages"].append(json.loads(pkt.data.decode()))

    def on_track(track, _pub, _p) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            async def pump():
                async for ev in rtc.AudioStream(track):
                    if any(ev.frame.data):
                        got["audio_frames"] += 1
            asyncio.ensure_future(pump())
    room.on("data_received", on_data)
    room.on("track_subscribed", on_track)
    await room.connect(os.environ["LIVEKIT_URL"], token(identity, room_name, {"workspace_id": os.environ["JARVIS_LIVE_WORKSPACE_ID"]}))
    try:
        deadline = time.monotonic() + timeout
        while expect_agent and got["audio_frames"] == 0 and time.monotonic() < deadline:   # greeting audio
            await asyncio.sleep(0.5)
        if speak:
            await speak_into(room, speak)
            while time.monotonic() < deadline and not any(m["type"] == "status" for m in got["messages"]):
                await asyncio.sleep(0.5)
        if not expect_agent:
            await asyncio.sleep(10)
    finally:
        await room.disconnect()
    return got


@pytest.fixture(scope="module")
def agent_worker():
    proc = subprocess.Popen([sys.executable, str(AGENT), "dev"], env=os.environ.copy(), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    time.sleep(8)
    assert proc.poll() is None, proc.stdout.read()
    yield proc
    proc.terminate()
    proc.wait(timeout=30)


@pytest.mark.asyncio
async def test_unauthenticated_identity_is_not_served(agent_worker):
    got = await session(f"guest-{uuid.uuid4().hex[:6]}", f"jarvis-live-{uuid.uuid4().hex[:8]}", speak=None, expect_agent=False)
    assert got["messages"] == [] and got["audio_frames"] == 0


@pytest.mark.asyncio
async def test_spoken_command_round_trip_and_reconnect(agent_worker):
    ident = f"user-{os.environ['JARVIS_LIVE_USER_ID']}"
    got = await session(ident, f"jarvis-live-{uuid.uuid4().hex[:8]}", speak="Find ten roofers in Denver", expect_agent=True)
    transcripts = [m["text"].lower() for m in got["messages"] if m["type"] == "transcript"]
    statuses = [m["text"] for m in got["messages"] if m["type"] == "status"]
    assert any("roof" in t and "denver" in t for t in transcripts), got["messages"]
    assert statuses and ("roofing" in statuses[-1].lower() or "denver" in statuses[-1].lower()), statuses
    assert got["audio_frames"] > 0                                     # JARVIS spoke (greeting and reply)
    again = await session(ident, f"jarvis-live-{uuid.uuid4().hex[:8]}", speak=None, expect_agent=True)
    assert again["audio_frames"] > 0                                   # a new session after disconnect works
