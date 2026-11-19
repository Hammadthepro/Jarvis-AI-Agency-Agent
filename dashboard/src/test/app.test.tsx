import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/api";
import { ErrorState, Pill } from "../components/ui";
import { ImportSchema, parseCsv } from "../pages/Campaigns";
import Console, { commandSchema } from "../pages/Console";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}

afterEach(() => vi.restoreAllMocks());
/** API path as the app requests it (the dashboard calls the same-origin /api proxy). */
const pathOf = (input: unknown) => new URL(String(input), "http://localhost").pathname.replace(/^\/api/, "");

describe("validation", () => {
  it("rejects empty and over-long commands", () => {
    expect(commandSchema.safeParse("  ").success).toBe(false);
    expect(commandSchema.safeParse("x".repeat(2001)).success).toBe(false);
    expect(commandSchema.safeParse("Find 10 plumbers in Houston").success).toBe(true);
  });
  it("parses CSV and requires rights attestation", () => {
    const rows = parseCsv("business_name,phone,city,state\nBayou City Plumbing Co,(713) 555-0142,Houston,TX\n");
    expect(rows).toEqual([{ business_name: "Bayou City Plumbing Co", phone: "(713) 555-0142", city: "Houston", state: "TX" }]);
    expect(ImportSchema.safeParse({ niche: "plumbing", city: "Houston", state: "TX", rights: false, rows }).success).toBe(false);
    expect(ImportSchema.safeParse({ niche: "plumbing", city: "Houston", state: "TX", rights: true, rows }).success).toBe(true);
  });
});

describe("honest states", () => {
  it("shows the error code and message, never a fake success", () => {
    render(<ErrorState error={new ApiError(403, "COMPLIANCE_BLOCKED", "No approval recorded")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("COMPLIANCE_BLOCKED");
    expect(screen.getByRole("alert")).toHaveTextContent("No approval recorded");
  });
  it("renders integration states as labelled pills", () => {
    render(<Pill>NOT_CONFIGURED</Pill>);
    expect(screen.getByText("NOT_CONFIGURED").className).toMatch(/warn/);
  });
});

describe("Console command workflow", () => {
  it("sends a command, shows the clarification and answers it", async () => {
    const responses: Record<string, unknown> = {
      "POST /commands": { id: "c1", status: "needs_clarification", raw_text: "x", parser: "deterministic",
        parsed: { niche: "plumbing", location: { city: "Houston", state: "TX" }, target_count: 100, minimum_google_rating: "3.5", website_filter: ["NO_WEBSITE"] },
        questions: [{ field: "missing_rating_policy", prompt: "Exclude unrated businesses?", options: ["exclude", "include_for_review"], preselect: "exclude" }] },
      "POST /commands/c1/answer": { id: "c1", status: "awaiting_confirmation", raw_text: "x", parser: "deterministic",
        parsed: { niche: "plumbing", location: { city: "Houston", state: "TX" }, target_count: 100, minimum_google_rating: "3.5", missing_rating_policy: "exclude", discovery_provider: "google_places" },
        questions: [], plan: { places_requests_max: 99, serp_searches_max: 375, firecrawl_credits_max: 290, claude_usd_max: 0.3, estimated_usd_max_if_paid: 14, note: "n" },
        compliance: { open: false, reason: "No approval recorded for 'google_places_prospect_selection'." } },
      "GET /commands": [],
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const urlPath = pathOf(input);
      const key = `${(init?.method ?? "GET").toUpperCase()} ${urlPath}`;
      if (urlPath === "/events") return new Response(new ReadableStream(), { status: 200 });
      return new Response(JSON.stringify(responses[key] ?? {}), { status: 200 });
    });
    wrap(<Console />);
    await userEvent.type(screen.getByLabelText("Command"), "Find 100 plumbers in Houston with at least a 3.5 Google rating and no website.");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Exclude unrated businesses?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "exclude" }));
    await waitFor(() => expect(screen.getByText(/blocked pending approval/)).toBeInTheDocument());
    expect(screen.getByText("99")).toBeInTheDocument();
  });

  it("shows API_UNREACHABLE when the API is down", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    wrap(<Console />);
    await userEvent.type(screen.getByLabelText("Command"), "Find 3 plumbers in Houston");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect((await screen.findAllByText("API_UNREACHABLE")).length).toBeGreaterThan(0);
  });
});

