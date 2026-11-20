import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Link } from "react-router";
import { z } from "zod";
import { api } from "../lib/api";
import { useVoiceSession, type VoicePhase, type VoiceStatus } from "../lib/voice";
import { JarvisOrb, type OrbMode } from "../components/JarvisOrb";
import { ActiveCampaigns, AttentionCard, IntegrationHealth, useCampaigns } from "../components/CommandCenter";
import { ErrorState } from "../components/ui";
import { IconMic, IconMicOff, IconPhoneOff, IconStop } from "../components/icons";

export type ConversationMessage = { id: number; role: "user" | "assistant"; channel: "text" | "voice"; content: string;
  meta: { kind?: string; agent?: string; notice?: string; campaign_id?: string; tools?: string[];
    provider_error?: { state?: string; http_status?: number; error_type?: string; request_id?: string } }; created_at: string };
export const turnSchema = z.string().trim().min(1, "Say or type something for JARVIS").max(2000);

const VOICE_TO_ORB: Partial<Record<VoicePhase, OrbMode>> = {
  listening: "listening", thinking: "thinking", speaking: "speaking", connecting: "connecting", waiting_agent: "joining",
  requesting_mic: "connecting", reconnecting: "connecting", error: "error", mic_denied: "error", not_ready: "unavailable",
};

/** Orb state from real signals only: the voice session, a text turn in flight, and running background work. */
export function orbMode(phase: VoicePhase, voiceReady: boolean | undefined, textBusy: boolean, working: boolean): OrbMode {
  if (VOICE_TO_ORB[phase]) return VOICE_TO_ORB[phase]!;
  if (textBusy) return "thinking";
  if (working) return "executing";
  return voiceReady === false ? "unavailable" : "idle";
}

