// Pure helpers for the Appointments page.
import type { Role } from "./auth";

export const STATUSES = [
  "pending", "confirmed", "cancelled", "rescheduled", "completed", "no_show", "failed",
] as const;

const CANCELLERS: readonly Role[] = ["patient", "front_desk", "admin"];

// Only hides the button. The API decides: it refuses a cancel the state machine forbids
// (a checked-in visit, a terminal status) and one on someone else's appointment.
export function canCancel(status: string, role: Role): boolean {
  return (status === "pending" || status === "confirmed") && CANCELLERS.includes(role);
}

export interface Filters { status: string; providerId: string }

export function appointmentsPath(filters: Filters, limit: number, offset: number): string {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.status) params.set("status", filters.status);
  if (filters.providerId) params.set("provider_id", filters.providerId);
  return `/appointments?${params}`;
}
