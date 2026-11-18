import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/api";
import { ErrorState, Pill } from "../components/ui";
import { ImportSchema, parseCsv } from "../pages/Campaigns";
import Console, { commandSchema } from "../pages/Console";
import { LoginSchema } from "../pages/Login";

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}

afterEach(() => vi.restoreAllMocks());

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
  it("validates login email", () => {
    expect(LoginSchema.safeParse({ email: "nope" }).success).toBe(false);
    expect(LoginSchema.safeParse({ email: "a@b.co" }).success).toBe(true);
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
      const url = new URL(String(input));
      const key = `${(init?.method ?? "GET").toUpperCase()} ${url.pathname}`;
      if (url.pathname === "/events") return new Response(new ReadableStream(), { status: 200 });
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
