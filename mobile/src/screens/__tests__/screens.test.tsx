import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

import type { ScreenProps } from "../../navigation";
import { setAccessToken } from "../../services/tokens";
import { useAuth } from "../../store/auth";
import { DEMO_CENTER, useMachines } from "../../store/machines";
import { installFakeServer, json, resetSecureStore } from "../../test-utils/fakeServer";
import { detail, MACHINE_ID, summary } from "../../test-utils/fixtures";
import { MachineScreen } from "../MachineScreen";
import { MapScreen } from "../MapScreen";
import { SignInScreen } from "../SignInScreen";

jest.mock("../../services/location", () => ({
  DEMO_CENTER: { lat: 46.77, lng: 23.59 },
  startPoint: jest.fn(() =>
    Promise.resolve({ point: { lat: 46.7712, lng: 23.6236 }, source: "device" }),
  ),
}));

const ME = {
  id: "7c1b6f9e-0000-4000-8000-000000000001",
  email: "customer@carepoint.demo",
  full_name: "Demo Customer",
  role: "customer",
  is_active: true,
  created_at: "2026-10-29T10:00:00Z",
};

beforeEach(() => {
  setAccessToken(null);
  resetSecureStore();
  useAuth.setState({ status: "signedOut", user: null });
  useMachines.setState({
    center: null,
    source: null,
    nearby: { status: "loading", data: null },
    details: {},
  });
});

function mapProps() {
  const navigate = jest.fn();
  const props = { navigation: { navigate }, route: { key: "Map", name: "Map" } };
  return { props: props as unknown as ScreenProps<"Map">, navigate };
}

function machineProps() {
  const props = {
    navigation: {},
    route: { key: "Machine", name: "Machine", params: { machineId: MACHINE_ID, name: "x" } },
  };
  return props as unknown as ScreenProps<"Machine">;
}

describe("SignInScreen", () => {
  it("signs in with the typed credentials", async () => {
    const { seen } = installFakeServer({
      "POST /api/v1/auth/login": () =>
        json(200, { access_token: "a", refresh_token: "r", expires_in: 900 }),
      "GET /api/v1/me": () => json(200, ME),
    });
    await render(<SignInScreen />);

    await fireEvent.changeText(screen.getByLabelText("Email"), " customer@carepoint.demo ");
    await fireEvent.changeText(screen.getByLabelText("Password"), "carepoint-customer-demo");
    await fireEvent.press(screen.getByText("Sign in"));

    await waitFor(() => expect(useAuth.getState().status).toBe("signedIn"));
    expect(seen[0]?.body).toEqual({
      email: "customer@carepoint.demo",
      password: "carepoint-customer-demo",
    });
  });

  it("shows the server's error for a wrong password", async () => {
    installFakeServer({
      "POST /api/v1/auth/login": () => json(401, { detail: "Incorrect email or password" }),
    });
    await render(<SignInScreen />);

    await fireEvent.changeText(screen.getByLabelText("Email"), "customer@carepoint.demo");
    await fireEvent.changeText(screen.getByLabelText("Password"), "wrong");
    await fireEvent.press(screen.getByText("Sign in"));

    expect(await screen.findByText("Incorrect email or password")).toBeTruthy();
    expect(useAuth.getState().status).toBe("signedOut");
  });

  it("checks the form before calling the server", async () => {
    const { seen } = installFakeServer({});
    await render(<SignInScreen />);

    await fireEvent.press(screen.getByText("New here? Create an account"));
    await fireEvent.changeText(screen.getByLabelText("Full name"), "Ana");
    await fireEvent.changeText(screen.getByLabelText("Email"), "ana@example.com");
    await fireEvent.changeText(screen.getByLabelText("Password"), "short");
    await fireEvent.press(screen.getByText("Create account"));

    expect(screen.getByText("Use a password of at least 12 characters.")).toBeTruthy();
    expect(seen).toHaveLength(0);
  });
});

describe("MapScreen", () => {
  it("lists nearby machines with their state and opens one", async () => {
    installFakeServer({
      "GET /api/v1/machines/nearby": () =>
        json(200, [
          summary(),
          summary({ id: "m2", name: "Hasdeu - Complex studentesc", online: false, distance_m: 3400 }),
          summary({ id: "m3", name: "Parcul Central", status: "maintenance", distance_m: 4100 }),
        ]),
    });
    const { props, navigate } = mapProps();
    await render(<MapScreen {...props} />);

    expect(await screen.findByText("Centru - Piata Unirii")).toBeTruthy();
    expect(screen.getByText("Open")).toBeTruthy();
    expect(screen.getByText("Offline")).toBeTruthy();
    expect(screen.getByText("Maintenance")).toBeTruthy();
    expect(screen.getByTestId("marker:Hasdeu - Complex studentesc")).toBeTruthy();

    await fireEvent.press(screen.getByText("Hasdeu - Complex studentesc"));
    expect(navigate).toHaveBeenCalledWith("Machine", { machineId: "m2", name: "Hasdeu - Complex studentesc" });
  });

  it("offers the demo area when nothing is near the phone", async () => {
    const { seen } = installFakeServer({ "GET /api/v1/machines/nearby": () => json(200, []) });
    await render(<MapScreen {...mapProps().props} />);

    await fireEvent.press(await screen.findByText("Show the Cluj-Napoca demo area"));

    expect(await screen.findByText(/Showing the Cluj-Napoca demo area/)).toBeTruthy();
    expect(useMachines.getState().center).toEqual(DEMO_CENTER);
    expect(seen.filter((s) => s.path === "/api/v1/machines/nearby")).toHaveLength(2);
  });

  it("shows a connection error", async () => {
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Network request failed")));
    await render(<MapScreen {...mapProps().props} />);

    expect(await screen.findByText(/Can't reach CarePoint/)).toBeTruthy();
  });
});

describe("MachineScreen", () => {
  it("shows each product with its price and what is left", async () => {
    installFakeServer({ [`GET /api/v1/machines/${MACHINE_ID}`]: () => json(200, detail()) });
    await render(<MachineScreen {...machineProps()} />);

    expect(await screen.findByText("Sanitary pads, regular (10)")).toBeTruthy();
    expect(screen.getByText("14.50 RON")).toBeTruthy();
    expect(screen.getByText("5 left")).toBeTruthy();
    expect(screen.getByText("1 left")).toBeTruthy();
    expect(screen.getByText("Low stock")).toBeTruthy(); // the sold-out slot isn't "low"
    expect(screen.getByText("Sold out")).toBeTruthy();
    expect(screen.getByText("Open")).toBeTruthy();
  });

  it("warns when the machine is offline", async () => {
    installFakeServer({
      [`GET /api/v1/machines/${MACHINE_ID}`]: () => json(200, detail({ online: false })),
    });
    await render(<MachineScreen {...machineProps()} />);

    expect(await screen.findByText(/offline right now/)).toBeTruthy();
    expect(screen.getByText("Offline")).toBeTruthy();
  });
});
