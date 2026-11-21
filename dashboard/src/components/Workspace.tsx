import { useEffect, useState } from "react";
import { Link } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiBlobUrl } from "../lib/api";
import { subscribeEvents } from "../lib/events";
import { ErrorState, Loading, Pill } from "./ui";

/** One JARVIS task, as the API reports it (every field is read from the database; nothing is estimated). */
export type WorkItem = {
  id: string; name: string; kind: "discovery" | "website_build"; status: string; state: string; command: string | null;
  current_operation: string; steps: Array<{ stage: string; label: string; state: string }>; completed_steps: string[];
  remaining_steps: string[]; started_at: string | null; updated_at: string | null; completed_at: string | null; elapsed_s: number;
  result: string | null; error: string | null; parent_task_id: string | null; running_now: number; retries: number;
  counts?: Record<string, number> | null;
};
export type WorkDetail = WorkItem & {
  timings: { stages: Array<{ task: string; tasks: number; retries: number; wall_s: number | null; busy_s: number | null; queue_wait_s: number | null }>;
    providers: Array<{ provider: string; operation: string; calls: number; failures: number; avg_ms: number | null; max_ms: number | null;
      tokens_in: number; tokens_out: number; estimated_cost_usd: number }> };
  activity: Array<{ message: string; event_type: string; created_at: string }>;
  failures: Array<{ task: string; status: string; attempts: number; code: string | null; detail: string | null }>;
};
type ProspectRow = { id: string; business_name: string | null; state: string; readiness: string | null; score: number | null;
  evidence_confidence: number | null; missing: string[] | null; website_check: string | null; listing_website: string | null;
  website_url: string | null; discovery_provider: string; preview_url: string | null; site_state: string | null };
type SiteRow = { id: string; slug: string; state: string; business_name: string | null; version: number | null; qa_status: string | null;
  deploy_status: string | null; preview_url: string | null; last_error: string | null };

export const ACTIVE_STATUSES = new Set(["QUEUED", "RUNNING", "PAUSED", "WAITING", "CANCEL_REQUESTED"]);
const TONE: Record<string, "ok" | "warn" | "bad" | "info" | "muted"> = {
  RUNNING: "info", QUEUED: "muted", PAUSED: "warn", WAITING: "warn", COMPLETED: "ok", FAILED: "bad", CANCEL_REQUESTED: "warn",
  CANCELLED: "muted", qualified: "ok", needs_research: "warn", not_a_fit: "muted",
};
const STEP_ICON: Record<string, string> = { done: "✓", running: "●", queued: "○", pending: "○", failed: "✕", partly_failed: "!",
  cancelled: "–", skipped: "–" };
const WEBSITE_CHECK_LABEL: Record<string, string> = {
  NO_WEBSITE_FOUND: "No website found (independent checks)", NO_WEBSITE_LISTED: "No website listed (not conclusive)",
  WEBSITE_FOUND: "Possible website (not confirmed)", WEBSITE_WORKING: "Working website", WEBSITE_BROKEN_CONFIRMED: "Website broken (confirmed twice)",
  WEBSITE_BLOCKED: "Website blocked our check", WEBSITE_RATE_LIMITED: "Website rate-limited our check",
  WEBSITE_AUTH_REQUIRED: "Website requires login", WEBSITE_REDIRECTED: "Website redirects elsewhere", WEBSITE_UNKNOWN: "Website check inconclusive",
};

export function elapsed(s: number): string {
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** Live task list: refreshed on every real task event (SSE), with a slow poll as a safety net. */
export function useWork() {
  const qc = useQueryClient();
  useEffect(() => {
    const unsubscribe = subscribeEvents(() => { void qc.invalidateQueries({ queryKey: ["work"] }); void qc.invalidateQueries({ queryKey: ["work-detail"] }); });
    return () => unsubscribe();
  }, [qc]);
  return useQuery({ queryKey: ["work"], queryFn: async () => { const r = await api<WorkItem[]>("/work?limit=20"); return Array.isArray(r) ? r : []; },
    refetchInterval: 4000 });
}

function useControl(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (action: string) => api<{ status: string }>(`/work/${id}/${action}`, { method: "POST" }),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ["work"] }); void qc.invalidateQueries({ queryKey: ["work-detail", id] }); },
  });
}

