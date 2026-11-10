// The assistant's gRPC-Web client (spec D-30): typed from contracts/proto, same origin
// (Vite and nginx proxy the service path to Envoy), the user's bearer token in metadata.
//
// `streamTurn` owns the two failure modes a chat stream has in a browser:
// - an expired access token (UNAUTHENTICATED): refresh once and replay the call;
// - a dropped connection mid-answer: reconnect with the same turn id and
//   `resume_from` = the last seq seen, so the server replays from its record instead of
//   generating again (spec 4.4). Bounded, with backoff.

import { Code, ConnectError, createClient, type Interceptor } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { readToken, refreshOnce, sessionEnded } from "@/api";
import { Assistant, type ChatEvent } from "@/gen/smarthealth/assistant/v1/assistant_pb";

const auth: Interceptor = (next) => (request) => {
  const token = readToken();
  if (token) request.header.set("authorization", `Bearer ${token}`);
  return next(request);
};

const transport = createGrpcWebTransport({
  baseUrl: typeof window === "undefined" ? "http://localhost" : window.location.origin,
  interceptors: [auth],
});

export const assistant = createClient(Assistant, transport);

const RESUME_ATTEMPTS = 3;

export type Opener = (resumeFrom: bigint, signal: AbortSignal) => AsyncIterable<ChatEvent>;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Run one turn's stream to its end, calling `onEvent` for every event in seq order.
 * `open` starts (or resumes) the call; it is called again after a drop.
 */
export async function streamTurn(
  open: Opener,
  onEvent: (event: ChatEvent) => void,
  signal: AbortSignal,
  delays: number[] = [300, 1000, 2500],
): Promise<void> {
  let lastSeq = 0n;
  let refreshed = false;
  let attempt = 0;
  for (;;) {
    try {
      for await (const event of open(lastSeq, signal)) {
        if (event.seq > lastSeq) lastSeq = event.seq;
        onEvent(event);
        attempt = 0;
        if (event.kind.case === "done") return;
      }
      return; // the server closed the stream cleanly
    } catch (error) {
      if (signal.aborted) return;
      const code = error instanceof ConnectError ? error.code : Code.Unavailable;
      if (code === Code.Unauthenticated && !refreshed) {
        refreshed = true;
        if (await refreshOnce()) continue;
        sessionEnded();
        throw error;
      }
      const transient = code === Code.Unavailable || code === Code.Unknown || code === Code.Internal;
      if (!transient || attempt >= RESUME_ATTEMPTS) throw error;
      await sleep(delays[Math.min(attempt, delays.length - 1)]);
      attempt += 1;
    }
  }
}
