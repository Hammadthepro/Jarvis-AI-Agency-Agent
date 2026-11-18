import { useState } from "react";
import { motion } from "framer-motion";
import { z } from "zod";
import { supabaseConfigured } from "../lib/config";
import { devSignIn, signInWithMagicLink, signInWithPassword } from "../lib/auth";
import { Button, Input } from "../components/ui";

export const LoginSchema = z.object({ email: z.string().email("Enter a valid email"), password: z.string().min(8, "At least 8 characters").optional() });

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(fn: () => Promise<void>) {
    setBusy(true); setMsg(null);
    try { await fn(); } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  }
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const r = LoginSchema.safeParse({ email, password: password || undefined });
    if (!r.success) { setMsg(r.error.issues[0].message); return; }
    if (!supabaseConfigured) return run(() => devSignIn(email));
    return run(() => (password ? signInWithPassword(email, password) : signInWithMagicLink(email).then(() => setMsg("Check your email for a sign-in link."))));
  };
  return (
    <div className="grid h-full place-items-center bg-[radial-gradient(ellipse_at_top,var(--color-ink-800),var(--color-ink-950))] px-4">
      <motion.form onSubmit={submit} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} className="w-full max-w-sm rounded-2xl border hairline bg-ink-900/90 p-6 glow">
        <div className="mb-5 text-center"><div className="text-sm font-semibold tracking-[0.3em] text-ink-100">JARVIS</div>
          <p className="mt-1 text-xs text-ink-400">Autonomous website agency operations</p></div>
        <label className="mb-1 block text-xs text-ink-400" htmlFor="email">Email</label>
        <Input id="email" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        {supabaseConfigured && (<><label className="mb-1 mt-3 block text-xs text-ink-400" htmlFor="pw">Password (or leave empty for a magic link)</label>
          <Input id="pw" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></>)}
        <Button type="submit" className="mt-4 w-full justify-center" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
        {!supabaseConfigured && <p className="mt-3 text-center text-[11px] text-warn-500">Supabase Auth is NOT_CONFIGURED: local development sign-in (works only when the API runs with JARVIS_ENV=local).</p>}
        {msg && <p role="alert" className="mt-3 text-center text-xs text-ink-200">{msg}</p>}
      </motion.form>
    </div>
  );
}
