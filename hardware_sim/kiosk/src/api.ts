// Client for the machine's local API (hardware_sim/carepoint_sim/kiosk_api.py).

export interface SlotView {
  slot: string;
  product_sku: string | null;
  product_name: string | null;
  capacity: number;
  quantity: number;
}

export type JamMode = "off" | "next" | "always";

export interface Faults {
  offline: boolean;
  jam: JamMode;
  partial_next: boolean;
  seconds_per_unit: number;
  drop_next_result: boolean;
}

export interface Dispensing {
  slot: string;
  quantity: number;
  units_done: number;
}

export interface MachineSnapshot {
  serial: string;
  machine_id: string | null;
  online: boolean;
  faults: Faults;
  dispensing: Dispensing | null;
  slots: SlotView[];
}

export type Refusal =
  | "invalid_code"
  | "too_many_attempts"
  | "pickup_in_progress"
  | "machine_unavailable"
  | "offline"
  | "busy";

export type VerifyResult =
  | { ok: true; command_id: string }
  | { ok: false; refusal: Refusal; retry_after_seconds?: number | null };

export interface DispenseOutcome {
  outcome: "dispensed" | "failed";
  units_dispensed: number;
  error_code: string | null;
}

export type MachineEvent =
  | { event: "snapshot" | "online" | "offline" | "faults" | "restocked"; machine: MachineSnapshot }
  | { event: "verified"; machine: MachineSnapshot; result: VerifyResult }
  | { event: "dispensing"; machine: MachineSnapshot; slot: string; quantity: number }
  | {
      event: "unit_dispensed";
      machine: MachineSnapshot;
      slot: string;
      unit: number;
      quantity: number;
    }
  | { event: "dispensed"; machine: MachineSnapshot; slot: string; result: DispenseOutcome };

const base = (serial: string): string => `/api/machines/${encodeURIComponent(serial)}`;

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    throw new Error(`${init?.method ?? "GET"} ${url} failed: ${response.status}`);
  }
  return (await response.json()) as T;
}

export const listMachines = (): Promise<MachineSnapshot[]> => request("/api/machines");

export const verifyCode = (serial: string, credential: string): Promise<VerifyResult> =>
  request(`${base(serial)}/verify`, { method: "POST", body: JSON.stringify({ credential }) });

export const setFaults = (serial: string, faults: Partial<Faults>): Promise<MachineSnapshot> =>
  request(`${base(serial)}/faults`, { method: "PUT", body: JSON.stringify(faults) });

export const restock = (serial: string): Promise<MachineSnapshot> =>
  request(`${base(serial)}/restock`, { method: "POST" });

/** Live machine events (server-sent); the browser reconnects on its own. */
export function subscribe(serial: string, onEvent: (event: MachineEvent) => void): () => void {
  const source = new EventSource(`${base(serial)}/events`);
  source.onmessage = (message: MessageEvent<string>) => {
    onEvent(JSON.parse(message.data) as MachineEvent);
  };
  return () => source.close();
}
