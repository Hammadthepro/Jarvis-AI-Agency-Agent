import { motion } from "framer-motion";

export type OrbMode = "idle" | "connecting" | "listening" | "thinking" | "speaking" | "executing" | "error" | "unavailable";

const LABEL: Record<OrbMode, string> = {
  idle: "Tap to talk", connecting: "Connecting…", listening: "Listening", thinking: "Thinking", speaking: "Speaking",
  executing: "Working on it", error: "Something needs attention", unavailable: "Voice not ready",
};

const TONE: Record<OrbMode, { ring: string; glow: string; core: string }> = {
  idle: { ring: "#2a6f80", glow: "rgba(19,183,218,0.18)", core: "#0e2a33" },
  connecting: { ring: "#3fd6f5", glow: "rgba(63,214,245,0.25)", core: "#0f3540" },
  listening: { ring: "#3fd6f5", glow: "rgba(63,214,245,0.45)", core: "#11414e" },
  thinking: { ring: "#8beaff", glow: "rgba(139,234,255,0.35)", core: "#123a45" },
  speaking: { ring: "#5ef0ff", glow: "rgba(94,240,255,0.55)", core: "#14505f" },
  executing: { ring: "#13b7da", glow: "rgba(19,183,218,0.35)", core: "#0f3540" },
  error: { ring: "#f0616d", glow: "rgba(240,97,109,0.35)", core: "#3a161b" },
  unavailable: { ring: "#4a5468", glow: "rgba(74,84,104,0.2)", core: "#151924" },
};

/** The central JARVIS visualizer. Its state comes from the real session (LiveKit agent state, task status, errors);
 *  `level` is the live audio level of whoever is speaking. */
export function JarvisOrb({ mode, level, onClick, label, size = 260 }: { mode: OrbMode; level: number; onClick: () => void; label: string; size?: number }) {
  const t = TONE[mode];
  const pulse = mode === "speaking" || mode === "listening" ? 1 + Math.min(0.14, level * 0.22) : 1;
  const spin = mode === "thinking" || mode === "connecting" ? "jarvis-spin-fast" : "jarvis-spin";
  return (
    <div className="relative grid place-items-center" style={{ width: size, height: size }}>
      <motion.div aria-hidden="true" className="absolute inset-0 rounded-full blur-2xl" style={{ background: t.glow }}
        animate={{ scale: mode === "idle" || mode === "unavailable" ? [1, 1.04, 1] : pulse * 1.05, opacity: mode === "unavailable" ? 0.5 : 1 }}
        transition={mode === "idle" || mode === "unavailable" ? { repeat: Infinity, duration: 4 } : { type: "spring", stiffness: 120, damping: 14 }} />
      <svg viewBox="0 0 200 200" className="absolute inset-0 h-full w-full" aria-hidden="true">
        <g className={spin}>
          <circle cx="100" cy="100" r="92" fill="none" stroke={t.ring} strokeOpacity="0.55" strokeWidth="1" strokeDasharray="2 6" />
        </g>
        <g className="jarvis-spin-rev">
          <circle cx="100" cy="100" r="80" fill="none" stroke={t.ring} strokeOpacity="0.8" strokeWidth="1.5" strokeDasharray="60 22 12 22" />
        </g>
        {mode === "executing" && (
          <g className="jarvis-spin-fast">
            <circle cx="100" cy="100" r="70" fill="none" stroke={t.ring} strokeWidth="2" strokeDasharray="30 410" strokeLinecap="round" />
          </g>
        )}
        <circle cx="100" cy="100" r="66" fill="none" stroke={t.ring} strokeOpacity="0.25" strokeWidth="6" />
      </svg>
      <motion.button type="button" onClick={onClick} aria-label={label}
        animate={{ scale: pulse }} transition={{ type: "spring", stiffness: 260, damping: 18 }}
        className="relative grid place-items-center rounded-full focus:outline-none focus-visible:ring-4 focus-visible:ring-arc-400/50"
        style={{ width: size * 0.56, height: size * 0.56, background: `radial-gradient(circle at 50% 38%, ${t.ring}55, ${t.core} 62%)`,
                 boxShadow: `inset 0 0 40px ${t.ring}40, 0 0 0 1px ${t.ring}60` }}>
        <span className="pointer-events-none flex flex-col items-center gap-1">
          <span className="text-[11px] font-semibold tracking-[0.35em] text-arc-300">JARVIS</span>
          <span className="text-[11px] text-ink-300">{LABEL[mode]}</span>
        </span>
      </motion.button>
    </div>
  );
}
