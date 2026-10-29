import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { Alert } from "react-native";

import type { ScreenProps } from "../../navigation";
import { resetServerClock } from "../../services/clock";
import { setAccessToken } from "../../services/tokens";
import { useMachines } from "../../store/machines";
import { useReservations } from "../../store/reservations";
import { installFakeServer, json, resetSecureStore } from "../../test-utils/fakeServer";
import { detail, MACHINE_ID } from "../../test-utils/fixtures";
import { order, pickup, reservation, RESERVATION_ID } from "../../test-utils/reservationFixtures";
import { MachineScreen } from "../MachineScreen";
import { OrdersScreen } from "../OrdersScreen";
import { ReservationScreen } from "../ReservationScreen";

jest.mock("@react-navigation/native", () => {
  const actual = jest.requireActual<typeof import("@react-navigation/native")>(
    "@react-navigation/native",
  );
  const { useEffect } = jest.requireActual<typeof import("react")>("react");
  return { ...actual, useFocusEffect: (effect: () => void) => useEffect(effect, [effect]) };
});

const R = `/api/v1/reservations/${RESERVATION_ID}`;
const M = `/api/v1/machines/${MACHINE_ID}`;

beforeEach(() => {
  setAccessToken("access");
  resetSecureStore();
  resetServerClock();
  useReservations.getState().clear();
  useMachines.setState({
    center: null,
    source: null,
    nearby: { status: "loading", data: null },
    details: {},
  });
});

function props<Name extends "Reservation" | "Machine" | "Orders">(name: Name, params: unknown) {
  const navigate = jest.fn();
  const value = { navigation: { navigate }, route: { key: name, name, params } };
  return { props: value as unknown as ScreenProps<Name>, navigate };
}

