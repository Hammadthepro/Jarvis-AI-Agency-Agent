import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Page } from "../components/Shell";
import { Card, ErrorState, Loading, Pill, when } from "../components/ui";

type A = { tasks: Array<{ id: string; task_type: string; status: string; attempts: number; max_attempts: number; error_code: string | null; error_detail: string | null; created_at: string }>;
  events: Array<{ id: number; event_type: string; message: string; created_at: string }>;
  audit: Array<{ id: number; actor_type: string; actor_id: string | null; action: string; entity_type: string | null; from_state: string | null; to_state: string | null; created_at: string }>;
  provider_errors: Array<{ provider: string; operation: string; outcome: string; http_status: number | null; created_at: string }>;
  dead_letters: Array<{ id: number; task_id: string; reason: string; created_at: string }> };

export default function ActivityPage() {
  const q = useQuery({ queryKey: ["activity"], queryFn: () => api<A>("/activity?limit=150"), refetchInterval: 8000 });
  if (q.error) return <Page title="Activity"><ErrorState error={q.error} /></Page>;
  if (!q.data) return <Page title="Activity"><Loading /></Page>;
  const d = q.data;
  return (
    <Page title="Activity & logs" subtitle="Task execution, provider errors, dead letters and the audit trail.">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Tasks" className="lg:col-span-2">
          <div className="max-h-96 overflow-auto scroll-thin"><table className="w-full text-xs"><thead className="text-left text-ink-500"><tr><th className="py-1">Task</th><th>Status</th><th>Attempts</th><th>Error</th><th>Created</th></tr></thead>
            <tbody className="divide-y divide-ink-800">{d.tasks.map((t) => <tr key={t.id}><td className="py-1 font-mono">{t.task_type}</td><td><Pill>{t.status}</Pill></td><td>{t.attempts}/{t.max_attempts}</td>
              <td className="max-w-md truncate text-ink-400" title={t.error_detail ?? ""}>{t.error_code}</td><td className="text-ink-500">{when(t.created_at)}</td></tr>)}</tbody></table></div>
        </Card>
        <Card title="Provider errors">{d.provider_errors.length === 0 ? <p className="text-sm text-ink-500">None.</p> : <ul className="space-y-1 text-xs">{d.provider_errors.map((e, i) => <li key={i} className="flex justify-between"><span>{e.provider} · {e.operation}</span><span><Pill>{e.outcome}</Pill> {e.http_status ?? ""}</span></li>)}</ul>}</Card>
        <Card title="Dead letters">{d.dead_letters.length === 0 ? <p className="text-sm text-ink-500">None.</p> : <ul className="space-y-1 text-xs">{d.dead_letters.map((e) => <li key={e.id}>{e.reason} <span className="text-ink-500">{when(e.created_at)}</span></li>)}</ul>}</Card>
        <Card title="Audit trail" className="lg:col-span-2">
          <ul className="max-h-96 space-y-1 overflow-auto scroll-thin text-xs">{d.audit.map((a) => <li key={a.id} className="flex gap-3"><span className="w-40 shrink-0 text-ink-500">{when(a.created_at)}</span>
            <span className="text-ink-300">{a.actor_type}:{a.actor_id?.slice(0, 8)}</span><span className="text-ink-100">{a.action}</span>
            {a.to_state && <span className="text-ink-400">{a.from_state ?? "∅"} → {a.to_state}</span>}</li>)}</ul>
        </Card>
      </div>
    </Page>
  );
}
