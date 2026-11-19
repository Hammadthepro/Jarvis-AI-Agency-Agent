import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Page } from "../components/Shell";
import { Button, Card, ErrorState, Loading, Pill, Stat, usd, when } from "../components/ui";

type Limit = { id: string; scope: string; provider: string | null; unit: string; period: string; cap: number; free_allowance: number | null; allow_paid: boolean };
type Usage = { summary: Array<{ provider: string; reconciled: boolean; unknown_spend: boolean; invoice_status: string; jarvis_cost_basis: string;
  jarvis: { estimated_micros: number; reserved_micros: number; committed_micros: number; ambiguous_micros: number; released_micros: number; committed_units: number; calls: number; response_tokens?: number };
  provider_reported: { status: string; detail?: string | null; unit?: string; reported_units?: number | null; reported_cost_micros?: number | null; source?: string; fetched_at?: string } }> };

type Campaign = { id: string; name: string; state: string; spent_micros: number; budget_cap_micros: number };

export default function UsagePage() {
  const qc = useQueryClient();
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => api<Usage>("/usage") });
  const limits = useQuery({ queryKey: ["limits"], queryFn: () => api<Limit[]>("/settings/limits") });
  const campaigns = useQuery({ queryKey: ["campaigns"], queryFn: () => api<Campaign[]>("/campaigns") });
  const syncUsage = useMutation({ mutationFn: () => api("/usage/sync", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["usage"] }) });
  const totals = (usage.data?.summary ?? []).reduce((a, u) => ({ committed: a.committed + u.jarvis.committed_micros, ambiguous: a.ambiguous + u.jarvis.ambiguous_micros,
    reserved: a.reserved + u.jarvis.reserved_micros, unknown: a.unknown + (u.reconciled ? 0 : 1) }), { committed: 0, ambiguous: 0, reserved: 0, unknown: 0 });
  return (
    <Page title="Usage and costs" subtitle="What JARVIS has spent this month, what providers report, and the caps that stop billable calls before they are sent.">
      <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Committed (local est.)" value={usd(totals.committed)} hint="Priced by JARVIS, not an invoice" />
        <Stat label="Ambiguous" value={usd(totals.ambiguous)} hint="Outcome unknown; still counted" />
        <Stat label="Reserved now" value={usd(totals.reserved)} hint="Calls in flight" />
        <Stat label="Unreconciled providers" value={totals.unknown} hint="Account usage UNKNOWN, not zero" />
      </div>
      <div className="grid gap-4">
        <Card title="Usage this month: JARVIS ledger vs provider-reported" className="lg:col-span-2"
          actions={<Button variant="ghost" onClick={() => syncUsage.mutate()} disabled={syncUsage.isPending}>Sync now</Button>}>
          {usage.error ? <ErrorState error={usage.error} /> : !usage.data ? <Loading /> : (
            <div className="overflow-x-auto"><table className="w-full text-sm"><thead className="text-left text-xs uppercase text-ink-500"><tr>
              <th className="py-1">Provider</th><th className="text-right">Estimated</th><th className="text-right">Reserved</th><th className="text-right" title="Units reported by the provider per response, priced by JARVIS. A local estimate, not an invoice.">Committed (local est.)</th>
              <th className="text-right">Ambiguous</th><th className="text-right">Released</th><th className="px-3">Provider-reported (account)</th><th className="px-3">Invoice</th></tr></thead>
              <tbody className="divide-y divide-ink-800">{usage.data.summary.map((u) => (
                <tr key={u.provider}><td className="py-2">{u.provider}</td>
                  <td className="text-right tabular-nums">{usd(u.jarvis.estimated_micros)}</td><td className="text-right tabular-nums">{usd(u.jarvis.reserved_micros)}</td>
                  <td className="text-right tabular-nums">{usd(u.jarvis.committed_micros)}</td><td className="text-right tabular-nums">{usd(u.jarvis.ambiguous_micros)}</td>
                  <td className="text-right tabular-nums">{usd(u.jarvis.released_micros)}</td>
                  <td className="px-3 text-xs"><Pill tone={u.reconciled ? "ok" : "warn"}>{u.provider_reported.status}</Pill>{" "}
                    {u.reconciled ? <span className="text-ink-300">{u.provider_reported.reported_units ?? "—"} {u.provider_reported.unit} · {u.provider_reported.source} · {when(u.provider_reported.fetched_at)}</span>
                      : <span className="text-warn-500">UNKNOWN (not zero){u.unknown_spend ? " · spend recorded but unreconciled" : ""}</span>}
                    {u.provider_reported.detail && <div className="text-[11px] text-ink-500">{u.provider_reported.detail}</div>}</td>
                  <td className="px-3 text-xs"><Pill tone={u.invoice_status === "CONFIRMED" ? "ok" : "warn"}>{u.invoice_status}</Pill></td></tr>))}</tbody></table></div>)}
          <p className="mt-2 text-xs text-ink-500">All JARVIS amounts are local estimates from the configured price table, not provider invoices. Estimated = worst case reserved before each call. Ambiguous calls (timeouts) stay counted until reconciled. Invoice is CONFIRMED only for an imported provider billing export.</p>
        </Card>
        <Card title="Budget limits" className="lg:col-span-2">
          {limits.error ? <ErrorState error={limits.error} /> : !limits.data ? <Loading /> : <LimitsTable rows={limits.data} />}
        </Card>
        <Card title="Spend by campaign (local estimate)">
          {campaigns.error ? <ErrorState error={campaigns.error} /> : !campaigns.data ? <Loading /> : campaigns.data.length === 0 ? <p className="text-sm text-ink-500">No campaigns yet.</p> : (
            <ul className="divide-y divide-ink-800 text-sm">{campaigns.data.slice(0, 20).map((c) => (
              <li key={c.id} className="flex items-center justify-between py-2"><span className="truncate">{c.name}</span>
                <span className="flex items-center gap-2 tabular-nums text-ink-300">{usd(c.spent_micros)} / cap {usd(c.budget_cap_micros)}<Pill>{c.state}</Pill></span></li>))}</ul>)}
        </Card>
      </div>
    </Page>
  );
}

