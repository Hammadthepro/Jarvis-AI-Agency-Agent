import { Fragment, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { api, ApiError } from "../lib/api";
import { humanize, STATE_HELP, useIntegrations, type Integration, type IntegrationField, type TestResult } from "../lib/integrations";
import { Page } from "../components/Shell";
import { Button, ErrorState, Loading, Pill, when } from "../components/ui";
import { IconAlert, IconExternal, ProviderMark } from "../components/icons";

const ORDER = ["anthropic", "serp_api", "firecrawl", "cloudflare", "livekit", "resend", "supabase", "google_places"];

export default function IntegrationsPage() {
  const q = useIntegrations();
  const [editing, setEditing] = useState<Integration | null>(null);
  if (q.error) return <Page title="Integrations"><ErrorState error={q.error} /></Page>;
  if (!q.data) return <Page title="Integrations"><Loading /></Page>;
  const { integrations, can_manage, vault, credentials_error } = q.data;
  const list = [...integrations].sort((a, b) => ORDER.indexOf(a.provider) - ORDER.indexOf(b.provider));
  const connected = list.filter((i) => i.state === "CONNECTED").length;
  return (
    <Page title="Integrations" subtitle="Connect the services JARVIS works with. Credentials are encrypted on the server and never sent back to the browser.">
      <div className="mb-5 grid gap-3 md:grid-cols-3">
        <Summary label="Validated" value={`${connected} of ${list.length}`} tone={connected === list.length ? "ok" : "muted"} />
        <Summary label="Credential storage" value={vault.ready ? "Encrypted (AES-256-GCM)" : "Not available"} tone={vault.ready ? "ok" : "warn"} />
        <Summary label="Your access" value={can_manage ? "Administrator: can edit" : "Read only"} tone={can_manage ? "info" : "muted"} />
      </div>
      {!vault.ready && (
        <Banner tone="warn" title="Credentials cannot be saved from the dashboard yet">{vault.detail}</Banner>
      )}
      {credentials_error && <Banner tone="bad" title="Stored credentials could not be loaded">{credentials_error}</Banner>}
      {!can_manage && (
        <Banner tone="info" title="Read-only">Only deployment administrators (JARVIS_ADMIN_EMAILS or JARVIS_ADMIN_USER_IDS on the server) can change
          credentials. Workspace owners can run connection tests.</Banner>
      )}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {list.map((i) => <IntegrationCard key={i.provider} i={i} canManage={can_manage} vaultReady={vault.ready} onConfigure={() => setEditing(i)} />)}
      </div>
      <AnimatePresence>
        {editing && <ConfigureDialog key={editing.provider} i={editing} onClose={() => setEditing(null)} />}
      </AnimatePresence>
    </Page>
  );
}

function Summary({ label, value, tone }: { label: string; value: string; tone: "ok" | "warn" | "muted" | "info" }) {
  return (
    <div className="rounded-xl border hairline bg-ink-900/80 p-4">
      <div className="text-xs uppercase tracking-wider text-ink-400">{label}</div>
      <div className="mt-1 flex items-center gap-2 text-sm font-medium text-ink-100"><Pill tone={tone}>{value}</Pill></div>
    </div>
  );
}

function Banner({ tone, title, children }: { tone: "warn" | "bad" | "info"; title: string; children: React.ReactNode }) {
  const c = { warn: "border-warn-500/30 bg-warn-500/10", bad: "border-bad-500/30 bg-bad-500/10", info: "border-signal-500/30 bg-signal-500/5" }[tone];
  return (
    <div role={tone === "info" ? "note" : "alert"} className={`mb-4 flex gap-3 rounded-xl border p-3 text-sm ${c}`}>
      <IconAlert className="mt-0.5 h-4 w-4 shrink-0 text-ink-200" />
      <div><div className="font-medium text-ink-100">{title}</div><div className="mt-0.5 text-ink-300">{children}</div></div>
    </div>
  );
}

function IntegrationCard({ i, canManage, vaultReady, onConfigure }: { i: Integration; canManage: boolean; vaultReady: boolean; onConfigure: () => void }) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["integrations"] });
  const test = useMutation({ mutationFn: () => api<TestResult>(`/integrations/${i.provider}/test`, { method: "POST" }), onSettled: refresh });
  const toggle = useMutation({ mutationFn: () => api(`/integrations/${i.provider}/${i.enabled ? "disable" : "enable"}`, { method: "POST" }), onSettled: refresh });
  const remove = useMutation({ mutationFn: () => api(`/integrations/${i.provider}/credentials`, { method: "DELETE" }), onSettled: refresh });
  const restricted = i.provider === "google_places";
  return (
    <section aria-label={i.name} className={`flex flex-col rounded-2xl border hairline bg-ink-900/80 p-4 ${restricted ? "opacity-90" : ""}`}>
      <header className="flex items-start gap-3">
        <ProviderMark provider={i.provider} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-ink-100">{i.name}</h2>
            {restricted && <Pill tone="warn">Restricted</Pill>}
          </div>
          <p className="mt-0.5 text-xs leading-relaxed text-ink-400">{i.purpose}</p>
        </div>
      </header>

      <div className="mt-4 flex items-center gap-2">
        <Pill>{i.state}</Pill>
        <span className="text-xs text-ink-400">{STATE_HELP[i.state] ?? ""}</span>
      </div>
      {i.detail && i.state !== "CONNECTED" && <p className="mt-2 line-clamp-3 break-words text-xs text-ink-300" title={i.detail}>{i.detail}</p>}

      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
        <dt className="text-ink-500">Last validated</dt><dd className="text-ink-200">{when(i.last_success_at)}</dd>
        <dt className="text-ink-500">Last checked</dt><dd className="text-ink-200">{when(i.last_checked_at)}</dd>
        {i.last_error && i.last_error !== i.detail && <><dt className="text-ink-500">Last error</dt><dd className="line-clamp-2 break-words text-bad-500" title={`${i.last_error} · ${when(i.last_error_at)}`}>{i.last_error}</dd></>}
        {i.fields.filter((f) => !f.secret && f.set && f.display).map((f) => (
          <FieldRow key={f.name} f={f} />
        ))}
        {i.fields.some((f) => f.secret) && (
          <><dt className="text-ink-500">Credentials</dt>
            <dd className="text-ink-200">{i.fields.filter((f) => f.secret).map((f) => `${f.label}: ${f.set ? `saved (${sourceLabel(f.source)})` : "missing"}`).join(" · ")}</dd></>
        )}
      </dl>

      {(i.quota || i.usage) && <Quota i={i} />}

      <div className="mt-3 flex flex-wrap gap-1">
        {i.capabilities.map((c) => (
          <span key={c} className={`rounded-md px-1.5 py-0.5 text-[10px] ring-1 ring-inset ${i.validated_capabilities.includes(c) && i.state === "CONNECTED" ? "bg-ok-500/10 text-ok-500 ring-ok-500/30" : "bg-ink-800 text-ink-400 ring-ink-700"}`}>{humanize(c)}</span>
        ))}
      </div>
      {i.notes.length > 0 && <ul className="mt-3 space-y-1 text-[11px] text-ink-400">{i.notes.map((n) => <li key={n}>• {n}</li>)}</ul>}
      {i.runtime && i.runtime.detail !== i.detail && i.runtime.detail !== i.last_error && (
        <p className="mt-2 line-clamp-2 text-[11px] text-warn-500" title={i.runtime.detail ?? ""}>Last call from a task: {i.runtime.state} · {when(i.runtime.at)}</p>
      )}

      <div className="mt-auto flex flex-wrap items-center gap-2 pt-4">
        {i.managed_in_dashboard && canManage && (
          <Button variant={i.configured ? "ghost" : "primary"} onClick={onConfigure} disabled={!vaultReady && i.fields.some((f) => f.secret)}>
            {i.configured ? "Update" : "Configure"}
          </Button>
        )}
        <Button variant="ghost" onClick={() => test.mutate()} disabled={test.isPending}>{test.isPending ? "Testing…" : "Test connection"}</Button>
        {canManage && i.managed_in_dashboard && (
          <Button variant="ghost" onClick={() => toggle.mutate()} disabled={toggle.isPending}>{i.enabled ? "Disable" : "Enable"}</Button>
        )}
        {canManage && i.has_dashboard_credentials && (
          <Button variant="ghost" className="text-bad-500" disabled={remove.isPending}
            onClick={() => { if (window.confirm(`Remove the ${i.name} credentials saved in the dashboard? Environment values, if any, will be used instead.`)) remove.mutate(); }}>
            Remove
          </Button>
        )}
        <a href={i.docs_url} target="_blank" rel="noreferrer noopener" className="ml-auto inline-flex items-center gap-1 text-xs text-ink-400 hover:text-ink-200">
          Provider console <IconExternal className="h-3 w-3" />
        </a>
      </div>
      {test.data && <TestOutcome r={test.data} shownDetail={i.detail} />}
      {[test.error, toggle.error, remove.error].filter(Boolean).map((e, n) => <div key={n} className="mt-3"><ErrorState error={e} /></div>)}
    </section>
  );
}

