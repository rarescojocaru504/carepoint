import * as SecureStore from "expo-secure-store";

import { setAccessToken } from "../../services/tokens";
import { installFakeServer, json, resetSecureStore } from "../../test-utils/fakeServer";
import {
  order,
  pickup,
  reservation,
  RESERVATION_ID,
  SLOT_ID,
} from "../../test-utils/reservationFixtures";
import { useAuth } from "../auth";
import { DECLINED_CARD, newIdempotencyKey, TEST_CARD, useReservations } from "../reservations";

const R = `/api/v1/reservations/${RESERVATION_ID}`;

beforeEach(() => {
  setAccessToken("access");
  resetSecureStore();
  jest.clearAllMocks();
  useReservations.getState().clear();
});

function paidResponse() {
  return json(201, {
    reservation: reservation({ status: "paid" }),
    order: order({ payment_status: "authorized", reservation_status: "paid" }),
    pickup: pickup(),
  });
}

describe("reserve", () => {
  it("sends a fresh idempotency key in the format the API accepts", async () => {
    const { seen } = installFakeServer({
      "POST /api/v1/reservations": () => json(201, reservation()),
    });

    const held = await useReservations.getState().reserve(SLOT_ID);
    await useReservations.getState().reserve(SLOT_ID);

    expect(held.status).toBe("held");
    const keys = seen.map((s) => (s.body as { idempotency_key: string }).idempotency_key);
    expect(keys[0]).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(keys[0]).not.toBe(keys[1]);
    expect(seen[0]?.body).toMatchObject({ slot_id: SLOT_ID, quantity: 1 });
  });

  it("generates keys that stay valid and distinct", () => {
    const keys = new Set(Array.from({ length: 200 }, newIdempotencyKey));
    expect(keys.size).toBe(200);
    keys.forEach((key) => expect(key).toMatch(/^[A-Za-z0-9_-]{8,64}$/));
  });

  it("reports the API's business error code", async () => {
    installFakeServer({
      "POST /api/v1/reservations": () =>
        json(409, { detail: { code: "insufficient_stock", message: "Not enough stock" } }),
    });

    await expect(useReservations.getState().reserve(SLOT_ID)).rejects.toMatchObject({
      code: "insufficient_stock",
    });
  });
});

describe("pay", () => {
  it("keeps the pickup code in memory and never writes it to storage", async () => {
    const { seen } = installFakeServer({ [`POST ${R}/checkout`]: () => paidResponse() });

    await useReservations.getState().pay(RESERVATION_ID);

    const state = useReservations.getState();
    expect(state.byId[RESERVATION_ID]?.status).toBe("paid");
    expect(state.pickups[RESERVATION_ID]?.code).toBe("482913");
    expect(seen[0]?.body).toEqual({ payment_method: TEST_CARD });
    expect(SecureStore.setItemAsync).not.toHaveBeenCalled();
  });

  it("leaves a declined reservation held, with no code", async () => {
    installFakeServer({
      [`POST ${R}/checkout`]: () =>
        json(402, { detail: { code: "payment_declined", message: "card declined" } }),
    });
    useReservations.setState({ byId: { [RESERVATION_ID]: reservation() } });

    await expect(
      useReservations.getState().pay(RESERVATION_ID, DECLINED_CARD),
    ).rejects.toMatchObject({ status: 402, code: "payment_declined" });
    expect(useReservations.getState().byId[RESERVATION_ID]?.status).toBe("held");
    expect(useReservations.getState().pickups[RESERVATION_ID]).toBeUndefined();
  });

  it("keeps no code when a repeated checkout returns none", async () => {
    installFakeServer({
      [`POST ${R}/checkout`]: () =>
        json(200, { reservation: reservation({ status: "paid" }), order: order(), pickup: null }),
    });

    await useReservations.getState().pay(RESERVATION_ID);

    expect(useReservations.getState().pickups[RESERVATION_ID]).toBeUndefined();
  });
});

describe("pickup codes", () => {
  it("replaces the code with a new one", async () => {
    installFakeServer({
      [`POST ${R}/pickup-code`]: () => json(200, pickup({ code: "111222" })),
    });
    useReservations.setState({ pickups: { [RESERVATION_ID]: pickup() } });

    await useReservations.getState().newPickupCode(RESERVATION_ID);

    expect(useReservations.getState().pickups[RESERVATION_ID]?.code).toBe("111222");
  });

  it("forgets a code once its reservation is over", async () => {
    installFakeServer({ [`GET ${R}`]: () => json(200, reservation({ status: "dispensed" })) });
    useReservations.setState({ pickups: { [RESERVATION_ID]: pickup() } });

    await useReservations.getState().load(RESERVATION_ID);

    expect(useReservations.getState().pickups[RESERVATION_ID]).toBeUndefined();
  });

  it("forgets every code on sign-out", async () => {
    installFakeServer({ "POST /api/v1/auth/logout": () => new Response(null, { status: 204 }) });
    useReservations.setState({ pickups: { [RESERVATION_ID]: pickup() } });

    await useAuth.getState().signOut();

    expect(useReservations.getState().pickups).toEqual({});
  });
});

describe("cancel", () => {
  it("cancels, then re-reads the reservation", async () => {
    const { seen } = installFakeServer({
      [`DELETE ${R}`]: () => new Response(null, { status: 204 }),
      [`GET ${R}`]: () => json(200, reservation({ status: "cancelled" })),
    });

    await useReservations.getState().cancel(RESERVATION_ID);

    expect(seen.map((s) => s.method)).toEqual(["DELETE", "GET"]);
    expect(useReservations.getState().byId[RESERVATION_ID]?.status).toBe("cancelled");
  });

  it("reports a pickup in progress", async () => {
    installFakeServer({
      [`DELETE ${R}`]: () =>
        json(409, { detail: { code: "pickup_in_progress", message: "being dispensed" } }),
    });

    await expect(useReservations.getState().cancel(RESERVATION_ID)).rejects.toMatchObject({
      code: "pickup_in_progress",
    });
  });
});

describe("lists", () => {
  it("loads my reservations and my orders", async () => {
    installFakeServer({
      "GET /api/v1/reservations": () => json(200, [reservation({ status: "paid" })]),
      "GET /api/v1/orders": () => json(200, [order()]),
    });

    await useReservations.getState().loadMine();
    await useReservations.getState().loadOrders();

    const state = useReservations.getState();
    expect(state.mine).toMatchObject({ status: "ready", data: [{ status: "paid" }] });
    expect(state.byId[RESERVATION_ID]?.status).toBe("paid");
    expect(state.orders).toMatchObject({ status: "ready", data: [{ payment_status: "captured" }] });
  });

  it("keeps the last list when a reload fails", async () => {
    installFakeServer({ "GET /api/v1/orders": () => json(200, [order()]) });
    await useReservations.getState().loadOrders();
    global.fetch = jest.fn(() => Promise.reject(new TypeError("Network request failed")));

    await useReservations.getState().loadOrders();

    expect(useReservations.getState().orders).toMatchObject({ status: "error", data: [order()] });
  });
});
