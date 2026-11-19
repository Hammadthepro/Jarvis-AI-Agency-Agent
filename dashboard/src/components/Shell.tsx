import { NavLink, Outlet } from "react-router";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "../lib/api";
import { ApiError } from "../lib/api";
import { apiBase } from "../lib/config";
import { IconActivity, IconCog, IconCoins, IconCommand, IconFlag, IconGauge, IconGlobe, IconMail, IconMic, IconPlug, IconUsers } from "./icons";

const NAV = [
  ["/", "Command Center", IconCommand], ["/voice", "Voice Agent", IconMic], ["/campaigns", "Campaigns", IconFlag],
  ["/prospects", "Prospects", IconUsers], ["/websites", "Websites & Previews", IconGlobe], ["/outreach", "Outreach", IconMail],
  ["/integrations", "Integrations", IconPlug], ["/activity", "Activity", IconActivity], ["/usage", "Usage & Costs", IconCoins],
  ["/overview", "Overview", IconGauge], ["/settings", "Settings", IconCog],
] as const;

export default function Shell() {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<{ email: string; role: string; auth_mode: string; environment: string }>("/me"),
    retry: (n, e) => !(e instanceof ApiError && e.status === 401) && n < 3, refetchInterval: (q) => (q.state.error ? 5000 : false) });
  return (
    <div className="flex h-full">
      <aside className="hidden w-60 shrink-0 flex-col border-r hairline bg-ink-900/60 p-4 md:flex">
        <div className="mb-6 flex items-center gap-2 px-2">
          <div className="grid h-7 w-7 place-items-center rounded-md bg-signal-500/20 ring-1 ring-signal-500/40">
            <div className="h-2.5 w-2.5 rounded-full bg-signal-400 shadow-[0_0_12px_var(--color-signal-400)]" />
          </div>
          <span className="text-sm font-semibold tracking-[0.2em] text-ink-100">JARVIS</span>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5" aria-label="Main">
          {NAV.map(([to, label, Icon]) => (
            <NavLink key={to} to={to} end={to === "/"}
              className={({ isActive }) => `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition ${isActive ? "bg-ink-800 text-ink-100" : "text-ink-400 hover:bg-ink-850 hover:text-ink-200"}`}>
              <Icon className="h-4 w-4 shrink-0" />{label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-4 border-t hairline pt-3 text-xs text-ink-400">
          {me.data?.auth_mode === "local_development"
            ? <div className="rounded-md bg-warn-500/10 px-2 py-1 text-[11px] text-warn-500" title="JARVIS_ENV=local with JARVIS_LOCAL_DEV_ACCESS=true. Not available in production.">Local development mode · no sign-in</div>
            : <div className="truncate">{me.data?.email ?? "…"}</div>}
          <div className="mt-1 text-[11px]">{me.data ? `${me.data.role} · ${me.data.environment}` : ""}</div>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <nav className="flex gap-1 overflow-x-auto border-b hairline p-2 scroll-thin md:hidden" aria-label="Mobile">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs ${isActive ? "bg-ink-800 text-ink-100" : "text-ink-400"}`}>{label}</NavLink>
          ))}
        </nav>
        {me.error && <ApiProblem error={me.error} />}
        <main className="min-h-0 flex-1 overflow-y-auto scroll-thin"><Outlet /></main>
      </div>
    </div>
  );
}

/** Explains why the dashboard cannot use the API. Deliberately not a sign-in form. */
function ApiProblem({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? error : null;
  const text = e?.status === 401
    ? "The API requires authentication and local development access is not enabled. For local development run scripts/dev-up.sh, which starts the API with JARVIS_ENV=local and JARVIS_LOCAL_DEV_ACCESS=true. Sign-in for production will be added later."
    : e?.code === "API_UNREACHABLE"
      ? `The JARVIS API is not reachable at ${apiBase}. Start it with scripts/dev-up.sh and check scripts/dev-status.sh.`
      : `The JARVIS API returned an error: ${e ? `${e.code} ${e.message}` : String(error)}`;
  return <div role="alert" className="border-b border-bad-500/30 bg-bad-500/10 px-4 py-2 text-sm text-ink-100">{text}</div>;
}

export function Page({ title, subtitle, actions, children }: { title: string; subtitle?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="mx-auto max-w-7xl px-4 py-6 md:px-8">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div><h1 className="text-xl font-semibold text-ink-100">{title}</h1>{subtitle && <p className="mt-1 text-sm text-ink-400">{subtitle}</p>}</div>
        {actions}
      </div>
      {children}
    </div>
  );
}
