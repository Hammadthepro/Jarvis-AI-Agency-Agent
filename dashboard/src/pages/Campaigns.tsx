import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import { api } from "../lib/api";
import { subscribeEvents, type JarvisEvent } from "../lib/events";
import { Page } from "../components/Shell";
import Console from "./Console";
import { Button, Card, Empty, ErrorState, Input, Loading, Pill, usd, when } from "../components/ui";

type Campaign = { id: string; name: string; state: string; current_stage: string | null; blocked_reason: string | null; discovery_provider: string;
  target_count: number; prospects: number; qualified: number; deployed: number; spent_micros: number; minimum_google_rating: string | null;
  website_filter: string[]; created_at: string };

export default function CampaignsPage() {
  const q = useQuery({ queryKey: ["campaigns"], queryFn: () => api<Campaign[]>("/campaigns"), refetchInterval: 5000 });
  const [showImport, setShowImport] = useState(false);
  const [sp] = useSearchParams();
  const [structured, setStructured] = useState(sp.get("structured") === "1");
  return (
    <Page title="Campaigns" subtitle="Talk to JARVIS on the main page. Here: every campaign, a structured command with explicit parameters, or an import."
      actions={<div className="flex gap-2"><Button variant="ghost" onClick={() => setStructured((s) => !s)}>{structured ? "Close structured command" : "Structured command"}</Button>
        <Button variant="ghost" onClick={() => setShowImport((s) => !s)}>{showImport ? "Close import" : "Import business list"}</Button></div>}>
      {structured && <div className="-mx-4 mb-4 md:-mx-8"><Console /></div>}
      {showImport && <ImportForm onDone={() => setShowImport(false)} />}
      {q.error ? <ErrorState error={q.error} /> : !q.data ? <Loading /> : q.data.length === 0 ? (
        <Empty title="No campaigns yet">Ask JARVIS, e.g. “Find 25 roofers in Denver with no website”.</Empty>
      ) : (
        <div className="overflow-x-auto rounded-xl border hairline">
          <table className="w-full text-sm">
            <thead className="bg-ink-900 text-left text-xs uppercase tracking-wider text-ink-400">
              <tr><th className="px-4 py-2">Campaign</th><th>State</th><th>Stage</th><th>Source</th><th className="text-right">Prospects</th>
                <th className="text-right">Qualified</th><th className="text-right">Previews</th><th className="px-4 text-right">Spend (est.)</th></tr>
            </thead>
            <tbody className="divide-y divide-ink-800">
              {q.data.map((c) => (
                <tr key={c.id} className="hover:bg-ink-900/60">
                  <td className="px-4 py-2"><Link className="text-ink-100 hover:underline" to={`/campaigns/${c.id}`}>{c.name}</Link>
                    <div className="text-xs text-ink-500">target {c.target_count}{c.minimum_google_rating ? ` · rating ≥ ${c.minimum_google_rating}` : ""} · {c.website_filter.join(", ")}</div></td>
                  <td><Pill>{c.state}</Pill></td><td className="text-ink-300">{c.current_stage ?? "—"}</td><td className="text-ink-400">{c.discovery_provider}</td>
                  <td className="text-right tabular-nums">{c.prospects}</td><td className="text-right tabular-nums">{c.qualified}</td>
                  <td className="text-right tabular-nums">{c.deployed}</td><td className="px-4 text-right tabular-nums">{usd(c.spent_micros)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Page>
  );
}

const Row = z.object({ business_name: z.string().min(2), phone: z.string().optional(), city: z.string().optional(), state: z.string().length(2).optional(), website: z.string().url().optional() });
export const ImportSchema = z.object({
  niche: z.string().min(2), city: z.string().min(2), state: z.string().length(2), rights: z.literal(true, { errorMap: () => ({ message: "Attest that you may use this list" }) }),
  rows: z.array(Row).min(1).max(1000),
});

export function parseCsv(text: string) {
  const lines = text.trim().split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return [];
  const head = lines[0].split(",").map((h) => h.trim().toLowerCase());
  return lines.slice(1).map((l) => {
    const cells = l.split(",").map((c) => c.trim());
    const o: Record<string, string> = {};
    head.forEach((h, i) => { if (cells[i]) o[h] = cells[i]; });
    return o;
  });
}

function ImportForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState({ niche: "", city: "", state: "", csv: "business_name,phone,city,state,website\n", rights: false, sites: 0, noSiteOnly: true });
  const [err, setErr] = useState<string | null>(null);
  const m = useMutation({
    mutationFn: (body: unknown) => api<{ campaign_id: string }>("/campaigns/import", { method: "POST", json: body }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["campaigns"] }); onDone(); },
  });
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const rows = parseCsv(form.csv);
    const r = ImportSchema.safeParse({ niche: form.niche, city: form.city, state: form.state.toUpperCase(), rights: form.rights, rows });
    if (!r.success) { setErr(r.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`)[0]); return; }
    setErr(null);
    m.mutate({ niche: r.data.niche, location: { city: r.data.city, state: r.data.state }, rows: r.data.rows, rights_attestation: true,
      website_filter: form.noSiteOnly ? ["NO_WEBSITE", "WEBSITE_BROKEN"] : ["ANY"], sites_to_build: form.sites });
  };
  return (
    <Card title="Import business list" className="mb-6">
      <form onSubmit={submit} className="grid gap-3 md:grid-cols-3">
        <div className="text-sm text-ink-300"><label htmlFor="imp-niche">Business type</label><Input id="imp-niche" value={form.niche} onChange={(e) => setForm({ ...form, niche: e.target.value })} /></div>
        <div className="text-sm text-ink-300"><label htmlFor="imp-city">City</label><Input id="imp-city" value={form.city} onChange={(e) => setForm({ ...form, city: e.target.value })} /></div>
        <div className="text-sm text-ink-300"><label htmlFor="imp-state">State (2 letters)</label><Input id="imp-state" value={form.state} maxLength={2} onChange={(e) => setForm({ ...form, state: e.target.value })} /></div>
        <label className="text-sm text-ink-300 md:col-span-3">CSV (business_name, phone, city, state, website)
          <textarea aria-label="CSV rows" value={form.csv} onChange={(e) => setForm({ ...form, csv: e.target.value })} rows={6}
            className="mt-1 w-full rounded-lg border hairline bg-ink-850 p-2 font-mono text-xs text-ink-100" /></label>
        <label className="flex items-center gap-2 text-sm text-ink-300"><input type="checkbox" checked={form.noSiteOnly} onChange={(e) => setForm({ ...form, noSiteOnly: e.target.checked })} />
          Keep only verified no-website / broken-website businesses</label>
        <label className="text-sm text-ink-300">Sites to build<Input aria-label="Sites to build" type="number" min={0} max={50} value={form.sites} onChange={(e) => setForm({ ...form, sites: Number(e.target.value) })} /></label>
        <label className="flex items-center gap-2 text-sm text-ink-300 md:col-span-3"><input type="checkbox" checked={form.rights} onChange={(e) => setForm({ ...form, rights: e.target.checked })} />
          I confirm I have the right to use this business list for prospecting.</label>
        <div className="md:col-span-3 flex items-center gap-3"><Button type="submit" disabled={m.isPending}>Create campaign</Button>
          {err && <span className="text-sm text-bad-500">{err}</span>}</div>
        {m.error && <div className="md:col-span-3"><ErrorState error={m.error} /></div>}
      </form>
    </Card>
  );
}

type Detail = { campaign: Campaign & { location: { city?: string; state?: string }; stages: string[]; budget_cap_micros: number; missing_rating_policy: string | null };
  funnel: Array<{ state: string; n: number }>; website: Array<{ status: string; site_health: string | null; n: number }>;
  tasks: Array<{ task_type: string; status: string; n: number }>; costs: Array<{ provider: string; status: string; micros: number; units: number }> };

export function CampaignDetailPage() {
  const { id } = useParams();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["campaign", id], queryFn: () => api<Detail>(`/campaigns/${id}`), refetchInterval: 4000 });
  const [events, setEvents] = useState<JarvisEvent[]>([]);
  useEffect(() => subscribeEvents((e) => {
    setEvents((p) => [e, ...p].slice(0, 100));
    if (e.event_type === "state" || e.event_type === "error") void qc.invalidateQueries({ queryKey: ["campaign", id] });
  }, { campaignId: id }), [id, qc]);
  const act = useMutation({ mutationFn: (a: string) => api(`/campaigns/${id}/${a}`, { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["campaign", id] }) });
  if (q.error) return <Page title="Campaign"><ErrorState error={q.error} /></Page>;
  if (!q.data) return <Page title="Campaign"><Loading /></Page>;
  const c = q.data.campaign;
  return (
    <Page title={c.name} subtitle={`${c.discovery_provider} · target ${c.target_count} · stages: ${c.stages.join(" → ")}`}
      actions={<div className="flex gap-2">
        {["running", "authorized"].includes(c.state) && <Button variant="ghost" onClick={() => act.mutate("pause")}>Pause</Button>}
        {["paused", "blocked"].includes(c.state) && <Button variant="ghost" onClick={() => act.mutate("resume")}>Resume</Button>}
        {!["completed", "cancelled"].includes(c.state) && <Button variant="danger" onClick={() => act.mutate("cancel")}>Cancel</Button>}
      </div>}>
      {act.error && <ErrorState error={act.error} />}
      <div className="mb-4 flex flex-wrap items-center gap-3"><Pill>{c.state}</Pill><span className="text-sm text-ink-400">stage: {c.current_stage ?? "—"}</span>
        {c.minimum_google_rating && <span className="text-sm text-ink-400">Google rating ≥ {c.minimum_google_rating} (unrated: {c.missing_rating_policy})</span>}</div>
      {c.blocked_reason && <div className="mb-4"><ErrorState error={new Error(c.blocked_reason)} /></div>}
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Prospect funnel"><Counts rows={q.data.funnel.map((f) => [f.state, f.n])} /></Card>
        <Card title="Website verification"><Counts rows={q.data.website.map((w) => [w.status + (w.site_health ? ` · ${w.site_health}` : ""), w.n])} /></Card>
        <Card title="Cost (estimated)"><Counts rows={q.data.costs.map((x) => [`${x.provider} · ${x.status}`, usd(x.micros)])} />
          <p className="mt-2 text-xs text-ink-500">Cap {usd(c.budget_cap_micros)}. Ambiguous calls stay counted until reconciled.</p></Card>
        <Card title="Tasks" className="lg:col-span-1"><Counts rows={q.data.tasks.map((t) => [`${t.task_type} · ${t.status}`, t.n])} /></Card>
        <Card title="Progress" className="lg:col-span-2">
          <ul className="max-h-80 space-y-1 overflow-y-auto scroll-thin text-sm">{events.map((e) => (
            <li key={e.id} className="flex gap-3"><span className="w-20 shrink-0 text-xs text-ink-500">{new Date(e.created_at).toLocaleTimeString()}</span><span className="text-ink-200">{e.message}</span></li>))}
            {events.length === 0 && <li className="text-ink-500">No recent events.</li>}</ul>
        </Card>
      </div>
      <div className="mt-4 flex gap-4 text-sm"><Link className="text-signal-300 hover:underline" to={`/prospects?campaign=${c.id}`}>View prospects</Link>
        <Link className="text-signal-300 hover:underline" to={`/websites?campaign=${c.id}`}>View websites</Link><span className="text-ink-500">Created {when(c.created_at)}</span></div>
    </Page>
  );
}

function Counts({ rows }: { rows: Array<[string, React.ReactNode]> }) {
  if (!rows.length) return <p className="text-sm text-ink-500">Nothing yet.</p>;
  return <ul className="space-y-1 text-sm">{rows.map(([k, v]) => <li key={k} className="flex justify-between gap-3"><span className="text-ink-300">{k}</span><span className="tabular-nums text-ink-100">{v}</span></li>)}</ul>;
}
