import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Link } from "react-router";
import { z } from "zod";
import { api, ApiError } from "../lib/api";
import { subscribeEvents, type JarvisEvent } from "../lib/events";
import { Button, Card, ErrorState, Pill, usd } from "../components/ui";
import { ActiveCampaigns, AttentionCard, BudgetCard, IntegrationHealth, VoiceLauncher } from "../components/CommandCenter";

const Question = z.object({ field: z.string(), prompt: z.string(), options: z.array(z.union([z.string(), z.number()])).optional(), preselect: z.string().optional() });
const CommandResp = z.object({
  id: z.string(), status: z.string(), raw_text: z.string(), parser: z.string().nullable(), parsed: z.record(z.any()).nullable(),
  questions: z.array(Question), error: z.string().nullable().optional(),
  plan: z.object({ places_requests_max: z.number(), serp_searches_max: z.number(), firecrawl_credits_max: z.number(), claude_usd_max: z.number(),
    estimated_usd_max_if_paid: z.number(), note: z.string() }).optional(),
  compliance: z.object({ open: z.boolean(), reason: z.string().nullable().optional(), intended_use: z.string().nullable().optional() }).optional(),
});
type Cmd = z.infer<typeof CommandResp>;
export const commandSchema = z.string().trim().min(3, "Tell JARVIS what to do").max(2000);