describe("ReservationScreen", () => {
  it("pays, then shows the QR code, the 6-digit code and the countdown", async () => {
    let paid = false;
    const { seen } = installFakeServer({
      [`GET ${R}`]: () => json(200, reservation({ status: paid ? "paid" : "held" })),
      [`GET ${M}`]: () => json(200, detail()),
      [`POST ${R}/checkout`]: () => {
        paid = true;
        return json(201, {
          reservation: reservation({ status: "paid" }),
          order: order({ payment_status: "authorized" }),
          pickup: pickup(),
        });
      },
    });
    await render(
      <ReservationScreen {...props("Reservation", { reservationId: RESERVATION_ID }).props} />,
    );

    expect(await screen.findByText("Held for you")).toBeTruthy();
    expect(screen.getByText(/^1[45]:\d\d$/)).toBeTruthy();
    expect(await screen.findByText("Universitate - Piata Universitatii")).toBeTruthy();

    await fireEvent.press(screen.getByText("Pay 14.50 RON"));

    expect(await screen.findByText("482 913")).toBeTruthy();
    expect(screen.getByTestId(`qr:CP1:${RESERVATION_ID}:482913`)).toBeTruthy();
    expect(screen.getByText("Pick up within")).toBeTruthy();
    expect(seen.find((s) => s.path.endsWith("/checkout"))?.body).toEqual({
      payment_method: "pm_card_ok",
    });
  });

  it("explains a declined card and stays payable", async () => {
    installFakeServer({
      [`GET ${R}`]: () => json(200, reservation()),
      [`GET ${M}`]: () => json(200, detail()),
      [`POST ${R}/checkout`]: () =>
        json(402, { detail: { code: "payment_declined", message: "card declined" } }),
    });
    await render(
      <ReservationScreen {...props("Reservation", { reservationId: RESERVATION_ID }).props} />,
    );

    await fireEvent.press(await screen.findByText("Demo: try a declined card"));

    expect(await screen.findByText("Your card was declined. Try another card.")).toBeTruthy();
    expect(screen.getByText("Pay 14.50 RON")).toBeTruthy();
  });

  it("issues a new code when the app no longer has it in memory", async () => {
    installFakeServer({
      [`GET ${R}`]: () => json(200, reservation({ status: "paid" })),
      [`GET ${M}`]: () => json(200, detail()),
      [`POST ${R}/pickup-code`]: () => json(200, pickup({ code: "777888" })),
    });
    await render(
      <ReservationScreen {...props("Reservation", { reservationId: RESERVATION_ID }).props} />,
    );

    await fireEvent.press(await screen.findByText("Show my pickup code"));

    expect(await screen.findByText("777 888")).toBeTruthy();
  });

  it("cancels after asking", async () => {
    let cancelled = false;
    installFakeServer({
      [`GET ${R}`]: () => json(200, reservation({ status: cancelled ? "cancelled" : "held" })),
      [`GET ${M}`]: () => json(200, detail()),
      [`DELETE ${R}`]: () => {
        cancelled = true;
        return new Response(null, { status: 204 });
      },
    });
    const alert = jest.spyOn(Alert, "alert");
    await render(
      <ReservationScreen {...props("Reservation", { reservationId: RESERVATION_ID }).props} />,
    );

    await fireEvent.press(await screen.findByText("Cancel reservation"));
    const buttons = alert.mock.calls[0]?.[2] ?? [];
    await act(async () => {
      buttons.find((b) => b.style === "destructive")?.onPress?.();
    });

    expect(await screen.findByText(/Cancelled. You haven't been charged./)).toBeTruthy();
  });

  it("re-reads the reservation when the countdown runs out", async () => {
    let calls = 0;
    installFakeServer({
      [`GET ${R}`]: () => {
        calls += 1;
        return json(
          200,
          calls === 1
            ? reservation({ expires_at: new Date(Date.now() + 1500).toISOString() })
            : reservation({ status: "expired" }),
        );
      },
      [`GET ${M}`]: () => json(200, detail()),
    });
    const { props: p, navigate } = props("Reservation", { reservationId: RESERVATION_ID });
    await render(<ReservationScreen {...p} />);

    expect(await screen.findByText(/This hold ran out/, {}, { timeout: 5000 })).toBeTruthy();
    await fireEvent.press(screen.getByText("Reserve again"));
    expect(navigate).toHaveBeenCalledWith("Machine", {
      machineId: MACHINE_ID,
      name: "Universitate - Piata Universitatii",
    });
  });
});

describe("MachineScreen reserve", () => {
  it("reserves a slot and opens the reservation", async () => {
    const { seen } = installFakeServer({
      [`GET ${M}`]: () => json(200, detail()),
      "POST /api/v1/reservations": () => json(201, reservation()),
    });
    const { props: p, navigate } = props("Machine", { machineId: MACHINE_ID, name: "x" });
    await render(<MachineScreen {...p} />);

    const [first] = await screen.findAllByText("Reserve");
    await fireEvent.press(first!);

    expect(navigate).toHaveBeenCalledWith("Reservation", { reservationId: RESERVATION_ID });
    expect(seen.find((s) => s.method === "POST")?.body).toMatchObject({
      slot_id: detail().slots[0]?.slot_id,
    });
    // Sold-out slots have no Reserve button: 3 slots, 1 sold out.
    expect(screen.getAllByText("Reserve")).toHaveLength(2);
  });

  it("explains when someone else got the last one", async () => {
    installFakeServer({
      [`GET ${M}`]: () => json(200, detail()),
      "POST /api/v1/reservations": () =>
        json(409, { detail: { code: "insufficient_stock", message: "Not enough stock" } }),
    });
    await render(
      <MachineScreen {...props("Machine", { machineId: MACHINE_ID, name: "x" }).props} />,
    );

    const [first] = await screen.findAllByText("Reserve");
    await fireEvent.press(first!);

    expect(await screen.findByText("Sorry, someone just reserved the last one.")).toBeTruthy();
  });

  it("offers no reserving at an offline machine", async () => {
    installFakeServer({ [`GET ${M}`]: () => json(200, detail({ online: false })) });
    await render(
      <MachineScreen {...props("Machine", { machineId: MACHINE_ID, name: "x" }).props} />,
    );

    const [first] = await screen.findAllByRole("button", { name: "Reserve" });
    expect(first?.props.accessibilityState).toMatchObject({ disabled: true });
  });
});

describe("OrdersScreen", () => {
  it("shows active reservations and the order history", async () => {
    installFakeServer({
      "GET /api/v1/reservations": () =>
        json(200, [
          reservation({ status: "paid" }),
          reservation({ id: "r-old", status: "expired" }),
        ]),
      "GET /api/v1/orders": () =>
        json(200, [
          order(),
          order({
            id: "o2",
            reservation_id: "r-old",
            payment_status: "voided",
            reservation_status: "expired",
          }),
        ]),
    });
    const { props: p, navigate } = props("Orders", undefined);
    await render(<OrdersScreen {...p} />);

    expect(await screen.findByText("Active")).toBeTruthy();
    expect(screen.getByText("Paid, ready to pick up")).toBeTruthy();
    expect(screen.getByText("Picked up · Charged")).toBeTruthy();
    expect(screen.getByText("Expired · Not charged")).toBeTruthy();

    await fireEvent.press(screen.getByText("Paid, ready to pick up"));
    expect(navigate).toHaveBeenCalledWith("Reservation", { reservationId: RESERVATION_ID });
  });
});
