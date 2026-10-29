import { type FormEvent, useCallback, useEffect, useState } from "react";

import {
  type AdminMachineDetail,
  type AdminSlot,
  type MachineStatus,
  type StockMovement,
  api,
  errorMessage,
} from "../api";
import { ago, clock } from "../format";
import { useLiveRefresh } from "../live";
import { StatusBadge } from "./Overview";

const REASONS: Record<StockMovement["reason"], string> = {
  initial: "Initial",
  restock: "Restock",
  dispense: "Dispensed",
  adjustment: "Adjustment",
  telemetry_correction: "Machine count",
};

export function MachineView({ machineId, back }: { machineId: string; back: () => void }) {
  const [machine, setMachine] = useState<AdminMachineDetail | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .machine(machineId)
      .then((m) => {
        setMachine(m);
        setError(null);
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, [machineId]);
  useEffect(load, [load]);
  useLiveRefresh(load, (notice) => notice.machine_id === null || notice.machine_id === machineId);

  if (machine === null) return <div className="page">{error ?? "Loading…"}</div>;
  const slot = machine.slots.find((s) => s.id === selected) ?? null;

  const setStatus = (status: MachineStatus): void => {
    api.setMachineStatus(machine.id, status).then(setMachine, (e: unknown) => setError(errorMessage(e)));
  };

  return (
    <div className="page">
      <button type="button" className="link" onClick={back}>
        ← All machines
      </button>
      <header className="machine-header">
        <div>
          <h1>
            {machine.serial_number} <StatusBadge machine={machine} />
          </h1>
          <p className="muted">
            {machine.name} · {machine.address}
          </p>
        </div>
        <dl className="facts">
          <div>
            <dt>Last seen</dt>
            <dd>{ago(machine.last_seen_at)}</dd>
          </div>
          <div>
            <dt>Temperature</dt>
            <dd>{machine.temperature_c === null ? "—" : `${machine.temperature_c.toFixed(1)} °C`}</dd>
          </div>
          <div>
            <dt>Door</dt>
            <dd>{machine.door_open === null ? "—" : machine.door_open ? "open" : "closed"}</dd>
          </div>
          <div>
            <dt>Firmware</dt>
            <dd>{machine.firmware_version ?? "—"}</dd>
          </div>
        </dl>
        <div className="actions">
          {machine.status === "active" ? (
            <button type="button" className="button" onClick={() => setStatus("maintenance")}>
              Put in maintenance
            </button>
          ) : (
            <button type="button" className="button" onClick={() => setStatus("active")}>
              Back in service
            </button>
          )}
        </div>
      </header>
      {error && <p className="error">{error}</p>}

      <div className="machine-grid">
        <section className="card">
          <h2>Slots</h2>
          <div className="slots" data-testid="slots">
            {machine.slots.map((s) => (
              <button
                type="button"
                key={s.id}
                className={["slot", s.sold_out && "bad", s.low_stock && "warn", selected === s.id && "selected"]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => setSelected(s.id)}
                data-testid={`slot-${s.slot_code}`}
              >
                <span className="slot-code">{s.slot_code}</span>
                <span className="slot-name">{s.product?.name ?? "No product"}</span>
                <span className="slot-qty">
                  <b data-testid={`qty-${s.slot_code}`}>{s.quantity}</b>/{s.capacity}
                </span>
                <span className="slot-meta">
                  {s.held > 0 ? `${s.held} held · ${s.available} free` : s.sold_out ? "sold out" : s.low_stock ? "low" : " "}
                </span>
              </button>
            ))}
          </div>
        </section>
        <section className="card">
          {slot === null ? (
            <p className="muted">Select a slot to restock it or see its history.</p>
          ) : (
            <SlotPanel key={slot.id} slot={slot} online={machine.online} />
          )}
        </section>
      </div>

      <section className="card">
        <h2>Recent events</h2>
        <ul className="events">
          {machine.recent_events.map((event) => (
            <li key={event.id}>
              <span className="muted">{clock(event.created_at)}</span>{" "}
              <span className={`badge${event.kind === "error" ? " bad" : ""}`}>{event.kind.replace("_", " ")}</span>{" "}
              {event.error_code ?? ""} {event.slot_code ? `slot ${event.slot_code}` : ""} {event.message ?? ""}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

function SlotPanel({ slot, online }: { slot: AdminSlot; online: boolean }) {
  const [history, setHistory] = useState<StockMovement[]>([]);
  const [reason, setReason] = useState<"restock" | "adjustment">("restock");
  const [amount, setAmount] = useState(Math.max(slot.capacity - slot.quantity, 1));
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const loadHistory = useCallback(() => {
    api.stockHistory(slot.id).then(setHistory, () => setHistory([]));
  }, [slot.id]);
  useEffect(loadHistory, [loadHistory, slot.quantity]);

  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.changeStock(slot.id, reason, amount, note.trim() || null);
      setMessage({
        ok: true,
        text: `Recorded: now ${result.quantity_after}/${slot.capacity}.${result.machine_notified ? " The machine was told." : ""}`,
      });
      setNote("");
      loadHistory();
    } catch (e: unknown) {
      setMessage({ ok: false, text: errorMessage(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h2>
        Slot {slot.slot_code} · {slot.product?.name ?? "no product"}
      </h2>
      {slot.product !== null && (
        <form className="stock-form" onSubmit={(e) => void submit(e)} aria-label="Record stock change">
          <label>
            Change
            <select value={reason} onChange={(e) => setReason(e.target.value as "restock" | "adjustment")}>
              <option value="restock">Restock (units added)</option>
              <option value="adjustment">Adjustment (counted)</option>
            </select>
          </label>
          <label>
            Units
            <input
              type="number"
              value={amount}
              min={reason === "restock" ? 1 : -slot.quantity}
              max={slot.capacity - slot.quantity}
              onChange={(e) => setAmount(Number(e.target.value))}
              aria-label="Units"
            />
          </label>
          <label className="wide">
            Note{reason === "adjustment" ? " (required)" : ""}
            <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={500} placeholder="e.g. weekly round" />
          </label>
          <button type="submit" className="button primary" disabled={busy || !online}>
            {online ? "Record" : "Machine offline"}
          </button>
        </form>
      )}
      {message && <p className={message.ok ? "success" : "error"}>{message.text}</p>}
      <h3>History</h3>
      <table className="table compact" data-testid="history">
        <tbody>
          {history.map((movement) => (
            <tr key={movement.id}>
              <td className="muted">{clock(movement.created_at)}</td>
              <td>{REASONS[movement.reason]}</td>
              <td className={`num ${movement.delta > 0 ? "plus" : "minus"}`}>
                {movement.delta > 0 ? `+${movement.delta}` : movement.delta}
              </td>
              <td className="num">{movement.quantity_after}</td>
              <td className="muted small">{movement.actor_email ?? movement.note ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
