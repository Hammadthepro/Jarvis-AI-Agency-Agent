import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "../lib/api";
import { useIntegrations } from "../lib/integrations";
import type { VoiceStatus } from "../lib/voice";
import { Card, ErrorState, Loading, Pill, usd } from "./ui";
import { IconMic } from "./icons";

type Campaign = { id: string; name: string; state: string; current_stage: string | null; blocked_reason: string | null; prospects: number;
  qualified: number; deployed: number; sites_to_build: number; spent_micros: number };
type Command = { id: string; raw_text: string; status: string; campaign_id: string | null; created_at: string };
type UsageSummary = { summary: Array<{ provider: string; reconciled: boolean; jarvis: { committed_micros: number; ambiguous_micros: number } }> };

const ACTIVE = new Set(["authorized", "running", "blocked", "paused"]);
const CORE = ["anthropic", "serp_api", "firecrawl", "cloudflare"];
const SHORT: Record<string, string> = { anthropic: "Claude", serp_api: "SerpApi", firecrawl: "Firecrawl", cloudflare: "Cloudflare Pages",
  livekit: "LiveKit (voice)", resend: "Resend (email)", supabase: "Supabase" };

const asList = <T,>(x: unknown): T[] => (Array.isArray(x) ? (x as T[]) : []);

export function useCampaigns() {
  return useQuery({ queryKey: ["campaigns"], queryFn: async () => asList<Campaign>(await api("/campaigns")), refetchInterval: 10_000 });
}

/** Voice entry point: shows the real readiness and opens the voice experience. */
export function VoiceLauncher() {
  const s = useQuery({ queryKey: ["voice-status"], queryFn: () => api<VoiceStatus>("/voice/status"), refetchInterval: 30_000 });
  const ready = s.data?.state === "OPERATIONAL";
  return (
    <Link to={ready ? "/voice?start=1" : "/voice"} aria-label={ready ? "Talk to JARVIS" : "Voice setup"}
      className="group flex items-center gap-3 rounded-full border hairline bg-ink-900/80 py-1.5 pl-1.5 pr-4 transition hover:border-signal-500/40">
      <span className={`grid h-9 w-9 place-items-center rounded-full ring-1 transition ${ready ? "bg-signal-500/20 ring-signal-400/40 text-signal-300 group-hover:bg-signal-500/30" : "bg-ink-800 ring-ink-600 text-ink-400"}`}>
        <IconMic className="h-4 w-4" />
      </span>
      <span className="flex flex-col whitespace-nowrap leading-tight">
        <span className="text-sm text-ink-100">{ready ? "Talk to JARVIS" : "Voice"}</span>
        <span className="text-[11px] text-ink-400">{s.data ? (ready ? "ready" : s.data.state === "NOT_CONFIGURED" ? "needs setup" : "not ready") : "checking…"}</span>
      </span>
    </Link>
  );
}

export function AttentionCard() {
  const cmds = useQuery({ queryKey: ["commands"], queryFn: () => api<Command[]>("/commands") });
  const commandList = asList<Command>(cmds.data);
  const camps = useCampaigns();
  const integ = useIntegrations();
  const items: Array<{ key: string; tone: "warn" | "bad" | "info"; text: string; to: string; cta: string }> = [];
  for (const c of commandList) {
    if (c.status === "awaiting_confirmation") items.push({ key: c.id, tone: "info", text: `Waiting for your authorization: “${c.raw_text}”`, to: "/", cta: "Review" });
    if (c.status === "needs_clarification") items.push({ key: c.id, tone: "info", text: `JARVIS has a question about: “${c.raw_text}”`, to: "/", cta: "Answer" });
  }
  for (const c of camps.data ?? []) {
    if (c.state === "blocked" || c.state === "requires_human_review")
      items.push({ key: c.id, tone: "warn", text: `${c.name}: ${c.blocked_reason ?? c.state.replace(/_/g, " ")}`, to: `/campaigns/${c.id}`, cta: "Open" });
  }
  for (const i of asList<NonNullable<typeof integ.data>["integrations"][number]>(integ.data?.integrations)) {
    if (CORE.includes(i.provider) && ["INVALID_CREDENTIALS", "PERMISSION_DENIED", "QUOTA_EXCEEDED", "CONNECTION_FAILED"].includes(i.state))
      items.push({ key: i.provider, tone: "bad", text: `${i.name}: ${i.state.replace(/_/g, " ").toLowerCase()}`, to: "/integrations", cta: "Fix" });
  }
  const loading = !cmds.data || !camps.data || !integ.data;
  return (
    <Card title="Needs your attention" actions={!loading && <Pill tone={items.length ? "warn" : "ok"}>{items.length ? String(items.length) : "clear"}</Pill>}>
      {cmds.error ? <ErrorState error={cmds.error} /> : loading ? <Loading /> : items.length === 0 ? (
        <p className="text-sm text-ink-400">No approvals, questions or blockers waiting.</p>
      ) : (
        <ul className="space-y-2 text-sm">{items.slice(0, 8).map((it) => (
          <li key={it.key} className={`flex items-start justify-between gap-3 rounded-lg px-3 py-2 ${it.tone === "bad" ? "bg-bad-500/10" : it.tone === "warn" ? "bg-warn-500/10" : "bg-signal-500/5"}`}>
            <span className="text-ink-200">{it.text}</span><Link to={it.to} className="shrink-0 text-xs text-signal-300 hover:underline">{it.cta}</Link>
          </li>))}</ul>
      )}
    </Card>
  );
}

