import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import { api } from "../lib/api";
import { useVoiceSession, type VoiceStatus } from "../lib/voice";
import { PHASE_LABEL, VoiceOrb, Waveform } from "../components/VoiceStage";
import { Button, Card, ErrorState, Loading, Pill, when } from "../components/ui";
import { IconCheck, IconMic, IconMicOff, IconPhoneOff, IconStop } from "../components/icons";

const CHECK_LABEL: Record<string, string> = {
  livekit_configured: "LiveKit credentials configured", livekit_validated: "LiveKit connection validated",
  agent_secret_configured: "Voice agent secret set on the server", agent_online: "JARVIS voice agent process running",
  agent_registered: "Voice agent registered with LiveKit", agent_job_process_ready: "Voice agent can start sessions (job process ready)",
};
const EXAMPLES = [
  "Find 10 independent roofing companies in Houston with outdated websites and prepare two redesign previews.",
  "What's the status?",
  "Confirm.",
];

export default function VoicePage() {
  const status = useQuery({ queryKey: ["voice-status"], queryFn: () => api<VoiceStatus>("/voice/status"), refetchInterval: 15_000 });
  const v = useVoiceSession();
  const [params] = useSearchParams();
  const started = useRef(false);
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!started.current && params.get("start") === "1" && status.data?.state === "OPERATIONAL") { started.current = true; void v.start(); }
  }, [params, status.data, v]);
  useEffect(() => { scroller.current?.scrollTo?.({ top: scroller.current.scrollHeight, behavior: "smooth" }); }, [v.lines]);

  const ready = status.data?.state === "OPERATIONAL";
  return (
    <div className="mx-auto grid max-w-7xl gap-6 px-4 py-6 md:px-8 lg:grid-cols-[1fr_360px]">
      <section className="flex flex-col items-center rounded-3xl border hairline bg-[radial-gradient(ellipse_at_top,_var(--color-ink-800),_var(--color-ink-950)_70%)] px-4 py-8 md:py-12">
        <div ref={v.audioEl} className="hidden" />
        <h1 className="text-lg font-semibold text-ink-100">Talk to JARVIS</h1>
        <p className="mt-1 max-w-md text-center text-sm text-ink-400">
          Ask for prospects, previews or a status update. JARVIS uses the same commands, approvals and budget limits as the text console.
        </p>
        <div className="my-8">
          <VoiceOrb phase={v.phase} level={Math.max(v.levels.mic, v.levels.agent)} active={v.active}
            onClick={() => (v.active ? void v.end() : void v.start())} />
        </div>
        <div className="flex items-center gap-2" aria-live="polite">
          <Pill tone={v.phase === "listening" ? "info" : v.phase === "speaking" ? "ok" : ["error", "mic_denied"].includes(v.phase) ? "bad" : v.phase === "not_ready" ? "warn" : "muted"}>
            {v.phase === "idle" && status.data && !ready ? PHASE_LABEL.not_ready : PHASE_LABEL[v.phase]}
          </Pill>
          {v.muted && <Pill tone="warn">Muted</Pill>}
        </div>
        {v.detail && <p role={["error", "mic_denied", "not_ready"].includes(v.phase) ? "alert" : undefined} className="mt-2 max-w-lg text-center text-xs text-ink-300">{v.detail}</p>}
        {(v.phase === "not_ready" || (status.data && !ready && !v.active)) && (
          <Link to="/integrations" className="mt-3 text-sm text-signal-300 underline">Open Integrations to finish voice setup</Link>
        )}
        <div className="mt-6 w-full max-w-xl"><Waveform mic={v.levels.mic} agent={v.levels.agent} active={v.active} /></div>
        <div className="mt-6 flex flex-wrap justify-center gap-2">
          {!v.active ? (
            <Button onClick={() => void v.start()} disabled={!ready}><IconMic className="h-4 w-4" />Start voice session</Button>
          ) : (
            <>
              <Button variant="ghost" onClick={() => void v.toggleMute()}>{v.muted ? <><IconMic className="h-4 w-4" />Unmute</> : <><IconMicOff className="h-4 w-4" />Mute</>}</Button>
              <Button variant="ghost" onClick={() => void v.interrupt()} disabled={v.phase !== "speaking"}><IconStop className="h-4 w-4" />Stop speaking</Button>
              <Button variant="danger" onClick={() => void v.end()}><IconPhoneOff className="h-4 w-4" />End session</Button>
            </>
          )}
        </div>
        <p className="mt-3 text-[11px] text-ink-500">You can also interrupt JARVIS at any time just by speaking.</p>
      </section>

      <div className="flex flex-col gap-4">
        <Card title="Voice readiness">
          {status.error ? <ErrorState error={status.error} /> : !status.data ? <Loading /> : (
            <>
              <div className="mb-3 flex items-center gap-2"><Pill tone={ready ? "ok" : "warn"}>{status.data.state}</Pill>
                {status.data.agent && <span className="text-xs text-ink-400">agent seen {when(status.data.agent.last_seen_at)}</span>}</div>
              <ul className="space-y-1.5 text-sm">
                {Object.entries(status.data.checks).map(([k, ok]) => (
                  <li key={k} className="flex items-center gap-2">
                    <span className={`grid h-4 w-4 place-items-center rounded-full ${ok ? "bg-ok-500/20 text-ok-500" : "bg-ink-700 text-ink-500"}`}>{ok ? <IconCheck className="h-3 w-3" /> : null}</span>
                    <span className={ok ? "text-ink-200" : "text-ink-400"}>{CHECK_LABEL[k] ?? k}</span>
                  </li>
                ))}
              </ul>
              {status.data.missing.length > 0 && (
                <ul className="mt-3 space-y-1 rounded-lg bg-warn-500/10 p-2.5 text-xs text-ink-200">{status.data.missing.map((m) => <li key={m}>• {m}</li>)}</ul>
              )}
            </>
          )}
        </Card>
        <Card title="Conversation" className="flex min-h-[280px] flex-col">
          <div ref={scroller} className="max-h-[50vh] space-y-2 overflow-y-auto scroll-thin" aria-live="polite">
            {v.lines.length === 0 && <p className="text-sm text-ink-500">The transcript appears here as you talk.</p>}
            {v.lines.map((l) => (
              <div key={l.id} className={`rounded-xl px-3 py-2 text-sm ${l.who === "you" ? "ml-6 bg-signal-500/10 text-ink-100" : "mr-6 bg-ink-850 text-ink-200"}`}>
                <div className="mb-0.5 text-[10px] uppercase tracking-wider text-ink-500">{l.who === "you" ? "You" : "JARVIS"}{!l.final && " · …"}</div>
                {l.text}
              </div>
            ))}
          </div>
        </Card>
        <Card title="Try saying">
          <ul className="space-y-2 text-xs text-ink-300">{EXAMPLES.map((e) => <li key={e}>“{e}”</li>)}</ul>
        </Card>
      </div>
    </div>
  );
}
