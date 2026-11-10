// The assistant (Part B, spec section 9): a chat that also offers guided, click-driven use
// cases. Starters come from the server for the caller's role; a turn streams prose mixed
// with interactive parts (choices, confirmations, result cards, follow-ups). Built from the
// AI Elements patterns: Chat, Message, Prompt Input, Suggestion, Confirmation, Tool, Sources.

import {
  ArrowUpIcon,
  BookOpenIcon,
  CalendarClockIcon,
  CalendarPlusIcon,
  CalendarXIcon,
  ChartColumnIcon,
  ClipboardListIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ListChecksIcon,
  type LucideIcon,
  MessageSquareTextIcon,
  NotebookPenIcon,
  PaperclipIcon,
  PillIcon,
  PlusIcon,
  RotateCcwIcon,
  SearchIcon,
  SirenIcon,
  SparklesIcon,
  SquareIcon,
  StethoscopeIcon,
  UsersIcon,
  XIcon,
} from "lucide-react";
import { useRef, useState, type FormEvent, type ReactElement } from "react";
import { useAuth } from "@/auth";
import {
  Attachment,
  AttachmentAction,
  AttachmentActions,
  AttachmentContent,
  AttachmentMedia,
  AttachmentTitle,
} from "@/components/ui/attachment";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupTextarea } from "@/components/ui/input-group";
import { Item, ItemContent, ItemDescription, ItemMedia, ItemTitle } from "@/components/ui/item";
import { Message, MessageContent } from "@/components/ui/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { isLatest, type AssistantTurn, type Part } from "@/assistant/conversation";
import { Choices, Confirmation, ErrorNote, Prose, ResultCard, Sources, Steps } from "@/assistant/parts";
import { uploadReport } from "@/assistant/upload";
import { useConversation } from "@/assistant/useConversation";
import type { Starter } from "@/gen/smarthealth/assistant/v1/assistant_pb";

const ICONS: Record<string, LucideIcon> = {
  "calendar-plus": CalendarPlusIcon,
  "calendar-clock": CalendarClockIcon,
  "calendar-x": CalendarXIcon,
  stethoscope: StethoscopeIcon,
  pill: PillIcon,
  "flask-conical": FlaskConicalIcon,
  "file-text": FileTextIcon,
  "clipboard-list": ClipboardListIcon,
  "list-checks": ListChecksIcon,
  users: UsersIcon,
  "message-square-text": MessageSquareTextIcon,
  "chart-column": ChartColumnIcon,
  "book-open": BookOpenIcon,
  siren: SirenIcon,
  "notebook-pen": NotebookPenIcon,
  search: SearchIcon,
};

const INTRO: Record<string, string> = {
  patient: "Book and manage appointments, prepare for tests, and understand your record.",
  provider: "Brief yourself on patients, review results, and look up clinic protocols.",
  front_desk: "Find capacity, book walk-ins, and handle the day's issues.",
  admin: "Operations numbers and trends, explained.",
};

