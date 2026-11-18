import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Page } from "../components/Shell";
import { Button, Card, Empty, ErrorState, Input, Loading, Pill, when } from "../components/ui";
import GooglePlacesListingPanel from "../components/GooglePlacesListingPanel";

type Row = { id: string; business_name: string | null; state: string; review_reason: string | null; website_status: string | null; site_health: string | null;
  website_confidence: string | null; score: number | null; preview_url: string | null; outreach_state: string | null; discovery_provider: string; has_google_place: boolean };

export default function ProspectsPage() {
  const [sp] = useSearchParams();
  const [filter, setFilter] = useState({ q: "", state: "", website: "" });
  const qs = new URLSearchParams({ ...(sp.get("campaign") ? { campaign_id: sp.get("campaign")! } : {}), ...(filter.state ? { state: filter.state } : {}),
    ...(filter.website ? { website: filter.website } : {}), ...(filter.q ? { q: filter.q } : {}) }).toString();
  const q = useQuery({ queryKey: ["prospects", qs], queryFn: () => api<Row[]>(`/prospects?${qs}`) });
  return (
    <Page title="Prospects" subtitle="Business details shown here come only from independently verified sources.">
      <div className="mb-4 grid gap-2 md:grid-cols-3">
        <Input placeholder="Search business name" value={filter.q} onChange={(e) => setFilter({ ...filter, q: e.target.value })} aria-label="Search" />
        <select aria-label="State" className="rounded-lg border hairline bg-ink-850 px-3 py-2 text-sm" value={filter.state} onChange={(e) => setFilter({ ...filter, state: e.target.value })}>
          <option value="">All states</option>{["discovered", "website_verified", "researched", "qualified", "rejected", "requires_human_review"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <select aria-label="Website status" className="rounded-lg border hairline bg-ink-850 px-3 py-2 text-sm" value={filter.website} onChange={(e) => setFilter({ ...filter, website: e.target.value })}>
          <option value="">All website statuses</option>{["VERIFIED_NO_WEBSITE", "VERIFIED_HAS_WEBSITE", "WEBSITE_UNCERTAIN", "CONFLICTING_EVIDENCE", "VERIFICATION_FAILED"].map((s) => <option key={s}>{s}</option>)}
        </select>
      </div>
      {q.error ? <ErrorState error={q.error} /> : !q.data ? <Loading /> : q.data.length === 0 ? <Empty title="No prospects match" /> : (
        <div className="overflow-x-auto rounded-xl border hairline">
          <table className="w-full text-sm">
            <thead className="bg-ink-900 text-left text-xs uppercase tracking-wider text-ink-400"><tr><th className="px-4 py-2">Business</th><th>State</th><th>Website</th><th className="text-right">Score</th><th>Preview</th><th className="px-4">Outreach</th></tr></thead>
            <tbody className="divide-y divide-ink-800">{q.data.map((p) => (
              <tr key={p.id} className="hover:bg-ink-900/60">
                <td className="px-4 py-2"><Link to={`/prospects/${p.id}`} className="text-ink-100 hover:underline">{p.business_name ?? <span className="italic text-ink-500">Identity unverified</span>}</Link>
                  <div className="text-xs text-ink-500">{p.discovery_provider}</div></td>
                <td><Pill>{p.state}</Pill>{p.review_reason && <div className="text-[11px] text-warn-500">{p.review_reason}</div>}</td>
                <td><Pill>{p.website_status ?? "unverified"}</Pill>{p.site_health && <span className="ml-1 text-xs text-ink-400">{p.site_health}</span>}</td>
                <td className="text-right tabular-nums">{p.score ?? "—"}</td>
                <td>{p.preview_url ? <a className="text-signal-300 hover:underline" href={p.preview_url} target="_blank" rel="noreferrer">open</a> : "—"}</td>
                <td className="px-4">{p.outreach_state ? <Pill>{p.outreach_state}</Pill> : "—"}</td>
              </tr>))}</tbody>
          </table>
        </div>
      )}
    </Page>
  );
}

type DetailT = { prospect: { id: string; state: string; review_reason: string | null; discovery_provider: string; campaign_id: string };
  facts: Array<{ id: string; fact_key: string; value: string; status: string; quote: string | null; confidence: string; source_type: string; source_ref: string; retrieved_at: string }>;
  verifications: Array<{ id: string; status: string; site_health: string | null; reason: string | null; final_url: string | null; confidence: string; checks: Array<{ check: string; status: string; result: string }>; checked_at: string; listing_sources: string[] }>;
  evidence: Array<{ id: string; source_type: string; source_ref: string; retrieved_at: string; excerpt: string | null }>;
  contacts: Array<{ contact_type: string; value: string; verification: string; is_public_business_contact: boolean; harvesting_prohibited: boolean }>;
  scores: Array<{ total: number; components: Array<{ rule: string; max: number; awarded: number; explanation: string }>; rule_set_version: number; computed_at: string }> };

export function ProspectDetailPage() {
  const { id } = useParams();
  const q = useQuery({ queryKey: ["prospect", id], queryFn: () => api<DetailT>(`/prospects/${id}`) });
  const [name, setName] = useState(""); const [how, setHow] = useState("");
  const confirm = useMutation({ mutationFn: () => api(`/prospects/${id}/confirm-identity`, { method: "POST", json: { business_name: name, how_confirmed: how } }), onSuccess: () => q.refetch() });
  if (q.error) return <Page title="Prospect"><ErrorState error={q.error} /></Page>;
  if (!q.data) return <Page title="Prospect"><Loading /></Page>;
  const d = q.data;
  const name0 = d.facts.find((f) => f.fact_key === "business_name" && f.status === "verified")?.value;
  const v = d.verifications[0];
  return (
    <Page title={name0 ?? "Unverified business"} subtitle={`${d.prospect.discovery_provider} · ${d.prospect.state}`}>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Website verification" className="lg:col-span-2">
          {v ? (<div className="space-y-2 text-sm">
            <div className="flex flex-wrap items-center gap-2"><Pill>{v.status}</Pill>{v.site_health && <Pill>{v.site_health}</Pill>}<span className="text-ink-400">confidence {v.confidence} · {v.reason}</span></div>
            {v.final_url && <a className="text-signal-300 hover:underline" href={v.final_url} target="_blank" rel="noreferrer">{v.final_url}</a>}
            <ul className="mt-2 grid gap-1 md:grid-cols-2">{v.checks.map((c, i) => <li key={i} className="flex justify-between rounded bg-ink-850 px-2 py-1 text-xs"><span>{c.check}</span><span className={c.status === "completed" ? "text-ink-300" : "text-warn-500"}>{c.status}: {c.result}</span></li>)}</ul>
            <p className="text-xs text-ink-500">Checked {when(v.checked_at)} · listing sources: {v.listing_sources.join(", ") || "none"}</p>
          </div>) : <p className="text-sm text-ink-500">Not verified yet.</p>}
        </Card>
        <Card title="Qualification">
          {d.scores[0] ? (<div><div className="text-3xl font-semibold">{d.scores[0].total}<span className="text-sm text-ink-500">/100</span></div>
            <ul className="mt-3 space-y-2 text-sm">{d.scores[0].components.map((c) => (
              <li key={c.rule}><div className="flex justify-between"><span className="text-ink-300">{c.rule.replace(/_/g, " ")}</span><span className="tabular-nums">{c.awarded}/{c.max}</span></div>
                <div className="text-xs text-ink-500">{c.explanation}</div></li>))}</ul>
            <p className="mt-2 text-xs text-ink-500">Rule set v{d.scores[0].rule_set_version}. Google ratings are never scoring inputs.</p></div>) : <p className="text-sm text-ink-500">Not scored yet.</p>}
        </Card>
        <Card title="Verified facts (independent sources)" className="lg:col-span-2">
          <ul className="space-y-2 text-sm">{d.facts.map((f) => (
            <li key={f.id} className="rounded-lg bg-ink-850 p-2"><div className="flex items-center justify-between gap-2"><span><span className="text-ink-400">{f.fact_key}:</span> {f.value}</span><Pill>{f.status}</Pill></div>
              {f.quote && <blockquote className="mt-1 border-l-2 border-ink-600 pl-2 text-xs text-ink-400">“{f.quote}”</blockquote>}
              <div className="mt-1 truncate text-[11px] text-ink-500">{f.source_type} · {f.source_ref}</div></li>))}
            {d.facts.length === 0 && <li className="text-ink-500">No verified facts.</li>}</ul>
        </Card>
        <div className="space-y-4">
          <GooglePlacesListingPanel prospectId={d.prospect.id} enabled={d.prospect.discovery_provider === "google_places"} />
          <Card title="Contacts"><ul className="space-y-1 text-sm">{d.contacts.map((c) => (
            <li key={c.value} className="flex justify-between gap-2"><span>{c.contact_type}: {c.value}</span>{c.harvesting_prohibited ? <Pill tone="bad">collection prohibited</Pill> : <Pill>{c.verification}</Pill>}</li>))}
            {d.contacts.length === 0 && <li className="text-ink-500">No verified contacts.</li>}</ul></Card>
          {d.prospect.state === "requires_human_review" && (
            <Card title="Confirm identity">
              <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); confirm.mutate(); }}>
                <Input placeholder="Business name as confirmed" value={name} onChange={(e) => setName(e.target.value)} aria-label="Business name" />
                <Input placeholder="How confirmed (e.g. called on 2026-11-18)" value={how} onChange={(e) => setHow(e.target.value)} aria-label="How confirmed" />
                <Button type="submit" disabled={confirm.isPending}>Confirm</Button>{confirm.error && <ErrorState error={confirm.error} />}
              </form>
            </Card>)}
        </div>
        <Card title="Evidence" className="lg:col-span-3">
          <ul className="divide-y divide-ink-800 text-xs">{d.evidence.map((e) => (
            <li key={e.id} className="py-2"><span className="text-ink-400">{e.source_type}</span> · <span className="break-all text-ink-300">{e.source_ref}</span> · {when(e.retrieved_at)}
              {e.excerpt && <div className="mt-1 text-ink-500">{e.excerpt}</div>}</li>))}</ul>
        </Card>
      </div>
    </Page>
  );
}