describe("Usage and costs", () => {
  it("labels JARVIS costs as local estimates and never shows an unconfirmed invoice", async () => {
    const { default: UsagePage } = await import("../pages/Usage");
    const usage = { summary: [{ provider: "anthropic", reconciled: false, unknown_spend: true, invoice_status: "UNAVAILABLE", jarvis_cost_basis: "local_estimate",
      jarvis: { estimated_micros: 9000, reserved_micros: 0, committed_micros: 4200, ambiguous_micros: 0, released_micros: 0, committed_units: 1500, calls: 1, response_tokens: 1500 },
      provider_reported: { status: "unavailable", detail: "Account-level usage and billing UNAVAILABLE" } }] };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = pathOf(input);
      const body = path === "/usage" ? usage : [];
      return new Response(JSON.stringify(body), { status: 200 });
    });
    wrap(<UsagePage />);
    expect(await screen.findByText("Committed (local est.)", { selector: "th" })).toBeInTheDocument();
    expect(screen.getByText("UNAVAILABLE")).toBeInTheDocument();
    expect(screen.getByText(/UNKNOWN \(not zero\)/)).toBeInTheDocument();
    expect(screen.getByText(/local estimates from the configured price table, not provider invoices/)).toBeInTheDocument();
    expect(screen.queryByText("CONFIRMED")).not.toBeInTheDocument();
  });
});

const field = (o: Partial<Record<string, unknown>>) => ({ name: "api_key", label: "API key", secret: true, required: true, hint: "", choices: [], default: null,
  set: false, source: null, display: null, ...o });
const card = (o: Record<string, unknown>) => ({ provider: "serp_api", name: "SerpApi", purpose: "Independent web search", docs_url: "https://x", notes: [],
  managed_in_dashboard: true, configured: false, enabled: true, state: "NOT_CONFIGURED", provider_state: null, detail: "Missing: API key", tested: false,
  last_checked_at: null, last_success_at: null, last_error: null, last_error_at: null, capabilities: ["website_verification"], validated_capabilities: [],
  quota: null, usage: null, fields: [field({})], has_dashboard_credentials: false, runtime: null, compliance: null, ...o });

describe("Integrations Center", () => {
  function mockApi(listing: Record<string, unknown>, calls: Array<{ path: string; method: string; body?: string }>) {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = pathOf(input); const method = init?.method ?? "GET";
      calls.push({ path, method, body: init?.body ? String(init.body) : undefined });
      if (path === "/integrations") return new Response(JSON.stringify(listing), { status: 200 });
      if (path.endsWith("/test")) return new Response(JSON.stringify({ provider: "serp_api", state: "INVALID_CREDENTIALS", detail: "Invalid API key.", http_status: 401 }), { status: 200 });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
  }
  const listing = (o: Record<string, unknown> = {}) => ({ can_manage: true, vault: { ready: true, key_id: "abc", detail: null }, credentials_error: null,
    integrations: [card({}), card({ provider: "google_places", name: "Google Places API (New)", state: "BLOCKED_PENDING_APPROVAL", configured: true }),
      card({ provider: "cloudflare", name: "Cloudflare Pages", state: "CONNECTED", configured: true, last_success_at: "2026-11-19T07:00:00Z",
        fields: [field({ name: "api_token", label: "API token", set: true, source: "dashboard", display: "••••••••" }),
          field({ name: "account_id", label: "Account ID", secret: false, set: true, source: "dashboard", display: "a".repeat(32) })] })], ...o });

  it("renders real states from the API and keeps secrets out of the page", async () => {
    const calls: Array<{ path: string; method: string; body?: string }> = [];
    mockApi(listing(), calls);
    const { default: IntegrationsPage } = await import("../pages/Integrations");
    wrap(<IntegrationsPage />);
    expect(await screen.findByText("NOT_CONFIGURED")).toBeInTheDocument();
    expect(screen.getByText("BLOCKED_PENDING_APPROVAL")).toBeInTheDocument();
    expect(screen.getByText("CONNECTED")).toBeInTheDocument();
    expect(screen.getByText(/API token: saved \(dashboard\)/)).toBeInTheDocument();
    await userEvent.click(within(screen.getByRole("region", { name: "SerpApi" })).getByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("INVALID_CREDENTIALS")).toBeInTheDocument();
    expect(screen.getByText("Invalid API key.")).toBeInTheDocument();
  });

  it("submits secrets write-only, never pre-fills them, and clears the form", async () => {
    const calls: Array<{ path: string; method: string; body?: string }> = [];
    mockApi(listing(), calls);
    const { default: IntegrationsPage } = await import("../pages/Integrations");
    wrap(<IntegrationsPage />);
    const region = await screen.findByRole("region", { name: "Cloudflare Pages" });
    await userEvent.click(within(region).getByRole("button", { name: "Update" }));
    const token = screen.getByLabelText(/API token/) as HTMLInputElement;
    expect(token.type).toBe("password");
    expect(token.value).toBe("");                                   // the saved secret is never sent back or pre-filled
    await userEvent.type(token, "cf-new-token-value-0123456789abcdef0123");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.path).toBe("/integrations/cloudflare/credentials");
    expect(JSON.parse(put.body!)).toEqual({ secrets: { api_token: "cf-new-token-value-0123456789abcdef0123" }, config: {} });
    expect(window.localStorage.length + window.sessionStorage.length).toBe(0);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("is read-only for non-admins and explains a missing encryption key", async () => {
    mockApi(listing({ can_manage: false, vault: { ready: false, key_id: null, detail: "JARVIS_ENCRYPTION_KEY is not set" } }), []);
    const { default: IntegrationsPage } = await import("../pages/Integrations");
    wrap(<IntegrationsPage />);
    expect(await screen.findByText(/JARVIS_ENCRYPTION_KEY is not set/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Configure" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Test connection" }).length).toBe(3);
  });
});

