import { useCallback, useEffect, useState } from "react";

import { type AdminReservation, api, errorMessage } from "../api";
import { clock, money, remaining } from "../format";
import { useLiveRefresh } from "../live";

const FILTERS = [
  ["", "All"],
  ["active", "Active"],
  ["paid", "Paid"],
  ["dispensed", "Dispensed"],
  ["expired", "Expired"],
  ["cancelled", "Cancelled"],
] as const;

const TONE: Record<AdminReservation["status"], string> = {
  held: "",
  paid: "info",
  dispensed: "good",
  expired: "warn",
  cancelled: "muted-badge",
};

/** All customers' reservations, newest first, updated live. */
export function Reservations() {
  const [filter, setFilter] = useState<string>("");
  const [rows, setRows] = useState<AdminReservation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  const load = useCallback(() => {
    api
      .reservations(filter)
      .then((next) => {
        setRows((previous) => {
          if (previous !== null) {
            const known = new Set(previous.map((r) => r.id));
            setFresh(new Set(next.filter((r) => !known.has(r.id)).map((r) => r.id)));
          }
          return next;
        });
        setError(null);
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [filter]);
  useEffect(load, [load]);
  useLiveRefresh(load, (notice) => notice.topic === "reservation");

  const cancel = (row: AdminReservation): void => {
    if (!window.confirm(`Cancel ${row.user_email}'s reservation of ${row.product_name}? Any payment is voided.`)) return;
    api.cancelReservation(row.id).then(load, (e: unknown) => setError(errorMessage(e)));
  };

  return (
    <div className="page">
      <h1>Reservations</h1>
      <div className="filters" role="tablist">
        {FILTERS.map(([value, label]) => (
          <button key={value} type="button" role="tab" aria-selected={filter === value}
            className={`chip${filter === value ? " active" : ""}`} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
      <section className="card">
        <table className="table" data-testid="reservations">
          <thead>
            <tr>
              <th>Created</th>
              <th>Customer</th>
              <th>Item</th>
              <th>Machine</th>
              <th>Status</th>
              <th className="num">Total</th>
              <th>Hold ends</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(rows ?? []).map((row) => (
              <tr key={row.id} className={fresh.has(row.id) ? "fresh" : undefined}>
                <td className="muted">{clock(row.created_at)}</td>
                <td>{row.user_email}</td>
                <td>
                  {row.quantity} × {row.product_name}
                </td>
                <td>
                  {row.machine_serial} · {row.slot_code}
                </td>
                <td>
                  <span className={`badge ${TONE[row.status]}`}>{row.status}</span>
                  {row.payment_status && <span className="muted small"> {row.payment_status}</span>}
                </td>
                <td className="num">{money(row.total_cents)}</td>
                <td className="muted">{row.status === "held" || row.status === "paid" ? remaining(row.expires_at) : "—"}</td>
                <td>
                  {(row.status === "held" || row.status === "paid") && (
                    <button type="button" className="button small" onClick={() => cancel(row)}>
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {rows !== null && rows.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  No reservations.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
