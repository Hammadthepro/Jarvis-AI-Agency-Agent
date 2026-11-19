import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Page } from "../components/Shell";
import { Card, ErrorState, Loading, Pill, Stat, usd, when } from "../components/ui";

type Overview = Record<string, number | string> & {
  workers: Array<{ worker_id: string; last_seen_at: string; version: string; db_state: string; alive: boolean }>;
};

export default function OverviewPage() {
  const q = useQuery({ queryKey: ["overview"], queryFn: () => api<Overview>("/overview"), refetchInterval: 10_000 });
  const integ = useQuery({ queryKey: ["integrations"], queryFn: () => api<Array<{ provider: string; state: string; configured: boolean; tested: boolean }>>("/integrations") });
  if (q.error) return <Page title="Overview"><ErrorState error={q.error} /></Page>;
  if (!q.data) return <Page title="Overview"><Loading /></Page>;
  const d = q.data;
  return (
    <Page title="Overview" subtitle="Every number below is counted from database records.">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Discovered" value={d.prospects_discovered} />
        <Stat label="Website verified" value={d.prospects_verified} hint={`${d.prospects_uncertain} uncertain / failed`} />
        <Stat label="No website (verified)" value={d.prospects_no_website} />
        <Stat label="Qualified" value={d.prospects_qualified} />
        <Stat label="Sites generated" value={d.sites_generated} />
        <Stat label="QA passed" value={d.sites_qa_passed} />
        <Stat label="Previews deployed" value={d.previews_deployed} />
        <Stat label="Outreach drafts" value={d.outreach_drafts} />
      </div>
      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        <Card title="System health">
          <dl className="space-y-2 text-sm">
            <Row k="Database" v={<Pill tone={d.db_state === "healthy" ? "ok" : "bad"}>{String(d.db_state)}</Pill>} />
            <Row k="Queue depth" v={d.queue_depth} />
            <Row k="Dead letters" v={<Pill tone={Number(d.dead_letters) ? "bad" : "ok"}>{String(d.dead_letters)}</Pill>} />
            <Row k="Running campaigns" v={d.campaigns_running} />
            <Row k="Spend this month (est.)" v={usd(Number(d.spend_month_micros))} />
          </dl>
        </Card>
        <Card title="Workers">
          {d.workers.length === 0 ? <p className="text-sm text-warn-500">No worker has reported a heartbeat. Background jobs will not run.</p> : (
            <ul className="space-y-2 text-sm">{d.workers.map((w) => (
              <li key={w.worker_id} className="flex items-center justify-between"><span className="truncate font-mono text-xs">{w.worker_id}</span>
                <span className="flex items-center gap-2 text-xs text-ink-400">{when(w.last_seen_at)}<Pill tone={w.alive ? "ok" : "bad"}>{w.alive ? "alive" : "stale"}</Pill></span></li>))}
            </ul>)}
        </Card>
        <Card title="Integrations">
          {integ.error ? <ErrorState error={integ.error} /> : (
            <ul className="space-y-2 text-sm">{(integ.data ?? []).map((i) => (
              <li key={i.provider} className="flex items-center justify-between"><span>{i.provider}</span>
                <span className="flex items-center gap-1">{i.configured && <Pill tone={i.tested ? "ok" : "muted"}>{i.tested ? "tested" : "untested"}</Pill>}<Pill>{i.state}</Pill></span></li>))}
            </ul>)}
        </Card>
      </div>
    </Page>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex items-center justify-between"><dt className="text-ink-400">{k}</dt><dd className="text-ink-100">{v}</dd></div>;
}
