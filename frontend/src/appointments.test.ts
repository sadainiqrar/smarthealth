import { describe, expect, it } from "vitest";
import {
  appointmentsPath, canCancel, canReschedule, localInputToIso, nextVisitAction,
} from "./appointments";
import type { Visit } from "./types";

const visit = (status: Visit["status"]): Visit => ({
  appointment_id: "a-1", status, checked_in_at: "2026-11-02T10:00:00Z",
  seen_at: null, completed_at: null,
});

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

describe("canReschedule", () => {
  it("offers a move only on a confirmed appointment nobody has checked in", () => {
    expect(canReschedule("confirmed", "patient", null)).toBe(true);
    expect(canReschedule("confirmed", "front_desk", visit("checked_in"))).toBe(false);
    expect(canReschedule("pending", "admin", null)).toBe(false);
  });

  it("never to a provider", () => {
    expect(canReschedule("confirmed", "provider", null)).toBe(false);
  });
});

describe("nextVisitAction", () => {
  it("walks check-in, seen, complete in order", () => {
    expect(nextVisitAction("confirmed", null, "admin")).toBe("check-in");
    expect(nextVisitAction("confirmed", visit("checked_in"), "admin")).toBe("seen");
    expect(nextVisitAction("confirmed", visit("in_progress"), "admin")).toBe("complete");
    expect(nextVisitAction("completed", visit("completed"), "admin")).toBe(null);
  });

  it("gives each step only to the role that may take it", () => {
    expect(nextVisitAction("confirmed", null, "front_desk")).toBe("check-in");
    expect(nextVisitAction("confirmed", null, "provider")).toBe(null);
    expect(nextVisitAction("confirmed", visit("checked_in"), "provider")).toBe("seen");
    expect(nextVisitAction("confirmed", visit("checked_in"), "front_desk")).toBe(null);
    expect(nextVisitAction("confirmed", visit("in_progress"), "provider")).toBe("complete");
    expect(nextVisitAction("confirmed", null, "patient")).toBe(null);
  });

  it("offers nothing on an appointment that is not confirmed", () => {
    for (const s of ["pending", "cancelled", "rescheduled", "no_show", "failed"]) {
      expect(nextVisitAction(s, null, "admin")).toBe(null);
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

describe("localInputToIso", () => {
  it("turns a datetime-local value into an ISO string with an offset", () => {
    expect(localInputToIso("2026-11-10T09:00")).toMatch(/^2026-11-1\dT\d\d:00:00\.000Z$/);
  });
});