const sourceLabel = (s: IntegrationField["source"]) => (s === "dashboard" ? "dashboard" : s === "environment" ? "server env" : "unreadable");

function FieldRow({ f }: { f: IntegrationField }) {
  return <><dt className="text-ink-500">{f.label}</dt><dd className="truncate font-mono text-[11px] text-ink-200" title={f.display ?? ""}>{f.display}</dd></>;
}

function Quota({ i }: { i: Integration }) {
  const entries = Object.entries(i.quota ?? {}).filter(([, v]) => v !== null && v !== undefined && typeof v !== "object");
  return (
    <div className="mt-3 rounded-lg bg-ink-850 p-2.5 text-xs">
      <div className="mb-1 text-[10px] uppercase tracking-wider text-ink-500">Provider-reported usage</div>
      {entries.length > 0 ? (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5">{entries.map(([k, v]) => <Fragment key={k}><dt className="text-ink-400">{humanize(k)}</dt><dd className="tabular-nums text-ink-200">{String(v)}</dd></Fragment>)}</dl>
      ) : i.usage ? (
        <div className="text-ink-300">{i.usage.status === "ok" ? `${i.usage.reported_units ?? "—"} ${i.usage.unit}` : `UNKNOWN (${i.usage.status})`}{i.usage.detail ? ` · ${i.usage.detail}` : ""}</div>
      ) : null}
    </div>
  );
}

