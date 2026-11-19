import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/** The browser always talks to the API through this server's own origin (/api/*), so it never needs the API's
 *  address: no localhost-from-the-browser problem in Codespaces and no CORS. JARVIS_API_PROXY_TARGET is where
 *  the dev/preview server forwards to (default: the API on this machine). A production build that is served
 *  elsewhere sets VITE_API_BASE_URL to the API's public URL instead. */
const target = process.env.JARVIS_API_PROXY_TARGET ?? "http://127.0.0.1:8000";
const proxy = { "/api": { target, changeOrigin: true, rewrite: (p: string) => p.replace(/^\/api/, "") } };
const allowedHosts = [".app.github.dev", ".githubpreview.dev", "localhost", "127.0.0.1"];

export default defineConfig({
  envDir: "..",                         // the repository-root .env, shared with the API and worker (only VITE_* is exposed)
  plugins: [react(), tailwindcss()],
  server: { host: true, port: 5173, strictPort: true, allowedHosts, proxy },
  preview: { host: true, port: 4173, strictPort: true, allowedHosts, proxy },
  test: { environment: "jsdom", globals: true, setupFiles: ["src/test/setup.ts"] },
} as never);
