import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { useIntegrations } from "../lib/integrations";
import { requestMicrophone, type VoiceStatus } from "../lib/voice";
import { Button, Card, ErrorState, Input, Loading, Pill } from "./ui";
import { IconCheck } from "./icons";

const CHECK_LABEL: Record<string, string> = {
  livekit_configured: "LiveKit credentials configured", livekit_validated: "LiveKit connection validated",
  agent_secret_configured: "Voice agent secret set on the server", agent_online: "JARVIS voice agent process running",
  agent_registered: "Voice agent registered with LiveKit", agent_job_process_ready: "Voice agent can start sessions (job process ready)",
  cartesia_configured: "Cartesia voice configured (JARVIS's speech)",
};

function field(i: { fields: Array<{ name: string; display: string | null; default: string | null; set: boolean }> } | undefined, name: string) {
  const f = i?.fields.find((x) => x.name === name);
  return f ? (f.display ?? f.default ?? (f.set ? "set" : null)) : null;
}

/** Voice: everything needed to talk to JARVIS (speech provider, transport, microphone) and a test. */
export function VoiceSettings() {
  const status = useQuery({ queryKey: ["voice-status"], queryFn: () => api<VoiceStatus>("/voice/status"), refetchInterval: 15_000 });
  const integ = useIntegrations();
  const list = integ.data?.integrations ?? [];
  const cartesia = list.find((i) => i.provider === "cartesia");
  const livekit = list.find((i) => i.provider === "livekit");
  const [mic, setMic] = useState<{ permission: string; inputs: number | null; tested?: string }>({ permission: "unknown", inputs: null });
  useEffect(() => {
    void (async () => {
      let permission = "unknown", inputs: number | null = null;
      try { permission = (await navigator.permissions?.query({ name: "microphone" as PermissionName }))?.state ?? "unknown"; } catch { /* not supported */ }
      try { inputs = (await navigator.mediaDevices?.enumerateDevices())?.filter((d) => d.kind === "audioinput").length ?? null; } catch { /* not supported */ }
      setMic({ permission, inputs });
    })();
  }, []);
  const ready = status.data?.state === "OPERATIONAL";
  const { hash } = useLocation();
  const loaded = !!status.data;
  useEffect(() => { if (hash === "#voice" && loaded) document.getElementById("voice")?.scrollIntoView({ block: "start" }); }, [hash, loaded]);
  return (
    <Card title="Voice" id="voice" className="lg:col-span-2">
      {status.error ? <ErrorState error={status.error} /> : !status.data ? <Loading /> : (
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <div className="mb-2 flex items-center gap-2"><Pill tone={ready ? "ok" : "warn"}>{status.data.state}</Pill>
              <span className="text-xs text-ink-400">voice readiness, checked by the API</span></div>
            <ul className="space-y-1.5 text-sm">{Object.entries(status.data.checks).map(([k, ok]) => (
              <li key={k} className="flex items-center gap-2">
                <span className={`grid h-4 w-4 place-items-center rounded-full ${ok ? "bg-ok-500/20 text-ok-500" : "bg-ink-700 text-ink-500"}`}>{ok ? <IconCheck className="h-3 w-3" /> : null}</span>
                <span className={ok ? "text-ink-200" : "text-ink-400"}>{CHECK_LABEL[k] ?? k}</span>
              </li>))}</ul>
            {status.data.missing.length > 0 && (
              <ul className="mt-3 space-y-1 rounded-lg bg-warn-500/10 p-2.5 text-xs text-ink-200">{status.data.missing.map((m) => <li key={m}>• {m}</li>)}</ul>)}
          </div>
          <div className="space-y-2 text-sm">
            <Row k="Speech (text-to-speech)" v={<>Cartesia <Pill tone={cartesia?.state === "CONNECTED" ? "ok" : cartesia?.state === "NOT_CONFIGURED" ? "warn" : "muted"}>{cartesia?.state ?? "…"}</Pill></>} />
            <Row k="Cartesia model" v={field(cartesia, "model") ?? "sonic-3"} />
            <Row k="Cartesia voice" v={<span className="font-mono text-xs">{field(cartesia, "voice_id") ?? "f786b574-daa5-4673-aa0c-cbe3e8534c02 (Katie)"}</span>} />
            <Row k="Speaking speed" v={field(cartesia, "speed") ?? "1.0"} />
            <Row k="Speech recognition" v="Deepgram Nova-3 (streaming, via LiveKit; VOICE_STT_MODEL)" />
            <Row k="Voice transport" v={<>LiveKit <Pill tone={livekit?.state === "CONNECTED" ? "ok" : "warn"}>{livekit?.state ?? "…"}</Pill></>} />
            <Row k="Microphone" v={`${mic.permission === "granted" ? "allowed" : mic.permission === "denied" ? "blocked in this browser" : "not asked yet"}${mic.inputs != null ? ` · ${mic.inputs} input device(s)` : ""}${mic.tested ? ` · test: ${mic.tested}` : ""}`} />
            {cartesia?.state === "NOT_CONFIGURED" && <p className="text-xs text-warn-500">JARVIS cannot speak until a Cartesia API key is saved. No other voice is used instead.</p>}
            <div className="flex flex-wrap gap-2 pt-1">
              <Button variant="ghost" onClick={async () => {
                setMic((m) => ({ ...m, tested: "…" }));
                const r = await requestMicrophone();
                setMic((m) => ({ ...m, tested: r, permission: r === "granted" ? "granted" : m.permission }));
              }}>Check microphone</Button>
              {ready ? <Link to="/" className="rounded-lg bg-signal-500 px-3 py-1.5 text-sm text-white">Test voice in the workspace (press the mic)</Link>
                : <Pill tone="muted">voice test available when voice is operational</Pill>}
              <Link to="/integrations" className="self-center text-xs text-signal-300 underline">Credentials (Integrations)</Link>
            </div>
          </div>
        </div>)}
    </Card>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return <div className="flex items-center justify-between gap-3 border-b hairline pb-1.5"><span className="text-ink-400">{k}</span><span className="text-right text-ink-200">{v}</span></div>;
}

type Provider = { provider: string; label: string; available: boolean; reason: string | null; fix: string | null };
type Evaluation = { id: string; status: string; providers: string[]; plan: { cities: Array<{ city: string; state: string }>; categories: string[]; limit: number };
  created_at: string; finished_at: string | null;
  results: { providers?: Record<string, { status: string; reason?: string; requests?: number; latency_ms?: { avg: number; max: number } | null;
    totals?: Record<string, number>; failures?: unknown[]; estimated_cost_usd?: number; cost_per_useful_usd?: number | null }>;
    overlap?: Record<string, Record<string, number>>; error?: string } };

/** Discovery providers: each one's own status, and the controlled side-by-side evaluation (real requests, aggregates only). */
export function DiscoverySettings() {
  const qc = useQueryClient();
  const prov = useQuery({ queryKey: ["discovery-providers"], queryFn: () => api<{ providers: Provider[] }>("/discovery/providers") });
  const evals = useQuery({ queryKey: ["evaluations"], queryFn: () => api<Evaluation[]>("/discovery/evaluations"), refetchInterval: 5000 });
  const [form, setForm] = useState({ city: "Anaheim", state: "CA", categories: "dentist", providers: ["google_places", "geoapify", "serp_api"], confirm: false });
  const run = useMutation({
    mutationFn: () => api("/discovery/evaluations", { method: "POST", json: { providers: form.providers, cities: [{ city: form.city, state: form.state }],
      categories: form.categories.split(",").map((c) => c.trim()).filter(Boolean).slice(0, 3), limit: 20, confirm_cost: form.confirm } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["evaluations"] }),
  });
  const last = Array.isArray(evals.data) ? evals.data[0] : undefined;
  return (
    <Card title="Discovery providers" className="lg:col-span-2">
      {prov.error ? <ErrorState error={prov.error} /> : !prov.data ? <Loading /> : (
        <ul className="grid gap-2 md:grid-cols-2">{prov.data.providers.map((p) => (
          <li key={p.provider} className="rounded-lg border hairline p-2.5 text-sm">
            <div className="flex items-center justify-between gap-2"><span className="text-ink-100">{p.label}</span>
              <Pill tone={p.available ? "ok" : "warn"}>{p.available ? "available" : "unavailable"}</Pill></div>
            {!p.available && <p className="mt-1 text-xs text-ink-400">{p.reason}{p.fix ? ` ${p.fix}` : ""}</p>}
          </li>))}</ul>)}
      <h3 className="mt-4 text-xs uppercase tracking-wider text-ink-400">Provider comparison</h3>
      {!last ? <p className="mt-1 text-sm text-ink-500">Not evaluated yet. Results below come only from real requests; nothing is estimated for a provider that did not run.</p> : (
        <div className="mt-2 overflow-x-auto">
          <p className="mb-1 text-xs text-ink-400">{last.status} · {last.plan.cities.map((c) => `${c.city}, ${c.state}`).join("; ")} · {last.plan.categories.join(", ")} · up to {last.plan.limit} each</p>
          <table className="w-full text-left text-xs tabular-nums">
            <thead className="text-ink-500"><tr><th>Provider</th><th>Status</th><th>Returned</th><th>Unique</th><th>Dupes</th><th>Website</th><th>Phone</th><th>Address</th><th>Category</th><th>Avg ms</th><th>Est. $</th><th>$ / useful</th></tr></thead>
            <tbody>{Object.entries(last.results.providers ?? {}).map(([k, r]) => (
              <tr key={k} className="text-ink-300"><td>{k}</td><td><Pill tone={r.status === "COMPLETED" ? "ok" : "warn"}>{r.status}</Pill></td>
                {r.status === "COMPLETED" && r.totals ? (<>
                  <td>{r.totals.returned}</td><td>{r.totals.unique}</td><td>{r.totals.duplicates}</td><td>{r.totals.website_listed_pct}%</td>
                  <td>{r.totals.phone_listed_pct}%</td><td>{r.totals.address_complete_pct}%</td><td>{r.totals.category_match_pct}%</td>
                  <td>{r.latency_ms?.avg ?? "–"}</td><td>{r.estimated_cost_usd}</td><td>{r.cost_per_useful_usd ?? "–"}</td></>)
                  : <td colSpan={10} className="text-ink-500">{r.reason ?? "no results"}</td>}
              </tr>))}</tbody></table>
          {last.results.overlap && Object.entries(last.results.overlap).map(([k, o]) => (
            <p key={k} className="mt-1 text-xs text-ink-400">{k.replace("~", " vs ")}: {Object.entries(o).map(([a, b]) => `${a.replace(/_/g, " ")} ${b}`).join(" · ")}</p>))}
          <p className="mt-1 text-[11px] text-ink-500">“Address” is completeness (street number + postcode), not verified accuracy. Website/phone are what the provider listed; JARVIS verifies websites independently.</p>
        </div>)}
      <form className="mt-3 grid gap-2 md:grid-cols-[1fr_80px_1fr_auto]" onSubmit={(e) => { e.preventDefault(); run.mutate(); }}>
        <Input aria-label="City" value={form.city} onChange={(e) => setForm({ ...form, city: e.target.value })} />
        <Input aria-label="State" value={form.state} onChange={(e) => setForm({ ...form, state: e.target.value.toUpperCase().slice(0, 2) })} />
        <Input aria-label="Categories (comma separated, up to 3)" value={form.categories} onChange={(e) => setForm({ ...form, categories: e.target.value })} />
        <Button type="submit" disabled={!form.confirm || run.isPending}>Run comparison</Button>
        <label className="flex items-center gap-2 text-xs text-ink-300 md:col-span-4">
          <input type="checkbox" checked={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.checked })} />
          I understand this sends real requests to each available provider (at most 3 cities × 3 categories; may use paid units within the budget caps).
        </label>
      </form>
      {run.error && <ErrorState error={run.error} />}
    </Card>
  );
}
