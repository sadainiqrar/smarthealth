// One conversation with the assistant: the turns on screen, and the calls that add to them.
//
// Every way in -- typing, picking an option, starting from a starter, approving a
// confirmation -- is a new turn with a client-chosen id, so a retry or a resume names the
// same turn and the server never generates it twice. Opening an existing thread loads its
// transcript and rebuilds it with the same reducer the live stream uses.

import { Code, ConnectError } from "@connectrpc/connect";
import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatEvent, StarterGroup } from "@/gen/smarthealth/assistant/v1/assistant_pb";
import { assistant, streamTurn, type Opener } from "./client";
import {
  applyEvent,
  fromTranscript,
  markChosen,
  markDecided,
  newAssistantTurn,
  type AssistantTurn,
  type Turn,
} from "./conversation";

export function newId(): string {
  return crypto.randomUUID();
}

export interface SendInput {
  /** What the user's bubble says. */
  text: string;
  message?: string;
  choice?: { optionsId: string; optionId: string };
  starterId?: string;
}

interface Options {
  /** The thread to show. */
  conversationId: string;
  /** Load its transcript from the server (an existing thread), or start empty (a new one). */
  existing: boolean;
  /** A turn finished: the thread list may have changed. */
  onTurnFinished?: (conversationId: string) => void;
  /** The thread does not exist (or is not the caller's). */
  onMissing?: () => void;
}

export function useConversation({ conversationId, existing, onTurnFinished, onMissing }: Options) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeTurn, setActiveTurn] = useState<string | null>(null);
  const [starters, setStarters] = useState<StarterGroup[]>([]);
  const [startersError, setStartersError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  // The thread the turns on screen belong to; a stream for another thread must not land here.
  const shown = useRef(conversationId);
  const callbacks = useRef({ onTurnFinished, onMissing });
  callbacks.current = { onTurnFinished, onMissing };

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

  useEffect(() => {
    if (shown.current === conversationId && turns.length > 0) return; // already on screen
    abort.current?.abort();
    shown.current = conversationId;
    setActiveTurn(null);
    setTurns([]);
    if (!existing) return;
    let live = true;
    setLoading(true);
    assistant
      .getConversation({ conversationId })
      .then((reply) => {
        if (!live) return;
        setTurns(fromTranscript(reply.turns.map((t) => ({ turnId: t.turnId, userText: t.userText, events: t.events }))));
      })
      .catch((error) => {
        if (live && error instanceof ConnectError && error.code === Code.NotFound) callbacks.current.onMissing?.();
      })
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- turns is read, not a trigger
  }, [conversationId, existing]);

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
      const thread = shown.current;
      setActiveTurn(turnId);
      try {
        await streamTurn(open, (event) => update(turnId, event), controller.signal);
      } catch (error) {
        fail(turnId, error instanceof Error ? error.message : "The assistant is unavailable.");
      } finally {
        setActiveTurn((current) => (current === turnId ? null : current));
        callbacks.current.onTurnFinished?.(thread);
      }
    },
    [update, fail],
  );

  const send = useCallback(
    (input: SendInput) => {
      if (activeTurn) return;
      const turnId = newId();
      setTurns((current) => {
        const marked = input.choice ? markChosen(current, input.choice.optionsId, input.choice.optionId) : current;
        return [...marked, { role: "user", id: newId(), text: input.text }, newAssistantTurn(turnId)];
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
      const turnId = newId();
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

  return { turns, loading, busy: activeTurn !== null, starters, startersError, send, decide, stop };
}
