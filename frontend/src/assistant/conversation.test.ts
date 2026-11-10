import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, it, vi } from "vitest";
import { ChatEventSchema, type ChatEvent } from "@/gen/smarthealth/assistant/v1/assistant_pb";
import { streamTurn } from "./client";
import { applyEvent, fromTranscript, isLatest, markChosen, markDecided, newAssistantTurn, type AssistantTurn, type Turn } from "./conversation";

vi.mock("@/api", () => ({ readToken: () => "t", refreshOnce: vi.fn(async () => true), sessionEnded: vi.fn() }));

function ev(seq: number, kind: ChatEvent["kind"]): ChatEvent {
  return create(ChatEventSchema, { turnId: "t1", seq: BigInt(seq), kind });
}

const fold = (events: ChatEvent[]) => events.reduce(applyEvent, newAssistantTurn("t1"));

describe("applyEvent", () => {
  it("joins consecutive tokens into one paragraph and splits on anything else", () => {
    const turn = fold([
      ev(1, { case: "token", value: "Hello " }),
      ev(2, { case: "token", value: "there" }),
      ev(3, { case: "tool", value: { callId: "c1", name: "search", label: "Searching", status: "running" } as never }),
      ev(4, { case: "token", value: "After" }),
    ]);
    expect(turn.parts.map((p) => p.kind)).toEqual(["text", "tool", "text"]);
    expect(turn.parts[0]).toMatchObject({ text: "Hello there" });
  });

  it("skips events already applied, so a resumed stream never doubles text", () => {
    const once = fold([ev(1, { case: "token", value: "a" }), ev(2, { case: "token", value: "b" })]);
    const replayed = [ev(1, { case: "token", value: "a" }), ev(2, { case: "token", value: "b" }), ev(3, { case: "token", value: "c" })]
      .reduce(applyEvent, once);
    expect(replayed.parts).toEqual([{ kind: "text", text: "abc" }]);
  });

  it("updates a tool step in place by call id", () => {
    const turn = fold([
      ev(1, { case: "tool", value: { callId: "c1", name: "slots", label: "Finding slots", status: "running" } as never }),
      ev(2, { case: "tool", value: { callId: "c1", name: "slots", label: "Finding slots", status: "done", summary: "3 free" } as never }),
    ]);
    expect(turn.parts).toHaveLength(1);
    expect(turn.parts[0]).toMatchObject({ status: "done", summary: "3 free" });
  });

  it("gathers citations into one sources part, without duplicates", () => {
    const c = (id: string) => ({ case: "citation" as const, value: { sourceId: id, title: id } as never });
    const turn = fold([ev(1, c("doc:a")), ev(2, c("doc:b")), ev(3, c("doc:a"))]);
    expect(turn.parts).toHaveLength(1);
    expect(turn.parts[0]).toMatchObject({ items: [{ sourceId: "doc:a" }, { sourceId: "doc:b" }] });
  });

  it("ends awaiting input when the turn paused on a choice", () => {
    const turn = fold([
      ev(1, { case: "options", value: { optionsId: "o1", prompt: "Which?", options: [{ id: "a", label: "A" }] } as never }),
      ev(2, { case: "done", value: { reason: "awaiting_input" } as never }),
    ]);
    expect(turn.status).toBe("awaiting_input");
    expect(turn.parts[0]).toMatchObject({ kind: "options", style: "list" });
  });

  it("stays failed when an error precedes done", () => {
    const turn = fold([
      ev(1, { case: "error", value: { code: "unavailable", message: "Part A is down" } as never }),
      ev(2, { case: "done", value: { reason: "complete" } as never }),
    ]);
    expect(turn.status).toBe("failed");
  });

  it("ignores a kind it does not know", () => {
    const turn = fold([ev(1, { case: undefined, value: undefined })]);
    expect(turn.parts).toEqual([]);
    expect(turn.lastSeq).toBe(1);
  });
});

