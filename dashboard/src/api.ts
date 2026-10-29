// Typed client for the admin API (backend/app/api/v1/admin.py, ADR-021).
//
// Tokens: the short-lived access token lives in memory only; the rotating
// refresh token in sessionStorage, so a reload keeps the operator signed in
// but closing the tab signs them out.

export type Role = "customer" | "operator" | "admin";
export type MachineStatus = "active" | "maintenance" | "decommissioned";
export type ReservationStatus = "held" | "paid" | "dispensed" | "expired" | "cancelled";
export type PaymentStatus = "authorized" | "captured" | "voided" | "failed";

export interface Me {
  id: string;
  email: string;
  full_name: string;
  role: Role;
}

export interface AdminSlot {
  id: string;
  slot_code: string;
  product: { id: string; sku: string; name: string } | null;
  quantity: number;
  capacity: number;
  low_stock_threshold: number;
  held: number;
  available: number;
  low_stock: boolean;
  sold_out: boolean;
}

export interface AdminMachine {
  id: string;
  serial_number: string;
  name: string;
  address: string;
  location: { lat: number; lng: number };
  status: MachineStatus;
  online: boolean;
  last_seen_at: string | null;
  firmware_version: string | null;
  door_open: boolean | null;
  temperature_c: number | null;
  telemetry_at: string | null;
  units: number;
  low_stock_slots: number;
  sold_out_slots: number;
  active_reservations: number;
}

export interface MachineEvent {
  id: number;
  kind: "connected" | "disconnected" | "error" | "door_opened" | "door_closed";
  error_code: string | null;
  slot_code: string | null;
  message: string | null;
  created_at: string;
}

export interface AdminMachineDetail extends AdminMachine {
  slots: AdminSlot[];
  recent_events: MachineEvent[];
}

export interface StockMovement {
  id: number;
  slot_id: string;
  delta: number;
  quantity_after: number;
  reason: "initial" | "restock" | "dispense" | "adjustment" | "telemetry_correction";
  actor_email: string | null;
  reservation_id: string | null;
  note: string | null;
  created_at: string;
}

export interface StockChangeResult extends StockMovement {
  machine_notified: boolean;
}

export interface AdminReservation {
  id: string;
  status: ReservationStatus;
  user_email: string;
  machine_id: string;
  machine_serial: string;
  slot_code: string;
  product_sku: string;
  product_name: string;
  quantity: number;
  total_cents: number;
  payment_status: PaymentStatus | null;
  created_at: string;
  expires_at: string;
}

export interface Kpis {
  generated_at: string;
  reservations_24h: number;
  reservations_7d: number;
  pickup_rate_7d: number | null;
  expired_7d: number;
  avg_hold_to_pickup_seconds_7d: number | null;
  stock_outs: number;
  low_stock_slots: number;
  machines_online: number;
  machines_total: number;
  uptime_24h: number | null;
}

export type ProductCategory =
  | "hygiene"
  | "menstrual_care"
  | "sexual_health"
  | "first_aid"
  | "pharmacy"
  | "baby_care"
  | "essentials";

export interface Product {
  id: string;
  sku: string;
  name: string;
  description: string | null;
  category: ProductCategory;
  price_cents: number;
  currency: string;
  image_url: string | null;
  is_active: boolean;
}

export interface Notice {
  topic: "hello" | "machine" | "stock" | "reservation";
  machine_id: string | null;
  change: string;
  at: string;
}

// --- session ---------------------------------------------------------------

const REFRESH_KEY = "carepoint.refresh";
let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : `request failed (${status})`);
  }
}

/** The human message from an error, including FastAPI's {"detail": ...} shapes. */
export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : String(error);
  const detail = error.detail;
  if (typeof detail === "string") return detail;
  if (detail !== null && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  if (Array.isArray(detail) && detail.length > 0) {
    const first: unknown = detail[0];
    if (first !== null && typeof first === "object" && "msg" in first) {
      return String((first as { msg: unknown }).msg).replace(/^Value error, /, "");
    }
  }
  return error.message;
}

function storeTokens(pair: { access_token: string; refresh_token: string }): void {
  accessToken = pair.access_token;
  try {
    sessionStorage.setItem(REFRESH_KEY, pair.refresh_token);
  } catch {
    // storage unavailable (private mode): stay signed in for this page only
  }
}

function storedRefreshToken(): string | null {
  try {
    return sessionStorage.getItem(REFRESH_KEY);
  } catch {
    return null;
  }
}

