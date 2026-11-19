import { z } from "zod";

/** VITE_API_BASE_URL: "/api" (default) uses the dev/preview server's same-origin proxy, which works locally and in
 *  Codespaces without exposing the API port. A deployed dashboard sets the API's absolute https URL. */
const ApiBase = z.union([z.string().regex(/^\/[A-Za-z0-9/_-]*$/, "a path such as /api"), z.string().url()]);
const Env = z.object({
  VITE_API_BASE_URL: ApiBase.default("/api"),
  VITE_SUPABASE_URL: z.string().url().optional(),
  VITE_SUPABASE_ANON_KEY: z.string().min(10).optional(),
});

const parsed = Env.safeParse(import.meta.env);
if (!parsed.success) console.error("Invalid dashboard configuration", parsed.error.flatten());
export const configError = parsed.success ? null : "Invalid dashboard configuration: " + JSON.stringify(parsed.error.flatten().fieldErrors);
export const config = parsed.success ? parsed.data : Env.parse({});
export const apiBase = config.VITE_API_BASE_URL.replace(/\/$/, "");
export const supabaseConfigured = Boolean(config.VITE_SUPABASE_URL && config.VITE_SUPABASE_ANON_KEY);
