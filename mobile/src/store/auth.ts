// The signed-in customer (ADR-024). Tokens themselves live in services/tokens.

import { create } from "zustand";

import * as client from "../services/api";
import type { Me } from "../services/api";
import { useReservations } from "./reservations";

/** "unreachable": a session is stored but the server couldn't be asked about it. */
export type AuthStatus = "restoring" | "unreachable" | "signedOut" | "signedIn";

interface AuthState {
  status: AuthStatus;
  user: Me | null;
  /** On app start: turn a stored refresh token back into a session. */
  restore: () => Promise<void>;
  signIn: (email: string, password: string) => Promise<void>;
  register: (fullName: string, email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

export const useAuth = create<AuthState>()((set) => ({
  status: "restoring",
  user: null,

  restore: async () => {
    set({ status: "restoring" });
    const result = await client.refreshSession();
    if (result === "invalid") {
      set({ status: "signedOut", user: null });
      return;
    }
    if (result === "unavailable") {
      set({ status: "unreachable" });
      return;
    }
    try {
      set({ status: "signedIn", user: await client.getMe() });
    } catch {
      set({ status: "unreachable" });
    }
  },

  signIn: async (email, password) => {
    const user = await client.signIn(email, password);
    set({ status: "signedIn", user });
  },

  register: async (fullName, email, password) => {
    const user = await client.register(fullName, email, password);
    set({ status: "signedIn", user });
  },

  signOut: async () => {
    set({ status: "signedOut", user: null });
    useReservations.getState().clear(); // pickup codes leave memory with the session
    await client.signOut();
  },
}));

// The server rejected the refresh token (expired, revoked or reused).
client.onSessionEnded(() => {
  useAuth.setState({ status: "signedOut", user: null });
  useReservations.getState().clear();
});
