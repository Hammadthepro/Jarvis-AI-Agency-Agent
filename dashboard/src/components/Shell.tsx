import { NavLink, Outlet } from "react-router";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "../lib/api";
import { signOut } from "../lib/auth";

const NAV = [
  ["/", "JARVIS"], ["/overview", "Overview"], ["/campaigns", "Campaigns"], ["/prospects", "Prospects"],
  ["/websites", "Website Studio"], ["/outreach", "Outreach"], ["/activity", "Activity"], ["/settings", "Settings"],
] as const;

export default function Shell() {
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<{ email: string; role: string }>("/me") });
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
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"}
              className={({ isActive }) => `rounded-lg px-3 py-2 text-sm transition ${isActive ? "bg-ink-800 text-ink-100" : "text-ink-400 hover:bg-ink-850 hover:text-ink-200"}`}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-4 border-t hairline pt-3 text-xs text-ink-400">
          <div className="truncate">{me.data?.email ?? "…"}</div>
          <div className="flex items-center justify-between"><span>{me.data?.role}</span>
            <button className="text-ink-300 hover:text-ink-100" onClick={() => void signOut()}>Sign out</button></div>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <nav className="flex gap-1 overflow-x-auto border-b hairline p-2 scroll-thin md:hidden" aria-label="Mobile">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs ${isActive ? "bg-ink-800 text-ink-100" : "text-ink-400"}`}>{label}</NavLink>
          ))}
        </nav>
        <main className="min-h-0 flex-1 overflow-y-auto scroll-thin"><Outlet /></main>
      </div>
    </div>
  );
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
