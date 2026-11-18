import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { api, apiBlobUrl } from "../lib/api";
import { Page } from "../components/Shell";
import { Card, Empty, ErrorState, Loading, Pill, usd, when } from "../components/ui";

type Site = { id: string; slug: string; state: string; business_name: string | null; headline: string | null; version: number | null; qa_status: string | null;
  qa_runner: string | null; preview_url: string | null; repair_cycles: number; cost_micros: number; design: Record<string, string> | null; model: string | null };

export default function WebsitesPage() {
  const [sp] = useSearchParams();
  const qs = sp.get("campaign") ? `?campaign_id=${sp.get("campaign")}` : "";
  const q = useQuery({ queryKey: ["sites", qs], queryFn: () => api<Site[]>(`/sites${qs}`), refetchInterval: 6000 });
  return (
    <Page title="Website Studio" subtitle="Generated previews. A site is only ‘passed’ after real-browser QA; previews are noindex and expire automatically.">
      {q.error ? <ErrorState error={q.error} /> : !q.data ? <Loading /> : q.data.length === 0 ? <Empty title="No websites yet">Campaigns with “sites to build” create them after qualification.</Empty> : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{q.data.map((s) => (
          <Link key={s.id} to={`/websites/${s.id}`} className="group rounded-xl border hairline bg-ink-900/80 p-4 transition hover:border-signal-500/40">
            <Thumb site={s} />
            <div className="mt-3 flex items-center justify-between"><div className="truncate font-medium text-ink-100">{s.business_name ?? s.slug}</div><Pill>{s.state}</Pill></div>
            <div className="mt-1 truncate text-sm text-ink-400">{s.headline}</div>
            <div className="mt-2 flex flex-wrap gap-2 text-xs text-ink-500"><span>v{s.version ?? "–"}</span><span>QA: {s.qa_status ?? "—"}</span><span>repairs: {s.repair_cycles}</span>
              <span>{usd(s.cost_micros)}</span>{s.design && <span>{s.design.archetype} · {s.design.palette}</span>}</div>
          </Link>))}</div>
      )}
    </Page>
  );
}

function Thumb({ site }: { site: Site }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!site.version || !site.qa_status) return;
    let url: string | null = null;
    apiBlobUrl(`/sites/${site.id}/artifact/${site.version}/qa/desktop.png`).then((u) => { url = u; setSrc(u); }).catch(() => setSrc(null));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [site.id, site.version, site.qa_status]);
  return <div className="aspect-[16/10] overflow-hidden rounded-lg bg-ink-850">{src ? <img src={src} alt={`Screenshot of ${site.business_name ?? site.slug}`} className="w-full object-cover object-top" /> : <div className="grid h-full place-items-center text-xs text-ink-500">No screenshot yet</div>}</div>;
}

type SiteDetail = { site: Site & { campaign_id: string; prospect_id: string };
  versions: Array<{ id: string; version: number; reason: string; file_count: number; total_bytes: number; created_at: string; model: string; prompt_version: string; design: Record<string, string>; spec: { strategy?: Record<string, string> } }>;
  qa_runs: Array<{ id: string; site_version_id: string; status: string; runner: string; metrics: Record<string, { load_ms: number; transfer_bytes: number }> | null; started_at: string; findings: Array<{ check_name: string; severity: string; viewport: string; message: string }> | null }>;
  deployments: Array<{ id: string; status: string; url: string | null; provider_deployment_id: string | null; created_at: string; expires_at: string | null }> };

