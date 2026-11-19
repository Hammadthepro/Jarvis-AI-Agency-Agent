import { z } from "zod";

const Env = z.object({
  VITE_API_BASE_URL: z.string().url().default("http://localhost:8000"),
  VITE_SUPABASE_URL: z.string().url().optional(),
  VITE_SUPABASE_ANON_KEY: z.string().min(10).optional(),
});

const parsed = Env.safeParse(import.meta.env);
if (!parsed.success) console.error("Invalid dashboard configuration", parsed.error.flatten());
export const config = parsed.success ? parsed.data : Env.parse({});
export const supabaseConfigured = Boolean(config.VITE_SUPABASE_URL && config.VITE_SUPABASE_ANON_KEY);
