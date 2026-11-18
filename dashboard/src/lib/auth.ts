import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { config, supabaseConfigured } from "./config";

export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(config.VITE_SUPABASE_URL!, config.VITE_SUPABASE_ANON_KEY!, { auth: { persistSession: true, autoRefreshToken: true } })
  : null;

const DEV_KEY = "jarvis.devtoken";
let listeners: Array<() => void> = [];
export const onAuthChange = (fn: () => void) => { listeners.push(fn); return () => { listeners = listeners.filter((l) => l !== fn); }; };
const notify = () => listeners.forEach((l) => l());
supabase?.auth.onAuthStateChange(() => notify());

export async function getToken(): Promise<string | null> {
  if (supabase) {
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token ?? null;
  }
  try { return sessionStorage.getItem(DEV_KEY); } catch { return null; }
}

export async function signInWithPassword(email: string, password: string) {
  if (!supabase) throw new Error("Supabase Auth is not configured");
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) throw error;
}

export async function signInWithMagicLink(email: string) {
  if (!supabase) throw new Error("Supabase Auth is not configured");
  const { error } = await supabase.auth.signInWithOtp({ email, options: { emailRedirectTo: window.location.origin } });
  if (error) throw error;
}

/** Local development only: the API refuses dev tokens outside JARVIS_ENV=local|test. */
export async function devSignIn(email: string) {
  const r = await fetch(`${config.VITE_API_BASE_URL}/auth/dev-token`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }),
  });
  const body = await r.json();
  if (!r.ok) throw new Error(body?.error?.message ?? "dev sign-in failed");
  sessionStorage.setItem(DEV_KEY, body.access_token);
  notify();
}

export async function signOut() {
  if (supabase) await supabase.auth.signOut();
  try { sessionStorage.removeItem(DEV_KEY); } catch { /* ignore */ }
  notify();
}