export default function Console() {
  const [text, setText] = useState("");
  const [validation, setValidation] = useState<string | null>(null);
  const [active, setActive] = useState<Cmd | null>(null);
  const [events, setEvents] = useState<JarvisEvent[]>([]);
  const [stream, setStream] = useState<"live" | "reconnecting">("reconnecting");
  const qc = useQueryClient();
  const history = useQuery({ queryKey: ["commands"], queryFn: () => api<Array<{ id: string; raw_text: string; status: string; campaign_id: string | null; created_at: string }>>("/commands") });
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => subscribeEvents((e) => setEvents((prev) => [e, ...prev].slice(0, 60)), { onStatus: setStream }), []);

  const send = useMutation({
    mutationFn: async (t: string) => CommandResp.parse(await api("/commands", { method: "POST", json: { text: t, channel: "text" } })),
    onSuccess: (c) => { setActive(c); setText(""); void qc.invalidateQueries({ queryKey: ["commands"] }); },
  });
  const answer = useMutation({
    mutationFn: async (answers: Record<string, unknown>) => CommandResp.parse(await api(`/commands/${active!.id}/answer`, { method: "POST", json: { answers } })),
    onSuccess: (c) => { setActive(c); void qc.invalidateQueries({ queryKey: ["commands"] }); },
  });
  const confirm = useMutation({
    mutationFn: async () => api<{ campaign_id: string }>(`/commands/${active!.id}/confirm`, { method: "POST", json: {} }),
    onSuccess: () => { void qc.invalidateQueries(); },
  });

  const submit = (e?: React.FormEvent) => {
    e?.preventDefault();
    const r = commandSchema.safeParse(text);
    if (!r.success) { setValidation(r.error.issues[0].message); return; }
    setValidation(null);
    send.mutate(r.data);
  };

  return (
    <div className="mx-auto grid max-w-7xl gap-6 px-4 py-6 md:px-8 lg:grid-cols-[1fr_380px]">
      <div className="flex flex-col gap-4">
        <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="rounded-2xl border hairline bg-gradient-to-b from-ink-850 to-ink-900 p-5 glow">
          <div className="mb-3 flex items-center justify-between">
            <div>
              <h1 className="text-lg font-semibold text-ink-100">What should JARVIS work on?</h1>
              <p className="text-sm text-ink-400">Describe the businesses and the previews you want. JARVIS checks the request, shows the worst-case cost and waits for your authorization.</p>
            </div>
            <VoiceLauncher />
          </div>
          <form onSubmit={submit} className="flex flex-col gap-2">
            <label htmlFor="cmd" className="sr-only">Command</label>
            <textarea id="cmd" ref={inputRef} value={text} onChange={(e) => setText(e.target.value)} rows={3}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }}
              placeholder="Find 100 plumbers in Houston with at least a 3.5 Google rating and no website."
              className="w-full resize-none rounded-xl border hairline bg-ink-950/60 p-3 text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none focus:ring-2 focus:ring-signal-500/40" />
            <div className="flex items-center justify-between">
              <span className="text-xs text-ink-500">{validation ?? "⌘/Ctrl + Enter to send"}</span>
              <Button type="submit" disabled={send.isPending}>{send.isPending ? "Thinking…" : "Send"}</Button>
            </div>
          </form>
          {send.error && <div className="mt-3"><ErrorState error={send.error} /></div>}
        </motion.div>

        <AnimatePresence mode="wait">
          {active && (
            <motion.div key={active.id + active.status} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <CommandCard cmd={active} onAnswer={(a) => answer.mutate(a)} onConfirm={() => confirm.mutate()}
                confirming={confirm.isPending} confirmError={confirm.error} confirmed={confirm.data?.campaign_id} />
              {answer.error && <ErrorState error={answer.error} />}
            </motion.div>
          )}
        </AnimatePresence>

        <ActiveCampaigns />

        <Card title="Recent commands">
          {history.error ? <ErrorState error={history.error} /> : (
            <ul className="divide-y divide-ink-800 text-sm">
              {(history.data ?? []).map((c) => (
                <li key={c.id} className="flex items-center justify-between gap-3 py-2">
                  <span className="truncate text-ink-200">{c.raw_text}</span>
                  <span className="flex shrink-0 items-center gap-2">
                    <Pill>{c.status}</Pill>
                    {c.campaign_id && <Link className="text-xs text-signal-300 hover:underline" to={`/campaigns/${c.campaign_id}`}>Open</Link>}
                  </span>
                </li>
              ))}
              {history.data?.length === 0 && <li className="py-2 text-ink-500">No commands yet.</li>}
            </ul>
          )}
        </Card>
      </div>

      <div className="flex flex-col gap-4">
      <AttentionCard />
      <IntegrationHealth />
      <BudgetCard />
      <Card title="Live activity" actions={<Pill tone={stream === "live" ? "ok" : "warn"}>{stream === "live" ? "live" : "reconnecting"}</Pill>} className="h-fit">
        <ul className="max-h-[50vh] space-y-2 overflow-y-auto scroll-thin text-sm" aria-live="polite">
          {events.map((e) => (
            <li key={e.id} className="rounded-lg bg-ink-850 px-3 py-2">
              <div className="flex items-center justify-between text-[11px] text-ink-500"><span>{e.event_type}</span><span>{new Date(e.created_at).toLocaleTimeString()}</span></div>
              <div className="text-ink-200">{e.message}</div>
            </li>
          ))}
          {events.length === 0 && <li className="text-ink-500">Waiting for activity…</li>}
        </ul>
      </Card>
      </div>
    </div>
  );
}