export default function Assistant() {
  const { session } = useAuth();
  const role = session?.role ?? "patient";
  const conversation = useConversation();
  const { turns, busy, starters, startersError, send, decide, stop, reset } = conversation;
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const canAttach = role === "patient";
  const canSend = (text.trim().length > 0 || files.length > 0) && !busy && !uploading;

  const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant") as AssistantTurn | undefined;
  const followUps =
    !busy && lastAssistant
      ? (lastAssistant.parts.filter((p) => p.kind === "suggestions").at(-1) as Extract<Part, { kind: "suggestions" }> | undefined)
      : undefined;

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) return;
    const typed = text.trim();
    let message = typed;
    let shown = typed;
    if (files.length > 0) {
      setUploading(true);
      setUploadError(null);
      try {
        const uploaded = [];
        for (const file of files) uploaded.push(await uploadReport(file));
        const names = uploaded.map((u) => `"${u.filename}" (document ${u.id})`).join(", ");
        message = `${typed}\n\n[I uploaded ${names}]`.trim();
        shown = [typed, ...uploaded.map((u) => `📎 ${u.filename}`)].filter(Boolean).join("\n");
      } catch (error) {
        setUploadError(error instanceof Error ? error.message : "Upload failed");
        setUploading(false);
        return;
      }
      setUploading(false);
      setFiles([]);
    }
    setText("");
    send({ text: shown, message });
  }

  function start(starter: Starter) {
    if (starter.mode === "prompt") send({ text: starter.prompt, message: starter.prompt });
    else send({ text: starter.title, starterId: starter.id });
  }

  return (
    <MessageScrollerProvider>
      <div className="assistant-page mx-auto flex h-[calc(100dvh-7.5rem)] w-full max-w-3xl flex-col gap-3">
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-0.5">
            <h1 className="flex items-center gap-2 text-lg font-semibold">
              <SparklesIcon className="size-4 text-primary" /> Assistant
            </h1>
            <p className="text-sm text-muted-foreground">{INTRO[role]}</p>
          </div>
          <Tooltip>
            <TooltipTrigger
              render={
                <Button variant="outline" size="icon" aria-label="New conversation" onClick={reset} disabled={busy} />
              }
            >
              <RotateCcwIcon />
            </TooltipTrigger>
            <TooltipContent>New conversation</TooltipContent>
          </Tooltip>
        </div>

        <div className="min-h-0 flex-1">
          {turns.length === 0 ? (
            <div className="flex h-full flex-col gap-6 overflow-y-auto pb-2">
              <Empty className="flex-none py-6">
                <EmptyHeader>
                  <EmptyMedia variant="icon">
                    <SparklesIcon />
                  </EmptyMedia>
                  <EmptyTitle>How can I help?</EmptyTitle>
                  <EmptyDescription>
                    Pick a task to be guided through it step by step, or just type. Anything that changes your
                    appointments is shown to you to confirm first.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
              {startersError && <p className="text-center text-sm text-destructive">Assistant unavailable: {startersError}</p>}
              {starters.map((group) => (
                <section key={group.title} className="flex flex-col gap-2">
                  <h2 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{group.title}</h2>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {group.starters.map((starter) => {
                      const Icon = ICONS[starter.icon] ?? SparklesIcon;
                      return (
                        <Item
                          key={starter.id}
                          variant="outline"
                          size="sm"
                          className="cursor-pointer transition-colors hover:bg-muted/60"
                          render={<button type="button" className="w-full text-left" onClick={() => start(starter)} />}
                        >
                          <ItemMedia variant="icon">
                            <Icon />
                          </ItemMedia>
                          <ItemContent>
                            <ItemTitle>{starter.title}</ItemTitle>
                            <ItemDescription>{starter.description}</ItemDescription>
                          </ItemContent>
                        </Item>
                      );
                    })}
                  </div>
                </section>
              ))}
            </div>
          ) : (
            <MessageScroller>
              <MessageScrollerViewport>
                <MessageScrollerContent aria-busy={busy} className="gap-6 py-2">
                  {turns.map((turn) =>
                    turn.role === "user" ? (
                      <MessageScrollerItem key={turn.id} messageId={turn.id} scrollAnchor>
                        <Message align="end">
                          <MessageContent>
                            <Bubble align="end" variant="muted">
                              <BubbleContent className="whitespace-pre-wrap">{turn.text}</BubbleContent>
                            </Bubble>
                          </MessageContent>
                        </Message>
                      </MessageScrollerItem>
                    ) : (
                      <MessageScrollerItem key={turn.turnId} messageId={turn.turnId}>
                        <Message>
                          <MessageContent className="gap-3">
                            <AssistantParts
                              turn={turn}
                              active={!busy && isLatest(turns, turn.turnId) && turn.status === "awaiting_input"}
                              onChoose={(optionsId, optionId, label) =>
                                send({ text: label, choice: { optionsId, optionId } })
                              }
                              onDecide={decide}
                            />
                          </MessageContent>
                        </Message>
                      </MessageScrollerItem>
                    ),
                  )}
                </MessageScrollerContent>
              </MessageScrollerViewport>
              <MessageScrollerButton />
            </MessageScroller>
          )}
        </div>

        <div className="flex flex-col gap-2">
          {followUps && followUps.items.length > 0 && (
            <div className="flex gap-2 overflow-x-auto pb-1">
              {followUps.items.map((item) => (
                <Button key={item} variant="outline" size="sm" className="shrink-0" onClick={() => send({ text: item, message: item })}>
                  {item}
                </Button>
              ))}
            </div>
          )}
          <form ref={formRef} onSubmit={onSubmit}>
            <input
              ref={fileRef}
              type="file"
              accept="application/pdf,image/png,image/jpeg"
              multiple
              className="sr-only"
              tabIndex={-1}
              onChange={(event) => {
                const selected = event.target.files;
                if (selected) setFiles((current) => [...current, ...Array.from(selected)]);
                event.target.value = "";
              }}
            />
            <InputGroup>
              {files.length > 0 && (
                <InputGroupAddon align="block-start" className="flex-wrap">
                  {files.map((file, index) => (
                    <Attachment key={`${file.name}-${index}`} size="sm">
                      <AttachmentMedia>
                        <FileTextIcon />
                      </AttachmentMedia>
                      <AttachmentContent>
                        <AttachmentTitle>{file.name}</AttachmentTitle>
                      </AttachmentContent>
                      <AttachmentActions>
                        <AttachmentAction
                          aria-label={`Remove ${file.name}`}
                          onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}
                        >
                          <XIcon />
                        </AttachmentAction>
                      </AttachmentActions>
                    </Attachment>
                  ))}
                </InputGroupAddon>
              )}
              <InputGroupTextarea
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder={busy ? "The assistant is answering…" : "Ask anything, or pick a task above…"}
                aria-label="Message"
                className="min-h-14 p-3"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    formRef.current?.requestSubmit();
                  }
                }}
              />
              <InputGroupAddon align="block-end">
                <DropdownMenu>
                  <DropdownMenuTrigger
                    render={
                      <InputGroupButton
                        type="button"
                        size="icon-sm"
                        variant="outline"
                        className="rounded-full"
                        aria-label="More"
                      />
                    }
                  >
                    <PlusIcon />
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start" side="top" className="w-56">
                    {canAttach && (
                      <DropdownMenuItem onClick={() => fileRef.current?.click()}>
                        <PaperclipIcon />
                        Attach a report (PDF, image)
                      </DropdownMenuItem>
                    )}
                    {starters.flatMap((g) => g.starters).slice(0, 6).map((starter) => {
                      const Icon = ICONS[starter.icon] ?? SparklesIcon;
                      return (
                        <DropdownMenuItem key={starter.id} disabled={busy} onClick={() => start(starter)}>
                          <Icon />
                          {starter.title}
                        </DropdownMenuItem>
                      );
                    })}
                  </DropdownMenuContent>
                </DropdownMenu>
                {uploadError && <span className="truncate text-xs text-destructive">{uploadError}</span>}
                {busy ? (
                  <InputGroupButton
                    type="button"
                    size="icon-sm"
                    variant="default"
                    className="ml-auto rounded-full"
                    aria-label="Stop generating"
                    onClick={stop}
                  >
                    <SquareIcon className="fill-current" />
                  </InputGroupButton>
                ) : (
                  <InputGroupButton
                    type="submit"
                    size="icon-sm"
                    variant="default"
                    className="ml-auto rounded-full"
                    disabled={!canSend}
                    aria-label="Send"
                  >
                    {uploading ? <Spinner /> : <ArrowUpIcon />}
                  </InputGroupButton>
                )}
              </InputGroupAddon>
            </InputGroup>
          </form>
          <p className="text-center text-xs text-muted-foreground">
            The assistant gives information, not diagnosis. In an emergency call your local emergency number.
          </p>
        </div>
      </div>
    </MessageScrollerProvider>
  );
}

