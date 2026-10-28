// Poll a booking while it is pending. Stop on any other status. Never key on
// `in_progress`: it is true only before the appointment row exists (README).
export const POLL_MS = 1000;
export const isTerminal = (status: string): boolean => status !== "pending";
