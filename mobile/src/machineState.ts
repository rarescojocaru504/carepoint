// How a machine is shown on the map and in lists.

import type { Schemas } from "./services/api";

export type MachineState = "available" | "soldOut" | "offline" | "maintenance";

type MachineLike = Pick<Schemas["MachineSummary"], "status" | "online"> & {
  units_available?: number;
};

export function machineState(machine: MachineLike): MachineState {
  if (machine.status !== "active") return "maintenance";
  if (!machine.online) return "offline";
  if (machine.units_available === 0) return "soldOut";
  return "available";
}

export const STATE_LABEL: Record<MachineState, string> = {
  available: "Open",
  soldOut: "Sold out",
  offline: "Offline",
  maintenance: "Maintenance",
};

export const STATE_COLOR: Record<MachineState, string> = {
  available: "#0f766e",
  soldOut: "#b45309",
  offline: "#b91c1c",
  maintenance: "#64748b",
};
