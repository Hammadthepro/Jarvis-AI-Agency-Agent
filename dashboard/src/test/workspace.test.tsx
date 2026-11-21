import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { ActiveTaskPanel, elapsed, type WorkItem } from "../components/Workspace";

vi.mock("../lib/events", () => ({ subscribeEvents: () => () => undefined }));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}
const task = (over: Partial<WorkItem>): WorkItem => ({
  id: "t1", name: "Veterinary clinic · Austin, TX", kind: "discovery", status: "RUNNING", state: "running", command: "find vets",
  current_operation: "Checking websites (3 of 8)", steps: [{ stage: "discovery", label: "Find businesses", state: "done" },
    { stage: "verification", label: "Check websites", state: "running" }], completed_steps: ["discovery"], remaining_steps: ["verification"],
  started_at: new Date().toISOString(), updated_at: null, completed_at: null, elapsed_s: 5, result: null, error: null,
  parent_task_id: null, running_now: 1, retries: 0, ...over,
});

describe("workspace", () => {
  it("formats elapsed time", () => {
    expect(elapsed(42)).toBe("42s");
    expect(elapsed(125)).toBe("2m 5s");
    expect(elapsed(3720)).toBe("1h 2m");
  });

  it("shows the real current operation and steps, with no percentage", () => {
    wrap(<ActiveTaskPanel task={task({})} />);
    expect(screen.getByTestId("current-operation").textContent).toBe("Checking websites (3 of 8)");
    expect(screen.getByRole("button", { name: "Pause" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/\d+%/);
  });

  it("offers resume for a paused task and retry for a failed one", () => {
    const { unmount } = wrap(<ActiveTaskPanel task={task({ status: "PAUSED", state: "paused" })} />);
    expect(screen.getByRole("button", { name: "Resume" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    unmount();
    wrap(<ActiveTaskPanel task={task({ status: "FAILED", state: "failed", error: "SerpApi NOT_CONFIGURED", completed_at: new Date().toISOString() })} />);
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("NOT_CONFIGURED");
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });

  it("says nothing is running when there is no task", () => {
    wrap(<ActiveTaskPanel task={undefined} />);
    expect(screen.getByText(/Nothing is running/)).toBeTruthy();
  });
});
