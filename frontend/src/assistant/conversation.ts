// The conversation as the page shows it, built from the assistant's event stream.
//
// Pure: `applyEvent` folds one ChatEvent into a turn, so the stream handling is tested
// without a server (conversation.test.ts). A turn's events arrive in `seq` order, and a
// resumed stream may repeat ones already applied -- those are skipped by seq, so a
// reconnect never doubles text.

import type { ChatEvent } from "@/gen/smarthealth/assistant/v1/assistant_pb";

export interface FieldView {
  label: string;
  value: string;
}

export type Part =
  | { kind: "text"; text: string }
  | { kind: "tool"; callId: string; name: string; label: string; status: string; summary: string }
  | { kind: "citations"; items: { sourceId: string; title: string; snippet: string }[] }
  | {
      kind: "options";
      id: string;
      prompt: string;
      style: string;
      options: { id: string; label: string; description: string; badge: string }[];
      // The option the user picked, once they have.
      chosen?: string;
    }
  | {
      kind: "confirmation";
      id: string;
      action: string;
      title: string;
      details: FieldView[];
      confirmLabel: string;
      denyLabel: string;
      tone: string;
      decision?: "approved" | "denied";
    }
  | { kind: "card"; cardKind: string; title: string; subtitle: string; fields: FieldView[]; badge: string; link: string }
  | { kind: "suggestions"; items: string[] }
  | { kind: "error"; code: string; message: string };

export type TurnStatus = "streaming" | "complete" | "cancelled" | "awaiting_input" | "failed";

export interface AssistantTurn {
  role: "assistant";
  turnId: string;
  lastSeq: number;
  status: TurnStatus;
  parts: Part[];
}

export interface UserTurn {
  role: "user";
  id: string;
  text: string;
}

export type Turn = UserTurn | AssistantTurn;

export function newAssistantTurn(turnId: string): AssistantTurn {
  return { role: "assistant", turnId, lastSeq: 0, status: "streaming", parts: [] };
}

function withPart(parts: Part[], part: Part): Part[] {
  return [...parts, part];
}

/** Fold one event into its turn. Returns the same object when the event is a replay. */
export function applyEvent(turn: AssistantTurn, event: ChatEvent): AssistantTurn {
  const seq = Number(event.seq);
  if (seq <= turn.lastSeq) return turn;
  const next: AssistantTurn = { ...turn, lastSeq: seq };
  const kind = event.kind;
  const parts = turn.parts;
  switch (kind.case) {
    case "token": {
      const last = parts.at(-1);
      // Consecutive tokens are one paragraph of prose; anything in between starts a new one.
      next.parts =
        last?.kind === "text"
          ? [...parts.slice(0, -1), { kind: "text", text: last.text + kind.value }]
          : withPart(parts, { kind: "text", text: kind.value });
      return next;
    }
    case "citation": {
      const item = { sourceId: kind.value.sourceId, title: kind.value.title, snippet: kind.value.snippet };
      const index = parts.findIndex((p) => p.kind === "citations");
      if (index === -1) {
        next.parts = withPart(parts, { kind: "citations", items: [item] });
      } else {
        const existing = parts[index] as Extract<Part, { kind: "citations" }>;
        if (existing.items.some((i) => i.sourceId === item.sourceId)) return next;
        next.parts = parts.map((p, i) => (i === index ? { ...existing, items: [...existing.items, item] } : p));
      }
      return next;
    }
    case "tool": {
      const t = kind.value;
      const view: Part = { kind: "tool", callId: t.callId, name: t.name, label: t.label, status: t.status, summary: t.summary };
      const index = parts.findIndex((p) => p.kind === "tool" && p.callId === t.callId);
      next.parts = index === -1 ? withPart(parts, view) : parts.map((p, i) => (i === index ? view : p));
      return next;
    }
    case "options": {
      const o = kind.value;
      next.parts = withPart(parts, {
        kind: "options",
        id: o.optionsId,
        prompt: o.prompt,
        style: o.style || "list",
        options: o.options.map((x) => ({ id: x.id, label: x.label, description: x.description, badge: x.badge })),
      });
      return next;
    }
    case "confirmation": {
      const c = kind.value;
      next.parts = withPart(parts, {
        kind: "confirmation",
        id: c.confirmationId,
        action: c.action,
        title: c.title,
        details: c.details.map((d) => ({ label: d.label, value: d.value })),
        confirmLabel: c.confirmLabel || "Confirm",
        denyLabel: c.denyLabel || "Cancel",
        tone: c.tone || "normal",
      });
      return next;
    }
    case "card": {
      const c = kind.value;
      next.parts = withPart(parts, {
        kind: "card",
        cardKind: c.kind,
        title: c.title,
        subtitle: c.subtitle,
        fields: c.fields.map((f) => ({ label: f.label, value: f.value })),
        badge: c.badge,
        link: c.link,
      });
      return next;
    }
    case "suggestions":
      next.parts = withPart(parts, { kind: "suggestions", items: [...kind.value.items] });
      return next;
    case "error":
      next.parts = withPart(parts, { kind: "error", code: kind.value.code, message: kind.value.message });
      next.status = "failed";
      return next;
    case "done": {
      const reason = kind.value.reason;
      next.status =
        turn.status === "failed"
          ? "failed"
          : reason === "cancelled" || reason === "awaiting_input"
            ? reason
            : "complete";
      return next;
    }
    default:
      // A kind this client does not know yet (the contract only grows): ignore it.
      return next;
  }
}

/** Record the user's pick on an Options part, so the list shows what was chosen. */
export function markChosen(turns: Turn[], optionsId: string, optionId: string): Turn[] {
  return turns.map((t) =>
    t.role !== "assistant"
      ? t
      : {
          ...t,
          parts: t.parts.map((p) => (p.kind === "options" && p.id === optionsId ? { ...p, chosen: optionId } : p)),
        },
  );
}

export function markDecided(turns: Turn[], confirmationId: string, approved: boolean): Turn[] {
  return turns.map((t) =>
    t.role !== "assistant"
      ? t
      : {
          ...t,
          parts: t.parts.map((p) =>
            p.kind === "confirmation" && p.id === confirmationId ? { ...p, decision: approved ? "approved" : "denied" } : p,
          ),
        },
  );
}

/** Interactive parts are answerable only while they belong to the latest turn. */
export function isLatest(turns: Turn[], turnId: string): boolean {
  const last = [...turns].reverse().find((t) => t.role === "assistant") as AssistantTurn | undefined;
  return last?.turnId === turnId;
}
