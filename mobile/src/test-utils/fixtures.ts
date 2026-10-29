// Sample API payloads, shaped exactly like the generated types.

import type { Schemas } from "../services/api";

export const MACHINE_ID = "11111111-1111-4111-8111-111111111111";

export function summary(overrides: Partial<Schemas["MachineSummary"]> = {}) {
  const machine: Schemas["MachineSummary"] = {
    id: MACHINE_ID,
    name: "Universitate - Piata Universitatii",
    address: "Bd. Regina Elisabeta 4-12, Bucuresti",
    location: { lat: 44.4355, lng: 26.1025 },
    status: "active",
    online: true,
    distance_m: 640,
    units_available: 9,
    products_available: 4,
    ...overrides,
  };
  return machine;
}

export function product(overrides: Partial<Schemas["ProductRead"]> = {}) {
  const value: Schemas["ProductRead"] = {
    id: "22222222-2222-4222-8222-222222222222",
    sku: "CP-PAD-REG-10",
    name: "Sanitary pads, regular (10)",
    description: "Individually wrapped",
    category: "menstrual_care",
    price_cents: 1450,
    currency: "RON",
    image_url: null,
    is_active: true,
    ...overrides,
  };
  return value;
}

export function detail(overrides: Partial<Schemas["MachineDetail"]> = {}) {
  const machine: Schemas["MachineDetail"] = {
    id: MACHINE_ID,
    name: "Universitate - Piata Universitatii",
    address: "Bd. Regina Elisabeta 4-12, Bucuresti",
    location: { lat: 44.4355, lng: 26.1025 },
    status: "active",
    online: true,
    last_seen_at: "2026-10-29T12:00:00Z",
    slots: [
      {
        slot_id: "33333333-3333-4333-8333-333333333331",
        slot_code: "A1",
        product: product(),
        available: 5,
        low_stock: false,
      },
      {
        slot_id: "33333333-3333-4333-8333-333333333332",
        slot_code: "A2",
        product: product({ id: "p2", sku: "CP-CND-3", name: "Condoms (3)", price_cents: 2500 }),
        available: 1,
        low_stock: true,
      },
      {
        slot_id: "33333333-3333-4333-8333-333333333333",
        slot_code: "B1",
        product: product({
          id: "p3",
          sku: "CP-PLS-20",
          name: "Plasters (20)",
          description: null,
          price_cents: 900,
        }),
        available: 0,
        low_stock: true,
      },
    ],
    ...overrides,
  };
  return machine;
}
