import { config } from "./config";
import { getToken } from "./auth";

export type JarvisEvent = { id: number; campaign_id: string | null; event_type: string; message: string; data: Record<string, unknown>; created_at: string };

/** Authenticated SSE over fetch (EventSource cannot send headers). Resumes with Last-Event-ID after drops. */
export function subscribeEvents(onEvent: (e: JarvisEvent) => void, opts: { campaignId?: string; onStatus?: (s: "live" | "reconnecting") => void } = {}) {
  let stopped = false;
  let lastId: string | null = null;
  const ctrl = { abort: () => {} };
  (async function loop() {
    let delay = 1000;
    while (!stopped) {
      const ac = new AbortController();
      ctrl.abort = () => ac.abort();
      try {
        const token = await getToken();
        const qs = opts.campaignId ? `?campaign_id=${opts.campaignId}` : "";
        const r = await fetch(`${config.VITE_API_BASE_URL}/events${qs}`, {
          headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(lastId ? { "Last-Event-ID": lastId } : {}) }, signal: ac.signal,
        });
        if (!r.ok || !r.body) throw new Error(`events ${r.status}`);
        opts.onStatus?.("live");
        delay = 1000;
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n\n")) >= 0) {
            const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
            const id = chunk.match(/^id: (.*)$/m)?.[1];
            const data = chunk.match(/^data: (.*)$/m)?.[1];
            if (id) lastId = id;
            if (data) onEvent(JSON.parse(data));
          }
        }
      } catch { /* fall through to reconnect */ }
      if (stopped) break;
      opts.onStatus?.("reconnecting");
      await new Promise((res) => setTimeout(res, delay));
      delay = Math.min(delay * 2, 15000);
    }
  })();
  return () => { stopped = true; ctrl.abort(); };
}
