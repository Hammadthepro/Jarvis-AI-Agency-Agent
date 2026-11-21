import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "../lib/api";
import { Page } from "../components/Shell";
import { Button, Card, ErrorState, Input, Loading, Pill, when } from "../components/ui";
import { DiscoverySettings, VoiceSettings } from "../components/SettingsSections";

type Gate = { scope: string; open: boolean; reason: string | null; intended_use: string | null; approver_name?: string; document_ref?: string; review_by?: string };

export default function SettingsPage() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<{ settings: Record<string, unknown>; models: Record<string, string>; environment: string; qa_runner_mode: string; backup: string; preview_project: string }>("/settings") });
  const compliance = useQuery({ queryKey: ["compliance"], queryFn: () => api<{ gates: Gate[] }>("/compliance") });
  const put = useMutation({ mutationFn: (b: Record<string, unknown>) => api("/settings", { method: "PUT", json: b }), onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }) });
  const [agency, setAgency] = useState({ agency_name: "", terms_of_use_url: "", privacy_policy_url: "" });
  const s = settings.data?.settings;
  return (
    <Page title="Settings" subtitle="Voice, discovery providers, workspace details, safety switches and the Google Places compliance gate.">
      <div className="grid gap-4 lg:grid-cols-2">
        <VoiceSettings />
        <DiscoverySettings />
        <Card title="Integrations and costs" className="lg:col-span-2">
          <p className="text-sm text-ink-300">Provider credentials and connection tests live in the <Link className="text-signal-300 underline" to="/integrations">Integrations Center</Link>.
            Spend, provider-reported usage and budget caps are under <Link className="text-signal-300 underline" to="/usage">Usage and costs</Link>.</p>
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
      </div>
    </Page>
  );
}
