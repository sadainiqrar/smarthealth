import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import type { Overview } from "@/health";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const overview: Overview = {
  window_hours: 24,
  generated_at: "2026-11-17T10:00:00Z",
  sources: { prometheus: "unavailable", jaeger: "ok" },
  components: [{ name: "API", job: "api", status: "unknown", reason: "no data" }],
  flows: [{
    key: "booking", label: "Booking", total: null, failed: null, failure_pct: null,
    p95_seconds: null, previous_total: null, status: "unknown", reason: "no data",
  }],
  backlogs: [{ key: "outbox", label: "Outbox", value: null, status: "unknown", reason: "no data" }],
  ai: {
    turns: null, errors: null, error_pct: null, first_token_p95_seconds: null,
    retrieval_no_results_pct: null, tokens: null, status: "unknown", reason: "no data",
  },
};

vi.mock("@/api", () => ({
  api: vi.fn((path: string) =>
    path.startsWith("/monitoring/failures")
      ? Promise.reject(new Error("boom"))
      : Promise.resolve(overview),
  ),
}));
vi.mock("@/assistant/client", () => ({ assistant: { startReport: vi.fn() } }));

import Health from "./Health";

it("shows its sections, the unseen-source banner and a failures error", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const host = document.createElement("div");
  const root = createRoot(host);
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter><Health /></MemoryRouter>
      </QueryClientProvider>,
    );
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  const text = host.textContent ?? "";
  for (const title of ["Critical flows", "Recent failures", "Backlogs", "AI health"]) {
    expect(text).toContain(title);
  }
  expect(text).toContain("Can't see metrics (Prometheus)");
  expect(text).toContain("Couldn't load failures");
  act(() => root.unmount());
});