describe("Voice", () => {
  it("maps real agent states and parses agent messages", async () => {
    const { phaseFromAgentState, parseJarvisMessage } = await import("../lib/voice");
    expect(phaseFromAgentState("listening")).toBe("listening");
    expect(phaseFromAgentState("thinking")).toBe("thinking");
    expect(phaseFromAgentState("speaking")).toBe("speaking");
    expect(phaseFromAgentState(undefined)).toBeNull();
    expect(parseJarvisMessage(new TextEncoder().encode(JSON.stringify({ type: "status", text: "Campaign authorized." })))).toEqual({ type: "status", text: "Campaign authorized." });
    expect(parseJarvisMessage(new TextEncoder().encode("garbage"))).toBeNull();
  });

  it("shows exactly what is missing, links to Integrations, and never requests a token when not ready", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = pathOf(input); calls.push(path);
      if (path === "/voice/status") return new Response(JSON.stringify({ state: "NOT_CONFIGURED", checks: { livekit_configured: false, livekit_validated: false,
        agent_secret_configured: false, agent_online: false }, missing: ["LiveKit server URL, API key and API secret (Integrations > LiveKit)."], agent: null, token_ttl_s: 600 }), { status: 200 });
      return new Response("{}", { status: 200 });
    });
    const { default: VoicePage } = await import("../pages/Voice");
    wrap(<VoicePage />);
    expect(await screen.findByText(/LiveKit server URL, API key and API secret/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Integrations/ })).toHaveAttribute("href", "/integrations");
    expect(screen.getByRole("button", { name: "Start voice session" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Talk to JARVIS" }));
    expect(await screen.findByText("Voice is not available yet")).toBeInTheDocument();
    expect(calls).not.toContain("/voice/token");
  });

  it("reports a denied microphone clearly", async () => {
    const { requestMicrophone } = await import("../lib/voice");
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: () => Promise.reject(Object.assign(new Error("no"), { name: "NotAllowedError" })) } });
    expect(await requestMicrophone()).toBe("denied");
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
    expect(await requestMicrophone()).toBe("unavailable");
  });
});

