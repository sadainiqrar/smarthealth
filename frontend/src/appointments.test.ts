import { describe, expect, it } from "vitest";
import { appointmentsPath, canCancel } from "./appointments";

describe("canCancel", () => {
  it("offers cancel only on a live appointment, to the roles the API allows", () => {
    for (const role of ["patient", "front_desk", "admin"] as const) {
      expect(canCancel("pending", role)).toBe(true);
      expect(canCancel("confirmed", role)).toBe(true);
    }
    expect(canCancel("confirmed", "provider")).toBe(false);
  });

  it("never offers cancel on a settled appointment", () => {
    for (const s of ["cancelled", "rescheduled", "completed", "no_show", "failed"]) {
      expect(canCancel(s, "admin")).toBe(false);
    }
  });
});

describe("appointmentsPath", () => {
  it("sends only the filters that are set", () => {
    expect(appointmentsPath({ status: "", providerId: "" }, 20, 0))
      .toBe("/appointments?limit=20&offset=0");
    expect(appointmentsPath({ status: "cancelled", providerId: "p-1" }, 20, 40))
      .toBe("/appointments?limit=20&offset=40&status=cancelled&provider_id=p-1");
  });
});
