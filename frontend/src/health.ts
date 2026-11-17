// Types for GET /monitoring/* and the pure helpers the System health page uses.

export type Status = "ok" | "warn" | "bad" | "idle" | "unknown";
export type SourceStatus = "ok" | "unavailable" | "not_configured";

export interface Component { name: string; job: string; status: Status; reason: string }
export interface Flow {
  key: string; label: string; total: number | null; failed: number | null;
  failure_pct: number | null; p95_seconds: number | null; previous_total: number | null;
  status: Status; reason: string;
}
export interface Backlog { key: string; label: string; value: number | null; status: Status; reason: string }
export interface AiHealth {
  turns: number | null; errors: number | null; error_pct: number | null;
  first_token_p95_seconds: number | null; retrieval_no_results_pct: number | null;
  tokens: number | null; status: Status; reason: string;
}
export interface Overview {
  window_hours: number; generated_at: string;
  sources: { prometheus: SourceStatus; jaeger: SourceStatus };
  components: Component[]; flows: Flow[]; backlogs: Backlog[]; ai: AiHealth;
}
export interface Failure {
  trace_id: string; at: string; service: string; operation: string;
  failed_step: string; message: string; trace_path: string;
}
export interface Failures { window_hours: number; source: SourceStatus; failures: Failure[] }

const LABEL: Record<Status, string> = {
  ok: "Healthy", warn: "Needs a look", bad: "Failing", idle: "No activity", unknown: "Can't tell",
};
const RANK: Record<Status, number> = { bad: 4, warn: 3, unknown: 2, ok: 1, idle: 0 };

export function statusLabel(status: Status): string {
  return LABEL[status];
}

export function overallStatus(statuses: Status[]): Status {
  return statuses.reduce<Status | null>((worst, s) => (worst === null || RANK[s] > RANK[worst] ? s : worst), null) ?? "unknown";
}

export function trend(now: number | null, before: number | null): "up" | "down" | "same" {
  if (now === null || before === null || now === before) return "same";
  return now > before ? "up" : "down";
}

export function unseenSources(sources: Overview["sources"]): string[] {
  const out: string[] = [];
  if (sources.prometheus !== "ok") out.push("metrics (Prometheus)");
  if (sources.jaeger !== "ok") out.push("traces (Jaeger)");
  return out;
}

/** Tailwind classes per status: dot colour and badge tone. */
export const TONE: Record<Status, string> = {
  ok: "bg-emerald-500", warn: "bg-amber-500", bad: "bg-red-500",
  idle: "bg-slate-400", unknown: "bg-slate-300",
};
