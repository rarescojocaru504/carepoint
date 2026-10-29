// Nearby machines and machine details (ADR-025). Public endpoints (ADR-011),
// read through the typed client.

import { create } from "zustand";

import { api, errorMessage, unwrap } from "../services/api";
import type { Schemas } from "../services/api";
import { DEMO_CENTER, startPoint } from "../services/location";
import type { Point, PointSource } from "../services/location";

export type MachineSummary = Schemas["MachineSummary"];
export type MachineDetail = Schemas["MachineDetail"];

/** The API's maximum: the whole demo city fits inside it. */
export const SEARCH_RADIUS_M = 20_000;

type Load<T> =
  | { status: "loading"; data: T | null }
  | { status: "ready"; data: T }
  | { status: "error"; data: T | null; error: string };

interface MachinesState {
  center: Point | null;
  source: PointSource | null;
  nearby: Load<MachineSummary[]>;
  details: Record<string, Load<MachineDetail>>;
  /** Find where we are, then load machines around it. */
  locate: () => Promise<void>;
  /** Search around a point; the map's "search the demo area" uses this. */
  loadNearby: (center: Point, source: PointSource) => Promise<void>;
  loadMachine: (id: string) => Promise<void>;
}

export const useMachines = create<MachinesState>()((set, get) => ({
  center: null,
  source: null,
  nearby: { status: "loading", data: null },
  details: {},

  locate: async () => {
    const { point, source } = await startPoint();
    await get().loadNearby(point, source);
  },

  loadNearby: async (center, source) => {
    set({ center, source, nearby: { status: "loading", data: get().nearby.data } });
    try {
      const machines = await unwrap(
        api.GET("/api/v1/machines/nearby", {
          params: {
            query: { lat: center.lat, lng: center.lng, radius_m: SEARCH_RADIUS_M, limit: 50 },
          },
        }),
      );
      set({ nearby: { status: "ready", data: machines } });
    } catch (error) {
      set({ nearby: { status: "error", data: get().nearby.data, error: errorMessage(error) } });
    }
  },

  loadMachine: async (id) => {
    const previous = get().details[id]?.data ?? null;
    set((s) => ({ details: { ...s.details, [id]: { status: "loading", data: previous } } }));
    try {
      const machine = await unwrap(
        api.GET("/api/v1/machines/{machine_id}", { params: { path: { machine_id: id } } }),
      );
      set((s) => ({ details: { ...s.details, [id]: { status: "ready", data: machine } } }));
    } catch (error) {
      const failed: Load<MachineDetail> = {
        status: "error",
        data: previous,
        error: errorMessage(error),
      };
      set((s) => ({ details: { ...s.details, [id]: failed } }));
    }
  },
}));

export { DEMO_CENTER };
