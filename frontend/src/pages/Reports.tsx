// Reports, for admin (PART-B-FR-4): written in the background, streamed while they are
// written. Starting one answers at once; the page then watches it -- and so can any other
// tab, or this one after a reload -- replaying what is written so far and following it to
// the end. Every figure comes from Part A's analytics; the model only explains them.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarRangeIcon, FileBarChartIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { assistant, streamTurn } from "@/assistant/client";
import { applyEvent, newAssistantTurn, type AssistantTurn } from "@/assistant/conversation";
import { ErrorNote, Prose, ResultCard, Steps } from "@/assistant/parts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

const REPORTS_KEY = ["assistant", "reports"] as const;

const STATUS: Record<string, { label: string; variant: "secondary" | "destructive" | "outline" }> = {
  running: { label: "Writing…", variant: "outline" },
  done: { label: "Done", variant: "secondary" },
  failed: { label: "Failed", variant: "destructive" },
};

function useReport(reportId: string | null, onFinished: () => void) {
  const [turn, setTurn] = useState<AssistantTurn | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!reportId) return;
    const controller = new AbortController();
    setTurn(newAssistantTurn(reportId));
    setError(null);
    streamTurn(
      (resumeFrom, signal) => assistant.watchReport({ reportId, resumeFrom }, { signal }),
      (event) => setTurn((current) => (current ? applyEvent(current, event) : current)),
      controller.signal,
    )
      .then(onFinished)
      .catch((e: Error) => setError(e.message));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- onFinished only refreshes a list
  }, [reportId]);
  return { turn, error };
}

export default function Reports() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("report");
  const client = useQueryClient();
  const [starting, setStarting] = useState<string | null>(null);
  const reports = useQuery({
    queryKey: REPORTS_KEY,
    queryFn: async () => (await assistant.listReports({ limit: 30 })).reports,
  });
  const refresh = () => void client.invalidateQueries({ queryKey: REPORTS_KEY });
  const { turn, error } = useReport(selected, refresh);
  const current = reports.data?.find((r) => r.reportId === selected);

  async function start(kind: string) {
    setStarting(kind);
    try {
      const job = await assistant.startReport({ kind });
      refresh();
      setParams({ report: job.reportId });
    } finally {
      setStarting(null);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-lg font-semibold">Reports</h1>
          <p className="text-sm text-muted-foreground">
            Written in the background from the analytics figures; you can leave and come back.
          </p>
        </div>
        <div className="flex gap-2">
          {[
            ["operations_7", "Weekly report"],
            ["operations_30", "Monthly report"],
          ].map(([kind, label]) => (
            <Button key={kind} onClick={() => void start(kind)} disabled={starting !== null}>
              {starting === kind ? <Spinner /> : <FileBarChartIcon />} {label}
            </Button>
          ))}
        </div>
      </div>

      <div className="grid gap-5 md:grid-cols-[15rem_1fr]">
        <nav className="flex flex-col gap-1">
          {reports.isPending && <Spinner />}
          {reports.data?.length === 0 && <p className="text-sm text-muted-foreground">No reports yet.</p>}
          {reports.data?.map((r) => (
            <button
              type="button"
              key={r.reportId}
              onClick={() => setParams({ report: r.reportId })}
              className={cn(
                "flex flex-col items-start gap-1 rounded-lg border-0 bg-transparent px-3 py-2 text-left transition-colors",
                r.reportId === selected ? "bg-muted" : "hover:bg-muted/60",
              )}
            >
              <span className="flex w-full items-center justify-between gap-2 text-sm font-medium">
                {r.title}
                <Badge variant={STATUS[r.status]?.variant ?? "outline"}>{STATUS[r.status]?.label ?? r.status}</Badge>
              </span>
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <CalendarRangeIcon className="size-3" /> {r.fromDate} – {r.toDate}
              </span>
            </button>
          ))}
        </nav>

        <Card className="min-h-80">
          <CardContent className="flex flex-col gap-4">
            {!selected && <p className="text-sm text-muted-foreground">Start a report, or pick one on the left.</p>}
            {current && <h2 className="text-base font-semibold">{current.title}</h2>}
            {error && <ErrorNote part={{ kind: "error", code: "unavailable", message: error }} />}
            {turn && <ReportBody turn={turn} />}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function ReportBody({ turn }: { turn: AssistantTurn }) {
  const steps = turn.parts.filter((p) => p.kind === "tool");
  return (
    <>
      {steps.length > 0 && <Steps steps={steps} />}
      {turn.parts.map((part, i) =>
        part.kind === "card" ? (
          <ResultCard key={i} part={part} />
        ) : part.kind === "text" ? (
          <Prose key={i} text={part.text} />
        ) : part.kind === "error" ? (
          <ErrorNote key={i} part={part} />
        ) : null,
      )}
      {turn.status === "streaming" && turn.parts.length < 3 && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Writing the report…
        </div>
      )}
    </>
  );
}
