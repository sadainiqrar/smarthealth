// How each part of an assistant turn is drawn, from the AI Elements patterns
// (ui.shadcn.com/docs/ai-elements): Trace rows for the steps it took, Markdown for prose,
// Item rows for choices and confirmations, Cards for results, hover cards for sources.

import {
  BookOpenIcon,
  CalendarCheckIcon,
  CheckIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  CircleXIcon,
  ExternalLinkIcon,
  LoaderCircleIcon,
  SearchIcon,
  ShieldAlertIcon,
  WrenchIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import Markdown from "react-markdown";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item";
import { Trace, TraceIcon, TraceLine, TraceTitle, TraceDescription } from "@/components/ui/trace";
import { cn } from "@/lib/utils";
import type { Part } from "./conversation";

type Of<K extends Part["kind"]> = Extract<Part, { kind: K }>;

export function Prose({ text }: { text: string }) {
  return (
    <div className="assistant-prose text-sm leading-relaxed">
      <Markdown>{text}</Markdown>
    </div>
  );
}

function toolIcon(status: string, name: string): ReactNode {
  if (status === "running") return <LoaderCircleIcon className="animate-spin" />;
  if (status === "failed") return <CircleXIcon />;
  if (name.startsWith("search")) return <SearchIcon />;
  if (status === "done") return <CircleCheckIcon />;
  return <WrenchIcon />;
}

/** Consecutive tool parts, as one Trace: what the assistant looked up on the user's behalf. */
export function Steps({ steps }: { steps: Of<"tool">[] }) {
  return (
    <Trace>
      {steps.map((step) => (
        <div key={step.callId} className="flex flex-col gap-0.5">
          <TraceLine status={step.status === "running" ? "running" : step.status === "failed" ? "error" : undefined}>
            <TraceIcon>{toolIcon(step.status, step.name)}</TraceIcon>
            <TraceTitle>{step.label}</TraceTitle>
          </TraceLine>
          {step.summary && step.status !== "running" && (
            <TraceDescription className="pl-6 text-xs">{step.summary}</TraceDescription>
          )}
        </div>
      ))}
    </Trace>
  );
}

export function Sources({ part }: { part: Of<"citations"> }) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-muted-foreground">Sources</span>
      {part.items.map((item, index) => (
        <HoverCard key={item.sourceId}>
          <HoverCardTrigger
            render={
              <Badge variant="outline" className="cursor-default gap-1">
                <BookOpenIcon className="size-3" />
                <span className="max-w-48 truncate">{item.title || item.sourceId}</span>
                <span className="text-muted-foreground">[{index + 1}]</span>
              </Badge>
            }
          />
          <HoverCardContent className="w-80 text-sm">
            <div className="font-medium">{item.title}</div>
            <div className="mt-1 font-mono text-xs text-muted-foreground">{item.sourceId}</div>
            {item.snippet && <p className="mt-2 text-muted-foreground">{item.snippet}</p>}
          </HoverCardContent>
        </HoverCard>
      ))}
    </div>
  );
}

export function Choices({
  part,
  active,
  onChoose,
}: {
  part: Of<"options">;
  active: boolean;
  onChoose: (optionId: string, label: string) => void;
}) {
  const answered = part.chosen !== undefined;
  if (part.style === "chips") {
    return (
      <div className="flex flex-col gap-2">
        {part.prompt && <p className="text-sm">{part.prompt}</p>}
        <div className="flex flex-wrap gap-2">
          {part.options.map((option) => (
            <Button
              key={option.id}
              size="sm"
              variant={part.chosen === option.id ? "default" : "outline"}
              disabled={!active || answered}
              onClick={() => onChoose(option.id, option.label)}
            >
              {part.chosen === option.id && <CheckIcon />}
              {option.label}
            </Button>
          ))}
        </div>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {part.prompt && <p className="text-sm">{part.prompt}</p>}
      <ItemGroup className="gap-2">
        {part.options.map((option) => {
          const chosen = part.chosen === option.id;
          const disabled = !active || answered;
          return (
            <Item
              key={option.id}
              variant="outline"
              size="sm"
              className={cn(
                "transition-colors",
                chosen && "border-primary bg-primary/5",
                answered && !chosen && "opacity-50",
                !disabled && "cursor-pointer hover:bg-muted/60",
              )}
              render={
                <button
                  type="button"
                  disabled={disabled}
                  onClick={() => onChoose(option.id, option.label)}
                  className="w-full text-left"
                />
              }
            >
              <ItemContent>
                <ItemTitle>
                  {option.label}
                  {option.badge && <Badge variant="secondary">{option.badge}</Badge>}
                </ItemTitle>
                {option.description && <ItemDescription>{option.description}</ItemDescription>}
              </ItemContent>
              {chosen && (
                <ItemActions>
                  <CheckIcon className="size-4 text-primary" />
                </ItemActions>
              )}
            </Item>
          );
        })}
      </ItemGroup>
    </div>
  );
}

export function Confirmation({
  part,
  active,
  onDecide,
}: {
  part: Of<"confirmation">;
  active: boolean;
  onDecide: (approved: boolean) => void;
}) {
  const destructive = part.tone === "destructive";
  return (
    <Item variant="outline" className={cn("items-start", destructive && "border-destructive/40")}>
      <ItemMedia variant="icon">{destructive ? <ShieldAlertIcon /> : <CalendarCheckIcon />}</ItemMedia>
      <ItemContent className="gap-2">
        <ItemTitle>{part.title}</ItemTitle>
        {part.details.length > 0 && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            {part.details.map((d) => (
              <FieldRow key={d.label} label={d.label} value={d.value} />
            ))}
          </dl>
        )}
        {part.decision ? (
          <div className="text-sm text-muted-foreground">
            {part.decision === "approved" ? "You confirmed this." : "You declined this. Nothing was changed."}
          </div>
        ) : (
          <div className="flex gap-2 pt-1">
            <Button
              size="sm"
              variant={destructive ? "destructive" : "default"}
              disabled={!active}
              onClick={() => onDecide(true)}
            >
              {part.confirmLabel}
            </Button>
            <Button size="sm" variant="outline" disabled={!active} onClick={() => onDecide(false)}>
              {part.denyLabel}
            </Button>
          </div>
        )}
      </ItemContent>
    </Item>
  );
}

function FieldRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{value}</dd>
    </>
  );
}

export function ResultCard({ part }: { part: Of<"card"> }) {
  return (
    <Card size="sm" className="max-w-md">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {part.title}
          {part.badge && <Badge variant="secondary">{part.badge}</Badge>}
        </CardTitle>
        {part.subtitle && <CardDescription>{part.subtitle}</CardDescription>}
      </CardHeader>
      {(part.fields.length > 0 || part.link) && (
        <CardContent className="flex flex-col gap-3">
          {part.fields.length > 0 && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              {part.fields.map((f) => (
                <FieldRow key={f.label} label={f.label} value={f.value} />
              ))}
            </dl>
          )}
          {part.link && (
            <Button size="sm" variant="outline" className="w-fit" render={<Link to={part.link} />}>
              Open <ExternalLinkIcon />
            </Button>
          )}
        </CardContent>
      )}
    </Card>
  );
}

export function ErrorNote({ part }: { part: Of<"error"> }) {
  return (
    <div className="flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
      <CircleAlertIcon className="mt-0.5 size-4 shrink-0" />
      <span>{part.message || "Something went wrong."}</span>
    </div>
  );
}
