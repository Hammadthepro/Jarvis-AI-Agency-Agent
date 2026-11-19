import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Page } from "../components/Shell";
import { Button, Card, Empty, ErrorState, Input, Loading, Pill } from "../components/ui";

type Readiness = { ready: boolean; items: Array<{ key: string; ok: boolean; detail: string }> };
type Draft = { id: string; campaign_id: string; state: string; subject: string; body_text: string; business_name: string | null; preview_url: string | null; has_contact: boolean; observations: Array<{ id: string; text: string }> };

export default function OutreachPage() {
  const qc = useQueryClient();
  const ready = useQuery({ queryKey: ["readiness"], queryFn: () => api<Readiness>("/outreach/readiness") });
  const drafts = useQuery({ queryKey: ["drafts"], queryFn: () => api<Draft[]>("/outreach/drafts") });
  const [sender, setSender] = useState({ postal_address: "", postal_address_type: "street", sending_domain: "", from_address: "", reply_to: "", attest: false });
  const save = useMutation({ mutationFn: () => api<Readiness>("/outreach/sender", { method: "PUT", json: { ...sender, attest_postal_address: sender.attest } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["readiness"] }) });
  const dns = useMutation({ mutationFn: () => api("/outreach/sender/check-dns", { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["readiness"] }) });
  const approve = useMutation({ mutationFn: (id: string) => api(`/outreach/drafts/${id}/approve`, { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["drafts"] }) });
  const send = useMutation({ mutationFn: (id: string) => api(`/outreach/drafts/${id}/send`, { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["drafts"] }) });
  return (
    <Page title="Outreach" subtitle="Drafts are always available. Sending stays blocked until every compliance check passes and you authorize the campaign.">
      <div className="grid gap-4 lg:grid-cols-[1fr_1.2fr]">
        <Card title={<span className="flex items-center gap-2">Sending readiness {ready.data && <Pill tone={ready.data.ready ? "ok" : "warn"}>{ready.data.ready ? "ready" : "blocked"}</Pill>}</span>}>
          {ready.error ? <ErrorState error={ready.error} /> : !ready.data ? <Loading /> : (
            <ul className="space-y-1 text-sm">{ready.data.items.map((i) => (
              <li key={i.key} className="flex items-start justify-between gap-3"><span className="text-ink-300">{i.detail}</span><Pill tone={i.ok ? "ok" : "warn"}>{i.ok ? "ok" : "missing"}</Pill></li>))}</ul>)}
          <form className="mt-4 grid gap-2" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
            <Input placeholder="Postal address (street, USPS PO Box, or registered CMRA mailbox)" value={sender.postal_address} onChange={(e) => setSender({ ...sender, postal_address: e.target.value })} aria-label="Postal address" />
            <select aria-label="Address type" className="rounded-lg border hairline bg-ink-850 px-3 py-2 text-sm" value={sender.postal_address_type} onChange={(e) => setSender({ ...sender, postal_address_type: e.target.value })}>
              <option value="street">Street address</option><option value="po_box">USPS-registered PO Box</option><option value="cmra_mailbox">CMRA private mailbox</option></select>
            <label className="flex items-center gap-2 text-xs text-ink-300"><input type="checkbox" checked={sender.attest} onChange={(e) => setSender({ ...sender, attest: e.target.checked })} />I attest this address is current and legitimately registered.</label>
            <Input placeholder="Sending domain (your own; not pages.dev)" value={sender.sending_domain} onChange={(e) => setSender({ ...sender, sending_domain: e.target.value })} aria-label="Sending domain" />
            <Input placeholder="From address" value={sender.from_address} onChange={(e) => setSender({ ...sender, from_address: e.target.value })} aria-label="From address" />
            <Input placeholder="Reply-to mailbox" value={sender.reply_to} onChange={(e) => setSender({ ...sender, reply_to: e.target.value })} aria-label="Reply-to" />
            <div className="flex gap-2"><Button type="submit" disabled={save.isPending}>Save sender</Button><Button type="button" variant="ghost" onClick={() => dns.mutate()}>Check DNS</Button></div>
            {(save.error || dns.error) && <ErrorState error={save.error ?? dns.error} />}
          </form>
        </Card>
        <Card title="Drafts">
          {drafts.error ? <ErrorState error={drafts.error} /> : !drafts.data ? <Loading /> : drafts.data.length === 0 ? <Empty title="No drafts yet">Drafts are created after a preview is deployed and verified.</Empty> : (
            <ul className="space-y-3">{drafts.data.map((d) => (
              <li key={d.id} className="rounded-lg bg-ink-850 p-3 text-sm">
                <div className="flex items-center justify-between gap-2"><span className="font-medium text-ink-100">{d.business_name ?? "—"}</span><Pill>{d.state}</Pill></div>
                <div className="mt-1 text-ink-200">{d.subject}</div>
                <pre className="mt-2 whitespace-pre-wrap font-sans text-xs text-ink-300">{d.body_text}</pre>
                <div className="mt-2 text-[11px] text-ink-500">Grounded in: {d.observations.map((o) => o.text).join(" · ")}</div>
                {!d.has_contact && <div className="mt-1 text-[11px] text-warn-500">No verified public business email (or collection prohibited): cannot be sent.</div>}
                <div className="mt-2 flex gap-2">
                  {d.state === "draft" && <Button variant="ghost" onClick={() => approve.mutate(d.id)}>Approve</Button>}
                  {d.state === "authorized" && <Button onClick={() => send.mutate(d.id)} disabled={!ready.data?.ready}>Send</Button>}
                </div>
              </li>))}</ul>)}
          {(approve.error || send.error) && <ErrorState error={approve.error ?? send.error} />}
        </Card>
      </div>
    </Page>
  );
}
