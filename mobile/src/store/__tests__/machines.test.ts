import * as Location from "expo-location";

import { setAccessToken } from "../../services/tokens";
import { installFakeServer, json, resetSecureStore } from "../../test-utils/fakeServer";
import { detail, MACHINE_ID, summary } from "../../test-utils/fixtures";
import { DEMO_CENTER, SEARCH_RADIUS_M, useMachines } from "../machines";

jest.mock("expo-location", () => ({
  PermissionStatus: { GRANTED: "granted", DENIED: "denied" },
  Accuracy: { Balanced: 3 },
  requestForegroundPermissionsAsync: jest.fn(),
  getLastKnownPositionAsync: jest.fn(),
  getCurrentPositionAsync: jest.fn(),
}));

const location = jest.mocked(Location);

beforeEach(() => {
  setAccessToken(null);
  resetSecureStore();
  jest.clearAllMocks();
  useMachines.setState({
    center: null,
    source: null,
    nearby: { status: "loading", data: null },
    details: {},
  });
});

function grant() {
  location.requestForegroundPermissionsAsync.mockResolvedValue({
    status: Location.PermissionStatus.GRANTED,
  } as Awaited<ReturnType<typeof Location.requestForegroundPermissionsAsync>>);
}

function position(lat: number, lng: number) {
  return { coords: { latitude: lat, longitude: lng } } as Location.LocationObject;
}

describe("locate", () => {
  it("searches around the phone when location is allowed", async () => {
    grant();
    location.getLastKnownPositionAsync.mockResolvedValue(position(44.4268, 26.1025));
    const { seen } = installFakeServer({
      "GET /api/v1/machines/nearby": () => json(200, [summary()]),
    });

    await useMachines.getState().locate();

    const state = useMachines.getState();
    expect(state.source).toBe("device");
    expect(state.center).toEqual({ lat: 44.4268, lng: 26.1025 });
    expect(state.nearby).toEqual({ status: "ready", data: [summary()] });
    expect(seen.map((s) => s.path)).toEqual(["/api/v1/machines/nearby"]);
  });

  it("sends the position and radius as query parameters", async () => {
    grant();
    location.getLastKnownPositionAsync.mockResolvedValue(null);
    location.getCurrentPositionAsync.mockResolvedValue(position(44.4268, 26.1025));
    const urls: string[] = [];
    global.fetch = jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      urls.push(new Request(input, init).url);
      return json(200, []);
    });

    await useMachines.getState().locate();

    const query = new URL(urls[0] ?? "").searchParams;
    expect(query.get("lat")).toBe("44.4268");
    expect(query.get("lng")).toBe("26.1025");
    expect(query.get("radius_m")).toBe(String(SEARCH_RADIUS_M));
  });

  it("falls back to the Bucharest demo area when location is refused", async () => {
    location.requestForegroundPermissionsAsync.mockResolvedValue({
      status: Location.PermissionStatus.DENIED,
    } as Awaited<ReturnType<typeof Location.requestForegroundPermissionsAsync>>);
    installFakeServer({ "GET /api/v1/machines/nearby": () => json(200, []) });

    await useMachines.getState().locate();

    expect(useMachines.getState()).toMatchObject({ center: DEMO_CENTER, source: "demo" });
    expect(location.getCurrentPositionAsync).not.toHaveBeenCalled();
  });

  it("falls back to the demo area when location services fail", async () => {
    location.requestForegroundPermissionsAsync.mockRejectedValue(new Error("services off"));
    installFakeServer({ "GET /api/v1/machines/nearby": () => json(200, []) });

    await useMachines.getState().locate();

    expect(useMachines.getState().source).toBe("demo");
  });

  it("keeps the last list and reports the error when a reload fails", async () => {
    useMachines.setState({ nearby: { status: "ready", data: [summary()] } });
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Network request failed")));

    await useMachines.getState().loadNearby(DEMO_CENTER, "demo");

    const nearby = useMachines.getState().nearby;
    expect(nearby.status).toBe("error");
    expect(nearby.data).toEqual([summary()]);
  });
});

describe("loadMachine", () => {
  it("loads a machine's slots", async () => {
    installFakeServer({ [`GET /api/v1/machines/${MACHINE_ID}`]: () => json(200, detail()) });

    await useMachines.getState().loadMachine(MACHINE_ID);

    expect(useMachines.getState().details[MACHINE_ID]).toEqual({
      status: "ready",
      data: detail(),
    });
  });

  it("reports an unknown machine", async () => {
    installFakeServer({});

    await useMachines.getState().loadMachine(MACHINE_ID);

    expect(useMachines.getState().details[MACHINE_ID]).toMatchObject({
      status: "error",
      data: null,
      error: "Not Found",
    });
  });
});