/** The task JARVIS is working on (or the selected one): what it is doing right now, its steps, and real controls. */
export function ActiveTaskPanel({ task }: { task: WorkItem | undefined }) {
  const ctl = useControl(task?.id ?? "");
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  if (!task) return (
    <section aria-label="Active task" className="rounded-2xl border hairline bg-ink-900/80 p-4">
      <h2 className="text-xs uppercase tracking-wider text-ink-400">Active task</h2>
      <p className="mt-2 text-sm text-ink-400">Nothing is running. Tell JARVIS what to do, by voice or text.</p>
    </section>
  );
  const live = ACTIVE_STATUSES.has(task.status) && task.started_at && !task.completed_at;
  const secs = live ? (now - new Date(task.started_at!).getTime()) / 1000 : task.elapsed_s;
  return (
    <section aria-label="Active task" data-testid="active-task" className="rounded-2xl border hairline bg-ink-900/80 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-xs uppercase tracking-wider text-ink-400">{task.kind === "website_build" ? "Website build" : "Task"}</h2>
          <p className="truncate font-medium text-ink-100" title={task.name}>{task.name}</p>
          {task.command && <p className="mt-0.5 line-clamp-2 text-xs text-ink-400">“{task.command}”</p>}
        </div>
        <Pill tone={TONE[task.status] ?? "muted"}>{task.status.replace(/_/g, " ")}</Pill>
      </div>
      <p className="mt-3 text-sm text-ink-200" data-testid="current-operation" aria-live="polite">{task.current_operation}</p>
      <p className="mt-1 text-xs text-ink-500">Elapsed {elapsed(secs)}{task.retries ? ` · ${task.retries} retr${task.retries === 1 ? "y" : "ies"}` : ""}</p>
      <ol className="mt-3 space-y-1 text-sm" aria-label="Steps">
        {task.steps.map((s) => (
          <li key={s.stage} className={`flex items-center gap-2 ${s.state === "done" ? "text-ink-300" : s.state === "running" ? "text-arc-300" : s.state.includes("failed") ? "text-bad-500" : "text-ink-500"}`}>
            <span aria-hidden className="w-4 text-center">{STEP_ICON[s.state] ?? "○"}</span><span>{s.label}</span>
            <span className="sr-only">: {s.state}</span>
          </li>))}
      </ol>
      {task.error && <p role="alert" className="mt-3 rounded-lg bg-warn-500/10 p-2 text-xs text-ink-100">{task.error}</p>}
      {task.result && !live && <p className="mt-3 text-xs text-ink-300">{task.result}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        {(task.status === "RUNNING" || task.status === "QUEUED") && <CtlButton onClick={() => ctl.mutate("pause")} busy={ctl.isPending}>Pause</CtlButton>}
        {(task.status === "PAUSED" || task.state === "blocked") && <CtlButton onClick={() => ctl.mutate("resume")} busy={ctl.isPending}>Resume</CtlButton>}
        {["RUNNING", "QUEUED", "PAUSED", "WAITING"].includes(task.status) && <CtlButton danger onClick={() => ctl.mutate("cancel")} busy={ctl.isPending}>Stop</CtlButton>}
        {(task.status === "FAILED" || task.state === "requires_human_review" || task.state === "blocked") && <CtlButton onClick={() => ctl.mutate("retry")} busy={ctl.isPending}>Retry</CtlButton>}
        <Link to={`/campaigns/${task.id}`} className="ml-auto self-center text-xs text-signal-300 hover:underline">Details</Link>
      </div>
      {ctl.error && <div className="mt-2"><ErrorState error={ctl.error} /></div>}
      <TaskTimings id={task.id} />
    </section>
  );
}

function CtlButton({ children, onClick, danger, busy }: { children: React.ReactNode; onClick: () => void; danger?: boolean; busy?: boolean }) {
  return <button type="button" disabled={busy} onClick={onClick}
    className={`rounded-full px-3 py-1 text-xs ring-1 transition disabled:opacity-50 ${danger ? "bg-bad-500/15 text-bad-500 ring-bad-500/40 hover:bg-bad-500/25" : "bg-ink-850 text-ink-100 ring-ink-600 hover:ring-arc-400/50"}`}>{children}</button>;
}

/** Measured timings for the task: per stage, queue waiting, retries, and per provider (from the gateway's records). */
function TaskTimings({ id }: { id: string }) {
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["work-detail", id], queryFn: () => api<WorkDetail>(`/work/${id}`), enabled: open, refetchInterval: open ? 5000 : false });
  return (
    <details className="mt-3 text-xs" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer text-ink-400">Timing and activity</summary>
      {q.error ? <ErrorState error={q.error} /> : !q.data ? <Loading /> : (
        <div className="mt-2 space-y-2">
          <table className="w-full text-left tabular-nums"><thead className="text-ink-500"><tr><th>Stage</th><th>Wall</th><th>Busy</th><th>Queue</th><th>Retries</th></tr></thead>
            <tbody>{q.data.timings.stages.map((s) => (
              <tr key={s.task} className="text-ink-300"><td>{s.task}</td><td>{s.wall_s ?? "–"}s</td><td>{s.busy_s ?? "–"}s</td><td>{s.queue_wait_s ?? "–"}s</td><td>{s.retries}</td></tr>))}</tbody></table>
          {q.data.timings.providers.length > 0 && (
            <table className="w-full text-left tabular-nums"><thead className="text-ink-500"><tr><th>Provider</th><th>Calls</th><th>Avg</th><th>Fail</th><th>Est. $</th></tr></thead>
              <tbody>{q.data.timings.providers.map((p) => (
                <tr key={`${p.provider}-${p.operation}`} className="text-ink-300"><td>{p.provider} {p.operation}</td><td>{p.calls}</td>
                  <td>{p.avg_ms ?? "–"}ms</td><td>{p.failures}</td><td>{p.estimated_cost_usd}</td></tr>))}</tbody></table>)}
          <ul className="max-h-40 space-y-1 overflow-y-auto scroll-thin">{q.data.activity.map((a, i) => (
            <li key={i} className="text-ink-400"><span className="text-ink-600">{new Date(a.created_at).toLocaleTimeString()}</span> {a.message}</li>))}</ul>
        </div>)}
    </details>
  );
}

