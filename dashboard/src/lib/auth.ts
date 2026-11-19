import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { config, supabaseConfigured } from "./config";

/** Authentication is deferred until the end of product development. In local development the API runs in its
 *  explicit local access mode (JARVIS_ENV=local + JARVIS_LOCAL_DEV_ACCESS=true), so requests carry no token and
 *  there is no sign-in screen. When Supabase Auth is configured later, its session token is attached here. */
export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(config.VITE_SUPABASE_URL!, config.VITE_SUPABASE_ANON_KEY!, { auth: { persistSession: true, autoRefreshToken: true } })
  : null;

export async function getToken(): Promise<string | null> {
  if (!supabase) return null;
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}