function LimitsTable({ rows }: { rows: Limit[] }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState<Record<string, string>>({});
  const m = useMutation({ mutationFn: ({ id, cap, allow_paid }: { id: string; cap: number; allow_paid: boolean }) => api(`/settings/limits/${id}`, { method: "PUT", json: { cap, allow_paid } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["limits"] }) });
  return (<>
    <table className="w-full text-sm"><thead className="text-left text-xs uppercase text-ink-500"><tr><th className="py-1">Scope</th><th>Unit</th><th>Period</th><th className="text-right">Cap</th><th className="text-right">Free allowance</th><th className="px-3">Change</th></tr></thead>
      <tbody className="divide-y divide-ink-800">{rows.map((l) => (
        <tr key={l.id}><td className="py-2">{l.scope}{l.provider ? ` · ${l.provider}` : ""}</td><td className="text-ink-400">{l.unit}</td><td className="text-ink-400">{l.period}</td>
          <td className="text-right tabular-nums">{l.unit === "usd_micros" ? usd(l.cap) : l.cap}</td><td className="text-right tabular-nums text-ink-400">{l.free_allowance ?? "—"}</td>
          <td className="px-3"><form className="flex gap-1" onSubmit={(e) => { e.preventDefault(); const raw = Number(edit[l.id]); if (!Number.isFinite(raw) || raw < 0) return;
            const cap = l.unit === "usd_micros" ? Math.round(raw * 1e6) : Math.round(raw);
            const paid = l.free_allowance != null && cap > l.free_allowance ? window.confirm("This cap exceeds the free allowance and permits paid usage. Continue?") : false;
            m.mutate({ id: l.id, cap, allow_paid: paid }); }}>
            <input aria-label={`New cap for ${l.unit}`} className="w-24 rounded border hairline bg-ink-950 px-2 py-0.5 text-xs" placeholder={l.unit === "usd_micros" ? "USD" : "units"} value={edit[l.id] ?? ""} onChange={(e) => setEdit({ ...edit, [l.id]: e.target.value })} />
            <Button type="submit" variant="ghost">Set</Button></form></td></tr>))}</tbody></table>
    {m.error && <ErrorState error={m.error} />}
    <p className="mt-2 text-xs text-ink-500">Every billable call reserves its worst-case cost first; a call that would exceed any cap is not sent. Google Cloud budget alerts are warnings, not caps.</p>
  </>);
}