function CommandCard({ cmd, onAnswer, onConfirm, confirming, confirmError, confirmed }: {
  cmd: Cmd; onAnswer: (a: Record<string, unknown>) => void; onConfirm: () => void; confirming: boolean; confirmError: unknown; confirmed?: string;
}) {
  const p = cmd.parsed ?? {};
  const blocked = confirmError instanceof ApiError && confirmError.code === "COMPLIANCE_BLOCKED";
  return (
    <Card title={<span className="flex items-center gap-2">JARVIS understood <Pill>{cmd.status}</Pill>{cmd.parser && <span className="text-xs text-ink-500">parser: {cmd.parser}</span>}</span>}>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm md:grid-cols-4">
        <Field k="Business type" v={p.niche} /><Field k="Location" v={p.location ? [p.location.city, p.location.state].filter(Boolean).join(", ") : undefined} />
        <Field k="Target" v={p.target_count} /><Field k="Min. Google rating" v={p.minimum_google_rating ?? "none"} />
        <Field k="Unrated businesses" v={p.missing_rating_policy ?? (p.minimum_google_rating ? "?" : "n/a")} />
        <Field k="Website filter" v={(p.website_filter ?? ["ANY"]).join(", ")} /><Field k="Sites to build" v={p.sites_to_build ?? 0} />
        <Field k="Source" v={p.discovery_provider} />
      </dl>
      {cmd.questions.length > 0 && (
        <div className="mt-4 space-y-3">
          {cmd.questions.map((q) => <QuestionRow key={q.field} q={q} onAnswer={onAnswer} />)}
        </div>
      )}
      {cmd.error && <p className="mt-3 text-xs text-warn-500">{cmd.error}</p>}
      {cmd.status === "awaiting_confirmation" && cmd.plan && (
        <div className="mt-4 rounded-lg border hairline bg-ink-850 p-3 text-sm">
          <div className="mb-2 text-xs uppercase tracking-wider text-ink-400">Worst-case plan</div>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Field k="Places requests" v={cmd.plan.places_requests_max} /><Field k="SERP searches" v={cmd.plan.serp_searches_max} />
            <Field k="Firecrawl credits" v={cmd.plan.firecrawl_credits_max} /><Field k="Claude (max)" v={usd(cmd.plan.claude_usd_max * 1e6)} />
          </div>
          <p className="mt-2 text-xs text-ink-400">{cmd.plan.note}</p>
          {cmd.compliance && !cmd.compliance.open && (
            <div className="mt-3 rounded-md border border-warn-500/30 bg-warn-500/10 p-2 text-xs text-warn-500">
              Google Places discovery is blocked pending approval: {cmd.compliance.reason}
              <div className="mt-1 text-ink-300">Alternative: <Link className="underline" to="/campaigns">import a business list you have the right to use</Link>.</div>
            </div>
          )}
          <div className="mt-3 flex items-center gap-3">
            <Button onClick={onConfirm} disabled={confirming || Boolean(confirmed)}>{confirmed ? "Authorized" : confirming ? "Authorizing…" : "Authorize campaign"}</Button>
            {confirmed && <Link className="text-sm text-signal-300 underline" to={`/campaigns/${confirmed}`}>View campaign</Link>}
          </div>
          {Boolean(confirmError) && <div className="mt-3">{blocked ? <ErrorState error={confirmError} /> : <ErrorState error={confirmError} />}</div>}
        </div>
      )}
    </Card>
  );
}

function QuestionRow({ q, onAnswer }: { q: z.infer<typeof Question>; onAnswer: (a: Record<string, unknown>) => void }) {
  const [v, setV] = useState("");
  return (
    <div className="rounded-lg border border-signal-500/30 bg-signal-500/5 p-3">
      <div className="text-sm text-ink-100">{q.prompt}</div>
      <div className="mt-2 flex flex-wrap gap-2">
        {q.options?.map((o) => (
          <Button key={String(o)} variant={q.preselect === o ? "primary" : "ghost"} onClick={() => onAnswer({ [q.field]: o })}>{String(o)}</Button>
        ))}
        {!q.options && (
          <form className="flex w-full gap-2" onSubmit={(e) => { e.preventDefault(); if (v.trim()) onAnswer({ [q.field]: q.field === "target_count" ? Number(v) : q.field === "location" ? { city: v } : v }); }}>
            <input aria-label={q.prompt} value={v} onChange={(e) => setV(e.target.value)} className="flex-1 rounded-md border hairline bg-ink-950 px-2 py-1 text-sm" />
            <Button type="submit" variant="ghost">Answer</Button>
          </form>
        )}
      </div>
    </div>
  );
}

function Field({ k, v }: { k: string; v: unknown }) {
  return <div><dt className="text-[11px] uppercase tracking-wider text-ink-500">{k}</dt><dd className="text-ink-100">{v === undefined || v === null ? "—" : String(v)}</dd></div>;
}
