// The app's live connection: GET /api/v1/events (ADR-026, ADR-028).
//
// Expo's fetch can stream a response body and send an Authorization header,
// so the stream is read with fetch, like the operator dashboard does. The
// connection keeps itself alive:
// - the server ends each stream when the access token expires: reconnect at
//   once, refreshing the token if the server says 401;
// - a failure (offline, server down, 429) is retried with backoff, and the
//   status turns "reconnecting" so the UI can say so;
// - a stream that goes silent for longer than the server's keep-alive is
//   treated as dead (a phone switching networks often leaves a socket that
//   never errors) and replaced.

import { API_URL } from "../config";
import type { Schemas } from "./api";
import { refreshSession } from "./api";
import { SseParser } from "./sse";
import { getAccessToken } from "./tokens";

export type Notice = Schemas["CustomerNotice"];
export type LiveStatus = "idle" | "connecting" | "live" | "reconnecting";

const TOPICS: ReadonlySet<string> = new Set(["hello", "stock", "machine", "reservation"]);

/** Backoff after failed attempts, in ms; the last value repeats. */
export const RETRY_DELAYS = [1_000, 2_000, 5_000, 10_000, 30_000];
/** The server sends a keep-alive every 15 s. */
export const SILENCE_LIMIT_MS = 45_000;

export function parseNotice(data: string): Notice | null {
  try {
    const value: unknown = JSON.parse(data);
    if (value === null || typeof value !== "object") return null;
    const { topic, change } = value as Record<string, unknown>;
    if (typeof topic !== "string" || !TOPICS.has(topic) || typeof change !== "string") return null;
    return value as Notice;
  } catch {
    return null;
  }
}

export interface LiveHandlers {
  onNotice: (notice: Notice) => void;
  onStatus: (status: LiveStatus) => void;
}

export interface LiveOptions {
  delays?: number[];
  silenceLimitMs?: number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
}

function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

type Outcome = "ended" | "unauthorized" | "failed";

export class LiveConnection {
  private controller: AbortController | null = null;

  constructor(
    private readonly handlers: LiveHandlers,
    private readonly options: LiveOptions = {},
  ) {}

  get running(): boolean {
    return this.controller !== null;
  }

  start(): void {
    if (this.controller !== null) return;
    const controller = new AbortController();
    this.controller = controller;
    void this.run(controller.signal);
  }

  stop(): void {
    this.controller?.abort();
    this.controller = null;
    this.handlers.onStatus("idle");
  }

  private async run(signal: AbortSignal): Promise<void> {
    const delays = this.options.delays ?? RETRY_DELAYS;
    const sleep = this.options.sleep ?? abortableSleep;
    let failures = 0;
    let refreshed = false;
    this.handlers.onStatus("connecting");
    while (!signal.aborted) {
      const outcome = await this.once(signal, () => {
        failures = 0;
        refreshed = false;
        this.handlers.onStatus("live");
      });
      if (signal.aborted) return;
      if (outcome === "ended") continue; // token expiry: reconnect straight away
      if (outcome === "unauthorized" && !refreshed) {
        refreshed = true;
        const result = await refreshSession();
        if (result === "invalid") return; // signed out; the auth store takes over
        if (result === "ok") continue;
      }
      this.handlers.onStatus("reconnecting");
      await sleep(delays[Math.min(failures, delays.length - 1)] ?? 30_000, signal);
      failures += 1;
    }
  }

  private async once(signal: AbortSignal, onOpen: () => void): Promise<Outcome> {
    // One stream's own abort: stop() or the silence watchdog.
    const stream = new AbortController();
    const forward = () => stream.abort();
    signal.addEventListener("abort", forward);
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const feed = () => {
      clearTimeout(watchdog);
      watchdog = setTimeout(forward, this.options.silenceLimitMs ?? SILENCE_LIMIT_MS);
    };
    try {
      const token = getAccessToken();
      if (token === null) return "unauthorized";
      feed();
      const response = await fetch(`${API_URL}/api/v1/events`, {
        headers: { authorization: `Bearer ${token}`, accept: "text/event-stream" },
        signal: stream.signal,
      });
      if (response.status === 401) return "unauthorized";
      if (!response.ok || response.body === null) return "failed";
      onOpen();
      // Read bytes and decode here rather than piping through a
      // TextDecoderStream: the body's stream and the decoder's can come from
      // different implementations, which pipeThrough doesn't accept.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SseParser();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) return stream.signal.aborted ? "failed" : "ended";
        feed();
        for (const data of parser.push(decoder.decode(value, { stream: true }))) {
          const notice = parseNotice(data);
          if (notice !== null) this.handlers.onNotice(notice);
        }
      }
    } catch {
      return "failed"; // offline, reset, or aborted by the watchdog
    } finally {
      clearTimeout(watchdog);
      signal.removeEventListener("abort", forward);
    }
  }
}