describe("answering interactive parts", () => {
  const turns: Turn[] = [
    {
      ...newAssistantTurn("t1"),
      parts: [
        { kind: "options", id: "o1", prompt: "", style: "list", options: [{ id: "a", label: "A", description: "", badge: "" }] },
        { kind: "confirmation", id: "k1", action: "book", title: "Book?", details: [], confirmLabel: "Book", denyLabel: "No", tone: "normal" },
      ],
    },
  ];

  it("records the chosen option and the decision", () => {
    const after = markDecided(markChosen(turns, "o1", "a"), "k1", false);
    const parts = (after[0] as typeof turns[0] & { role: "assistant" }).parts;
    expect(parts[0]).toMatchObject({ chosen: "a" });
    expect(parts[1]).toMatchObject({ decision: "denied" });
  });

  it("only the latest assistant turn is answerable", () => {
    const two: Turn[] = [...turns, { role: "user", id: "u", text: "x" }, newAssistantTurn("t2")];
    expect(isLatest(two, "t1")).toBe(false);
    expect(isLatest(two, "t2")).toBe(true);
  });
});

describe("streamTurn", () => {
  it("resumes from the last seq after a dropped connection", async () => {
    const calls: bigint[] = [];
    let first = true;
    const open = (resumeFrom: bigint) => {
      calls.push(resumeFrom);
      return (async function* () {
        if (first) {
          first = false;
          yield ev(1, { case: "token", value: "a" });
          throw new ConnectError("dropped", Code.Unavailable);
        }
        yield ev(2, { case: "token", value: "b" });
        yield ev(3, { case: "done", value: { reason: "complete" } as never });
      })();
    };
    const seen: number[] = [];
    await streamTurn(open, (e) => seen.push(Number(e.seq)), new AbortController().signal, [0]);
    expect(calls).toEqual([0n, 1n]);
    expect(seen).toEqual([1, 2, 3]);
  });

  it("refreshes an expired token once and replays", async () => {
    let n = 0;
    const open = () =>
      (async function* () {
        n += 1;
        if (n === 1) throw new ConnectError("expired", Code.Unauthenticated);
        yield ev(1, { case: "done", value: { reason: "complete" } as never });
      })();
    await streamTurn(open, () => {}, new AbortController().signal, [0]);
    expect(n).toBe(2);
  });

  it("gives up on a non-transient error", async () => {
    const open = () =>
      (async function* () {
        yield* [];
        throw new ConnectError("no", Code.PermissionDenied);
      })();
    await expect(streamTurn(open, () => {}, new AbortController().signal, [0])).rejects.toThrow("no");
  });
});

describe("fromTranscript", () => {
  it("rebuilds the thread and restores the user's pick and decision", () => {
    const options = ev(1, { case: "options", value: { optionsId: "o1", prompt: "Who?", options: [{ id: "p1", label: "Dr A" }] } as never });
    const confirm = ev(1, { case: "confirmation", value: { confirmationId: "k1", title: "Book?" } as never });
    const done = (seq: number, reason: string) => ev(seq, { case: "done", value: { reason } as never });
    const turns = fromTranscript([
      { turnId: "a", userText: "Book an appointment", events: [options, done(2, "awaiting_input")] },
      { turnId: "b", userText: "Dr A", events: [confirm, done(2, "awaiting_input")] },
      { turnId: "c", userText: "[approved]", events: [ev(1, { case: "token", value: "Done" }), done(2, "complete")] },
    ]);
    expect(turns.map((t) => t.role)).toEqual(["user", "assistant", "user", "assistant", "assistant"]);
    const [, first, , second] = turns as AssistantTurn[];
    expect(first.parts[0]).toMatchObject({ kind: "options", chosen: "p1" });
    expect(second.parts[0]).toMatchObject({ kind: "confirmation", decision: "approved" });
    expect((turns[4] as AssistantTurn).status).toBe("complete");
  });
});
