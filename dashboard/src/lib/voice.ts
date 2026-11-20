import { useCallback, useEffect, useRef, useState } from "react";
import type { Participant, RemoteTrack, Room, TextStreamReader } from "livekit-client";
import { api, ApiError } from "./api";

export type VoicePhase =
  | "idle" | "requesting_mic" | "mic_denied" | "connecting" | "waiting_agent" | "listening" | "thinking" | "speaking"
  | "reconnecting" | "ended" | "not_ready" | "error";

export type VoiceStatus = {
  state: "OPERATIONAL" | "NOT_READY" | "NOT_CONFIGURED" | "DISABLED";
  checks: Record<string, boolean>; missing: string[]; agent: { agent_id: string; last_seen_at: string } | null; token_ttl_s: number;
};

/** How long the browser waits for the JARVIS agent to join AND start its voice session. The first job on a small
 *  machine can take well over 15 s (process start, model load); after this the room is ended and the user can retry. */
export const AGENT_JOIN_TIMEOUT_MS = 45_000;

/** Agent readiness from the participant's published state: joined but still initializing is not ready. */
export function agentReady(state: string | undefined): boolean {
  return state === "listening" || state === "thinking" || state === "speaking";
}

export type TranscriptLine = { id: string; who: "you" | "jarvis" | "system"; text: string; final: boolean; at: number; stage?: string };

/** Live caption of the turn the owner is speaking right now.
 *
 *  LiveKit publishes the owner's speech as `lk.transcription` text streams. For each recognised segment it opens a NEW
 *  stream on every interim update (each carrying the whole segment text so far), all with the same `lk.segment_id`;
 *  the last one has `lk.transcription_final: "true"`. One spoken turn can contain several segments. So: key by segment
 *  id (never by stream id), replace the text on each update, and show all segments of the open turn as ONE caption.
 *  When the agent commits the turn (its "transcript" message), the caption is cleared and those segments are closed:
 *  a late update for a committed segment is ignored and never creates a second message. */
export type Caption = { order: string[]; text: Record<string, string>; final: Record<string, boolean>; closed: Record<string, true> };
export const emptyCaption = (): Caption => ({ order: [], text: {}, final: {}, closed: {} });

export function applySegment(c: Caption, segmentId: string, text: string, final: boolean): Caption {
  if (c.closed[segmentId]) return c;
  const order = c.order.includes(segmentId) ? c.order : [...c.order, segmentId];
  return { order, text: { ...c.text, [segmentId]: text }, final: { ...c.final, [segmentId]: final }, closed: c.closed };
}

export function captionText(c: Caption): string {
  return c.order.map((id) => c.text[id]?.trim()).filter(Boolean).join(" ");
}

/** The agent committed the owner's turn: clear the caption and close its segments. */
export function commitCaption(c: Caption): Caption {
  const closed = { ...c.closed };
  for (const id of c.order) closed[id] = true;
  return { order: [], text: {}, final: {}, closed };
}

/** Maps the LiveKit agent's published state (participant attribute `lk.agent.state`) to the UI phase. */
export function phaseFromAgentState(s: string | undefined): VoicePhase | null {
  switch (s) {
    case "listening": return "listening";
    case "thinking": return "thinking";
    case "speaking": return "speaking";
    case "initializing": return "waiting_agent";
    default: return null;
  }
}

/** Parses a JARVIS data message (topic "jarvis") from the voice agent. */
export function parseJarvisMessage(payload: Uint8Array): { type: string; text: string; turn?: string; stage?: string } | null {
  try {
    const m = JSON.parse(new TextDecoder().decode(payload));
    if (typeof m?.type !== "string" || typeof m?.text !== "string") return null;
    return { type: m.type, text: m.text, ...(typeof m.turn === "string" ? { turn: m.turn } : {}), ...(typeof m.stage === "string" ? { stage: m.stage } : {}) };
  } catch { return null; }
}

