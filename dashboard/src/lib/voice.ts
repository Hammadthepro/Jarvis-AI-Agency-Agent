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

export type TranscriptLine = { id: string; who: "you" | "jarvis" | "system"; text: string; final: boolean; at: number };

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
export function parseJarvisMessage(payload: Uint8Array): { type: string; text: string } | null {
  try {
    const m = JSON.parse(new TextDecoder().decode(payload));
    return typeof m?.type === "string" && typeof m?.text === "string" ? { type: m.type, text: m.text } : null;
  } catch { return null; }
}

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
    setDetail(""); setLines([]); setMuted(false);
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
      const agentState = () => [...room.remoteParticipants.values()].find((p) => isAgent(p))?.attributes["lk.agent.state"];

      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind === Track.Kind.Audio) {
          const el = track.attach(); el.setAttribute("data-jarvis-audio", "1"); audioEl.current?.appendChild(el);
          attachAnalyser("agent", track.mediaStreamTrack);
        }
      });
      room.on(RoomEvent.ParticipantAttributesChanged, (_changed: Record<string, string>, p: Participant) => {
        if (!isAgent(p)) return;
        const ph = phaseFromAgentState(p.attributes["lk.agent.state"]);
        if (ph) setPhase(ph);
      });
      // Joining is not ready: stay in "waiting_agent" until the agent publishes listening/thinking/speaking.
      room.on(RoomEvent.ParticipantConnected, (p: Participant) => {
        if (isAgent(p)) { setDetail("JARVIS joined; starting its voice…"); const ph = phaseFromAgentState(p.attributes["lk.agent.state"]); if (ph && ph !== "waiting_agent") setPhase(ph); }
      });
      room.on(RoomEvent.ParticipantDisconnected, (p: Participant) => {
        if (isAgent(p) && roomRef.current === room) { setPhase("error"); setDetail("The JARVIS voice agent left the room. Start a new session."); void end("error"); }
      });
      room.on(RoomEvent.DataReceived, (payload: Uint8Array, _p?: Participant, _k?: unknown, topic?: string) => {
        if (topic !== "jarvis") return;
        const m = parseJarvisMessage(payload);
        if (!m) return;
        if (m.type === "status" || m.type === "progress") upsert({ id: `j-${Date.now()}-${Math.random()}`, who: "jarvis", text: m.text, final: true, at: Date.now() });
      });
      room.registerTextStreamHandler("lk.transcription", async (reader: TextStreamReader, from: { identity: string }) => {
        const id = reader.info.id;
        const who = from.identity === room.localParticipant.identity ? "you" : "jarvis";
        if (who === "jarvis") return;   // JARVIS replies arrive as 'status' messages (the exact text spoken)
        let text = "";
        for await (const chunk of reader) { text += chunk; upsert({ id, who, text, final: false, at: Date.now() }); }
        upsert({ id, who, text, final: true, at: Date.now() });
      });
      room.on(RoomEvent.Reconnecting, () => setPhase("reconnecting"));
      room.on(RoomEvent.Reconnected, () => setPhase(phaseFromAgentState(agentState()) ?? "waiting_agent"));
      room.on(RoomEvent.Disconnected, () => {
        if (roomRef.current === room) { roomRef.current = null; teardownAudio(); setPhase("ended"); setDetail("The voice session ended."); }
      });

      await room.connect(t.url, t.token);
      await room.localParticipant.setMicrophoneEnabled(true);
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

  const active = ["connecting", "waiting_agent", "listening", "thinking", "speaking", "reconnecting"].includes(phase);
  return { phase, detail, muted, lines, levels, active, start, end: () => end("user"), toggleMute, interrupt, audioEl };
}
