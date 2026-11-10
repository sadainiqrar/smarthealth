// The user's assistant threads: newest first, the open one highlighted, a dot on any that
// ended on a question still waiting for them. Deleting asks once, inline.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleDotIcon, MessageSquarePlusIcon, MessageSquareTextIcon, Trash2Icon } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";
import { assistant } from "./client";

export const THREADS_KEY = ["assistant", "threads"] as const;

export function useThreads() {
  return useQuery({
    queryKey: THREADS_KEY,
    queryFn: async () => (await assistant.listConversations({ limit: 50 })).conversations,
  });
}

function ago(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function Threads({
  activeId,
  onOpen,
  onNew,
  onDeleted,
  className,
}: {
  activeId: string | null;
  onOpen: (id: string) => void;
  onNew: () => void;
  onDeleted: (id: string) => void;
  className?: string;
}) {
  const threads = useThreads();
  const client = useQueryClient();
  const [confirming, setConfirming] = useState<string | null>(null);

  async function remove(id: string) {
    setConfirming(null);
    await assistant.deleteConversation({ conversationId: id });
    await client.invalidateQueries({ queryKey: THREADS_KEY });
    onDeleted(id);
  }

  return (
    <aside className={cn("flex min-h-0 flex-col gap-2", className)}>
      <Button variant="outline" className="justify-start" onClick={onNew}>
        <MessageSquarePlusIcon /> New chat
      </Button>
      <div className="px-1 pt-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Chats</div>
      <nav className="-mx-1 flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-1">
        {threads.isPending && (
          <div className="flex items-center gap-2 px-2 py-1 text-sm text-muted-foreground">
            <Spinner /> Loading…
          </div>
        )}
        {threads.isError && <div className="px-2 text-sm text-destructive">Couldn't load your chats.</div>}
        {threads.data?.length === 0 && <div className="px-2 text-sm text-muted-foreground">No chats yet.</div>}
        {threads.data?.map((thread) => {
          const active = thread.conversationId === activeId;
          return (
            <div
              key={thread.conversationId}
              className={cn(
                "group flex items-center gap-1 rounded-lg pr-1 transition-colors",
                active ? "bg-muted" : "hover:bg-muted/60",
              )}
            >
              {confirming === thread.conversationId ? (
                <div className="flex flex-1 items-center justify-between gap-2 px-2 py-1.5 text-sm">
                  <span className="text-muted-foreground">Delete chat?</span>
                  <span className="flex gap-1">
                    <Button size="xs" variant="destructive" onClick={() => void remove(thread.conversationId)}>
                      Delete
                    </Button>
                    <Button size="xs" variant="ghost" onClick={() => setConfirming(null)}>
                      Keep
                    </Button>
                  </span>
                </div>
              ) : (
                <>
                  <button
                    type="button"
                    onClick={() => onOpen(thread.conversationId)}
                    aria-current={active ? "page" : undefined}
                    className="flex min-w-0 flex-1 flex-col items-start gap-0.5 rounded-lg border-0 bg-transparent px-2 py-1.5 text-left"
                  >
                    <span className="flex w-full items-center gap-1.5 text-sm">
                      {thread.awaitingInput ? (
                        <CircleDotIcon className="size-3.5 shrink-0 text-primary" aria-label="Waiting for your answer" />
                      ) : (
                        <MessageSquareTextIcon className="size-3.5 shrink-0 text-muted-foreground" />
                      )}
                      <span className="truncate">{thread.title}</span>
                    </span>
                    <span className="pl-5 text-xs text-muted-foreground">
                      {ago(thread.updatedAt)}
                      {thread.awaitingInput ? " · waiting for you" : ""}
                    </span>
                  </button>
                  <Button
                    size="icon-xs"
                    variant="ghost"
                    aria-label={`Delete ${thread.title}`}
                    className="opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
                    onClick={() => setConfirming(thread.conversationId)}
                  >
                    <Trash2Icon />
                  </Button>
                </>
              )}
            </div>
          );
        })}
      </nav>
    </aside>
  );
}