function TestOutcome({ r, shownDetail }: { r: TestResult; shownDetail: string | null }) {
  const ok = r.state === "CONNECTED";
  return (
    <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} role="status"
      className={`mt-3 rounded-lg border p-2.5 text-xs ${ok ? "border-ok-500/30 bg-ok-500/10" : "border-warn-500/30 bg-warn-500/10"}`}>
      <div className="flex items-center gap-2"><span className="text-ink-400">Test result</span><Pill>{r.state}</Pill>{r.http_status ? <span className="text-ink-400">HTTP {r.http_status}</span> : null}</div>
      {r.detail && r.detail !== shownDetail && <div className="mt-1 break-words text-ink-200">{r.detail}</div>}
      {ok && r.result && <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-ink-300">{JSON.stringify(r.result, null, 2)}</pre>}
    </motion.div>
  );
}

/** Credential form. Secret values live only in this component's state until submitted, then are cleared.
 *  They are never stored in localStorage/sessionStorage, never pre-filled, and never returned by the API. */
function ConfigureDialog({ i, onClose }: { i: Integration; onClose: () => void }) {
  const qc = useQueryClient();
  const initial = Object.fromEntries(i.fields.map((f) => [f.name, f.secret ? "" : (f.source === "dashboard" || f.source === "environment" ? f.display ?? "" : f.default ?? "")]));
  const [values, setValues] = useState<Record<string, string>>(initial);
  const [local, setLocal] = useState<Record<string, string>>({});
  const first = useRef<HTMLInputElement | HTMLSelectElement | null>(null);
  useEffect(() => { first.current?.focus(); const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose(); window.addEventListener("keydown", esc); return () => window.removeEventListener("keydown", esc); }, [onClose]);

  const save = useMutation({
    mutationFn: async () => {
      const secrets: Record<string, string> = {}, config: Record<string, string> = {};
      for (const f of i.fields) {
        const v = (values[f.name] ?? "").trim();
        if (!v) continue;
        if (f.secret) secrets[f.name] = v;
        else if (v !== initial[f.name] || !f.set) config[f.name] = v;
      }
      return api(`/integrations/${i.provider}/credentials`, { method: "PUT", json: { secrets, config } });
    },
    onSuccess: async () => {
      setValues(Object.fromEntries(i.fields.map((f) => [f.name, f.secret ? "" : values[f.name] ?? ""])));
      await qc.invalidateQueries({ queryKey: ["integrations"] });
      onClose();
    },
    onError: () => setValues((v) => Object.fromEntries(Object.entries(v).map(([k, x]) => [k, i.fields.find((f) => f.name === k)?.secret ? "" : x]))),
  });
  const serverFields = save.error instanceof ApiError ? (save.error.details as { fields?: Record<string, string> }).fields : undefined;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const errs: Record<string, string> = {};
    for (const f of i.fields) if (f.required && !f.set && !(values[f.name] ?? "").trim()) errs[f.name] = "required";
    setLocal(errs);
    if (Object.keys(errs).length === 0) save.mutate();
  };

  return (
    <motion.div className="fixed inset-0 z-50 grid place-items-end bg-ink-950/70 backdrop-blur-sm md:place-items-center" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <motion.div role="dialog" aria-modal="true" aria-labelledby="cfg-title" initial={{ y: 24, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 24, opacity: 0 }}
        className="w-full max-w-lg rounded-t-2xl border hairline bg-ink-900 p-5 shadow-2xl md:rounded-2xl">
        <div className="mb-4 flex items-center gap-3">
          <ProviderMark provider={i.provider} />
          <div><h2 id="cfg-title" className="text-base font-semibold text-ink-100">Configure {i.name}</h2>
            <p className="text-xs text-ink-400">Saved values are encrypted on the server. Leave a secret blank to keep the saved one.</p></div>
        </div>
        <form onSubmit={submit} className="space-y-3" autoComplete="off">
          {i.fields.map((f, n) => {
            const err = local[f.name] ?? serverFields?.[f.name];
            const id = `f-${i.provider}-${f.name}`;
            return (
              <div key={f.name}>
                <label htmlFor={id} className="mb-1 flex items-center justify-between text-xs text-ink-300">
                  <span>{f.label}{f.required ? "" : " (optional)"}</span>
                  {f.set && <span className="text-ink-500">{f.secret ? `saved · ${sourceLabel(f.source)}` : sourceLabel(f.source)}</span>}
                </label>
                {f.choices.length > 0 ? (
                  <select id={id} ref={n === 0 ? (el) => { first.current = el; } : undefined} value={values[f.name] ?? ""}
                    onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                    className="w-full rounded-lg border hairline bg-ink-850 px-3 py-2 text-sm text-ink-100">
                    {f.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                ) : (
                  <input id={id} ref={n === 0 ? (el) => { first.current = el; } : undefined} type={f.secret ? "password" : "text"}
                    autoComplete={f.secret ? "new-password" : "off"} spellCheck={false} value={values[f.name] ?? ""}
                    placeholder={f.secret && f.set ? "•••••••• (unchanged)" : f.hint || ""} aria-invalid={Boolean(err)}
                    onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
                    className="w-full rounded-lg border hairline bg-ink-850 px-3 py-2 font-mono text-sm text-ink-100 placeholder:font-sans placeholder:text-ink-500 focus:outline-none focus:ring-2 focus:ring-signal-500/50" />
                )}
                {f.hint && <p className="mt-1 text-[11px] text-ink-500">{f.hint}</p>}
                {err && <p className="mt-1 text-[11px] text-bad-500">{f.label} {err}</p>}
              </div>
            );
          })}
          {save.error && !serverFields && <ErrorState error={save.error} />}
          <div className="flex items-center justify-end gap-2 pt-2">
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save"}</Button>
          </div>
          <p className="text-[11px] text-ink-500">After saving, run Test connection: JARVIS only reports CONNECTED after the provider accepts the credentials.</p>
        </form>
      </motion.div>
    </motion.div>
  );
}

