import { setAccessToken, setRefreshToken } from "../tokens";
import { json, resetSecureStore } from "../../test-utils/fakeServer";
import { LiveConnection, parseNotice } from "../live";
import type { LiveStatus, Notice } from "../live";
import { SseParser } from "../sse";

const NOTICE = {
  topic: "stock",
  machine_id: "11111111-1111-4111-8111-111111111111",
  reservation_id: null,
  change: "availability",
  at: "2026-10-29T12:00:00Z",
};

describe("SseParser", () => {
  it("returns each event's data once it is complete, across chunk boundaries", () => {
    const parser = new SseParser();
    expect(parser.push('data: {"a":')).toEqual([]);
    expect(parser.push('1}\n\n: keep-alive\n\ndata: {"b":2}\r\n\r\n')).toEqual([
      '{"a":1}',
      '{"b":2}',
    ]);
  });

  it("joins multi-line data and ignores other fields", () => {
    const parser = new SseParser();
    expect(parser.push("event: x\nid: 1\ndata: one\ndata:two\n\n")).toEqual(["one\ntwo"]);
  });
});

describe("parseNotice", () => {
  it("accepts the four topics and rejects anything else", () => {
    expect(parseNotice(JSON.stringify(NOTICE))).toEqual(NOTICE);
    expect(parseNotice(JSON.stringify({ ...NOTICE, topic: "telemetry" }))).toBeNull();
    expect(parseNotice(JSON.stringify({ topic: "stock" }))).toBeNull();
    expect(parseNotice("not json")).toBeNull();
    expect(parseNotice("null")).toBeNull();
  });
});

// --- a fake streaming server ------------------------------------------------------

interface Stream {
  authorization: string | null;
  send: (notice: object) => void;
  end: () => void;
}

/** Each GET /api/v1/events opens a stream the test can write to or end;
 * `respond` can answer instead (401, 503, a thrown network error). */
function fakeEventServer(
  respond?: (attempt: number, authorization: string | null) => Response | Error | null,
) {
  const streams: Stream[] = [];
  let attempts = 0;
  const refreshes: string[] = [];
  global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input !== "string") throw new TypeError("Unsupported BodyInit type");
    const url = new URL(input);
    const headers = new Headers(init?.headers);
    if (url.pathname === "/api/v1/auth/refresh") {
      refreshes.push(String(init?.body));
      return json(200, { access_token: "fresh", refresh_token: "r2", expires_in: 900 });
    }
    attempts += 1;
    const authorization = headers.get("authorization");
    const answer = respond?.(attempts, authorization) ?? null;
    if (answer instanceof Error) throw answer;
    if (answer !== null) return answer;
    const encoder = new TextEncoder();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start: (c) => {
        controller = c;
      },
    });
    init?.signal?.addEventListener("abort", () => controller.error(new Error("aborted")));
    const stream: Stream = {
      authorization,
      send: (notice) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(notice)}\n\n`)),
      end: () => controller.close(),
    };
    streams.push(stream);
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  });
  return { streams, refreshes, attempts: () => attempts };
}

async function until(check: () => boolean, ms = 2000): Promise<void> {
  const deadline = Date.now() + ms;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function connection(options: ConstructorParameters<typeof LiveConnection>[1] = {}) {
  const notices: Notice[] = [];
  const statuses: LiveStatus[] = [];
  const sleeps: number[] = [];
  const live = new LiveConnection(
    { onNotice: (n) => notices.push(n), onStatus: (s) => statuses.push(s) },
    {
      sleep: (ms) => {
        sleeps.push(ms);
        return new Promise((resolve) => setTimeout(resolve, 1)); // yield to timers
      },
      ...options,
    },
  );
  return { live, notices, statuses, sleeps };
}

beforeEach(() => {
  setAccessToken("access");
  resetSecureStore();
});

describe("LiveConnection", () => {
  it("streams notices with the access token and reports being live", async () => {
    const server = fakeEventServer();
    const { live, notices, statuses } = connection();

    live.start();
    await until(() => server.streams.length === 1);
    server.streams[0]?.send(NOTICE);
    await until(() => notices.length === 1);
    live.stop();

    expect(server.streams[0]?.authorization).toBe("Bearer access");
    expect(notices).toEqual([NOTICE]);
    expect(statuses).toEqual(["connecting", "live", "idle"]);
  });

  it("reconnects at once when the server ends the stream at token expiry", async () => {
    const server = fakeEventServer();
    const { live, statuses, sleeps } = connection();

    live.start();
    await until(() => server.streams.length === 1);
    server.streams[0]?.end();
    await until(() => server.streams.length === 2);
    live.stop();

    expect(sleeps).toEqual([]);
    expect(statuses).not.toContain("reconnecting");
  });

  it("refreshes the token once when the stream answers 401", async () => {
    await setRefreshToken("r1");
    const server = fakeEventServer((attempt) =>
      attempt === 1 ? json(401, { detail: "Invalid or expired token" }) : null,
    );
    const { live } = connection();

    live.start();
    await until(() => server.streams.length === 1);
    live.stop();

    expect(server.refreshes).toHaveLength(1);
    expect(server.streams[0]?.authorization).toBe("Bearer fresh");
  });

  it("stops when the session can't be refreshed", async () => {
    await setRefreshToken("revoked");
    const server = fakeEventServer(() => json(401, { detail: "nope" }));
    global.fetch = jest.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      return path === "/api/v1/auth/refresh"
        ? json(401, { detail: "Invalid or expired refresh token" })
        : json(401, { detail: "nope" });
    });
    const { live, statuses } = connection();

    live.start();
    await until(() => (global.fetch as jest.Mock).mock.calls.length === 2);
    await new Promise((resolve) => setTimeout(resolve, 20));

    expect((global.fetch as jest.Mock).mock.calls).toHaveLength(2); // no retry loop
    expect(statuses).toEqual(["connecting"]);
    expect(server.streams).toHaveLength(0);
    live.stop();
  });

  it("backs off while the server is unreachable, then goes live again", async () => {
    const server = fakeEventServer((attempt) =>
      attempt <= 3 ? new TypeError("Network request failed") : null,
    );
    const { live, statuses, sleeps } = connection({ delays: [10, 20, 40] });

    live.start();
    await until(() => server.streams.length === 1);
    live.stop();

    expect(sleeps).toEqual([10, 20, 40]);
    expect(statuses).toEqual([
      "connecting",
      "reconnecting",
      "reconnecting",
      "reconnecting",
      "live",
      "idle",
    ]);
  });

  it("treats 429 and 503 as temporary", async () => {
    const server = fakeEventServer((attempt) =>
      attempt === 1 ? json(429, { detail: "Too many" }) : attempt === 2 ? json(503, {}) : null,
    );
    const { live, sleeps } = connection({ delays: [5] });

    live.start();
    await until(() => server.streams.length === 1);
    live.stop();

    expect(sleeps).toEqual([5, 5]);
  });

  it("replaces a stream that goes silent", async () => {
    const server = fakeEventServer();
    const { live, statuses } = connection({ silenceLimitMs: 30 });

    live.start();
    await until(() => server.streams.length === 2, 3000); // the first was given up on
    live.stop();

    expect(statuses).toContain("reconnecting");
  });

  it("stops for good", async () => {
    const server = fakeEventServer();
    const { live, statuses } = connection();

    live.start();
    live.start(); // idempotent
    await until(() => server.streams.length === 1);
    live.stop();
    await new Promise((resolve) => setTimeout(resolve, 30));

    expect(server.attempts()).toBe(1);
    expect(live.running).toBe(false);
    expect(statuses.at(-1)).toBe("idle");
  });
});