export function ActiveCampaigns() {
  const camps = useCampaigns();
  const active = (camps.data ?? []).filter((c) => ACTIVE.has(c.state)).slice(0, 4);
  return (
    <Card title="Active work" actions={<Link to="/campaigns" className="text-xs text-signal-300 hover:underline">All campaigns</Link>}>
      {camps.error ? <ErrorState error={camps.error} /> : !camps.data ? <Loading /> : active.length === 0 ? (
        <p className="text-sm text-ink-400">Nothing is running. Give JARVIS a command, or import a business list from Campaigns.</p>
      ) : (
        <ul className="space-y-3">{active.map((c) => (
          <li key={c.id}>
            <Link to={`/campaigns/${c.id}`} className="block rounded-lg border hairline p-3 transition hover:border-signal-500/40">
              <div className="flex items-center justify-between gap-2"><span className="truncate text-sm text-ink-100">{c.name}</span><Pill>{c.state}</Pill></div>
              <div className="mt-1 text-xs text-ink-400">Stage: {c.current_stage?.replace(/_/g, " ") ?? "queued"} · {c.prospects} prospects · {c.qualified} qualified
                {c.sites_to_build ? ` · ${c.deployed}/${c.sites_to_build} previews live` : ""}</div>
              {c.blocked_reason && <div className="mt-1 text-xs text-warn-500">{c.blocked_reason}</div>}
            </Link>
          </li>))}</ul>
      )}
    </Card>
  );
}

export function IntegrationHealth() {
  const q = useIntegrations();
  const list = asList<NonNullable<typeof q.data>["integrations"][number]>(q.data?.integrations).filter((i) => i.provider !== "google_places");
  const ok = list.filter((i) => i.state === "CONNECTED").length;
  return (
    <Card title="Integration health" actions={<Link to="/integrations" className="text-xs text-signal-300 hover:underline">Manage</Link>}>
      {q.error ? <ErrorState error={q.error} /> : !q.data ? <Loading /> : (
        <>
          <div className="mb-2 text-xs text-ink-400">{ok} of {list.length} validated against the provider</div>
          <ul className="space-y-1 text-xs">{list.map((i) => (
            <li key={i.provider} className="flex items-center justify-between gap-2 rounded-md bg-ink-850 px-2.5 py-1.5"><span className="text-ink-300">{SHORT[i.provider] ?? i.name}</span><Pill>{i.state}</Pill></li>))}</ul>
        </>
      )}
    </Card>
  );
}

export function BudgetCard() {
  const u = useQuery({ queryKey: ["usage"], queryFn: () => api<UsageSummary>("/usage"), refetchInterval: 60_000 });
  const committed = asList<UsageSummary["summary"][number]>(u.data?.summary).reduce((a, s) => a + s.jarvis.committed_micros + s.jarvis.ambiguous_micros, 0);
  const unknown = asList<{ reconciled: boolean }>(u.data?.summary).filter((s) => !s.reconciled).length;
  return (
    <Card title="Spend this month" actions={<Link to="/usage" className="text-xs text-signal-300 hover:underline">Details</Link>}>
      {u.error ? <ErrorState error={u.error} /> : !u.data ? <Loading /> : (
        <div>
          <div className="text-2xl font-semibold tabular-nums text-ink-100">{usd(committed)}</div>
          <div className="text-xs text-ink-400">JARVIS local estimate (committed + ambiguous). Not an invoice.</div>
          {unknown > 0 && <div className="mt-2 text-xs text-warn-500">{unknown} provider account{unknown > 1 ? "s" : ""}: usage UNKNOWN (not zero).</div>}
        </div>
      )}
    </Card>
  );
}
