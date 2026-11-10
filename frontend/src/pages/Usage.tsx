// AI usage, for admin: how much each user asks of the assistant's model, and what it costs.
//
// Two sources, joined here in the admin's browser: the assistant's usage totals (user ids
// and roles only -- Part B holds no names or emails) and Part A's admin-only GET /users,
// which names them. Every number is the assistant's own record; cost is an estimate from the
// configured price per million tokens.

import { useQuery } from "@tanstack/react-query";
import { ActivityIcon, CoinsIcon, MessagesSquareIcon, TimerIcon } from "lucide-react";
import { useState } from "react";
import { api } from "@/api";
import { assistant } from "@/assistant/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { UserUsage } from "@/gen/smarthealth/assistant/v1/assistant_pb";

interface DirectoryEntry {
  id: string;
  email: string;
  role: string;
  is_active: boolean;
  name: string | null;
}

const RANGES = [
  { id: "1", label: "Today", days: 1 },
  { id: "7", label: "7 days", days: 7 },
  { id: "30", label: "30 days", days: 30 },
  { id: "90", label: "90 days", days: 90 },
];

function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

const number = new Intl.NumberFormat();
const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 });

function seconds(ms: number): string {
  return ms ? `${(ms / 1000).toFixed(1)} s` : "—";
}

function when(iso: string): string {
  return iso ? new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
}

const ROLE_LABEL: Record<string, string> = { patient: "Patient", provider: "Provider", front_desk: "Front desk", admin: "Admin" };

export default function Usage() {
  const [range, setRange] = useState("7");
  const days = RANGES.find((r) => r.id === range)!.days;
  const to = new Date();
  const from = new Date(Date.now() - (days - 1) * 86_400_000);

  const usage = useQuery({
    queryKey: ["assistant", "usage", range],
    queryFn: () => assistant.usage({ fromDate: isoDay(from), toDate: isoDay(to) }),
  });
  const ids = usage.data?.users.map((u) => u.userId) ?? [];
  const directory = useQuery({
    queryKey: ["users", ...ids],
    enabled: ids.length > 0,
    queryFn: () =>
      api<DirectoryEntry[]>(`/users?${ids.map((id) => `ids=${encodeURIComponent(id)}`).join("&")}`),
  });
  const people = new Map((directory.data ?? []).map((u) => [u.id, u]));
  const totals = usage.data?.totals;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <h1 className="text-lg font-semibold">AI usage</h1>
          <p className="text-sm text-muted-foreground">
            What each user asks of the assistant's model
            {usage.data && usage.data.provider !== "none" && (
              <>
                {" "}— <span className="font-mono text-xs">{usage.data.provider} / {usage.data.model}</span>
              </>
            )}
          </p>
        </div>
        <div className="flex gap-1">
          {RANGES.map((r) => (
            <Button key={r.id} size="sm" variant={r.id === range ? "default" : "outline"} onClick={() => setRange(r.id)}>
              {r.label}
            </Button>
          ))}
        </div>
      </div>

      {usage.isPending && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Loading usage…
        </div>
      )}
      {usage.isError && <p className="text-sm text-destructive">Couldn't load usage: {usage.error.message}</p>}

      {totals && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat icon={<MessagesSquareIcon />} label="Conversations" value={number.format(totals.conversations)}
                note={`${number.format(totals.turns)} turns, ${number.format(totals.modelTurns)} used the model`} />
          <Stat icon={<ActivityIcon />} label="Tokens" value={number.format(Number(totals.inputTokens + totals.outputTokens))}
                note={`${number.format(Number(totals.inputTokens))} in · ${number.format(Number(totals.outputTokens))} out`} />
          <Stat icon={<CoinsIcon />} label="Estimated cost" value={money.format(totals.costUsd)}
                note={`at $${usage.data!.priceInputPerMtok} / $${usage.data!.priceOutputPerMtok} per 1M tokens`} />
          <Stat icon={<TimerIcon />} label="Time to first word" value={seconds(totals.avgFirstTokenMs)}
                note={`whole answer ${seconds(totals.avgTotalMs)} · ${totals.errors} errors`} />
        </div>
      )}

      {usage.data && (
        <Card>
          <CardHeader>
            <CardTitle>By user</CardTitle>
            <CardDescription>
              {usage.data.fromDate} to {usage.data.toDate} (UTC), heaviest first. A turn a guided flow answered alone uses no tokens.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {usage.data.users.length === 0 ? (
              <p className="text-sm text-muted-foreground">Nobody used the assistant in this period.</p>
            ) : (
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>User</TableHead>
                      <TableHead>Role</TableHead>
                      <TableHead className="text-right">Chats</TableHead>
                      <TableHead className="text-right">Turns</TableHead>
                      <TableHead className="text-right">Tokens in</TableHead>
                      <TableHead className="text-right">Tokens out</TableHead>
                      <TableHead className="text-right">Est. cost</TableHead>
                      <TableHead className="text-right">First word</TableHead>
                      <TableHead className="text-right">Errors</TableHead>
                      <TableHead>Last active</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {usage.data.users.map((row) => (
                      <UsageRow key={row.userId} row={row} person={people.get(row.userId)} loading={directory.isPending} />
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            {directory.isError && (
              <p className="mt-2 text-xs text-destructive">Couldn't load names and emails: {directory.error.message}</p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function Stat({ icon, label, value, note }: { icon: React.ReactNode; label: string; value: string; note: string }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardDescription className="flex items-center gap-1.5 [&_svg]:size-4">
          {icon}
          {label}
        </CardDescription>
        <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>
      </CardHeader>
      <CardContent className="text-xs text-muted-foreground">{note}</CardContent>
    </Card>
  );
}

function UsageRow({ row, person, loading }: { row: UserUsage; person?: DirectoryEntry; loading: boolean }) {
  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">{person?.name ?? person?.email ?? (loading ? "…" : "Unknown user")}</span>
          <span className="text-xs text-muted-foreground">
            {person ? (person.name ? person.email : "") : <span className="font-mono">{row.userId.slice(0, 8)}</span>}
            {person && !person.is_active && " · deactivated"}
          </span>
        </div>
      </TableCell>
      <TableCell>
        <Badge variant="secondary">{ROLE_LABEL[row.role] ?? row.role}</Badge>
      </TableCell>
      <TableCell className="text-right tabular-nums">{row.conversations}</TableCell>
      <TableCell className="text-right tabular-nums">
        {row.turns}
        <span className="text-xs text-muted-foreground"> ({row.modelTurns} model)</span>
      </TableCell>
      <TableCell className="text-right tabular-nums">{number.format(Number(row.inputTokens))}</TableCell>
      <TableCell className="text-right tabular-nums">{number.format(Number(row.outputTokens))}</TableCell>
      <TableCell className="text-right tabular-nums">{money.format(row.costUsd)}</TableCell>
      <TableCell className="text-right tabular-nums">{seconds(row.avgFirstTokenMs)}</TableCell>
      <TableCell className="text-right tabular-nums">{row.errors || "—"}</TableCell>
      <TableCell className="whitespace-nowrap text-sm">{when(row.lastActive)}</TableCell>
    </TableRow>
  );
}
