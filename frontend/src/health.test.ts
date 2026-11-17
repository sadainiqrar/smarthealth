import { describe, expect, it } from "vitest";
import { overallStatus, statusLabel, trend, unseenSources } from "./health";

describe("health helpers", () => {
  it("names each status in plain words", () => {
    expect(statusLabel("ok")).toBe("Healthy");
    expect(statusLabel("warn")).toBe("Needs a look");
    expect(statusLabel("bad")).toBe("Failing");
    expect(statusLabel("idle")).toBe("No activity");
    expect(statusLabel("unknown")).toBe("Can't tell");
  });

  it("overall status is the worst one", () => {
    expect(overallStatus(["ok", "idle", "warn"])).toBe("warn");
    expect(overallStatus(["ok", "bad", "unknown"])).toBe("bad");
    expect(overallStatus([])).toBe("unknown");
  });

  it("trend compares with the window before", () => {
    expect(trend(40, 35)).toBe("up");
    expect(trend(30, 35)).toBe("down");
    expect(trend(35, 35)).toBe("same");
    expect(trend(null, 35)).toBe("same");
  });

  it("lists the sources it cannot see", () => {
    expect(unseenSources({ prometheus: "unavailable", jaeger: "ok" })).toEqual(["metrics (Prometheus)"]);
    expect(unseenSources({ prometheus: "ok", jaeger: "not_configured" })).toEqual(["traces (Jaeger)"]);
  });
});
