import { apiBase } from "./config";
import { getToken } from "./auth";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: Record<string, unknown> = {}) {
    super(message);
  }
}

export async function api<T = unknown>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const token = await getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.json !== undefined) headers.set("Content-Type", "application/json");
  let resp: Response;
  try {
    resp = await fetch(`${apiBase}${path}`, { ...init, headers, body: init.json !== undefined ? JSON.stringify(init.json) : init.body });
  } catch {
    throw new ApiError(0, "API_UNREACHABLE", `The JARVIS API is unreachable at ${apiBase}. Check that the API service is running (scripts/dev-status.sh).`);
  }
  const text = await resp.text();
  const body = text ? safeJson(text) : null;
  if (!resp.ok) {
    const e = (body as { error?: { code: string; message: string; details?: Record<string, unknown> } })?.error;
    throw new ApiError(resp.status, e?.code ?? `HTTP_${resp.status}`, e?.message ?? resp.statusText, e?.details ?? (body as Record<string, unknown>) ?? {});
  }
  return body as T;
}

function safeJson(t: string): unknown { try { return JSON.parse(t); } catch { return t; } }

export async function apiBlobUrl(path: string): Promise<string> {
  const token = await getToken();
  const r = await fetch(`${apiBase}${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!r.ok) throw new ApiError(r.status, `HTTP_${r.status}`, "file unavailable");
  return URL.createObjectURL(await r.blob());
}