/** Recent tasks; selecting one shows its state and results. */
export function TaskHistory({ items, selected, onSelect }: { items: WorkItem[]; selected?: string; onSelect: (id: string) => void }) {
  return (
    <section aria-label="Task history" className="rounded-2xl border hairline bg-ink-900/80 p-4">
      <h2 className="mb-2 text-xs uppercase tracking-wider text-ink-400">Task history</h2>
      {items.length === 0 ? <p className="text-sm text-ink-500">No tasks yet.</p> : (
        <ul className="space-y-1">{items.map((t) => (
          <li key={t.id}>
            <button type="button" onClick={() => onSelect(t.id)} aria-pressed={selected === t.id}
              className={`flex w-full items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-sm transition ${selected === t.id ? "bg-arc-500/10" : "hover:bg-ink-850"}`}>
              <span className="min-w-0 truncate text-ink-200">{t.name}</span>
              <Pill tone={TONE[t.status] ?? "muted"}>{t.status.replace(/_/g, " ")}</Pill>
            </button>
          </li>))}</ul>)}
    </section>
  );
}

/** What a task produced: prospects (with readiness and evidence) and websites (with screenshots, QA and previews). */
export function ResultsPanel({ task }: { task: WorkItem | undefined }) {
  const [tab, setTab] = useState<"prospects" | "websites">("prospects");
  const id = task?.id;
  const isBuild = task?.kind === "website_build";
  useEffect(() => { if (isBuild) setTab("websites"); }, [isBuild, id]);
  const prospects = useQuery({ queryKey: ["work-prospects", id], enabled: Boolean(id) && !isBuild, refetchInterval: 5000,
    queryFn: async () => { const r = await api<ProspectRow[]>(`/prospects?campaign_id=${id}&limit=50`); return Array.isArray(r) ? r : []; } });
  const sites = useQuery({ queryKey: ["work-sites", id], enabled: Boolean(id), refetchInterval: 5000,
    queryFn: async () => { const r = await api<SiteRow[]>(`/sites?campaign_id=${id}`); return Array.isArray(r) ? r : []; } });
  if (!task) return null;
  return (
    <section aria-label="Results" className="rounded-2xl border hairline bg-ink-900/80 p-4">
      <div className="mb-3 flex items-center gap-1" role="tablist">
        {!isBuild && <Tab on={tab === "prospects"} onClick={() => setTab("prospects")}>Prospects{prospects.data ? ` (${prospects.data.length})` : ""}</Tab>}
        <Tab on={tab === "websites"} onClick={() => setTab("websites")}>Websites{sites.data ? ` (${sites.data.length})` : ""}</Tab>
      </div>
      {tab === "prospects" && !isBuild && (prospects.error ? <ErrorState error={prospects.error} /> : !prospects.data ? <Loading /> :
        prospects.data.length === 0 ? <p className="text-sm text-ink-500">No businesses yet.</p> : (
          <ul className="space-y-2">{prospects.data.map((p) => <ProspectItem key={p.id} p={p} />)}</ul>))}
      {tab === "websites" && (sites.error ? <ErrorState error={sites.error} /> : !sites.data ? <Loading /> :
        sites.data.length === 0 ? <p className="text-sm text-ink-500">No websites for this task{isBuild ? " yet" : ". Ask JARVIS to build one"}.</p> : (
          <ul className="space-y-3">{sites.data.map((s) => <SiteItem key={s.id} s={s} />)}</ul>))}
    </section>
  );
}

