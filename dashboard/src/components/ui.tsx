import { motion } from "framer-motion";
import type { ReactNode } from "react";
import { ApiError } from "../lib/api";

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border hairline bg-ink-900/80 ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b hairline px-4 py-3">
          <h2 className="text-sm font-medium text-ink-200">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className="rounded-xl border hairline bg-ink-900/80 p-4">
      <div className="text-xs uppercase tracking-wider text-ink-400">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-ink-100">{value}</div>
      {hint && <div className="mt-1 text-xs text-ink-400">{hint}</div>}
    </motion.div>
  );
}

const TONES = {
  ok: "bg-ok-500/15 text-ok-500 ring-ok-500/30", warn: "bg-warn-500/15 text-warn-500 ring-warn-500/30",
  bad: "bg-bad-500/15 text-bad-500 ring-bad-500/30", info: "bg-signal-500/15 text-signal-300 ring-signal-500/30",
  muted: "bg-ink-700/50 text-ink-300 ring-ink-600/50",
} as const;
type Tone = keyof typeof TONES;
const STATE_TONE: Record<string, Tone> = {
  CONNECTED: "ok", completed: "ok", passed: "ok", deployed: "ok", verified: "ok", qualified: "ok", succeeded: "ok", sent: "ok",
  VERIFIED_NO_WEBSITE: "info", VERIFIED_HAS_WEBSITE: "muted", running: "info", authorized: "info", draft: "muted",
  NOT_CONFIGURED: "warn", BLOCKED_PENDING_APPROVAL: "warn", blocked: "warn", requires_human_review: "warn", WEBSITE_UNCERTAIN: "warn",
  CONFLICTING_EVIDENCE: "warn", paused: "warn", retry_wait: "warn", unknown_provider_result: "warn", BACKUP_NOT_PROTECTED: "warn",
  failed: "bad", dead: "bad", VERIFICATION_FAILED: "bad", INVALID_CREDENTIALS: "bad", PERMISSION_DENIED: "bad", QUOTA_EXCEEDED: "bad",
  PROVIDER_ERROR: "bad", RATE_LIMITED: "warn", TEMPORARILY_UNAVAILABLE: "warn", DISABLED: "muted", rejected: "muted", cancelled: "muted",
};
export function Pill({ children, tone }: { children: ReactNode; tone?: Tone }) {
  const t = tone ?? STATE_TONE[String(children)] ?? "muted";
  return <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${TONES[t]}`}>{children}</span>;
}

type BtnProps = React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "ghost" | "danger" };
export function Button({ children, variant = "primary", className = "", ...rest }: BtnProps) {
  const v = {
    primary: "bg-signal-500 text-white hover:bg-signal-400 disabled:bg-ink-700 disabled:text-ink-400",
    ghost: "border hairline text-ink-200 hover:bg-ink-800 disabled:text-ink-500",
    danger: "bg-bad-500/90 text-white hover:bg-bad-500",
  }[variant];
  return <button {...rest} className={`inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed ${v} ${className}`}>{children}</button>;
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`w-full rounded-lg border hairline bg-ink-850 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none focus:ring-2 focus:ring-signal-500/50 ${props.className ?? ""}`} />;
}

export function ErrorState({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? error : null;
  return (
    <div role="alert" className="rounded-lg border border-bad-500/30 bg-bad-500/10 p-3 text-sm text-bad-500">
      <div className="font-mono text-xs">{e?.code ?? "ERROR"}</div>
      <div className="mt-1 text-ink-200">{e?.message ?? String(error)}</div>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed hairline p-8 text-center">
      <div className="text-sm font-medium text-ink-200">{title}</div>
      {children && <div className="mt-1 text-sm text-ink-400">{children}</div>}
    </div>
  );
}

export function Loading() {
  return <div className="h-24 animate-pulse rounded-lg bg-ink-850" aria-busy="true" aria-label="Loading" />;
}

export const usd = (micros: number | null | undefined) => `$${((micros ?? 0) / 1e6).toFixed(2)}`;
export const when = (s?: string | null) => (s ? new Date(s).toLocaleString() : "—");
