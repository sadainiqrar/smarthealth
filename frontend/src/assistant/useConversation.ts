// One conversation with the assistant: the turns on screen, and the calls that add to them.
//
// Every way in -- typing, picking an option, starting from a starter, approving a
// confirmation -- is a new turn with a client-chosen id, so a retry or a resume names the
// same turn and the server never generates it twice.

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatEvent, StarterGroup } from "@/gen/smarthealth/assistant/v1/assistant_pb";
import { assistant, streamTurn, type Opener } from "./client";
import {
  applyEvent,
  markChosen,
  markDecided,
  newAssistantTurn,
  type AssistantTurn,
  type Turn,
} from "./conversation";

function id(): string {
  return crypto.randomUUID();
}

export interface SendInput {
  /** What the user's bubble says. */
  text: string;
  message?: string;
  choice?: { optionsId: string; optionId: string };
  starterId?: string;
}

export function useConversation() {
  const [conversationId, setConversationId] = useState(id);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [activeTurn, setActiveTurn] = useState<string | null>(null);
  const [starters, setStarters] = useState<StarterGroup[]>([]);
  const [startersError, setStartersError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    let live = true;
    assistant
      .starters({})
      .then((reply) => live && setStarters(reply.groups))
      .catch((error: Error) => live && setStartersError(error.message));
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => () => abort.current?.abort(), []);

  const update = useCallback((turnId: string, event: ChatEvent) => {
    setTurns((current) =>
      current.map((t) => (t.role === "assistant" && t.turnId === turnId ? applyEvent(t, event) : t)),
    );
  }, []);

  const fail = useCallback((turnId: string, message: string) => {
    setTurns((current) =>
      current.map((t) =>
        t.role === "assistant" && t.turnId === turnId
          ? ({
              ...t,
              status: "failed",
              parts: [...t.parts, { kind: "error", code: "unavailable", message }],
            } satisfies AssistantTurn)
          : t,
      ),
    );
  }, []);

  const run = useCallback(
    async (turnId: string, open: Opener) => {
      const controller = new AbortController();
      abort.current = controller;
      setActiveTurn(turnId);
      try {
        await streamTurn(open, (event) => update(turnId, event), controller.signal);
      } catch (error) {
        fail(turnId, error instanceof Error ? error.message : "The assistant is unavailable.");
      } finally {
        setActiveTurn((current) => (current === turnId ? null : current));
      }
    },
    [update, fail],
  );

  const send = useCallback(
    (input: SendInput) => {
      if (activeTurn) return;
      const turnId = id();
      setTurns((current) => {
        const marked = input.choice ? markChosen(current, input.choice.optionsId, input.choice.optionId) : current;
        return [...marked, { role: "user", id: id(), text: input.text }, newAssistantTurn(turnId)];
      });
      void run(turnId, (resumeFrom, signal) =>
        assistant.chat(
          {
            conversationId,
            turnId,
            message: input.message ?? "",
            choice: input.choice,
            starterId: input.starterId ?? "",
            resumeFrom,
          },
          { signal },
        ),
      );
    },
    [activeTurn, conversationId, run],
  );

  const decide = useCallback(
    (confirmationId: string, approved: boolean) => {
      if (activeTurn) return;
      const turnId = id();
      setTurns((current) => [...markDecided(current, confirmationId, approved), newAssistantTurn(turnId)]);
      void run(turnId, (resumeFrom, signal) =>
        assistant.confirm({ conversationId, turnId, confirmationId, approved, resumeFrom }, { signal }),
      );
    },
    [activeTurn, conversationId, run],
  );

  const stop = useCallback(() => {
    if (!activeTurn) return;
    // The server stops generating and ends the stream with Done("cancelled"); aborting
    // locally as well would lose that ending, so only ask.
    void assistant.cancel({ turnId: activeTurn }).catch(() => abort.current?.abort());
  }, [activeTurn]);

  const reset = useCallback(() => {
    abort.current?.abort();
    setTurns([]);
    setActiveTurn(null);
    setConversationId(id());
  }, []);

  return { conversationId, turns, busy: activeTurn !== null, starters, startersError, send, decide, stop, reset };
}