export default function CommandCenterPage() {
  const qc = useQueryClient();
  const v = useVoiceSession();
  const voice = useQuery({ queryKey: ["voice-status"], refetchInterval: 20_000,
    queryFn: async () => { const s = await api<VoiceStatus>("/voice/status"); return s && typeof s === "object" && "state" in s ? { ...s, missing: s.missing ?? [] } : null; } });
  const messages = useQuery({ queryKey: ["conversation"], queryFn: () => api<ConversationMessage[]>("/conversation/messages?limit=60"),
    refetchInterval: v.active ? 1500 : 5000 });
  const camps = useCampaigns();
  const working = (camps.data ?? []).some((c) => c.state === "running" || c.state === "authorized");
  const [text, setText] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const send = useMutation({
    mutationFn: (t: string) => api<{ say: string }>("/conversation/turn", { method: "POST", json: { text: t } }),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ["conversation"] }); void qc.invalidateQueries({ queryKey: ["campaigns"] }); },
  });
  const say = (t: string) => { const r = turnSchema.safeParse(t); if (!r.success) { setHint(r.error.issues[0].message); return; } setHint(null); setText(""); send.mutate(r.data); };

  const ready = voice.data?.state === "OPERATIONAL";
  const mode = orbMode(v.phase, voice.data ? ready : undefined, send.isPending, working);
  const list = Array.isArray(messages.data) ? messages.data : [];
  const interim = v.caption;                                 // ONE live caption for the turn being spoken
  const voiceError = v.lines.filter((l) => l.who === "system").slice(-1)[0];
  const voiceTurns = v.lines.length;
  useEffect(() => { if (voiceTurns) void qc.invalidateQueries({ queryKey: ["conversation"] }); }, [voiceTurns, qc]);
  const last = list[list.length - 1];
  const awaitingYes = last?.role === "assistant" && (last.meta?.kind === "proposal" || last.meta?.kind === "confirm_needed");
  useEffect(() => { feed.current?.scrollTo?.({ top: feed.current.scrollHeight, behavior: "smooth" }); }, [list.length, interim]);

  const orbClick = () => { if (v.active) void v.end(); else if (ready) void v.start(); else void v.start(); };
  const orbLabel = v.active ? "End voice session" : ready ? "Talk to JARVIS" : "Check voice availability";
  const status = useMemo(() => {
    if (v.detail && ["error", "mic_denied", "not_ready", "ended"].includes(v.phase)) return v.detail;
    if (voice.data && !ready && !v.active) return `Voice: ${voice.data.missing[0] ?? voice.data.state}`;
    return null;
  }, [v.detail, v.phase, voice.data, ready, v.active]);

  return (
    <div className="mx-auto grid max-w-7xl gap-6 px-4 py-4 md:px-8 md:py-6 xl:grid-cols-[1fr_340px]">
      <section className="relative flex h-[calc(100dvh-6.5rem)] min-h-[560px] flex-col overflow-hidden rounded-3xl md:h-[calc(100dvh-3rem)] border border-arc-700/40 bg-[radial-gradient(ellipse_at_top,_rgba(19,183,218,0.10),_var(--color-ink-950)_60%)]">
        <div ref={v.audioEl} className="hidden" />
        <div className="flex shrink-0 flex-col items-center px-4 pt-6">
          <JarvisOrb mode={mode} level={Math.max(v.levels.mic, v.levels.agent)} onClick={orbClick} label={orbLabel} size={list.length > 2 ? 190 : 250} />
          <AnimatePresence>
            {v.active && (
              <motion.div initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-3 flex gap-2">
                <RoundBtn onClick={() => void v.toggleMute()} label={v.muted ? "Unmute" : "Mute"}>{v.muted ? <IconMicOff className="h-4 w-4" /> : <IconMic className="h-4 w-4" />}</RoundBtn>
                <RoundBtn onClick={() => void v.interrupt()} label="Stop speaking" disabled={v.phase !== "speaking"}><IconStop className="h-4 w-4" /></RoundBtn>
                <RoundBtn onClick={() => void v.end()} label="End voice session" danger><IconPhoneOff className="h-4 w-4" /></RoundBtn>
              </motion.div>
            )}
          </AnimatePresence>
          {v.audioBlocked && (
            <button type="button" onClick={() => void v.enableAudio()} className="mt-3 rounded-full bg-warn-500/15 px-3 py-1 text-xs text-ink-100">
              Your browser blocked JARVIS's audio. Tap to enable sound.
            </button>
          )}
          {v.active && voiceError && (
            <p role="alert" className="mt-3 max-w-xl text-center text-xs text-bad-500">{voiceError.text}</p>
          )}
          {status && (
            <p role="status" className="mt-3 max-w-xl text-center text-xs text-ink-300">
              {status} {voice.data && !ready && <Link to="/integrations" className="text-arc-300 underline">Set up</Link>}
            </p>
          )}
        </div>

        <div ref={feed} className="mt-4 min-h-0 flex-1 space-y-3 overflow-y-auto px-4 pb-4 scroll-thin md:px-10" aria-live="polite" aria-label="Conversation">
          {messages.error && <ErrorState error={messages.error} />}
          {list.length === 0 && !messages.isLoading && (
            <p className="pt-6 text-center text-sm text-ink-500">Say “find businesses in Miami that need a new website”, or name a business you want me to look at.</p>
          )}
          {list.map((m) => <Bubble key={m.id} m={m} />)}
          {interim && <div data-testid="live-caption" className="ml-auto max-w-[85%] rounded-2xl bg-arc-500/10 px-4 py-2 text-sm italic text-ink-300">{interim}…</div>}
          {v.reply && v.phase === "speaking"
            ? <div data-testid="live-reply" className="mr-auto max-w-[85%] rounded-2xl border border-arc-700/30 bg-ink-900/80 px-4 py-2 text-sm text-ink-100">{v.reply}</div>
            : (send.isPending || v.phase === "thinking") && <div className="mr-auto rounded-2xl bg-ink-850 px-4 py-2 text-sm text-ink-400">JARVIS is thinking…</div>}
          {send.error && <ErrorState error={send.error} />}
        </div>

        <div className="border-t border-arc-700/30 bg-ink-950/60 p-3 md:px-10">
          {awaitingYes && !send.isPending && (
            <div className="mb-2 flex justify-center gap-2">
              <button className="rounded-full bg-arc-500/90 px-4 py-1.5 text-sm font-medium text-ink-950 hover:bg-arc-400" onClick={() => say("Yes, go ahead.")}>Yes, go ahead</button>
              <button className="rounded-full border border-ink-600 px-4 py-1.5 text-sm text-ink-200 hover:bg-ink-800" onClick={() => say("Cancel that.")}>Cancel</button>
            </div>
          )}
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); say(text); }}>
            <label htmlFor="turn" className="sr-only">Message JARVIS</label>
            <input id="turn" value={text} onChange={(e) => setText(e.target.value)} placeholder="Or type to JARVIS…" autoComplete="off"
              className="flex-1 rounded-full border border-ink-700 bg-ink-900/80 px-4 py-2 text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none focus:ring-2 focus:ring-arc-400/40" />
            <button type="submit" disabled={send.isPending} className="rounded-full bg-ink-800 px-4 text-sm text-ink-100 ring-1 ring-ink-600 hover:bg-ink-700 disabled:opacity-50">Send</button>
          </form>
          {hint && <p className="mt-1 text-center text-xs text-ink-500">{hint}</p>}
        </div>
      </section>

      <aside className="flex flex-col gap-4 xl:h-[calc(100dvh-3rem)] xl:overflow-y-auto xl:pr-1 scroll-thin">
        <ActiveCampaigns />
        <AttentionCard />
        <IntegrationHealth />
      </aside>
    </div>
  );
}

