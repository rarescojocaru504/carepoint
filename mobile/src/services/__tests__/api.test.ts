import * as SecureStore from "expo-secure-store";

import { apiBaseUrl } from "../../config";
import { installFakeServer, json, resetSecureStore } from "../../test-utils/fakeServer";
import {
  api,
  ApiError,
  errorMessage,
  onSessionEnded,
  refreshSession,
  signIn,
  signOut,
  unwrap,
} from "../api";
import { getAccessToken, getRefreshToken, setAccessToken, setRefreshToken } from "../tokens";

const ME = {
  id: "7c1b6f9e-0000-4000-8000-000000000001",
  email: "customer@carepoint.demo",
  full_name: "Demo Customer",
  role: "customer",
  is_active: true,
  created_at: "2026-10-29T10:00:00Z",
};

function pair(n: number) {
  return { access_token: `access-${n}`, refresh_token: `refresh-${n}`, expires_in: 900 };
}

beforeEach(async () => {
  setAccessToken(null);
  resetSecureStore();
  jest.clearAllMocks();
});

describe("signIn", () => {
  it("keeps the access token in memory and only the refresh token in SecureStore", async () => {
    const { seen } = installFakeServer({
      "POST /api/v1/auth/login": () => json(200, pair(1)),
      "GET /api/v1/me": () => json(200, ME),
    });

    const me = await signIn("customer@carepoint.demo", "carepoint-customer-demo");

    expect(me.email).toBe(ME.email);
    expect(getAccessToken()).toBe("access-1");
    expect(await getRefreshToken()).toBe("refresh-1");
    const stored = (SecureStore.setItemAsync as jest.Mock).mock.calls.map((call) => call[1]);
    expect(stored).toEqual(["refresh-1"]);
    expect(seen[0]?.authorization).toBeNull(); // login never carries a token
    expect(seen[1]?.authorization).toBe("Bearer access-1");
  });

  it("reports a wrong password as an ApiError with the server's message", async () => {
    installFakeServer({
      "POST /api/v1/auth/login": () => json(401, { detail: "Incorrect email or password" }),
    });

    const failure = signIn("customer@carepoint.demo", "wrong");

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ status: 401 });
    expect(await getRefreshToken()).toBeNull();
  });
});

describe("refresh on 401", () => {
  it("refreshes once and retries the request with its body intact", async () => {
    setAccessToken("expired");
    await setRefreshToken("refresh-0");
    const { seen } = installFakeServer({
      "POST /api/v1/auth/refresh": () => json(200, pair(1)),
      "POST /api/v1/reservations": (req) =>
        req.authorization === "Bearer access-1"
          ? json(201, { id: "r1" })
          : json(401, { detail: "Invalid or expired token" }),
    });

    const body = { slot_id: "00000000-0000-4000-8000-000000000001", quantity: 1 };
    const result = await api.POST("/api/v1/reservations", { body });

    expect(result.response.status).toBe(201);
    const reservations = seen.filter((s) => s.path === "/api/v1/reservations");
    expect(reservations).toHaveLength(2);
    expect(reservations[1]?.body).toEqual(body);
    expect(seen.find((s) => s.path === "/api/v1/auth/refresh")?.body).toEqual({
      refresh_token: "refresh-0",
    });
    expect(await getRefreshToken()).toBe("refresh-1");
  });

  it("shares one refresh between concurrent requests", async () => {
    setAccessToken("expired");
    await setRefreshToken("refresh-0");
    const { seen } = installFakeServer({
      "POST /api/v1/auth/refresh": () => json(200, pair(1)),
      "GET /api/v1/me": (req) =>
        req.authorization === "Bearer access-1" ? json(200, ME) : json(401, { detail: "x" }),
    });

    const results = await Promise.all([
      unwrap(api.GET("/api/v1/me")),
      unwrap(api.GET("/api/v1/me")),
      unwrap(api.GET("/api/v1/me")),
    ]);

    expect(results.map((r) => r.email)).toEqual([ME.email, ME.email, ME.email]);
    // Presenting a rotated refresh token twice would revoke the whole family (ADR-009).
    expect(seen.filter((s) => s.path === "/api/v1/auth/refresh")).toHaveLength(1);
  });

  it("signs out when the server rejects the refresh token", async () => {
    setAccessToken("expired");
    await setRefreshToken("revoked");
    installFakeServer({
      "POST /api/v1/auth/refresh": () => json(401, { detail: "Invalid or expired refresh token" }),
      "GET /api/v1/me": () => json(401, { detail: "Invalid or expired token" }),
    });
    const ended = jest.fn();
    const unsubscribe = onSessionEnded(ended);

    await expect(unwrap(api.GET("/api/v1/me"))).rejects.toMatchObject({ status: 401 });

    unsubscribe();
    expect(ended).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBeNull();
    expect(await getRefreshToken()).toBeNull();
  });

  it("keeps the refresh token when the server can't be reached", async () => {
    await setRefreshToken("refresh-0");
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Network request failed")));

    expect(await refreshSession()).toBe("unavailable");
    expect(await getRefreshToken()).toBe("refresh-0");
  });

  it("keeps the refresh token when the server is busy", async () => {
    await setRefreshToken("refresh-0");
    installFakeServer({
      "POST /api/v1/auth/refresh": () => json(429, { detail: "Too many requests" }),
    });

    expect(await refreshSession()).toBe("unavailable");
    expect(await getRefreshToken()).toBe("refresh-0");
  });

  it("does not try to refresh without a stored token", async () => {
    const { seen } = installFakeServer({
      "GET /api/v1/me": () => json(401, { detail: "Not authenticated" }),
    });

    await expect(unwrap(api.GET("/api/v1/me"))).rejects.toMatchObject({ status: 401 });
    expect(seen.map((s) => s.path)).toEqual(["/api/v1/me"]);
  });
});

describe("signOut", () => {
  it("forgets the tokens and revokes the session on the server", async () => {
    setAccessToken("access-1");
    await setRefreshToken("refresh-1");
    const { seen } = installFakeServer({
      "POST /api/v1/auth/logout": () => new Response(null, { status: 204 }),
    });

    await signOut();

    expect(getAccessToken()).toBeNull();
    expect(await getRefreshToken()).toBeNull();
    expect(seen[0]?.body).toEqual({ refresh_token: "refresh-1" });
  });

  it("still signs out on this device when offline", async () => {
    await setRefreshToken("refresh-1");
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Network request failed")));

    await signOut();

    expect(await getRefreshToken()).toBeNull();
  });
});

describe("errors", () => {
  it("reads every FastAPI error shape", () => {
    expect(errorMessage(new ApiError(409, "Email already registered"))).toBe(
      "Email already registered",
    );
    const business = new ApiError(409, { code: "insufficient_stock", message: "Only 1 left" });
    expect(errorMessage(business)).toBe("Only 1 left");
    expect(business.code).toBe("insufficient_stock");
    const validation = new ApiError(422, [{ msg: "Value error, password too short" }]);
    expect(errorMessage(validation)).toBe("password too short");
    expect(validation.code).toBeNull();
    expect(errorMessage(new TypeError("Network request failed"))).toMatch(/connection/);
  });
});

describe("apiBaseUrl", () => {
  it("defaults to localhost and drops trailing slashes", () => {
    expect(apiBaseUrl(undefined)).toBe("http://localhost:8000");
    expect(apiBaseUrl("  ")).toBe("http://localhost:8000");
    expect(apiBaseUrl("http://192.168.1.23:8000/")).toBe("http://192.168.1.23:8000");
  });
});
