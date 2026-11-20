"""Writes real synthesized speech (LiveKit Inference TTS) to a 48 kHz mono WAV for the browser voice test, which feeds it
to Chromium as the microphone (--use-file-for-fake-audio-capture). Each argument is either text to speak or "@<seconds>"
of silence, e.g.  python make_speech_wav.py out.wav @15 "Find potential customers for" @0.7 "web development in Denver" @25 ...
Needs LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET (LiveKit Cloud)."""
import asyncio
import os
import sys
import wave


async def main(out: str, parts: list[str]) -> None:
    from livekit.agents import inference
    from livekit.agents.utils import http_context
    rate = 48000
    pcm = bytearray()
    async with http_context.open():
        tts = inference.TTS(model=os.environ.get("VOICE_TTS_MODEL", "cartesia/sonic-2"), sample_rate=rate)
        for part in parts:
            if part.startswith("@"):
                pcm += b"\0\0" * int(rate * float(part[1:]))
                continue
            async for chunk in tts.synthesize(part):
                pcm += bytes(chunk.frame.data)
    pcm += b"\0\0" * int(rate * 4.0)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    print(f"{out}: {len(pcm) / 2 / rate:.1f} s")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