/** Turns https URLs in a reply into links (preview links JARVIS shows you); everything else stays plain text. */
export function linkify(text: string): (string | { url: string })[] {
  const out: (string | { url: string })[] = [];
  let last = 0;
  for (const mt of text.matchAll(/https:\/\/[^\s<>"')]+[^\s<>"').,;:!?]/g)) {
    if (mt.index! > last) out.push(text.slice(last, mt.index));
    out.push({ url: mt[0] });
    last = mt.index! + mt[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function Bubble({ m }: { m: ConversationMessage }) {
  const you = m.role === "user";
  return (
    <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
      className={`max-w-[85%] rounded-2xl px-4 py-2 text-sm ${you ? "ml-auto bg-arc-500/10 text-ink-100" : m.meta?.kind === "provider_error" ? "mr-auto border border-bad-500/40 bg-bad-500/10 text-ink-100" : "mr-auto border border-arc-700/30 bg-ink-900/80 text-ink-100"}`}>
      <div className="mb-0.5 flex items-center gap-2 text-[10px] uppercase tracking-wider text-ink-500">
        <span>{you ? "You" : "JARVIS"}</span><span>{m.meta?.kind === "progress" ? "update" : m.channel === "voice" ? "voice" : "text"}</span>
        {!you && (m.meta?.agent === "basic_parser" || m.meta?.agent === "deterministic") && <span title="Configured to use the basic command parser, not Claude">basic parser</span>}
        {!you && m.meta?.kind === "provider_error" && <span className="text-bad-500">Claude unavailable{m.meta.provider_error?.state ? ` · ${m.meta.provider_error.state}` : ""}</span>}
      </div>
      <div className="whitespace-pre-wrap leading-relaxed">
        {linkify(m.content).map((part, i) => typeof part === "string" ? <span key={i}>{part}</span>
          : <a key={i} href={part.url} target="_blank" rel="noopener noreferrer" className="break-all text-arc-300 underline">{part.url}</a>)}
      </div>
      {m.meta?.notice && <div className="mt-1 text-[11px] text-warn-500">{m.meta.notice}</div>}
      {m.meta?.campaign_id && <Link to={`/campaigns/${m.meta.campaign_id}`} className="mt-1 inline-block text-[11px] text-arc-300 hover:underline">Open the work</Link>}
    </motion.div>
  );
}

function RoundBtn({ children, onClick, label, disabled, danger }: { children: React.ReactNode; onClick: () => void; label: string; disabled?: boolean; danger?: boolean }) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} aria-label={label} title={label}
      className={`grid h-10 w-10 place-items-center rounded-full ring-1 transition disabled:opacity-40 ${danger ? "bg-bad-500/20 text-bad-500 ring-bad-500/40 hover:bg-bad-500/30" : "bg-ink-900 text-ink-100 ring-ink-600 hover:ring-arc-400/50"}`}>
      {children}
    </button>
  );
}
