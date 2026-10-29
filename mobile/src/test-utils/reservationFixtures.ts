import type { Order, Pickup, Reservation } from "../store/reservations";
import { MACHINE_ID } from "./fixtures";

export const RESERVATION_ID = "44444444-4444-4444-8444-444444444444";
export const SLOT_ID = "33333333-3333-4333-8333-333333333331";

export function reservation(overrides: Partial<Reservation> = {}): Reservation {
  return {
    id: RESERVATION_ID,
    machine_id: MACHINE_ID,
    slot_id: SLOT_ID,
    product_sku: "CP-PAD-REG-10",
    product_name: "Sanitary pads, regular (10)",
    quantity: 1,
    unit_price_cents: 1450,
    total_cents: 1450,
    currency: "RON",
    status: "held",
    created_at: "2026-10-29T12:00:00Z",
    expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    ...overrides,
  };
}

export function pickup(overrides: Partial<Pickup> = {}): Pickup {
  return {
    code: "482913",
    qr_payload: `CP1:${RESERVATION_ID}:482913`,
    expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
    ...overrides,
  };
}

export function order(overrides: Partial<Order> = {}): Order {
  return {
    id: "55555555-5555-4555-8555-555555555555",
    reservation_id: RESERVATION_ID,
    product_sku: "CP-PAD-REG-10",
    product_name: "Sanitary pads, regular (10)",
    quantity: 1,
    amount_cents: 1450,
    currency: "RON",
    payment_status: "captured",
    reservation_status: "dispensed",
    created_at: "2026-10-29T12:01:00Z",
    updated_at: "2026-10-29T12:05:00Z",
    ...overrides,
  };
}
