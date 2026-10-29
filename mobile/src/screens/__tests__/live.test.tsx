import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { AppState, Text } from "react-native";
import type { AppStateStatus } from "react-native";
import { SafeAreaProvider } from "react-native-safe-area-context";

import { ActiveReservations } from "../../components/ActiveReservations";
import { ConnectionBanner, useLiveUpdates } from "../../components/LiveUpdates";
import type { ScreenProps } from "../../navigation";
import { setAccessToken } from "../../services/tokens";
import { useAuth } from "../../store/auth";
import { useLive } from "../../store/live";
import { useMachines } from "../../store/machines";
import { useReservations } from "../../store/reservations";
import { installFakeServer, json } from "../../test-utils/fakeServer";
import { detail, MACHINE_ID } from "../../test-utils/fixtures";
import { reservation, RESERVATION_ID } from "../../test-utils/reservationFixtures";
import { ReservationScreen } from "../ReservationScreen";

const ME = {
  id: "7c1b6f9e-0000-4000-8000-000000000001",
  email: "customer@carepoint.demo",
  full_name: "Demo Customer",
  role: "customer" as const,
  is_active: true,
  created_at: "2026-10-29T10:00:00Z",
};

afterEach(() => jest.restoreAllMocks());

beforeEach(() => {
  setAccessToken("access");
  useReservations.getState().clear();
  useMachines.setState({
    center: null,
    source: null,
    nearby: { status: "loading", data: null },
    details: {},
  });
});

describe("useLiveUpdates", () => {
  function Probe() {
    useLiveUpdates();
    return <Text>probe</Text>;
  }

  it("runs while signed in and in the foreground", async () => {
    const start = jest.fn();
    const stop = jest.fn();
    useLive.setState({ start, stop });
    let onChange: ((state: AppStateStatus) => void) | undefined;
    jest.spyOn(AppState, "addEventListener").mockImplementation((_, handler) => {
      onChange = handler;
      return { remove: jest.fn() } as unknown as ReturnType<typeof AppState.addEventListener>;
    });
    useAuth.setState({ status: "signedIn", user: ME });

    await render(<Probe />);
    expect(start).toHaveBeenCalledTimes(1);

    await act(async () => onChange?.("background"));
    expect(stop).toHaveBeenCalled();
    await act(async () => onChange?.("active"));
    expect(start).toHaveBeenCalledTimes(2);

    stop.mockClear();
    await act(async () => {
      useAuth.setState({ status: "signedOut", user: null });
    });
    expect(stop).toHaveBeenCalled();
  });
});

describe("ConnectionBanner", () => {
  it("appears only while reconnecting", async () => {
    useLive.setState({ status: "live" });
    await render(
      <SafeAreaProvider>
        <ConnectionBanner />
      </SafeAreaProvider>,
    );
    expect(screen.queryByText(/Reconnecting/)).toBeNull();

    await act(async () => useLive.setState({ status: "reconnecting" }));
    expect(screen.getByText("Reconnecting… live updates are paused.")).toBeTruthy();
  });
});

describe("ActiveReservations", () => {
  it("lists running holds soonest first and disappears when they end", async () => {
    const soon = new Date(Date.now() + 5 * 60_000).toISOString();
    const later = new Date(Date.now() + 14 * 60_000).toISOString();
    useReservations.setState({
      byId: {
        a: reservation({ id: "a", status: "held", expires_at: later, product_name: "Condoms (3)" }),
        b: reservation({ id: "b", status: "paid", expires_at: soon }),
        c: reservation({ id: "c", status: "expired", product_name: "Old" }),
      },
    });
    const onOpen = jest.fn();
    await render(<ActiveReservations onOpen={onOpen} />);

    const rows = screen.getAllByRole("button");
    expect(rows).toHaveLength(2);
    expect(screen.getByText(/^Pick up within [45]:\d\d$/)).toBeTruthy();
    expect(screen.queryByText("Old")).toBeNull();
    await fireEvent.press(rows[0]!);
    expect(onOpen).toHaveBeenCalledWith("b");

    await act(async () =>
      useReservations.setState({ byId: { b: reservation({ id: "b", status: "dispensed" }) } }),
    );
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});

describe("ReservationScreen and the machine", () => {
  function props() {
    const value = {
      navigation: { navigate: jest.fn() },
      route: { key: "R", name: "Reservation", params: { reservationId: RESERVATION_ID } },
    };
    return value as unknown as ScreenProps<"Reservation">;
  }

  it("warns when the machine holding a paid item goes offline", async () => {
    let online = true;
    installFakeServer({
      [`GET /api/v1/reservations/${RESERVATION_ID}`]: () =>
        json(200, reservation({ status: "paid" })),
      [`GET /api/v1/machines/${MACHINE_ID}`]: () => json(200, detail({ online })),
    });
    await render(<ReservationScreen {...props()} />);
    expect(await screen.findByText("Pick up within")).toBeTruthy();
    expect(screen.queryByText(/offline right now/)).toBeNull();

    online = false;
    await act(() => useMachines.getState().loadMachine(MACHINE_ID)); // as a live notice would

    expect(await screen.findByText(/This machine is offline right now/)).toBeTruthy();
  });

  it("says nothing about the machine once the reservation is over", async () => {
    installFakeServer({
      [`GET /api/v1/reservations/${RESERVATION_ID}`]: () =>
        json(200, reservation({ status: "dispensed" })),
      [`GET /api/v1/machines/${MACHINE_ID}`]: () => json(200, detail({ online: false })),
    });
    await render(<ReservationScreen {...props()} />);

    expect(await screen.findByText(/Picked up/)).toBeTruthy();
    expect(screen.queryByText(/offline right now/)).toBeNull();
  });
});