/** A turn's parts in order; runs of tool steps fold into one Trace. */
function AssistantParts({
  turn,
  active,
  onChoose,
  onDecide,
}: {
  turn: AssistantTurn;
  active: boolean;
  onChoose: (optionsId: string, optionId: string, label: string) => void;
  onDecide: (confirmationId: string, approved: boolean) => void;
}) {
  const blocks: ReactElement[] = [];
  let steps: Extract<Part, { kind: "tool" }>[] = [];
  const flush = (key: string) => {
    if (steps.length) blocks.push(<Steps key={key} steps={steps} />);
    steps = [];
  };
  turn.parts.forEach((part, index) => {
    if (part.kind === "tool") {
      steps.push(part);
      return;
    }
    flush(`steps-${index}`);
    const key = `${part.kind}-${index}`;
    switch (part.kind) {
      case "text":
        blocks.push(<Prose key={key} text={part.text} />);
        break;
      case "citations":
        blocks.push(<Sources key={key} part={part} />);
        break;
      case "options":
        blocks.push(
          <Choices key={key} part={part} active={active} onChoose={(id, label) => onChoose(part.id, id, label)} />,
        );
        break;
      case "confirmation":
        blocks.push(<Confirmation key={key} part={part} active={active} onDecide={(ok) => onDecide(part.id, ok)} />);
        break;
      case "card":
        blocks.push(<ResultCard key={key} part={part} />);
        break;
      case "error":
        blocks.push(<ErrorNote key={key} part={part} />);
        break;
      case "suggestions":
        break; // shown above the input, for the latest turn only
    }
  });
  flush("steps-end");
  if (turn.status === "streaming" && turn.parts.length === 0) {
    blocks.push(
      <div key="thinking" className="flex items-center gap-2 text-sm text-muted-foreground">
        <Spinner /> Thinking…
      </div>,
    );
  }
  if (turn.status === "cancelled") {
    blocks.push(
      <div key="stopped" className="text-xs text-muted-foreground">
        Stopped.
      </div>,
    );
  }
  return <>{blocks}</>;
}
