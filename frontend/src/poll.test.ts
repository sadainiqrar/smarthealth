import { expect, it } from "vitest";
import { isTerminal } from "./poll";

it("keeps polling only while pending", () => {
  expect(isTerminal("pending")).toBe(false);
  for (const s of ["confirmed", "failed", "cancelled", "completed", "no_show", "rescheduled"]) {
    expect(isTerminal(s)).toBe(true);
  }
});
