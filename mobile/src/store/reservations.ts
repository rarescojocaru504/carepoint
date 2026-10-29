// The customer's reservations, pickup codes and orders (ADR-027).
//
// Pickup codes live in this store's memory and nowhere else: never in
// SecureStore, AsyncStorage or logs. The server keeps only a hash (ADR-016),
// so a code lost when the app restarts is replaced by issuing a new one.

import { create } from "zustand";

import { api, expectOk, unwrap } from "../services/api";
import type { Schemas } from "../services/api";

export type Reservation = Schemas["ReservationRead"];
export type Order = Schemas["OrderRead"];
export type Pickup = Schemas["PickupCredentialRead"];

/** Mock payment tokens (ADR-015): the demo can show a decline too. */
export const TEST_CARD = "pm_card_ok";
export const DECLINED_CARD = "pm_card_declined";

const ACTIVE: ReadonlySet<Reservation["status"]> = new Set(["held", "paid"]);

export function isActive(reservation: Reservation): boolean {
  return ACTIVE.has(reservation.status);
}

/** A fresh key per tap of "Reserve": a retried request can't make a second hold. */
export function newIdempotencyKey(): string {
  const random = () => Math.random().toString(36).slice(2, 10).padEnd(8, "0");
  return `${Date.now().toString(36)}-${random()}${random()}`;
}

type ListState<T> =
  | { status: "idle" | "loading"; data: T[] | null }
  | { status: "ready"; data: T[] }
  | { status: "error"; data: T[] | null; error: unknown };

interface ReservationsState {
  byId: Record<string, Reservation>;
  /** Pickup credentials by reservation id. Memory only. */
  pickups: Record<string, Pickup>;
  mine: ListState<Reservation>;
  orders: ListState<Order>;

  reserve: (slotId: string, quantity?: number) => Promise<Reservation>;
  pay: (reservationId: string, paymentMethod?: string) => Promise<Reservation>;
  /** Issue a new code; the previous one stops working at the machine. */
  newPickupCode: (reservationId: string) => Promise<Pickup>;
  cancel: (reservationId: string) => Promise<void>;
  load: (reservationId: string) => Promise<Reservation>;
  loadMine: () => Promise<void>;
  loadOrders: () => Promise<void>;
  /** Forget everything, e.g. on sign-out. */
  clear: () => void;
}

const EMPTY = {
  byId: {},
  pickups: {},
  mine: { status: "idle", data: null },
  orders: { status: "idle", data: null },
} as const;

function path(reservationId: string) {
  return { params: { path: { reservation_id: reservationId } } };
}

export const useReservations = create<ReservationsState>()((set, get) => {
  const remember = (reservation: Reservation) => {
    set((s) => {
      const pickups = { ...s.pickups };
      if (!isActive(reservation)) delete pickups[reservation.id]; // a dead code is useless
      return { byId: { ...s.byId, [reservation.id]: reservation }, pickups };
    });
    return reservation;
  };

  return {
    ...EMPTY,

    reserve: async (slotId, quantity = 1) => {
      const reservation = await unwrap(
        api.POST("/api/v1/reservations", {
          body: { slot_id: slotId, quantity, idempotency_key: newIdempotencyKey() },
        }),
      );
      return remember(reservation);
    },

    pay: async (reservationId, paymentMethod = TEST_CARD) => {
      const result = await unwrap(
        api.POST("/api/v1/reservations/{reservation_id}/checkout", {
          ...path(reservationId),
          body: { payment_method: paymentMethod },
        }),
      );
      const pickup = result.pickup;
      if (pickup !== null && pickup !== undefined) {
        set((s) => ({ pickups: { ...s.pickups, [reservationId]: pickup } }));
      }
      return remember(result.reservation);
    },

    newPickupCode: async (reservationId) => {
      const pickup = await unwrap(
        api.POST("/api/v1/reservations/{reservation_id}/pickup-code", path(reservationId)),
      );
      set((s) => ({ pickups: { ...s.pickups, [reservationId]: pickup } }));
      return pickup;
    },

    cancel: async (reservationId) => {
      await expectOk(api.DELETE("/api/v1/reservations/{reservation_id}", path(reservationId)));
      await get().load(reservationId);
    },

    load: async (reservationId) =>
      remember(await unwrap(api.GET("/api/v1/reservations/{reservation_id}", path(reservationId)))),

    loadMine: async () => {
      set((s) => ({ mine: { status: "loading", data: s.mine.data } }));
      try {
        const list = await unwrap(api.GET("/api/v1/reservations", {}));
        list.forEach(remember);
        set({ mine: { status: "ready", data: list } });
      } catch (error) {
        set((s) => ({ mine: { status: "error", data: s.mine.data, error } }));
      }
    },

    loadOrders: async () => {
      set((s) => ({ orders: { status: "loading", data: s.orders.data } }));
      try {
        const list = await unwrap(api.GET("/api/v1/orders", {}));
        set({ orders: { status: "ready", data: list } });
      } catch (error) {
        set((s) => ({ orders: { status: "error", data: s.orders.data, error } }));
      }
    },

    clear: () => set(EMPTY),
  };
});
