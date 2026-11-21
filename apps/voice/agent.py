"""JARVIS LiveKit voice agent.

Pipeline (one continuous session; nothing to press per sentence):
  browser mic -> LiveKit room -> streaming STT (LiveKit Inference, deepgram/nova-3: interim + final transcripts)
  -> end-of-turn decision (LiveKit turn detector model + VAD + endpointing delays) -> ONE committed user turn
  -> POST /internal/voice/command (the same JARVIS conversation as typed text: same Claude agent, history, tools,
     approvals and budget) -> Claude's text streamed as it is written -> Cartesia TTS (official LiveKit plugin, Sonic 3,
     WebSocket kept open and prewarmed per session) -> room audio -> browser.

Claude's reply is streamed (NDJSON deltas from the API); speech starts on the first sentence. While Claude is running a
tool there is no text to speak yet. If Cartesia is not configured the session says so and ends: no other voice is used. Preemptive generation is OFF on purpose: a JARVIS turn is stored
and can propose or start work, so it must never run on words the owner has not finished saying.

The session needs an LLM object for LiveKit to generate replies at all (AgentActivity skips the reply when
`session.llm is None`). `JarvisBridgeLLM` is that object; `JarvisVoice.llm_node` sends the turn to JARVIS instead of
calling any model directly, so there is exactly one brain for voice and text.

After a campaign is authorized, the agent polls its real progress and speaks stage changes (it never invents progress).
Every stage is logged as `voice.pipeline turn=<room>:t<n> stage=<stage> t_ms=<ms since session start>`; the turn id
is sent to the API, which logs the Claude request/response under the same id.

Identity: the browser joins with a token minted by the JARVIS API (POST /voice/token): identity `user-<uuid>`,
metadata {"workspace_id": ...}. Only the API can mint such tokens (it holds the LiveKit secret). The session is
bound to that one participant and closes when they disconnect.

Controls (data messages on topic "jarvis-control" from that participant only):
  {"type": "interrupt"}  stop speaking now (barge-in also works by simply talking)
  {"type": "end"}        end the session
  {"type": "client", "event": "mic_published" | "audio_playing" | "audio_blocked"}  browser-side pipeline stages (logged)

Messages to the browser (topic "jarvis"): {"type": "transcript" | "status" | "progress" | "reply" | "error", "text",
"turn", "stage"?}. "transcript" is the committed user turn (exactly one per turn), "status" is the reply that is
spoken, "error" names the failing stage (stt, tts, claude, jarvis_api).

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
import time
import uuid
from collections import deque

import httpx

log = logging.getLogger("jarvis.voice")
API = os.environ.get("JARVIS_API_URL", "http://api:8000")
SECRET = os.environ.get("VOICE_AGENT_SHARED_SECRET", "")
STT = os.environ.get("VOICE_STT_MODEL", "deepgram/nova-3")
# JARVIS's voice: Cartesia through the official LiveKit plugin (livekit-plugins-cartesia, same release as livekit-agents),
# billed to the Cartesia account of CARTESIA_API_KEY. If Cartesia is not configured the session says so and ends:
# no other speech provider (and no LiveKit Inference route) is ever substituted.
# Defaults: sonic-3 (the newest model the installed plugin lists), the plugin's default English agent voice "Katie",
# natural speed. All of them can be set in Integrations > Cartesia or with these variables.
CARTESIA_DEFAULTS = {"model": "sonic-3", "voice_id": "f786b574-daa5-4673-aa0c-cbe3e8534c02", "speed": "1.0",
                     "emotion": None, "language": "en"}
CARTESIA_MODELS = ("sonic-3", "sonic-2", "sonic-turbo", "sonic-lite", "sonic-preview", "sonic")   # plugin 1.8.12 TTSModels
# Send larger chunks of text with more context once speech has started (the first sentence still goes out at once):
# fewer "every sentence is a separate announcement" breaks, more natural prosody (plugin text_pacing).
TTS_TEXT_PACING = os.environ.get("VOICE_TTS_TEXT_PACING", "1") == "1"
# Explicit dispatch: the API names this agent in each browser token (roomConfig.agents), so every new JARVIS room gets
# exactly one agent as soon as the browser joins. Automatic dispatch is not used.
AGENT_NAME = os.environ.get("VOICE_AGENT_NAME", "jarvis")
GREETING = "JARVIS here. What should I work on?"
VERSION = "4"
# End of turn: "local" (default) = LiveKit's turn detector model running locally (v1-mini), which judges from the audio
# whether the owner has finished, so a mid-sentence pause doesn't end the turn; "cloud" = LiveKit Inference v1 (billed by
# LiveKit); "vad" = silence only.
TURN_DETECTION = os.environ.get("VOICE_TURN_DETECTION", "local")
ADAPTIVE_INTERRUPTION = os.environ.get("VOICE_ADAPTIVE_INTERRUPTION") == "1"      # LiveKit Inference: billed; off
# Dynamic endpointing range: the shortest silence that may end a turn, and the longest JARVIS waits when the detector
# thinks the owner isn't finished.
ENDPOINT_MIN_S = float(os.environ.get("VOICE_ENDPOINT_MIN_DELAY", "0.5"))
ENDPOINT_MAX_S = float(os.environ.get("VOICE_ENDPOINT_MAX_DELAY", "3.0"))
LOG_TRANSCRIPTS = os.environ.get("VOICE_LOG_TRANSCRIPTS") == "1"     # transcript text in logs only when asked
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


def pending_user_turn(items, sent: set[str]) -> tuple[str, list[str], str | None]:
    """The user's words to send now: every user message after JARVIS's last reply that has not been sent yet (a turn
    whose request was cut short by an interruption is merged with the new one instead of being lost).
    Returns (text, item ids, what the owner heard of an interrupted previous reply or None)."""
    items = list(items)
    texts, ids, heard = [], [], None
    for item in reversed(items):
        role = getattr(item, "role", None)
        if role == "assistant":
            if getattr(item, "interrupted", False):
                heard = (getattr(item, "text_content", None) or "").strip()
            break
        if role == "user" and getattr(item, "id", None) not in sent:
            t = (getattr(item, "text_content", None) or "").strip()
            if t:
                texts.append(t)
                ids.append(getattr(item, "id", ""))
    return " ".join(reversed(texts)).strip(), ids[::-1], heard


_WORD = re.compile(r"[a-z0-9']+")


def looks_like_echo(heard: str, recent_agent: list[str]) -> bool:
    """True when a 'user' transcript is JARVIS's own recent speech picked up by the microphone (speakers, no headset):
    at least four words, all found in order inside something JARVIS just said."""
    words = _WORD.findall(heard.lower())
    if len(words) < 4:
        return False
    needle = " " + " ".join(words) + " "
    return any(needle in " " + " ".join(_WORD.findall(a.lower())) + " " for a in recent_agent)


class VoiceNotConfigured(Exception):
    """A required voice provider is missing; the message says exactly what to configure."""


def cartesia_settings(api: str = API, secret: str = SECRET, http: httpx.Client | None = None) -> dict:
    """Cartesia key and voice settings: from the environment (CARTESIA_*), else from the Integrations Center via the
    API's internal endpoint. Read for every session, so a key saved in the dashboard applies without a restart.
    Returns {"state": "CONFIGURED" | "NOT_CONFIGURED" | "DISABLED", ...}; never logs the key."""
    if os.environ.get("CARTESIA_API_KEY"):
        return {"state": "CONFIGURED", "source": "environment", "api_key": os.environ["CARTESIA_API_KEY"],
                "model": os.environ.get("CARTESIA_MODEL") or CARTESIA_DEFAULTS["model"],
                "voice_id": os.environ.get("CARTESIA_VOICE_ID") or CARTESIA_DEFAULTS["voice_id"],
                "speed": os.environ.get("CARTESIA_SPEED") or CARTESIA_DEFAULTS["speed"],
                "emotion": os.environ.get("CARTESIA_EMOTION") or None,
                "language": os.environ.get("CARTESIA_LANGUAGE") or CARTESIA_DEFAULTS["language"]}
    c = http or httpx.Client(timeout=10)
    try:
        r = c.get(f"{api}/internal/voice/config", headers={"X-Voice-Agent-Key": secret})
        cfg = (r.json() if r.status_code == 200 else {}).get("cartesia") or {"state": "NOT_CONFIGURED"}
    except (httpx.HTTPError, ValueError) as exc:
        return {"state": "NOT_CONFIGURED", "reason": f"the JARVIS API could not be reached for the voice settings ({exc})"}
    finally:
        if http is None:
            c.close()
    return {**CARTESIA_DEFAULTS, **{k: v for k, v in cfg.items() if v not in (None, "")}, "source": "integrations_center"} \
        if cfg.get("state") == "CONFIGURED" else cfg


def tts_options(cfg: dict) -> dict:
    """Validated arguments for livekit.plugins.cartesia.TTS (a bad value fails at session start, not mid-sentence)."""
    if cfg.get("state") != "CONFIGURED":
        why = "is disabled in the Integrations Center" if cfg.get("state") == "DISABLED" else \
            f"is NOT_CONFIGURED{': ' + cfg['reason'] if cfg.get('reason') else ''}"
        raise VoiceNotConfigured(f"JARVIS's voice (Cartesia) {why}. Add a Cartesia API key in Integrations > Cartesia "
                                 "(or set CARTESIA_API_KEY for the voice agent).")
    model = cfg.get("model") or CARTESIA_DEFAULTS["model"]
    if model not in CARTESIA_MODELS:
        raise ValueError(f"Cartesia model {model!r} is not supported by livekit-plugins-cartesia 1.8.12 ({', '.join(CARTESIA_MODELS)})")
    raw = str(cfg.get("speed") or "1.0")
    try:
        speed: float | str = float(raw)
        if not 0.6 <= speed <= 2.0:
            raise ValueError(f"Cartesia speed {raw} is outside 0.6-2.0")
    except ValueError as exc:
        if raw not in ("slowest", "slow", "normal", "fast", "fastest"):
            raise ValueError(f"Cartesia speed must be 0.6-2.0 or slow/normal/fast, not {raw!r}") from exc
        speed = raw
    opts = {"api_key": cfg["api_key"], "model": model, "voice": cfg.get("voice_id") or CARTESIA_DEFAULTS["voice_id"],
            "language": cfg.get("language") or "en", "text_pacing": TTS_TEXT_PACING}
    if speed not in (1.0, "normal"):
        opts["speed"] = speed
    if cfg.get("emotion"):
        opts["emotion"] = [e.strip() for e in str(cfg["emotion"]).split(",") if e.strip()]
    return opts


def build_tts(cfg: dict):
    from livekit.plugins import cartesia
    return cartesia.TTS(**tts_options(cfg))


_URL = re.compile(r"https?://\S+")
_IDS = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b(?:RM|TR|PA)_[A-Za-z0-9]{6,}(?::t\d+)?\b")
_MD = re.compile(r"[*_`#>]+")


def speakable(text: str) -> str:
    """What is said aloud: links are in the chat, not read character by character; internal ids are never spoken;
    markdown symbols are dropped."""
    out = _IDS.sub("", _URL.sub("the link in the chat", text))
    return re.sub(r"[ \t]{2,}", " ", _MD.sub("", out))


async def speakable_stream(chunks):
    """Applies `speakable` to streamed text without splitting a URL across chunks (holds back an unfinished word)."""
    buf = ""
    async for chunk in chunks:
        buf += chunk
        cut = max(buf.rfind(" "), buf.rfind("\n"))
        if cut == -1:
            continue
        ready, buf = buf[:cut + 1], buf[cut + 1:]
        yield speakable(ready)
    if buf:
        yield speakable(buf)


def turn_handling(detector=None) -> dict:
    """LiveKit turn handling for JARVIS.
    - Endpointing is dynamic: the wait after the owner stops adapts to their own pauses (between min and max).
    - Interruption is VAD-based and local. The "adaptive" detector would call LiveKit Inference (a billable cloud
      model), so it is only used when VOICE_ADAPTIVE_INTERRUPTION=1 is set deliberately.
    - A false interruption (a cough, "mm-hm") resumes the reply instead of dropping it.
    - Preemptive generation is off (see module docstring)."""
    interruption = {"enabled": True, "mode": "adaptive" if ADAPTIVE_INTERRUPTION else "vad", "min_duration": 0.4,
                    "resume_false_interruption": True, "false_interruption_timeout": 1.5}
    return {
        "turn_detection": detector if detector is not None else "vad",
        "endpointing": {"mode": "dynamic", "min_delay": ENDPOINT_MIN_S, "max_delay": ENDPOINT_MAX_S},
        "interruption": interruption,
        "preemptive_generation": {"enabled": False},
    }


def make_turn_detector():
    """LiveKit's end-of-turn model. Default: the LOCAL v1-mini model (runs in the job process, no per-use charge).
    VOICE_TURN_DETECTION=cloud uses LiveKit Inference's v1 model (billed by LiveKit: enable only deliberately);
    VOICE_TURN_DETECTION=vad uses silence only."""
    if TURN_DETECTION == "vad":
        return None
    from livekit.agents import inference
    return inference.TurnDetector(version="v1" if TURN_DETECTION == "cloud" else "v1-mini")


class PipelineTrace:
    """Correlation ids and timestamps for every stage of one voice session (mic -> transcript -> turn -> JARVIS/Claude ->
    TTS -> browser playback). Logs `voice.pipeline` lines and keeps the last events for tests and diagnostics."""

    def __init__(self, ref: str, clock=time.monotonic) -> None:
        self.ref = re.sub(r"[^A-Za-z0-9._-]", "-", ref or "room")[:60]
        self.clock, self.t0 = clock, clock()
        self.n, self.turn, self.open = 0, None, False
        self.interims = 0
        self.events: deque = deque(maxlen=500)

    def begin_turn(self) -> str:
        """Called when the owner starts speaking; a new id unless a turn is already open."""
        if not self.open:
            self.n += 1
            self.turn, self.open, self.interims = f"{self.ref}:t{self.n}", True, 0
        return self.turn

    def commit(self) -> str:
        """The turn is complete and goes to JARVIS; later speech starts a new turn."""
        turn = self.begin_turn()
        self.open = False
        return turn

    def mark(self, stage: str, turn: str | None = None, **kv) -> None:
        ms = int((self.clock() - self.t0) * 1000)
        turn = turn or self.turn or f"{self.ref}:t0"
        self.events.append({"turn": turn, "stage": stage, "t_ms": ms, **kv})
        extra = " ".join(f"{k}={v}" for k, v in kv.items() if v is not None)
        log.info("voice.pipeline turn=%s stage=%s t_ms=%d %s", turn, stage, ms, extra)

    def latency(self, turn: str | None = None) -> dict:
        """Measured (never estimated) milliseconds for one turn, from this session's own events:
        endpointing = owner stopped speaking -> turn committed; first_text = commit -> first Claude text;
        first_audio = commit -> TTS playout started; audible = commit -> the browser heard JARVIS; total = commit ->
        reply finished. Missing stages are omitted."""
        turn = turn or self.turn
        ev = [e for e in self.events if e["turn"] == turn]
        first = {}
        for e in ev:
            first.setdefault(e["stage"], e["t_ms"])
        c = first.get("turn_commit")
        if c is None:
            return {}
        out = {}
        if "speech_end" in first and first["speech_end"] <= c:
            out["endpointing_ms"] = c - max(e["t_ms"] for e in ev if e["stage"] == "speech_end" and e["t_ms"] <= c)
        for name, stage in (("first_text_ms", "claude_first_text"), ("first_audio_ms", "tts_playout_start"),
                            ("audible_ms", "browser_reply_audible"), ("total_ms", "reply_played")):
            if stage in first and first[stage] >= c:
                out[name] = first[stage] - c
        ttfb = next((e.get("ttfb_ms") for e in ev if e["stage"] == "tts_metrics"), None)
        if ttfb is not None:
            out["tts_ttfb_ms"] = ttfb
        stop = next((e.get("interrupt_to_stop_ms") for e in ev if e["stage"] == "speech_stopped_after_barge_in"), None)
        if stop is not None:
            out["interrupt_to_stop_ms"] = stop
        return out

    def log_latency(self, turn: str | None = None) -> dict:
        lat = self.latency(turn)
        if lat:
            log.info("voice.latency turn=%s %s", turn or self.turn, " ".join(f"{k}={v}" for k, v in lat.items()))
        return lat

    def stages(self, turn: str | None = None) -> list[str]:
        return [e["stage"] for e in self.events if turn is None or e["turn"] == turn]


ERROR_STAGE = {"stt_error": "stt", "tts_error": "tts", "llm_error": "claude"}
STAGE_LABEL = {"stt": "Speech recognition", "tts": "Speech synthesis", "claude": "Claude", "jarvis_api": "The JARVIS service"}


def describe_error(err) -> tuple[str, str]:
    """(stage, sentence) for a LiveKit session error event's `error` (STTError / TTSError / LLMError / other)."""
    stage = ERROR_STAGE.get(getattr(err, "type", ""), "voice")
    inner = getattr(err, "error", err)
    retry = " Retrying." if getattr(err, "recoverable", False) else ""
    return stage, _sanitize(f"{STAGE_LABEL.get(stage, 'Voice')} error: {type(inner).__name__}: {inner}.{retry}")