export function signOut(): void {
  const refresh = storedRefreshToken();
  accessToken = null;
  try {
    sessionStorage.removeItem(REFRESH_KEY);
  } catch {
    // nothing stored
  }
  if (refresh !== null) {
    void fetch("/api/v1/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    });
  }
}

/** Swap the refresh token for a new pair; one refresh at a time. */
export function refreshSession(): Promise<boolean> {
  if (refreshing !== null) return refreshing;
  const refresh = storedRefreshToken();
  if (refresh === null) return Promise.resolve(false);
  refreshing = (async () => {
    try {
      const response = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refresh }),
      });
      if (!response.ok) {
        accessToken = null;
        sessionStorage.removeItem(REFRESH_KEY);
        return false;
      }
      storeTokens((await response.json()) as { access_token: string; refresh_token: string });
      return true;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

export function currentAccessToken(): string | null {
  return accessToken;
}

async function request<T>(method: string, path: string, body?: unknown, retried = false): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (accessToken !== null) headers.Authorization = `Bearer ${accessToken}`;
  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    body: body === undefined ? null : JSON.stringify(body),
  });
  if (response.status === 401 && !retried && (await refreshSession())) {
    return request<T>(method, path, body, true);
  }
  if (!response.ok) {
    let detail: unknown = response.statusText;
    try {
      detail = ((await response.json()) as { detail?: unknown }).detail ?? detail;
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

export async function signIn(email: string, password: string): Promise<Me> {
  const response = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, response.status === 429 ? "Too many attempts, wait a minute" : "Wrong email or password");
  }
  storeTokens((await response.json()) as { access_token: string; refresh_token: string });
  return me();
}

export const me = (): Promise<Me> => request("GET", "/me");

// --- admin API --------------------------------------------------------------

export const api = {
  machines: () => request<AdminMachine[]>("GET", "/admin/machines"),
  machine: (id: string) => request<AdminMachineDetail>("GET", `/admin/machines/${id}`),
  setMachineStatus: (id: string, status: MachineStatus) =>
    request<AdminMachineDetail>("PATCH", `/admin/machines/${id}`, { status }),
  stockHistory: (slotId: string) => request<StockMovement[]>("GET", `/admin/slots/${slotId}/movements?limit=20`),
  changeStock: (slotId: string, reason: "restock" | "adjustment", delta: number, note: string | null) =>
    request<StockChangeResult>("POST", "/admin/stock-movements", { slot_id: slotId, reason, delta, note }),
  reservations: (status: string, machineId?: string) => {
    const query = new URLSearchParams({ limit: "100" });
    if (status) query.set("status", status);
    if (machineId) query.set("machine_id", machineId);
    return request<AdminReservation[]>("GET", `/admin/reservations?${query.toString()}`);
  },
  cancelReservation: (id: string) => request<void>("POST", `/admin/reservations/${id}/cancel`),
  products: () => request<Product[]>("GET", "/admin/products"),
  createProduct: (product: Omit<Product, "id" | "is_active">) => request<Product>("POST", "/admin/products", product),
  updateProduct: (id: string, changes: Partial<Omit<Product, "id" | "sku" | "currency">>) =>
    request<Product>("PATCH", `/admin/products/${id}`, changes),
  deleteProduct: (id: string) => request<void>("DELETE", `/admin/products/${id}`),
  kpis: () => request<Kpis>("GET", "/admin/kpis"),
};

// --- live notices (ADR-022) ---------------------------------------------------

/** Read the notice stream with fetch() (EventSource cannot send the token) and
 *  reconnect on any end, refreshing the token when it has expired. */
export function subscribeNotices(onNotice: (notice: Notice) => void, onState: (live: boolean) => void): () => void {
  const controller = new AbortController();
  let stopped = false;

  const run = async (): Promise<void> => {
    let delay = 1000;
    while (!stopped) {
      try {
        let response = await fetch("/api/v1/admin/events", {
          headers: accessToken === null ? {} : { Authorization: `Bearer ${accessToken}` },
          signal: controller.signal,
        });
        if (response.status === 401 && (await refreshSession())) {
          response = await fetch("/api/v1/admin/events", {
            headers: { Authorization: `Bearer ${accessToken ?? ""}` },
            signal: controller.signal,
          });
        }
        if (!response.ok || response.body === null) throw new Error(`stream ${response.status}`);
        onState(true);
        delay = 1000;
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += value;
          let end: number;
          while ((end = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, end);
            buffer = buffer.slice(end + 2);
            for (const line of frame.split("\n")) {
              if (line.startsWith("data: ")) onNotice(JSON.parse(line.slice(6)) as Notice);
            }
          }
        }
      } catch {
        if (stopped) return;
      }
      onState(false);
      // The stream ends at token expiry: refresh before reconnecting.
      await refreshSession();
      await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 15000);
    }
  };
  void run();
  return () => {
    stopped = true;
    controller.abort();
  };
}
