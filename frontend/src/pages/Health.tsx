// System health, for admin: is everything running, what failed recently and why, and how
// the AI is doing -- Part A's GET /monitoring/* in plain words (system health spec §5).

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ArrowDownIcon, ArrowUpIcon, FileTextIcon, MinusIcon } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "@/api";
import { assistant } from "@/assistant/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  type Failures, type Overview, type Status, overallStatus, statusLabel, TONE, trend, unseenSources,
} from "@/health";

const RANGES = [
  { hours: 1, label: "1 hour" },
  { hours: 6, label: "6 hours" },
  { hours: 24, label: "24 hours" },
];

function Dot({ status }: { status: Status }) {
  return (
    <>
      <span className={`inline-block size-2.5 shrink-0 rounded-full ${TONE[status]}`} aria-hidden />
      <span className="sr-only">{statusLabel(status)}</span>
    </>
  );
}

function Pill({ status }: { status: Status }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium">
      <span className={`inline-block size-2.5 shrink-0 rounded-full ${TONE[status]}`} aria-hidden /> {statusLabel(status)}
    </span>
  );
}

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export default function Health() {
  const [hours, setHours] = useState(24);
  const [starting, setStarting] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const navigate = useNavigate();
  const overview = useQuery({
    queryKey: ["monitoring", "overview", hours],
    queryFn: () => api<Overview>(`/monitoring/overview?hours=${hours}`),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
  const failures = useQuery({
    queryKey: ["monitoring", "failures", hours],
    queryFn: () => api<Failures>(`/monitoring/failures?hours=${hours}&limit=20`),
    refetchInterval: 30_000,
    placeholderData: keepPreviousData,
  });
  const data = overview.data;

  async function writeReport() {
    setStarting(true);
    setReportError(null);
    try {
      const job = await assistant.startReport({ kind: "system_health" });
      navigate(`/reports?report=${encodeURIComponent(job.reportId)}`);
    } catch (e) {
      setReportError(e instanceof Error ? e.message : "unknown error");
    } finally {
      setStarting(false);
    }
  }

  const overall = data
    ? overallStatus([...data.components, ...data.flows, ...data.backlogs, data.ai].map((x) => x.status))
    : "unknown";
  const unseen = data ? unseenSources(data.sources) : [];

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <h1 className="flex items-center gap-2 text-lg font-semibold">
            System health {data && <Pill status={overall} />}
          </h1>
          <p className="text-sm text-muted-foreground">
            Every part of the platform, its critical flows and recent failures — refreshed every 30 seconds
          </p>
        </div>
        <div className="flex flex-wrap gap-1">
          {RANGES.map((r) => (
            <Button key={r.hours} size="sm" variant={r.hours === hours ? "default" : "outline"} onClick={() => setHours(r.hours)}>
              {r.label}
            </Button>
          ))}
          <Button size="sm" variant="secondary" onClick={() => void writeReport()} disabled={starting}>
            {starting ? <Spinner /> : <FileTextIcon />} Write a health report
          </Button>
        </div>
      </div>

      {reportError && <p className="text-sm text-destructive">Couldn't start the report: {reportError}</p>}
      {overview.isPending && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner /> Checking…</div>
      )}
      {overview.isError && <p className="text-sm text-destructive">Couldn't load health: {overview.error.message}</p>}
      {unseen.length > 0 && (
        <p className="rounded-md border border-amber-400 bg-muted px-3 py-2 text-sm text-foreground">
          Can't see {unseen.join(" or ")} right now — those cards say "Can't tell" until it is back.
        </p>
      )}

      {data && (
        <>
          <section className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            {data.components.map((c) => (
              <Card key={c.job} className="gap-1 py-3">
                <CardContent className="flex flex-col gap-1 px-3">
                  <span className="flex items-center gap-2 text-sm font-medium"><Dot status={c.status} /> {c.name}</span>
                  <span className="text-xs text-muted-foreground">{c.reason}</span>
                </CardContent>
              </Card>
            ))}
          </section>

          <Card>
            <CardHeader>
              <CardTitle>Critical flows</CardTitle>
              <CardDescription>Runs in the last {RANGES.find((r) => r.hours === hours)!.label}, against the window before</CardDescription>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Flow</TableHead><TableHead className="text-right">Runs</TableHead>
                    <TableHead className="text-right">Failed</TableHead><TableHead className="text-right">Slowest 5% take</TableHead>
                    <TableHead>Status</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.flows.map((f) => {
                    const t = trend(f.total, f.previous_total);
                    return (
                      <TableRow key={f.key}>
                        <TableCell className="font-medium">{f.label}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          <span className="inline-flex items-center gap-1">
                            {f.total ?? "—"}
                            {t === "up" ? <ArrowUpIcon className="size-3" role="img" aria-label="more than the window before" />
                              : t === "down" ? <ArrowDownIcon className="size-3" role="img" aria-label="fewer than the window before" />
                              : <MinusIcon className="size-3 opacity-40" role="img" aria-label="same as the window before" />}
                          </span>
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{f.failed ?? "—"}{f.failure_pct !== null && f.failed ? ` (${f.failure_pct}%)` : ""}</TableCell>
                        <TableCell className="text-right tabular-nums">{f.p95_seconds !== null ? `${f.p95_seconds} s` : "—"}</TableCell>
                        <TableCell><Pill status={f.status} /> <span className="text-xs text-muted-foreground">{f.reason}</span></TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent failures</CardTitle>
              <CardDescription>Newest first, from the traces. "View trace" opens Jaeger (tools login).</CardDescription>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              {failures.isPending ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner /> Loading failures…</div>
              ) : failures.isError ? (
                <p className="text-sm text-destructive">Couldn't load failures: {failures.error.message}</p>
              ) : failures.data.source === "not_configured" ? (
                <p className="text-sm text-muted-foreground">Traces are not configured here.</p>
              ) : failures.data.source !== "ok" ? (
                <p className="text-sm text-muted-foreground">Can't reach the traces right now (Jaeger).</p>
              ) : failures.data.failures.length === 0 ? (
                <p className="text-sm text-muted-foreground">No failures in this window.</p>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow><TableHead>When</TableHead><TableHead>Where</TableHead><TableHead>What failed</TableHead><TableHead /></TableRow>
                  </TableHeader>
                  <TableBody>
                    {failures.data.failures.map((f) => (
                      <TableRow key={f.trace_id}>
                        <TableCell className="whitespace-nowrap">{when(f.at)}</TableCell>
                        <TableCell><div className="font-medium">{f.operation}</div><div className="text-xs text-muted-foreground">{f.service}</div></TableCell>
                        <TableCell><div>{f.failed_step}</div><div className="text-xs text-destructive">{f.message}</div></TableCell>
                        <TableCell><a className="text-sm underline" href={f.trace_path} target="_blank" rel="noreferrer"
                             aria-label={`View trace for ${f.operation} at ${when(f.at)} (opens in a new tab)`}>View trace</a></TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>

          <section className="grid gap-3 lg:grid-cols-2">
            <Card>
              <CardHeader><CardTitle>Backlogs</CardTitle><CardDescription>Work waiting to be done</CardDescription></CardHeader>
              <CardContent className="flex flex-col gap-2">
                {data.backlogs.map((b) => (
                  <div key={b.key} className="flex items-center justify-between gap-3 text-sm">
                    <span className="flex items-center gap-2"><Dot status={b.status} /> {b.label}</span>
                    <span className="text-muted-foreground">{b.reason}</span>
                  </div>
                ))}
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">AI health <Pill status={data.ai.status} /></CardTitle>
                <CardDescription>{data.ai.reason} · <Link className="underline" to="/usage">AI usage</Link></CardDescription>
              </CardHeader>
              <CardContent className="grid grid-cols-2 gap-3 text-sm">
                <div><div className="text-muted-foreground">Answers</div><div className="text-lg font-semibold">{data.ai.turns ?? "—"}</div></div>
                <div><div className="text-muted-foreground">Errors</div><div className="text-lg font-semibold">{data.ai.error_pct !== null ? `${data.ai.error_pct}%` : "—"}</div></div>
                <div><div className="text-muted-foreground">First word (slowest 5%)</div><div className="text-lg font-semibold">{data.ai.first_token_p95_seconds !== null ? `${data.ai.first_token_p95_seconds} s` : "—"}</div></div>
                <div><div className="text-muted-foreground">Searches with no result</div><div className="text-lg font-semibold">{data.ai.retrieval_no_results_pct !== null ? `${data.ai.retrieval_no_results_pct}%` : "—"}</div></div>
              </CardContent>
            </Card>
          </section>
          <p className="text-xs text-muted-foreground">Checked {when(data.generated_at)}. Business figures are on Reports; engineering detail is in Grafana.</p>
        </>
      )}
    </div>
  );
}