const STAGE_NAME: Record<string, string> = { stt: "Speech recognition", tts: "Speech synthesis", claude: "Claude", jarvis_api: "JARVIS service", voice: "Voice" };
export const stageName = (s?: string) => STAGE_NAME[s ?? ""] ?? "Voice";

/** Requests microphone permission explicitly so a denial is reported clearly instead of as a generic failure. */
export async function requestMicrophone(): Promise<"granted" | "denied" | "unavailable"> {
  if (!navigator.mediaDevices?.getUserMedia) return "unavailable";
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
    return "granted";
  } catch (e) {
    const name = (e as DOMException)?.name;
    return name === "NotAllowedError" || name === "SecurityError" ? "denied" : "unavailable";
  }
}

export function useVoiceSession() {
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [detail, setDetail] = useState<string>("");
  const [muted, setMuted] = useState(false);
  const [lines, setLines] = useState<TranscriptLine[]>([]);
  const [caption, setCaption] = useState<Caption>(emptyCaption);
  const [audioBlocked, setAudioBlocked] = useState(false);
  const [reply, setReply] = useState("");             // JARVIS's words as they are spoken (synchronised with the audio)
  const [levels, setLevels] = useState<{ mic: number; agent: number }>({ mic: 0, agent: 0 });
  const roomRef = useRef<Room | null>(null);
  const roomName = useRef<string | null>(null);
  const audioEl = useRef<HTMLDivElement | null>(null);
  const analysers = useRef<{ mic?: AnalyserNode; agent?: AnalyserNode; ctx?: AudioContext }>({});
  const raf = useRef<number | null>(null);

  const upsert = useCallback((l: TranscriptLine) => {
    setLines((prev) => {
      const i = prev.findIndex((p) => p.id === l.id);
      if (i === -1) return [...prev, l].slice(-200);
      const next = [...prev]; next[i] = l; return next;
    });
  }, []);

  const teardownAudio = useCallback(() => {
    if (raf.current) cancelAnimationFrame(raf.current);
    raf.current = null;
    void analysers.current.ctx?.close().catch(() => undefined);
    analysers.current = {};
    setLevels({ mic: 0, agent: 0 });
  }, []);

  const meter = useCallback(() => {
    const read = (a?: AnalyserNode) => {
      if (!a) return 0;
      const buf = new Uint8Array(a.fftSize);
      a.getByteTimeDomainData(buf);
      let sum = 0;
      for (const v of buf) sum += ((v - 128) / 128) ** 2;
      return Math.min(1, Math.sqrt(sum / buf.length) * 3);
    };
    setLevels({ mic: read(analysers.current.mic), agent: read(analysers.current.agent) });
    raf.current = requestAnimationFrame(meter);
  }, []);

  const attachAnalyser = useCallback((kind: "mic" | "agent", track: MediaStreamTrack) => {
    try {
      const ctx = analysers.current.ctx ?? new AudioContext();
      analysers.current.ctx = ctx;
      const src = ctx.createMediaStreamSource(new MediaStream([track]));
      const an = ctx.createAnalyser(); an.fftSize = 512;
      src.connect(an);
      analysers.current[kind] = an;
      if (!raf.current) raf.current = requestAnimationFrame(meter);
    } catch { /* visualization is optional; audio still works */ }
  }, [meter]);

  const end = useCallback(async (reason: "user" | "error" = "user") => {
    const room = roomRef.current;
    roomRef.current = null;
    if (room) {
      try { await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({ type: "end" })), { reliable: true, topic: "jarvis-control" }); } catch { /* best effort */ }
      await room.disconnect();
    }
    if (roomName.current) { void api("/voice/end", { method: "POST", json: { room: roomName.current } }).catch(() => undefined); roomName.current = null; }
    teardownAudio();
    if (reason === "user") setPhase("ended");
  }, [teardownAudio]);

  useEffect(() => () => { void end("user"); }, [end]);

  const start = useCallback(async () => {
    setDetail(""); setLines([]); setMuted(false); setCaption(emptyCaption()); setAudioBlocked(false); setReply("");
    const status = await api<VoiceStatus>("/voice/status").catch((e) => { setPhase("error"); setDetail(e instanceof Error ? e.message : String(e)); return null; });
    if (!status) return;
    if (status.state !== "OPERATIONAL") { setPhase("not_ready"); setDetail(status.missing.join(" ")); return; }
    setPhase("requesting_mic");
    const mic = await requestMicrophone();
    if (mic !== "granted") {
      setPhase("mic_denied");
      setDetail(mic === "denied" ? "Microphone access was blocked. Allow it in your browser's site settings, then try again."
        : "No microphone is available in this browser or device.");
      return;
    }
    setPhase("connecting");
    try {
      const t = await api<{ url: string; token: string; room: string }>("/voice/token", { method: "POST" });
      roomName.current = t.room;
      const { Room, RoomEvent, Track, ConnectionState } = await import("livekit-client");
      const room = new Room({ adaptiveStream: true, dynacast: true, disconnectOnPageLeave: true });
      roomRef.current = room;
      const isAgent = (p?: Participant) => Boolean(p && p.identity !== room.localParticipant.identity);
      const client = (event: "mic_published" | "audio_playing" | "audio_blocked") => {
        void room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({ type: "client", event })), { reliable: true, topic: "jarvis-control" }).catch(() => undefined);
      };
      const agentState = () => [...room.remoteParticipants.values()].find((p) => isAgent(p))?.attributes["lk.agent.state"];

      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind === Track.Kind.Audio) {
          const el = track.attach(); el.setAttribute("data-jarvis-audio", "1"); audioEl.current?.appendChild(el);
          el.addEventListener("playing", () => client("audio_playing"), { once: true });
          attachAnalyser("agent", track.mediaStreamTrack);
        }
      });
      room.on(RoomEvent.ParticipantAttributesChanged, (_changed: Record<string, string>, p: Participant) => {
        if (!isAgent(p)) return;
        const ph = phaseFromAgentState(p.attributes["lk.agent.state"]);
        if (ph) setPhase(ph);
        if (ph === "listening") setReply("");
      });
      // Joining is not ready: stay in "waiting_agent" until the agent publishes listening/thinking/speaking.
      room.on(RoomEvent.ParticipantConnected, (p: Participant) => {
        if (isAgent(p)) { setDetail("JARVIS joined; starting its voice…"); const ph = phaseFromAgentState(p.attributes["lk.agent.state"]); if (ph && ph !== "waiting_agent") setPhase(ph); }
      });
      room.on(RoomEvent.ParticipantDisconnected, (p: Participant) => {
        if (isAgent(p) && roomRef.current === room) { setPhase("error"); setDetail("The JARVIS voice agent left the room. Start a new session."); void end("error"); }
      });
      // Browsers may block autoplay until the page gets a gesture; the Start click is one, so unlock audio now and
      // report when playback really is blocked instead of leaving JARVIS silently muted.
      room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
        const blocked = !room.canPlaybackAudio; setAudioBlocked(blocked); if (blocked) client("audio_blocked");
      });
      room.on(RoomEvent.DataReceived, (payload: Uint8Array, _p?: Participant, _k?: unknown, topic?: string) => {
        if (topic !== "jarvis") return;
        const m = parseJarvisMessage(payload);
        if (!m) return;
        const at = Date.now();
        if (m.type === "transcript") {                 // the committed turn: exactly one message per turn
          setCaption(commitCaption);
          upsert({ id: `you-${m.turn ?? at}`, who: "you", text: m.text, final: true, at });
        } else if (m.type === "status" || m.type === "progress") {
          upsert({ id: m.type === "status" && m.turn ? `jarvis-${m.turn}` : `j-${at}-${Math.random()}`, who: "jarvis", text: m.text, final: true, at });
        } else if (m.type === "error") {
          upsert({ id: `err-${m.turn ?? at}-${m.stage ?? "voice"}`, who: "system", text: `${stageName(m.stage)}: ${m.text}`, final: true, at, stage: m.stage });
        }
      });
      room.registerTextStreamHandler("lk.transcription", async (reader: TextStreamReader, from: { identity: string }) => {
        if (from.identity !== room.localParticipant.identity) {
          // JARVIS's own speech: LiveKit streams the words in step with the audio. Shown live; the stored reply
          // ('status' message, then the conversation feed) is the record.
          let said = "";
          for await (const chunk of reader) { said += chunk; setReply(said); }
          return;
        }
        const attrs = reader.info.attributes ?? {};
        const seg = attrs["lk.segment_id"] ?? reader.info.id;
        const final = attrs["lk.transcription_final"] === "true";
        let text = "";
        for await (const chunk of reader) text += chunk;        // each stream carries the whole segment text so far
        setCaption((c) => applySegment(c, seg, text, final));
      });
      room.on(RoomEvent.Reconnecting, () => setPhase("reconnecting"));
      room.on(RoomEvent.Reconnected, () => setPhase(phaseFromAgentState(agentState()) ?? "waiting_agent"));
      room.on(RoomEvent.Disconnected, () => {
        if (roomRef.current === room) { roomRef.current = null; teardownAudio(); setPhase("ended"); setDetail("The voice session ended."); }
      });

      await room.connect(t.url, t.token);
      await room.startAudio().catch(() => undefined);
      // Echo cancellation keeps JARVIS's own voice (from speakers) out of the microphone; the agent also drops echoes.
      await room.localParticipant.setMicrophoneEnabled(true, { echoCancellation: true, noiseSuppression: true, autoGainControl: true });
      client("mic_published");
      const pub = room.localParticipant.getTrackPublication(Track.Source.Microphone);
      if (pub?.track?.mediaStreamTrack) attachAnalyser("mic", pub.track.mediaStreamTrack);
      setPhase(agentReady(agentState()) ? phaseFromAgentState(agentState())! : "waiting_agent");
      if (room.state !== ConnectionState.Connected) setPhase("reconnecting");
      setTimeout(() => {
        if (roomRef.current !== room || agentReady(agentState())) return;
        const joined = [...room.remoteParticipants.values()].some((p) => isAgent(p));
        setPhase("error");
        setDetail(joined ? "JARVIS joined but its voice session did not start within 45 seconds (speech services unavailable?). Check scripts/dev-status.sh and .jarvis/logs/voice.log, then try again."
          : "The JARVIS voice agent did not join within 45 seconds. Check that it is running (scripts/dev-status.sh), then try again.");
        void end("error");
      }, AGENT_JOIN_TIMEOUT_MS);
    } catch (e) {
      teardownAudio();
      if (e instanceof ApiError && e.status === 424) { setPhase("not_ready"); setDetail(String((e.details as { detail?: string }).detail ?? e.message)); }
      else { setPhase("error"); setDetail(e instanceof Error ? e.message : String(e)); }
      await roomRef.current?.disconnect(); roomRef.current = null;
    }
  }, [attachAnalyser, end, teardownAudio, upsert]);

  const toggleMute = useCallback(async () => {
    const room = roomRef.current; if (!room) return;
    await room.localParticipant.setMicrophoneEnabled(muted);
    setMuted(!muted);
  }, [muted]);

  const interrupt = useCallback(async () => {
    const room = roomRef.current; if (!room) return;
    await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({ type: "interrupt" })), { reliable: true, topic: "jarvis-control" });
  }, []);

  const enableAudio = useCallback(async () => {
    const room = roomRef.current; if (!room) return;
    await room.startAudio().catch(() => undefined);
    setAudioBlocked(!room.canPlaybackAudio);
  }, []);

  const active = ["connecting", "waiting_agent", "listening", "thinking", "speaking", "reconnecting"].includes(phase);
  return { phase, detail, muted, lines, caption: captionText(caption), reply, audioBlocked, enableAudio, levels, active, start,
    end: () => end("user"), toggleMute, interrupt, audioEl };
}
