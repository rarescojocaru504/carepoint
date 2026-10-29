import { refreshSession } from "../../services/api";
import { getRefreshToken, setAccessToken, setRefreshToken } from "../../services/tokens";
import { installFakeServer, json, resetSecureStore } from "../../test-utils/fakeServer";
import { useAuth } from "../auth";

const ME = {
  id: "7c1b6f9e-0000-4000-8000-000000000001",
  email: "customer@carepoint.demo",
  full_name: "Demo Customer",
  role: "customer",
  is_active: true,
  created_at: "2026-10-29T10:00:00Z",
};
const PAIR = { access_token: "access-1", refresh_token: "refresh-1", expires_in: 900 };

beforeEach(async () => {
  setAccessToken(null);
  resetSecureStore();
  useAuth.setState({ status: "restoring", user: null });
});

describe("restore", () => {
  it("is signed out when nothing is stored", async () => {
    installFakeServer({});
    await useAuth.getState().restore();
    expect(useAuth.getState().status).toBe("signedOut");
  });

  it("turns a stored refresh token back into a session", async () => {
    await setRefreshToken("refresh-0");
    installFakeServer({
      "POST /api/v1/auth/refresh": () => json(200, PAIR),
      "GET /api/v1/me": () => json(200, ME),
    });

    await useAuth.getState().restore();

    expect(useAuth.getState()).toMatchObject({ status: "signedIn", user: { email: ME.email } });
  });

  it("is unreachable, not signed out, when the server can't be asked", async () => {
    await setRefreshToken("refresh-0");
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Network request failed")));

    await useAuth.getState().restore();

    expect(useAuth.getState().status).toBe("unreachable");
    expect(await getRefreshToken()).toBe("refresh-0");
  });
});

describe("sign in, register and out", () => {
  it("signs in", async () => {
    installFakeServer({
      "POST /api/v1/auth/login": () => json(200, PAIR),
      "GET /api/v1/me": () => json(200, ME),
    });

    await useAuth.getState().signIn(ME.email, "carepoint-customer-demo");

    expect(useAuth.getState()).toMatchObject({ status: "signedIn", user: { email: ME.email } });
  });

  it("registers, then signs in with the same credentials", async () => {
    const { seen } = installFakeServer({
      "POST /api/v1/auth/register": () => json(201, ME),
      "POST /api/v1/auth/login": () => json(200, PAIR),
      "GET /api/v1/me": () => json(200, ME),
    });

    await useAuth.getState().register("Demo Customer", ME.email, "a-long-password");

    expect(seen.map((s) => s.path)).toEqual([
      "/api/v1/auth/register",
      "/api/v1/auth/login",
      "/api/v1/me",
    ]);
    expect(seen[0]?.body).toEqual({
      full_name: "Demo Customer",
      email: ME.email,
      password: "a-long-password",
    });
    expect(useAuth.getState().status).toBe("signedIn");
  });

  it("stays signed out when sign-in fails", async () => {
    installFakeServer({
      "POST /api/v1/auth/login": () => json(401, { detail: "Incorrect email or password" }),
    });

    await expect(useAuth.getState().signIn(ME.email, "wrong")).rejects.toThrow(
      "Incorrect email or password",
    );
    expect(useAuth.getState().status).toBe("restoring");
    expect(useAuth.getState().user).toBeNull();
  });

  it("signs out", async () => {
    useAuth.setState({ status: "signedIn", user: { ...ME, role: "customer" } });
    await setRefreshToken("refresh-1");
    installFakeServer({ "POST /api/v1/auth/logout": () => new Response(null, { status: 204 }) });

    await useAuth.getState().signOut();

    expect(useAuth.getState()).toMatchObject({ status: "signedOut", user: null });
  });

  it("signs out when the server ends the session", async () => {
    useAuth.setState({ status: "signedIn", user: { ...ME, role: "customer" } });
    await setRefreshToken("revoked");
    installFakeServer({ "POST /api/v1/auth/refresh": () => json(401, { detail: "revoked" }) });

    await refreshSession();

    expect(useAuth.getState()).toMatchObject({ status: "signedOut", user: null });
  });
});
