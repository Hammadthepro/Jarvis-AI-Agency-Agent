import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Page } from "../components/Shell";
import { Button, Card, ErrorState, Input, Loading, Pill, usd, when } from "../components/ui";

type Integration = { provider: string; configured: boolean; state: string; tested: boolean; last_checked_at: string | null; detail: string | null };
type Limit = { id: string; scope: string; provider: string | null; unit: string; period: string; cap: number; free_allowance: number | null; allow_paid: boolean };
type Gate = { scope: string; open: boolean; reason: string | null; intended_use: string | null; approver_name?: string; document_ref?: string; review_by?: string };
type Usage = { summary: Array<{ provider: string; reconciled: boolean; unknown_spend: boolean;
  jarvis: { estimated_micros: number; reserved_micros: number; committed_micros: number; ambiguous_micros: number; released_micros: number; committed_units: number; calls: number };
  provider_reported: { status: string; detail?: string | null; unit?: string; reported_units?: number | null; reported_cost_micros?: number | null; source?: string; fetched_at?: string } }> };

export default function SettingsPage() {
  const qc = useQueryClient();
  const integ = useQuery({ queryKey: ["integrations"], queryFn: () => api<Integration[]>("/integrations") });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<{ settings: Record<string, unknown>; models: Record<string, string>; environment: string; qa_runner_mode: string; backup: string; preview_project: string }>("/settings") });
  const limits = useQuery({ queryKey: ["limits"], queryFn: () => api<Limit[]>("/settings/limits") });
  const compliance = useQuery({ queryKey: ["compliance"], queryFn: () => api<{ gates: Gate[] }>("/compliance") });
  const usage = useQuery({ queryKey: ["usage"], queryFn: () => api<Usage>("/usage") });
  const syncUsage = useMutation({ mutationFn: () => api("/usage/sync", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["usage"] }) });
  const test = useMutation({ mutationFn: (p: string) => api<{ state: string; detail?: string }>(`/integrations/${p}/test`, { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["integrations"] }) });
  const put = useMutation({ mutationFn: (b: Record<string, unknown>) => api("/settings", { method: "PUT", json: b }), onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }) });
  const [agency, setAgency] = useState({ agency_name: "", terms_of_use_url: "", privacy_policy_url: "" });
  const s = settings.data?.settings;
  return (
    <Page title="Settings" subtitle="Credentials are configured server-side as environment variables; they are never entered in or sent to the browser.">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Integrations" className="lg:col-span-2">
          {integ.error ? <ErrorState error={integ.error} /> : !integ.data ? <Loading /> : (
            <table className="w-full text-sm"><thead className="text-left text-xs uppercase text-ink-500"><tr><th className="py-1">Provider</th><th>Configured</th><th>State</th><th>Tested</th><th>Detail</th><th /></tr></thead>
              <tbody className="divide-y divide-ink-800">{integ.data.map((i) => (
                <tr key={i.provider}><td className="py-2">{i.provider}</td><td>{i.configured ? <Pill tone="ok">yes</Pill> : <Pill tone="warn">no</Pill>}</td><td><Pill>{i.state}</Pill></td>
                  <td className="text-xs text-ink-400">{i.tested ? `ok · ${when(i.last_checked_at)}` : "not tested"}</td><td className="max-w-sm text-xs text-ink-400">{i.detail}</td>
                  <td>{i.configured && <Button variant="ghost" onClick={() => test.mutate(i.provider)} disabled={test.isPending}>Test</Button>}</td></tr>))}</tbody></table>)}
          {test.data && <p className="mt-2 text-xs text-ink-300">Last test: {test.data.state} {test.data.detail ?? ""}</p>}
          {test.error && <ErrorState error={test.error} />}
        </Card>
        <Card title="Google Places compliance gate">
          {compliance.error ? <ErrorState error={compliance.error} /> : !compliance.data ? <Loading /> : (
            <ul className="space-y-3 text-sm">{compliance.data.gates.map((g) => (
              <li key={g.scope}><div className="flex items-center justify-between"><span className="font-mono text-xs">{g.scope}</span><Pill tone={g.open ? "ok" : "warn"}>{g.open ? "approved" : "blocked"}</Pill></div>
                <p className="mt-1 text-xs text-ink-400">{g.open ? `Approved by ${g.approver_name} (${g.document_ref}), review by ${when(g.review_by)}` : g.reason}</p>
                <p className="mt-1 text-[11px] italic text-ink-500">{g.intended_use}</p></li>))}</ul>)}
          <p className="mt-3 text-xs text-ink-500">Approvals are recorded by the workspace owner with a document reference from Google or qualified legal counsel (POST /compliance/approvals).</p>
        </Card>
        <Card title="Workspace & safety">
          {!s ? <Loading /> : (<div className="space-y-3 text-sm">
            <div className="flex items-center justify-between"><span>Global kill switch</span>
              <Button variant={s.global_kill_switch ? "danger" : "ghost"} onClick={() => put.mutate({ global_kill_switch: !s.global_kill_switch })}>{s.global_kill_switch ? "ON: click to resume" : "Off"}</Button></div>
            <div className="text-xs text-ink-400">Environment: {settings.data?.environment} · QA runner: {settings.data?.qa_runner_mode} · Preview project: {settings.data?.preview_project}</div>
            <div className="text-xs text-ink-400">Models: {Object.entries(settings.data?.models ?? {}).map(([k, v]) => `${k}=${v}`).join(" · ")}</div>
            <div className="flex items-center gap-2 text-xs">Backups: <Pill>{settings.data?.backup}</Pill></div>
            <form className="grid gap-2" onSubmit={(e) => { e.preventDefault(); put.mutate(Object.fromEntries(Object.entries(agency).filter(([, v]) => v))); }}>
              <Input placeholder={`Agency name (${String(s.agency_name ?? "unset")})`} value={agency.agency_name} onChange={(e) => setAgency({ ...agency, agency_name: e.target.value })} aria-label="Agency name" />
              <Input placeholder={`Terms of Use URL (${String(s.terms_of_use_url ?? "unset")})`} value={agency.terms_of_use_url} onChange={(e) => setAgency({ ...agency, terms_of_use_url: e.target.value })} aria-label="Terms URL" />
              <Input placeholder={`Privacy Policy URL (${String(s.privacy_policy_url ?? "unset")})`} value={agency.privacy_policy_url} onChange={(e) => setAgency({ ...agency, privacy_policy_url: e.target.value })} aria-label="Privacy URL" />
              <Button type="submit" variant="ghost">Save</Button>
            </form>{put.error && <ErrorState error={put.error} />}
          </div>)}
        </Card>
        <Card title="Usage this month: JARVIS ledger vs provider-reported" className="lg:col-span-2"
          actions={<Button variant="ghost" onClick={() => syncUsage.mutate()} disabled={syncUsage.isPending}>Sync now</Button>}>
          {usage.error ? <ErrorState error={usage.error} /> : !usage.data ? <Loading /> : (
            <div className="overflow-x-auto"><table className="w-full text-sm"><thead className="text-left text-xs uppercase text-ink-500"><tr>
              <th className="py-1">Provider</th><th className="text-right">Estimated</th><th className="text-right">Reserved</th><th className="text-right">Committed</th>
              <th className="text-right">Ambiguous</th><th className="text-right">Released</th><th className="px-3">Provider-reported</th></tr></thead>
              <tbody className="divide-y divide-ink-800">{usage.data.summary.map((u) => (
                <tr key={u.provider}><td className="py-2">{u.provider}</td>
                  <td className="text-right tabular-nums">{usd(u.jarvis.estimated_micros)}</td><td className="text-right tabular-nums">{usd(u.jarvis.reserved_micros)}</td>
                  <td className="text-right tabular-nums">{usd(u.jarvis.committed_micros)}</td><td className="text-right tabular-nums">{usd(u.jarvis.ambiguous_micros)}</td>
                  <td className="text-right tabular-nums">{usd(u.jarvis.released_micros)}</td>
                  <td className="px-3 text-xs"><Pill tone={u.reconciled ? "ok" : "warn"}>{u.provider_reported.status}</Pill>{" "}
                    {u.reconciled ? <span className="text-ink-300">{u.provider_reported.reported_units ?? "—"} {u.provider_reported.unit} · {u.provider_reported.source} · {when(u.provider_reported.fetched_at)}</span>
                      : <span className="text-warn-500">UNKNOWN (not zero){u.unknown_spend ? " · spend recorded but unreconciled" : ""}</span>}
                    {u.provider_reported.detail && <div className="text-[11px] text-ink-500">{u.provider_reported.detail}</div>}</td></tr>))}</tbody></table></div>)}
          <p className="mt-2 text-xs text-ink-500">Estimated = worst case reserved before each call. Ambiguous calls (timeouts) stay counted until reconciled.</p>
        </Card>
        <Card title="Budget limits" className="lg:col-span-2">
          {limits.error ? <ErrorState error={limits.error} /> : !limits.data ? <Loading /> : <LimitsTable rows={limits.data} />}
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
