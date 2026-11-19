import { useEffect, useRef } from "react";
import { motion } from "framer-motion";
import type { VoicePhase } from "../lib/voice";

export const PHASE_LABEL: Record<VoicePhase, string> = {
  idle: "Ready when you are", requesting_mic: "Waiting for microphone permission…", mic_denied: "Microphone blocked",
  connecting: "Connecting securely…", waiting_agent: "Waiting for JARVIS to join…", listening: "Listening",
  thinking: "Working on it…", speaking: "Speaking", reconnecting: "Connection lost. Reconnecting…", ended: "Session ended",
  not_ready: "Voice is not available yet", error: "Voice error",
};

const RING: Record<string, string> = {
  listening: "from-signal-400/60 to-signal-500/10", thinking: "from-warn-500/50 to-warn-500/5", speaking: "from-ok-500/60 to-ok-500/10",
  reconnecting: "from-warn-500/40 to-warn-500/5", error: "from-bad-500/50 to-bad-500/5", mic_denied: "from-bad-500/50 to-bad-500/5",
};

/** The central voice control. Its look follows the agent's real state; level drives the pulse. */
export function VoiceOrb({ phase, level, active, onClick, size = 176 }: { phase: VoicePhase; level: number; active: boolean; onClick: () => void; size?: number }) {
  const ring = RING[phase] ?? "from-ink-600/60 to-ink-800/10";
  const scale = 1 + Math.min(0.18, level * 0.25);
  return (
    <div className="relative grid place-items-center" style={{ width: size, height: size }}>
      <motion.div aria-hidden="true" className={`absolute inset-0 rounded-full bg-gradient-to-b ${ring} blur-xl`}
        animate={{ scale: active ? [1, 1.06, 1] : 1, opacity: active ? 0.9 : 0.4 }} transition={{ repeat: active ? Infinity : 0, duration: phase === "thinking" ? 1.1 : 2.4 }} />
      {phase === "thinking" && (
        <motion.div aria-hidden="true" className="absolute inset-3 rounded-full border-2 border-transparent border-t-warn-500/80"
          animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: 1.2, ease: "linear" }} />
      )}
      <motion.button type="button" onClick={onClick} aria-pressed={active}
        aria-label={active ? "End voice session" : "Talk to JARVIS"}
        animate={{ scale }} transition={{ type: "spring", stiffness: 300, damping: 20 }}
        className={`relative grid h-[72%] w-[72%] place-items-center rounded-full ring-1 transition focus:outline-none focus-visible:ring-4 focus-visible:ring-signal-500/50 ${
          active ? "bg-gradient-to-b from-ink-700 to-ink-900 ring-signal-400/50" : "bg-gradient-to-b from-ink-800 to-ink-950 ring-ink-600 hover:ring-signal-400/40"}`}>
        <svg viewBox="0 0 24 24" className={`h-10 w-10 ${active ? "text-signal-300" : "text-ink-200"}`} fill="none" stroke="currentColor" strokeWidth={1.6} aria-hidden="true">
          <path strokeLinecap="round" d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm6-3a6 6 0 0 1-12 0M12 18v3" />
        </svg>
      </motion.button>
    </div>
  );
}

/** Scrolling level trace for the operator's microphone and JARVIS's voice. */
export function Waveform({ mic, agent, active }: { mic: number; agent: number; active: boolean }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const hist = useRef<Array<[number, number]>>(Array.from({ length: 96 }, () => [0, 0] as [number, number]));
  useEffect(() => {
    hist.current.push([active ? mic : 0, active ? agent : 0]);
    if (hist.current.length > 96) hist.current.shift();
    const c = canvas.current;
    let g: CanvasRenderingContext2D | null = null;
    try { g = c?.getContext("2d") ?? null; } catch { g = null; }   // no canvas support: levels are simply not drawn
    if (!c || !g) return;
    const w = c.width, h = c.height, bw = w / 96;
    g.clearRect(0, 0, w, h);
    hist.current.forEach(([m, a], i) => {
      const x = i * bw;
      const mh = Math.max(2, m * h * 0.9), ah = Math.max(2, a * h * 0.9);
      g.fillStyle = "rgba(138,162,255,0.85)"; g.fillRect(x, (h - mh) / 2, bw * 0.45, mh);
      g.fillStyle = "rgba(53,194,139,0.85)"; g.fillRect(x + bw * 0.5, (h - ah) / 2, bw * 0.45, ah);
    });
  }, [mic, agent, active]);
  return (
    <div className="w-full">
      <canvas ref={canvas} width={768} height={72} className="h-[72px] w-full" role="img" aria-label="Live audio levels" />
      <div className="mt-1 flex justify-center gap-4 text-[11px] text-ink-500">
        <span className="flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-signal-400" />You</span>
        <span className="flex items-center gap-1"><i className="h-2 w-2 rounded-full bg-ok-500" />JARVIS</span>
      </div>
    </div>
  );
}