describe("No login screen", () => {
  function mockAll(me: { status: number; body: unknown }) {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const p = pathOf(input);
      if (p === "/events") return new Response(new ReadableStream(), { status: 200 });
      if (p === "/me") return new Response(JSON.stringify(me.body), { status: me.status });
      return new Response(JSON.stringify(p === "/integrations" ? { integrations: [], can_manage: true, vault: { ready: true }, credentials_error: null } : []), { status: 200 });
    });
  }

  it("opens directly to the Command Center with no sign-in form, using the same-origin API proxy", async () => {
    mockAll({ status: 200, body: { email: "local-operator@jarvis.local", role: "owner", auth_mode: "local_development", environment: "local" } });
    const { App } = await import("../main");
    wrap(<App />);
    expect(await screen.findByLabelText("Message JARVIS")).toBeInTheDocument();          // voice-first JARVIS screen
    expect(await screen.findByText(/Local development mode/)).toBeInTheDocument();
    expect(document.querySelector('input[type="password"], input[type="email"]')).toBeNull();
    expect(screen.queryByRole("button", { name: /sign in|sign out/i })).toBeNull();
    const calls = (globalThis.fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls.map((c) => String(c[0]));
    expect(calls.every((u) => u.startsWith("/api/"))).toBe(true);           // never localhost:8000 from the browser
  });

  it("explains a 401 instead of showing a login form", async () => {
    mockAll({ status: 401, body: { error: { code: "UNAUTHORIZED", message: "missing bearer token" } } });
    const { App } = await import("../main");
    wrap(<App />);
    expect(await screen.findByText(/local development access is not enabled/)).toBeInTheDocument();
    expect(document.querySelector('input[type="password"], input[type="email"]')).toBeNull();
  });
});

describe("Voice-first JARVIS screen", () => {
  it("derives the orb state only from real signals", async () => {
    const { orbMode } = await import("../pages/CommandCenter");
    expect(orbMode("listening", true, false, false)).toBe("listening");
    expect(orbMode("speaking", true, false, true)).toBe("speaking");
    expect(orbMode("idle", true, true, false)).toBe("thinking");
    expect(orbMode("idle", true, false, true)).toBe("executing");
    expect(orbMode("idle", false, false, false)).toBe("unavailable");
    expect(orbMode("mic_denied", true, false, false)).toBe("error");
    expect(orbMode("idle", true, false, false)).toBe("idle");
  });

  it("shows the shared voice+text conversation and confirms through the same conversation", async () => {
    const posts: Array<{ path: string; body: string }> = [];
    const convo = [
      { id: 1, role: "user", channel: "voice", content: "Look at Bayou City Plumbing and build them a better site.", meta: {}, created_at: "2026-11-19T10:00:00Z" },
      { id: 2, role: "assistant", channel: "voice", content: "I can verify, research and rebuild their site. Shall I start?", meta: { kind: "proposal", agent: "claude" }, created_at: "2026-11-19T10:00:02Z" },
    ];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const p = pathOf(input);
      if (init?.method === "POST") { posts.push({ path: p, body: String(init.body) }); return new Response(JSON.stringify({ say: "On it." }), { status: 200 }); }
      if (p === "/conversation/messages") return new Response(JSON.stringify(convo), { status: 200 });
      if (p === "/voice/status") return new Response(JSON.stringify({ state: "NOT_CONFIGURED", checks: {}, missing: ["LiveKit server URL, API key and API secret (Integrations > LiveKit)."], agent: null, token_ttl_s: 600 }), { status: 200 });
      if (p === "/integrations") return new Response(JSON.stringify({ integrations: [], can_manage: true, vault: { ready: true }, credentials_error: null }), { status: 200 });
      return new Response("[]", { status: 200 });
    });
    const { default: CommandCenterPage } = await import("../pages/CommandCenter");
    wrap(<CommandCenterPage />);
    expect(await screen.findByText("Look at Bayou City Plumbing and build them a better site.")).toBeInTheDocument();
    expect(screen.getByText(/Shall I start\?/)).toBeInTheDocument();
    expect(await screen.findByText(/Voice: LiveKit server URL/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Yes, go ahead" }));
    await waitFor(() => expect(posts).toEqual([{ path: "/conversation/turn", body: JSON.stringify({ text: "Yes, go ahead." }) }]));
    await userEvent.type(screen.getByLabelText("Message JARVIS"), "What have you completed today?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(posts[1]).toEqual({ path: "/conversation/turn", body: JSON.stringify({ text: "What have you completed today?" }) }));
  });
});
