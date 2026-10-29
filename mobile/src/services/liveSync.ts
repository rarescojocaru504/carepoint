// What a live notice makes the app re-read (ADR-028). Notices carry no data
// (ADR-026): each says what changed, and the stores re-read it through the API.
// Re-reads are debounced per target, so a burst of notices (a dispense sends
// several) costs one request.

import { useMachines } from "../store/machines";
import { isActive, useReservations } from "../store/reservations";
import type { Notice } from "./live";

export const DEBOUNCE_MS = 300;

const timers = new Map<string, ReturnType<typeof setTimeout>>();

function soon(key: string, action: () => Promise<unknown>): void {
  clearTimeout(timers.get(key));
  timers.set(
    key,
    setTimeout(() => {
      timers.delete(key);
      action().catch(() => undefined); // a failed re-read leaves the last data on screen
    }, DEBOUNCE_MS),
  );
}

export function cancelPendingSync(): void {
  timers.forEach((timer) => clearTimeout(timer));
  timers.clear();
}

function refreshNearby(): void {
  const { center, source, loadNearby } = useMachines.getState();
  if (center !== null && source !== null) soon("nearby", () => loadNearby(center, source));
}

function refreshMachine(machineId: string | null | undefined): void {
  const { details, loadMachine } = useMachines.getState();
  const ids = machineId ? [machineId] : Object.keys(details);
  ids
    .filter((id) => details[id] !== undefined)
    .forEach((id) => soon(`machine:${id}`, () => loadMachine(id)));
}

function refreshReservation(reservationId: string): void {
  const { load } = useReservations.getState();
  soon(`reservation:${reservationId}`, () => load(reservationId));
}

function refreshLists(): void {
  const { mine, orders, loadMine, loadOrders } = useReservations.getState();
  if (mine.status !== "idle") soon("mine", loadMine);
  if (orders.status !== "idle") soon("orders", loadOrders);
}

/** Re-read everything on screen: after (re)connecting, notices may have been missed. */
export function refreshAll(): void {
  refreshNearby();
  refreshMachine(null);
  Object.values(useReservations.getState().byId)
    .filter(isActive)
    .forEach((reservation) => refreshReservation(reservation.id));
  refreshLists();
}

export function applyNotice(notice: Notice): void {
  switch (notice.topic) {
    case "hello":
      refreshAll();
      return;
    case "stock":
    case "machine":
      refreshNearby();
      refreshMachine(notice.machine_id);
      return;
    case "reservation":
      if (notice.reservation_id) refreshReservation(notice.reservation_id);
      refreshLists();
      return;
  }
}
