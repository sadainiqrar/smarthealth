// Pure helpers for the appointment pages.
import type { Role } from "./auth";
import type { Visit } from "./types";

export const STATUSES = [
  "pending", "confirmed", "cancelled", "rescheduled", "completed", "no_show", "failed",
] as const;

const CANCELLERS: readonly Role[] = ["patient", "front_desk", "admin"];

// These only decide which buttons to show. The API decides: it refuses a move the state
// machine forbids, a role it does not allow, and someone else's appointment.

export function canCancel(status: string, role: Role): boolean {
  return (status === "pending" || status === "confirmed") && CANCELLERS.includes(role);
}

// Only a confirmed appointment nobody has checked in can move; the same people as cancel.
export function canReschedule(status: string, role: Role, visit: Visit | null): boolean {
  return status === "confirmed" && visit === null && CANCELLERS.includes(role);
}

export type VisitAction = "check-in" | "seen" | "complete";

const VISIT_ROLES: Record<VisitAction, readonly Role[]> = {
  "check-in": ["front_desk", "admin"],
  seen: ["provider", "admin"],
  complete: ["provider", "admin"],
};

export const VISIT_LABELS: Record<VisitAction, string> = {
  "check-in": "Check in", seen: "Mark seen", complete: "Complete visit",
};

// The one next step of the visit, mirroring the server's VISIT_PRECONDITIONS (state.py): check in a
// confirmed appointment with no visit, mark a checked-in one seen, complete one in progress.
export function nextVisitAction(status: string, visit: Visit | null, role: Role): VisitAction | null {
  if (status !== "confirmed") return null;
  let action: VisitAction | null = null;
  if (visit === null) action = "check-in";
  else if (visit.status === "checked_in") action = "seen";
  else if (visit.status === "in_progress") action = "complete";
  return action && VISIT_ROLES[action].includes(role) ? action : null;
}

export interface Filters { status: string; providerId: string }

export function appointmentsPath(filters: Filters, limit: number, offset: number): string {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.status) params.set("status", filters.status);
  if (filters.providerId) params.set("provider_id", filters.providerId);
  return `/appointments?${params}`;
}

// A <input type="datetime-local"> value has no offset; the API refuses one without (422).
// Read it as the browser's local time and send UTC.
export function localInputToIso(value: string): string {
  return new Date(value).toISOString();
}
