import { useEffect, useRef, useState } from "react";
import type { Room, RemoteTrack } from "livekit-client";
import { api, ApiError } from "../lib/api";

type VState = "idle" | "connecting" | "listening" | "not_configured" | "error";

/** LiveKit voice session. The server-side JARVIS voice agent (apps/voice) transcribes speech, sends it
 *  to the same /commands API as text, and speaks progress back. Missing credentials => NOT_CONFIGURED. */
export default function VoicePanel({ onTranscript }: { onTranscript: (t: string) => void }) {
  const [state, setState] = useState<VState>("idle");
  const [detail, setDetail] = useState<string>("");
  const roomRef = useRef<Room | null>(null);
  const audioRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => { void roomRef.current?.disconnect(); }, []);

  async function start() {
    setState("connecting");
    try {
      const t = await api<{ url: string; token: string; room: string }>("/voice/token", { method: "POST" });
      const { Room, RoomEvent, Track } = await import("livekit-client");
      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
        if (track.kind === Track.Kind.Audio) audioRef.current?.appendChild(track.attach());
      });
      room.on(RoomEvent.DataReceived, (payload, _p, _k, topic) => {
        if (topic !== "jarvis") return;
        try {
          const msg = JSON.parse(new TextDecoder().decode(payload));
          if (msg.type === "transcript" && msg.final) onTranscript(msg.text);
          if (msg.type === "status") setDetail(msg.text);
        } catch { /* ignore malformed */ }
      });
      room.on(RoomEvent.Disconnected, () => setState("idle"));
      await room.connect(t.url, t.token);
      await room.localParticipant.setMicrophoneEnabled(true);
      setState("listening");
    } catch (e) {
      if (e instanceof ApiError && e.status === 424) { setState("not_configured"); setDetail(String((e.details as { detail?: string }).detail ?? e.message)); }
      else { setState("error"); setDetail(e instanceof Error ? e.message : String(e)); }
    }
  }
  async function stop() {
    await roomRef.current?.disconnect();
    roomRef.current = null;
    setState("idle");
  }

  return (
    <div className="flex items-center gap-2">
      <div ref={audioRef} className="hidden" />
      {state === "not_configured" && <span className="text-xs text-warn-500" title={detail}>Voice: NOT_CONFIGURED</span>}
      {state === "error" && <span className="text-xs text-bad-500" title={detail}>Voice error</span>}
      {state === "listening" && <span className="text-xs text-ok-500">{detail || "Listening…"}</span>}
      <button type="button" onClick={() => (state === "listening" ? void stop() : void start())} aria-pressed={state === "listening"}
        aria-label={state === "listening" ? "Stop voice session" : "Start voice session"}
        className={`relative grid h-10 w-10 place-items-center rounded-full ring-1 transition ${state === "listening" ? "bg-signal-500 ring-signal-300" : "bg-ink-800 ring-ink-600 hover:bg-ink-700"}`}>
        {state === "listening" && <span className="absolute inset-0 animate-ping rounded-full bg-signal-500/40" />}
        <svg viewBox="0 0 24 24" className="relative h-4 w-4 fill-current text-ink-100" aria-hidden="true"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-2.08A7 7 0 0 0 19 12h-2Z" /></svg>
      </button>
    </div>
  );
}
