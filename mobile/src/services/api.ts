// Typed client for the CarePoint API (ADR-024).
//
// Every path, parameter and body is typed from mobile/openapi.json, which the
// backend exports and drift-tests; `npm run gen:api` turns it into
// api-types.ts. Like the dashboard's client (ADR-023), a 401 triggers one
// token refresh, shared by every request that hit it, and the request is then
// retried once, so a rotated refresh token is never presented twice.

import createClient from "openapi-fetch";

import { API_URL } from "../config";
import type { components, paths } from "./api-types";
import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  setAccessToken,
  setRefreshToken,
} from "./tokens";

export type Schemas = components["schemas"];
export type Me = Schemas["UserRead"];
type TokenPair = Schemas["TokenPair"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `Request failed (${status})`);
    this.name = "ApiError";
  }

  /** The stable business error code, e.g. "insufficient_stock", if any. */
  get code(): string | null {
    const detail = this.detail;
    if (detail !== null && typeof detail === "object" && "code" in detail) {
      const code: unknown = detail.code;
      return typeof code === "string" ? code : null;
    }
    return null;
  }
}

/** The human message from an error, including FastAPI's {"detail": ...} shapes. */
export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    if (error instanceof TypeError) return "Can't reach CarePoint. Check your connection.";
    return error instanceof Error ? error.message : String(error);
  }
  const detail = error.detail;
  if (typeof detail === "string") return detail;
  if (detail !== null && typeof detail === "object" && "message" in detail) {
    return String(detail.message);
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const first: unknown = detail[0];
    if (first !== null && typeof first === "object" && "msg" in first) {
      return String(first.msg).replace(/^Value error, /, "");
    }
  }
  return error.message;
}

// --- session ---------------------------------------------------------------

/** "unavailable": no answer (offline, rate-limited, server down); the stored
 * refresh token is kept so the session can be restored later. */
export type RefreshResult = "ok" | "invalid" | "unavailable";

type Listener = () => void;
const sessionEndedListeners = new Set<Listener>();

/** Called when the server rejects the refresh token, so the UI can sign out. */
export function onSessionEnded(listener: Listener): () => void {
  sessionEndedListeners.add(listener);
  return () => {
    sessionEndedListeners.delete(listener);
  };
}

interface Outgoing {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

/** openapi-fetch hands us a Request object. It is re-sent as a plain URL and
 * init, never as a Request: React Native's fetch can't read a Request built by
 * a different implementation ("Unsupported BodyInit type"). Every body this
 * API takes is JSON, so reading it as text once is safe, and the same text
 * serves the retry after a refresh. */
async function outgoing(request: Request): Promise<Outgoing> {
  const headers: Record<string, string> = {};
  request.headers.forEach((value, key) => {
    headers[key] = value;
  });
  const text = request.method === "GET" || request.method === "HEAD" ? "" : await request.text();
  return { url: request.url, method: request.method, headers, body: text === "" ? undefined : text };
}

function send(out: Outgoing, accessToken: string | null): Promise<Response> {
  const headers = { ...out.headers };
  if (accessToken !== null) headers.authorization = `Bearer ${accessToken}`;
  // fetch is looked up per call, not captured at import, so tests can replace it.
  return fetch(out.url, { method: out.method, headers, body: out.body });
}

// Auth endpoints never carry the access token and never trigger a refresh.
const publicClient = createClient<paths>({
  baseUrl: API_URL,
  fetch: async (request: Request) => send(await outgoing(request), null),
});

async function storePair(pair: TokenPair): Promise<void> {
  setAccessToken(pair.access_token);
  await setRefreshToken(pair.refresh_token);
}

let refreshing: Promise<RefreshResult> | null = null;

/** Swap the stored refresh token for a new pair; one refresh at a time. */
export function refreshSession(): Promise<RefreshResult> {
  if (refreshing !== null) return refreshing;
  refreshing = (async (): Promise<RefreshResult> => {
    try {
      const refreshToken = await getRefreshToken();
      if (refreshToken === null) return "invalid";
      let result;
      try {
        result = await publicClient.POST("/api/v1/auth/refresh", {
          body: { refresh_token: refreshToken },
        });
      } catch {
        return "unavailable";
      }
      if (result.data !== undefined) {
        await storePair(result.data);
        return "ok";
      }
      if (result.response.status === 401 || result.response.status === 422) {
        await clearTokens();
        sessionEndedListeners.forEach((listener) => listener());
        return "invalid";
      }
      return "unavailable";
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

async function authorizedFetch(request: Request): Promise<Response> {
  const out = await outgoing(request);
  const response = await send(out, getAccessToken());
  if (response.status !== 401) return response;
  if ((await refreshSession()) !== "ok") return response;
  return send(out, getAccessToken());
}

export const api = createClient<paths>({ baseUrl: API_URL, fetch: authorizedFetch });

type Result<T> = { data?: T; error?: unknown; response: Response };

/** The response body, or an ApiError carrying FastAPI's `detail`. */
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok || data === undefined) {
    const detail: unknown =
      error !== null && typeof error === "object" && "detail" in error
        ? error.detail
        : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return data;
}

// --- auth --------------------------------------------------------------------

export async function getMe(): Promise<Me> {
  return unwrap(api.GET("/api/v1/me"));
}

export async function signIn(email: string, password: string): Promise<Me> {
  const pair = await unwrap(publicClient.POST("/api/v1/auth/login", { body: { email, password } }));
  await storePair(pair);
  return getMe();
}

export async function register(fullName: string, email: string, password: string): Promise<Me> {
  await unwrap(
    publicClient.POST("/api/v1/auth/register", {
      body: { full_name: fullName, email, password },
    }),
  );
  return signIn(email, password);
}

/** Forget the session on this device, then revoke it on the server (best effort). */
export async function signOut(): Promise<void> {
  const refreshToken = await getRefreshToken();
  await clearTokens();
  if (refreshToken !== null) {
    try {
      await publicClient.POST("/api/v1/auth/logout", { body: { refresh_token: refreshToken } });
    } catch {
      // offline: the token expires on its own
    }
  }
}