def parse_control(payload: bytes, topic: str | None, sender: str | None, owner: str) -> str | None:
    """Accepts control messages only from the session owner on the control topic."""
    if topic != "jarvis-control" or sender != owner:
        return None
    try:
        kind = json.loads(payload.decode()).get("type")
    except (ValueError, AttributeError):
        return None
    if kind == "client":
        try:
            event = json.loads(payload.decode()).get("event")
        except (ValueError, AttributeError):
            return None
        return f"client:{event}" if event in ("mic_published", "audio_playing", "audio_blocked", "reply_audible") else None
    return kind if kind in ("interrupt", "end") else None


class TurnHandler:
    """Transport-independent logic for one voice session (unit-tested without LiveKit)."""

    def __init__(self, user_id: str, workspace_id: str, publish, http: httpx.AsyncClient | None = None,
                 api: str = API, secret: str = SECRET, speak=None, poll_s: float = 20.0,
                 trace: PipelineTrace | None = None) -> None:
        self.user_id, self.workspace_id, self.publish = user_id, workspace_id, publish
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5))
        self.api, self.secret = api, secret
        self.speak, self.poll_s = speak, poll_s
        self.trace = trace or PipelineTrace("local")
        self.watchers: dict[str, asyncio.Task] = {}
        self.sent: set[str] = set()                 # chat item ids already sent to JARVIS
        self.recent_agent: deque = deque(maxlen=6)  # what JARVIS said lately (echo detection)

    def _h(self) -> dict:
        return {"X-Voice-Agent-Key": self.secret}

    def agent_said(self, text: str) -> None:
        if text:
            self.recent_agent.append(text)

    async def respond(self, items):
        """One committed user turn from the LiveKit chat context -> JARVIS. Returns an async stream of reply text for
        LiveKit to speak as it arrives, or None to say nothing (empty turn, or an echo of JARVIS's own voice)."""
        text, ids, heard = pending_user_turn(items, self.sent)
        turn = self.trace.commit()
        self.trace.mark("turn_commit", turn, chars=len(text), merged=len(ids), interrupted_prev=heard is not None,
                        text=text if LOG_TRANSCRIPTS else None)
        if not text:
            self.trace.mark("turn_empty", turn)
            return None
        if looks_like_echo(text, list(self.recent_agent)):
            self.sent.update(ids)
            self.trace.mark("turn_ignored_echo", turn)
            return None
        self.sent.update(ids)
        return self.stream_reply(text, turn_id=turn, interrupted_heard=heard)

    async def handle(self, text: str, turn_id: str | None = None, interrupted_heard: str | None = None) -> str | None:
        """The whole reply as one string (progress narration, tests)."""
        if not text:
            return None
        parts = [p async for p in self.stream_reply(text, turn_id=turn_id, interrupted_heard=interrupted_heard)]
        return "".join(parts).strip() or None

    async def stream_reply(self, text: str, turn_id: str | None = None, interrupted_heard: str | None = None):
        """Streams JARVIS's reply: text deltas from Claude are yielded as they arrive (LiveKit's TTS starts on the first
        sentence). The turn runs on the server, so if the owner interrupts and this stream is closed, the turn still
        completes and is stored; only the rest of the speech is dropped."""
        turn = turn_id or self.trace.commit()
        await self.publish("transcript", text, turn=turn)
        self.trace.mark("reply_generation_start", turn)
        body = {"user_id": self.user_id, "workspace_id": self.workspace_id, "text": text[:2000], "turn_id": turn}
        if interrupted_heard is not None:
            body["interrupted_heard"] = interrupted_heard[:2000]
        t0 = time.monotonic()
        self.trace.mark("claude_request_start", turn)
        spoken: list[str] = []
        data: dict | None = None
        failure: tuple[str, str] | None = None
        try:
            async with self.http.stream("POST", f"{self.api}/internal/voice/command/stream", headers=self._h(), json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    try:
                        err = json.loads(raw).get("error") or {}
                    except (ValueError, AttributeError):
                        err = {}
                    reason = _sanitize(f"HTTP {r.status_code}: {err.get('message') or raw[:200]!r}")
                    failure = (reason, f"The JARVIS service refused that, so nothing was started. {reason}")
                elif not r.headers.get("content-type", "").startswith("application/x-ndjson"):
                    data = json.loads(await r.aread())              # a non-streaming reply: the whole answer at once
                else:
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        ev = json.loads(line)
                        if "delta" in ev:
                            if not spoken:
                                self.trace.mark("claude_first_text", turn, ms=int((time.monotonic() - t0) * 1000))
                            spoken.append(ev["delta"])
                            yield ev["delta"]
                        elif "final" in ev:
                            data = ev["final"]
                            break
                        elif "error" in ev:
                            e = ev["error"] or {}
                            failure = (_sanitize(f"{e.get('code')}: {e.get('message')}"),
                                       "Something went wrong on my side, so nothing was started. Please say that again.")
                            break
        except (httpx.HTTPError, ValueError) as exc:
            failure = (_sanitize(f"{type(exc).__name__}: {exc}"),
                       "I couldn't reach the JARVIS service, so nothing was started. Please try again in a moment.")
        except (GeneratorExit, asyncio.CancelledError):
            # The owner talked over the reply: speech stops here. The server finishes the turn and stores it.
            self.trace.mark("reply_interrupted", turn, spoken_chars=sum(map(len, spoken)))
            self.agent_said("".join(spoken))
            raise
        if failure:
            say = await self._api_failure(turn, *failure)
            if not spoken:
                yield say
            return
        data = data or {}
        say = data.get("say") or "I didn't get a reply from JARVIS."
        self.trace.mark("claude_response", turn, kind=data.get("kind"), agent=data.get("agent"),
                        ms=int((time.monotonic() - t0) * 1000), replayed=data.get("replayed"), streamed=bool(spoken))
        if not spoken:                                 # replayed turn, basic parser, or a non-streaming server
            yield say
        if data.get("kind") == "provider_error":
            await self.publish("error", say, turn=turn, stage="claude")
        await self.publish("status", say, turn=turn)
        if data.get("kind"):
            await self.publish("reply", json.dumps({"kind": data["kind"], "command_id": data.get("command_id"),
                                                    "campaign_id": data.get("campaign_id")}), turn=turn)
        if data.get("campaign_id") and self.speak is not None:
            self.watch(data["campaign_id"])
        self.agent_said(say)
        self.trace.mark("tts_requested", turn, chars=len(say))

    async def _api_failure(self, turn: str, reason: str, say: str) -> str:
        self.trace.mark("jarvis_api_error", turn, error=reason)
        await self.publish("error", f"The JARVIS service failed: {reason}", turn=turn, stage="jarvis_api")
        await self.publish("status", say, turn=turn)
        self.agent_said(say)
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
            n = p.get("counts") or {}
            key = (p.get("state"), p.get("stage"), n.get("sites_passed"), n.get("deployed"))
            if key != last:
                if last is not None:
                    await self.publish("progress", p["say"], turn=None)
                    self.agent_said(p["say"])
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
WORKER_STATE: dict = {"registered": False, "worker_id": None, "last_error": None, "tts": None}


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
                      json={"agent_id": AGENT_ID, "version": VERSION, "detail": detail or WORKER_STATE.get("tts"), "event": event,
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
_BRIDGE_CLASS = None


def jarvis_bridge_llm_class():
    """The session's LLM object. LiveKit only generates replies when the session has an LLM (agent_activity skips the
    reply when it is None, which is why JARVIS used to hear the owner and never answer). Replies never come from
    here: `JarvisVoice.llm_node` sends each turn to the shared JARVIS conversation. Calling `chat` directly is a bug."""
    global _BRIDGE_CLASS
    if _BRIDGE_CLASS is None:
        from livekit.agents import llm

        class JarvisBridgeLLM(llm.LLM):
            @property
            def model(self) -> str:
                return "jarvis-conversation"

            @property
            def provider(self) -> str:
                return "jarvis"

            def chat(self, **_kw):
                raise RuntimeError("JarvisBridgeLLM.chat called: JARVIS replies come from JarvisVoice.llm_node "
                                   "(the shared JARVIS conversation API), never from a direct model call")

        _BRIDGE_CLASS = JarvisBridgeLLM
    return _BRIDGE_CLASS


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
                # Called once per committed user turn (never on interim transcripts: preemptive generation is off).
                # The turn goes to the shared JARVIS conversation: the same Claude agent, memory, tools and approvals.
                return await self.turns.respond(chat_ctx.items)

            async def tts_node(self, text, model_settings):
                async for frame in Agent.default.tts_node(self, speakable_stream(text), model_settings):
                    yield frame

            async def on_exit(self) -> None:
                await self.turns.aclose()

        _AGENT_CLASS = JarvisVoice
    return _AGENT_CLASS


def wire_session(session, trace: PipelineTrace, turns: TurnHandler, publish) -> None:
    """Logs every pipeline stage under the turn's correlation id and surfaces STT/TTS/Claude errors to the browser."""

    st = {"agent": "initializing", "barge_in_at": None}

    def on_user_state(ev) -> None:
        if ev.new_state == "speaking":
            if st["agent"] == "speaking":
                st["barge_in_at"] = trace.clock()
                trace.mark("barge_in")
            trace.mark("speech_start", trace.begin_turn())
        elif ev.old_state == "speaking":
            trace.mark("speech_end")

    def on_transcribed(ev) -> None:
        turn = trace.begin_turn()
        if ev.is_final:
            trace.mark("final_transcript", turn, chars=len(ev.transcript), text=ev.transcript if LOG_TRANSCRIPTS else None)
        else:
            trace.interims += 1
            if trace.interims == 1:
                trace.mark("interim_transcript", turn, chars=len(ev.transcript))

    def on_agent_state(ev) -> None:
        st["agent"] = ev.new_state
        trace.mark(f"agent_{ev.new_state}")
        if ev.new_state == "speaking":
            trace.mark("tts_playout_start")
        elif ev.old_state == "speaking" and st["barge_in_at"] is not None:
            trace.mark("speech_stopped_after_barge_in", interrupt_to_stop_ms=int((trace.clock() - st["barge_in_at"]) * 1000))
            st["barge_in_at"] = None

    def on_metrics(ev) -> None:
        m = getattr(ev, "metrics", None)
        kind = type(m).__name__
        if kind == "EOUMetrics":
            trace.mark("eou_metrics", end_of_utterance_ms=int(m.end_of_utterance_delay * 1000),
                       transcription_ms=int(m.transcription_delay * 1000))
        elif kind == "TTSMetrics":
            trace.mark("tts_metrics", ttfb_ms=int(m.ttfb * 1000), audio_s=round(m.audio_duration, 2))

    def on_item(ev) -> None:
        item = ev.item
        if getattr(item, "role", None) == "assistant":
            trace.mark("reply_played", interrupted=getattr(item, "interrupted", False))
            trace.log_latency()

    def on_error(ev) -> None:
        stage, text = describe_error(ev.error)
        trace.mark(f"{stage}_error", error=text)
        asyncio.ensure_future(publish("error", text, turn=trace.turn, stage=stage))

    def on_no_transcript(_ev) -> None:
        trace.mark("stt_timeout")
        asyncio.ensure_future(publish("error", "I heard you speak but speech recognition returned no words. Please say it "
                                      "again.", turn=trace.turn, stage="stt"))

    def on_close(ev) -> None:
        if ev.error is not None:
            stage, text = describe_error(ev.error)
            trace.mark("session_closed_error", stage=stage, error=text)
        else:
            trace.mark("session_closed", reason=getattr(ev, "reason", None))

    session.on("user_state_changed", on_user_state)
    session.on("user_input_transcribed", on_transcribed)
    session.on("agent_state_changed", on_agent_state)
    session.on("conversation_item_added", on_item)
    session.on("error", on_error)
    session.on("user_transcription_timeout", on_no_transcript)
    session.on("close", on_close)
    session.on("metrics_collected", on_metrics)


async def entrypoint(ctx) -> None:
    """One JARVIS voice session in a dispatched room (runs inside a job process)."""
    from livekit import rtc
    from livekit.agents import AgentSession, RoomInputOptions
    await ctx.connect()
    trace = PipelineTrace(ctx.room.name)
    trace.mark("agent_joined")
    # Voice settings are read per session and the Cartesia WebSocket is opened (prewarmed) while the owner's browser is
    # still joining, so the first reply does not pay the connection setup.
    cfg = await asyncio.to_thread(cartesia_settings)
    tts, voice_error = None, None
    try:
        tts = build_tts(cfg)
        tts.prewarm()
        trace.mark("tts_prewarmed", provider="cartesia", model=cfg.get("model"), voice=cfg.get("voice_id"),
                   speed=cfg.get("speed"), source=cfg.get("source"))
    except (VoiceNotConfigured, ValueError) as exc:
        voice_error = str(exc)
        trace.mark("tts_not_configured", error=voice_error)
        log.error("voice.tts_unavailable %s", voice_error)
    participant = await ctx.wait_for_participant()
    ident = parse_identity(participant.identity, participant.metadata)
    if ident is None:
        log.warning("voice.rejected_participant %s", participant.identity)
        return

    async def publish(kind: str, text: str, turn: str | None = None, stage: str | None = None) -> None:
        msg = {"type": kind, "text": text, "final": True, "turn": turn}
        if stage:
            msg["stage"] = stage
        try:
            await ctx.room.local_participant.publish_data(json.dumps(msg).encode(), reliable=True, topic="jarvis")
        except Exception as exc:  # noqa: BLE001 - never let a UI message break the audio loop
            log.warning("voice.publish_failed %s", exc)

    def on_track(track, _pub, p) -> None:
        if p.identity == participant.identity and track.kind == rtc.TrackKind.KIND_AUDIO:
            trace.mark("mic_audio_subscribed")
    ctx.room.on("track_subscribed", on_track)
    ctx.room.on("local_track_published", lambda pub, _t: trace.mark("agent_audio_track_published", kind=str(pub.kind)))

    if tts is None:
        # No substitute voice: tell the owner exactly what is missing, then end the session.
        await publish("error", voice_error or "JARVIS's voice is not configured.", stage="tts")
        await asyncio.sleep(1.0)                      # let the message reach the browser before the room closes
        ctx.shutdown(reason="voice NOT_CONFIGURED")
        return

    vad = ctx.proc.userdata.get("vad")
    if vad is None:                                   # prewarm did not run (e.g. a custom launcher): load now
        from livekit.plugins import silero
        vad = silero.VAD.load()
    detector = make_turn_detector()
    session = AgentSession(stt=STT, tts=tts, vad=vad, llm=jarvis_bridge_llm_class()(),
                           turn_handling=turn_handling(detector))
    trace.mark("session_configured", stt=STT, tts=f"cartesia/{cfg.get('model')}", voice=cfg.get("voice_id"),
               speed=cfg.get("speed"), turn_detection=TURN_DETECTION, endpointing=f"dynamic {ENDPOINT_MIN_S}-{ENDPOINT_MAX_S}s",
               interruption="adaptive" if ADAPTIVE_INTERRUPTION else "vad", preemptive=False)

    async def speak(text: str) -> None:
        session.say(text, allow_interruptions=True)

    turns = TurnHandler(*ident, publish, speak=speak, trace=trace)
    wire_session(session, trace, turns, publish)

    def on_data(packet) -> None:
        sender = getattr(getattr(packet, "participant", None), "identity", None)
        kind = parse_control(packet.data, getattr(packet, "topic", None), sender, participant.identity)
        if kind == "interrupt":
            trace.mark("interrupt_requested")
            session.interrupt()
        elif kind == "end":
            ctx.shutdown(reason="ended by user")
        elif kind and kind.startswith("client:"):
            trace.mark("browser_" + kind.removeprefix("client:"))
            if kind == "client:reply_audible":
                trace.log_latency()
    ctx.room.on("data_received", on_data)

    await session.start(room=ctx.room, agent=jarvis_agent_class()(turns),
                        room_input_options=RoomInputOptions(participant_identity=participant.identity, close_on_disconnect=True))
    turns.agent_said(GREETING)
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
    voice_cfg = cartesia_settings()
    WORKER_STATE["tts"] = f"cartesia/{voice_cfg.get('model')} voice={voice_cfg.get('voice_id')}" \
        if voice_cfg.get("state") == "CONFIGURED" else f"cartesia {voice_cfg.get('state')}"
    log.info("voice.tts %s (source=%s)", WORKER_STATE["tts"], voice_cfg.get("source", "-"))
    start_heartbeat()
    from livekit.agents import cli
    cli.run_app(build_server())