export function SiteDetailPage() {
  const { id } = useParams();
  const q = useQuery({ queryKey: ["site", id], queryFn: () => api<SiteDetail>(`/sites/${id}`) });
  const [shot, setShot] = useState<{ desktop?: string; mobile?: string }>({});
  const v = q.data?.versions[0];
  useEffect(() => {
    if (!v) return;
    const urls: string[] = [];
    (["desktop", "mobile"] as const).forEach((k) => apiBlobUrl(`/sites/${id}/artifact/${v.version}/qa/${k}.png`).then((u) => { urls.push(u); setShot((s) => ({ ...s, [k]: u })); }).catch(() => undefined));
    return () => urls.forEach((u) => URL.revokeObjectURL(u));
  }, [id, v]);
  if (q.error) return <Page title="Website"><ErrorState error={q.error} /></Page>;
  if (!q.data) return <Page title="Website"><Loading /></Page>;
  const d = q.data;
  const dep = d.deployments.find((x) => x.status === "verified");
  return (
    <Page title={d.site.slug} subtitle={`state: ${d.site.state} · ${d.versions.length} versions · ${d.qa_runs.length} QA runs`}
      actions={dep?.url ? <a className="rounded-lg bg-signal-500 px-3 py-1.5 text-sm font-medium text-white" href={dep.url} target="_blank" rel="noreferrer">Open preview</a> : <Pill tone="muted">no verified preview</Pill>}>
      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <Card title="Screenshots (from QA run)">
          <div className="grid gap-3 md:grid-cols-[2fr_1fr]">
            {shot.desktop ? <img src={shot.desktop} alt="Desktop screenshot" className="max-h-[600px] w-full rounded-lg object-cover object-top" /> : <div className="text-sm text-ink-500">No desktop screenshot.</div>}
            {shot.mobile ? <img src={shot.mobile} alt="Mobile screenshot" className="max-h-[600px] w-full rounded-lg object-cover object-top" /> : <div className="text-sm text-ink-500">No mobile screenshot.</div>}
          </div>
        </Card>
        <div className="space-y-4">
          <Card title="Strategy">{v?.spec.strategy ? <dl className="space-y-2 text-sm">{Object.entries(v.spec.strategy).map(([k, val]) => <div key={k}><dt className="text-xs uppercase text-ink-500">{k}</dt><dd className="text-ink-200">{val}</dd></div>)}</dl> : "—"}
            {v && <p className="mt-2 text-xs text-ink-500">{v.model} · {v.prompt_version} · {v.design.archetype}/{v.design.palette}/{v.design.type_pair}</p>}</Card>
          <Card title="Deployments">{d.deployments.length === 0 ? <p className="text-sm text-ink-500">Not deployed.</p> : <ul className="space-y-2 text-sm">{d.deployments.map((x) => (
            <li key={x.id}><div className="flex items-center gap-2"><Pill>{x.status}</Pill><span className="truncate text-xs text-ink-400">{x.provider_deployment_id}</span></div>
              {x.url && <a className="break-all text-xs text-signal-300" href={x.url} target="_blank" rel="noreferrer">{x.url}</a>}<div className="text-[11px] text-ink-500">expires {when(x.expires_at)}</div></li>))}</ul>}</Card>
        </div>
        <Card title="QA history" className="lg:col-span-2">
          <ul className="space-y-3">{d.qa_runs.map((r) => (
            <li key={r.id} className="rounded-lg bg-ink-850 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2"><Pill>{r.status}</Pill><span className="text-xs text-ink-400">{r.runner} · {when(r.started_at)}</span>
                {r.metrics && Object.entries(r.metrics).map(([vp, m]) => <span key={vp} className="text-xs text-ink-500">{vp}: {m.load_ms}ms, {Math.round(m.transfer_bytes / 1024)}KB</span>)}</div>
              {(r.findings ?? []).length > 0 && <ul className="mt-2 space-y-1 text-xs">{(r.findings ?? []).map((f, i) => <li key={i}><Pill tone={f.severity === "critical" || f.severity === "serious" ? "bad" : "muted"}>{f.severity}</Pill> <span className="text-ink-300">{f.check_name}</span> <span className="text-ink-500">[{f.viewport}] {f.message}</span></li>)}</ul>}
            </li>))}</ul>
        </Card>
      </div>
    </Page>
  );
}
