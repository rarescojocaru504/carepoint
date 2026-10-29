// The live connection's state for the UI, and starting and stopping it (ADR-028).

import { create } from "zustand";

import { LiveConnection } from "../services/live";
import type { LiveStatus } from "../services/live";
import { applyNotice, cancelPendingSync } from "../services/liveSync";

interface LiveState {
  status: LiveStatus;
  start: () => void;
  stop: () => void;
}

export const useLive = create<LiveState>()((set) => {
  const connection = new LiveConnection({
    onNotice: applyNotice,
    onStatus: (status) => set({ status }),
  });
  return {
    status: "idle",
    start: () => connection.start(),
    stop: () => {
      connection.stop();
      cancelPendingSync();
    },
  };
});