function Tab({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button type="button" role="tab" aria-selected={on} onClick={onClick}
    className={`rounded-full px-3 py-1 text-xs transition ${on ? "bg-arc-500/15 text-arc-200" : "text-ink-400 hover:text-ink-200"}`}>{children}</button>;
}

function ProspectItem({ p }: { p: ProspectRow }) {
  const [open, setOpen] = useState(false);
  const d = useQuery({ queryKey: ["prospect", p.id], enabled: open,
    queryFn: () => api<{ scores: Array<{ components: Array<{ rule: string; awarded: number; explanation: string }>; missing: string[]; reasons: string[] }>;
      sources: Array<{ source: string; provider: string | null; fields: string[] }>;
      verifications: Array<{ http_evidence: Array<{ check: string; status: number | null; verdict: string; final_url: string | null }> }> }>(`/prospects/${p.id}`) });
  return (
    <li className="rounded-lg border hairline p-2.5">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} className="flex w-full items-start justify-between gap-2 text-left">
        <span className="min-w-0">
          <span className="block truncate text-sm text-ink-100">{p.business_name ?? "(name not verified)"}</span>
          <span className="block text-xs text-ink-400">{WEBSITE_CHECK_LABEL[p.website_check ?? ""] ?? (p.state === "discovered" ? "Checking website…" : p.state.replace(/_/g, " "))}</span>
        </span>
        <span className="flex shrink-0 flex-col items-end gap-1">
          {p.readiness ? <Pill tone={TONE[p.readiness]}>{p.readiness.replace(/_/g, " ")}{p.score != null ? ` · ${p.score}` : ""}</Pill> : <Pill tone="muted">{p.state}</Pill>}
          {p.evidence_confidence != null && <span className="text-[10px] text-ink-500">evidence {p.evidence_confidence}/100</span>}
        </span>
      </button>
      {!open && p.missing && p.missing.length > 0 && <p className="mt-1 truncate text-[11px] text-warn-500">Missing: {p.missing[0]}</p>}
      {open && (d.error ? <ErrorState error={d.error} /> : !d.data ? <Loading /> : (
        <div className="mt-2 space-y-2 text-xs">
          {d.data.scores[0] && (<>
            <ul className="space-y-0.5">{d.data.scores[0].components.map((c) => (
              <li key={c.rule} className="flex justify-between gap-2"><span className="text-ink-300">{c.rule.replace(/_/g, " ")}</span>
                <span className="text-right text-ink-400"><b className="tabular-nums text-ink-200">{c.awarded}</b> · {c.explanation}</span></li>))}</ul>
            {d.data.scores[0].missing.length > 0 && <p className="text-warn-500">Missing: {d.data.scores[0].missing.join("; ")}</p>}
            {d.data.scores[0].reasons.length > 0 && <p className="text-ink-400">Why: {d.data.scores[0].reasons.join("; ")}</p>}
          </>)}
          <p className="text-ink-400">Sources: {d.data.sources.map((s) => `${s.source} (${s.fields.join(", ")})`).join(" · ") || "none"}</p>
          {d.data.verifications[0]?.http_evidence?.length > 0 && (
            <p className="text-ink-500">HTTP: {d.data.verifications[0].http_evidence.slice(0, 4).map((h) => `${h.check} ${h.status ?? "–"} ${h.verdict}`).join(" · ")}</p>)}
          <Link to={`/prospects/${p.id}`} className="text-signal-300 hover:underline">Full evidence</Link>
        </div>))}
    </li>
  );
}

