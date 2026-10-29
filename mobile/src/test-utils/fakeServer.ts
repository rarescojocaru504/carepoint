// A tiny stand-in for the CarePoint API: route handlers keyed by "METHOD /path",
// installed as global fetch. Records every request for assertions.

import * as SecureStore from "expo-secure-store";

export interface Seen {
  method: string;
  path: string;
  authorization: string | null;
  body: unknown;
}

type Handler = (seen: Seen) => Response | Promise<Response>;

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function installFakeServer(routes: Record<string, Handler>): { seen: Seen[] } {
  const seen: Seen[] = [];
  global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init);
    const url = new URL(request.url);
    const text = await request.text();
    const entry: Seen = {
      method: request.method,
      path: url.pathname,
      authorization: request.headers.get("Authorization"),
      body: text === "" ? null : (JSON.parse(text) as unknown),
    };
    seen.push(entry);
    const handler = routes[`${entry.method} ${entry.path}`];
    if (handler === undefined) return json(404, { detail: "Not Found" });
    return handler(entry);
  });
  return { seen };
}

/** Empty the in-memory SecureStore mock from jest.setup.ts. */
export function resetSecureStore(): void {
  (SecureStore as unknown as { __reset: () => void }).__reset();
}