function SiteItem({ s }: { s: SiteRow }) {
  const qc = useQueryClient();
  const act = useMutation({ mutationFn: (a: string) => api(`/sites/${s.id}/${a}`, { method: "POST" }),
    onSettled: () => { void qc.invalidateQueries({ queryKey: ["work-sites"] }); void qc.invalidateQueries({ queryKey: ["work"] }); } });
  const [shot, setShot] = useState<{ desktop?: string; mobile?: string }>({});
  useEffect(() => {
    if (!s.version || !s.qa_status) return;
    const urls: string[] = [];
    (["desktop", "mobile"] as const).forEach((k) => apiBlobUrl(`/sites/${s.id}/artifact/${s.version}/qa/${k}.png`)
      .then((u) => { urls.push(u); setShot((x) => ({ ...x, [k]: u })); }).catch(() => undefined));
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [s.id, s.version, s.qa_status]);
  const failed = s.state === "failed" || s.state === "requires_human_review";
  return (
    <li className="rounded-lg border hairline p-2.5" data-testid="site-result">
      <div className="flex items-center justify-between gap-2"><span className="truncate text-sm text-ink-100">{s.business_name ?? s.slug}</span>
        <Pill tone={s.state === "deployed" ? "ok" : failed ? "bad" : "info"}>{s.state}</Pill></div>
      <div className="mt-2 grid grid-cols-[3fr_1fr] gap-2">
        {shot.desktop ? <img src={shot.desktop} alt={`Desktop screenshot of the ${s.business_name ?? ""} preview`} className="h-32 w-full rounded object-cover object-top" />
          : <div className="grid h-32 place-items-center rounded bg-ink-850 text-[11px] text-ink-500">{s.qa_status ? "Loading screenshot…" : "No screenshot yet"}</div>}
        {shot.mobile ? <img src={shot.mobile} alt={`Mobile screenshot of the ${s.business_name ?? ""} preview`} className="h-32 w-full rounded object-cover object-top" />
          : <div className="h-32 rounded bg-ink-850" />}
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5 text-[11px]">
        <Pill tone="muted">built: {s.version ? `v${s.version}` : "no"}</Pill>
        <Pill tone={s.qa_status === "passed" ? "ok" : s.qa_status === "failed" || s.qa_status === "error" ? "bad" : "muted"}>QA: {s.qa_status ?? "not run"}</Pill>
        <Pill tone={s.deploy_status === "verified" ? "ok" : s.deploy_status === "failed" ? "bad" : "muted"}>deploy: {s.deploy_status ?? "not yet"}</Pill>
      </div>
      {s.preview_url && <a href={s.preview_url} target="_blank" rel="noopener noreferrer" className="mt-2 block break-all text-xs text-signal-300 underline">{s.preview_url}</a>}
      {s.last_error && <p role="alert" className="mt-2 text-[11px] text-bad-500">{s.last_error}</p>}
      <div className="mt-2 flex flex-wrap gap-2">
        {s.preview_url && <a href={s.preview_url} target="_blank" rel="noopener noreferrer" className="rounded-full bg-signal-500/90 px-3 py-1 text-xs text-white">Open preview</a>}
        <CtlButton busy={act.isPending} onClick={() => act.mutate("rebuild")}>Rebuild</CtlButton>
        {s.version && <CtlButton busy={act.isPending} onClick={() => act.mutate("qa")}>Run QA again</CtlButton>}
        <Link to={`/websites/${s.id}`} className="self-center text-xs text-signal-300 hover:underline">Build details</Link>
      </div>
      {act.error && <div className="mt-2"><ErrorState error={act.error} /></div>}
    </li>
  );
}
