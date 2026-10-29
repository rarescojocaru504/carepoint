import { useState } from "react";

import { type Faults, type JamMode, type MachineSnapshot, restock, setFaults } from "../api";

/** Demo controls for presenters: inject faults, refill the machine. */
export function OperatorPanel({ machine }: { machine: MachineSnapshot }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const run = async (action: () => Promise<unknown>): Promise<void> => {
    setBusy(true);
    try {
      await action();
    } finally {
      setBusy(false);
    }
  };
  const update = (faults: Partial<Faults>): Promise<void> =>
    run(() => setFaults(machine.serial, faults));

  if (!open) {
    return (
      <button type="button" className="operator-toggle" onClick={() => setOpen(true)} aria-label="Operator panel">
        ⚙
      </button>
    );
  }
  const { faults } = machine;
  return (
    <aside className="operator" aria-label="Operator panel">
      <header>
        <h3>Operator · demo faults</h3>
        <button type="button" className="button" onClick={() => setOpen(false)}>
          Close
        </button>
      </header>
      <label className="check">
        <input type="checkbox" checked={faults.offline} disabled={busy}
          onChange={(e) => void update({ offline: e.target.checked })} />
        Offline (drop the connection)
      </label>
      <label>
        Jam
        <select value={faults.jam} disabled={busy}
          onChange={(e) => void update({ jam: e.target.value as JamMode })}>
          <option value="off">off</option>
          <option value="next">next dispense</option>
          <option value="always">always</option>
        </select>
      </label>
      <label className="check">
        <input type="checkbox" checked={faults.partial_next} disabled={busy}
          onChange={(e) => void update({ partial_next: e.target.checked })} />
        Partial dispense (next multi-item order)
      </label>
      <label className="check">
        <input type="checkbox" checked={faults.drop_next_result} disabled={busy}
          onChange={(e) => void update({ drop_next_result: e.target.checked })} />
        Lose connection right after the next dispense
      </label>
      <label>
        Seconds per item
        <input type="number" min={0} max={30} step={0.5} value={faults.seconds_per_unit} disabled={busy}
          onChange={(e) => void update({ seconds_per_unit: Number(e.target.value) })} />
      </label>
      <button type="button" className="button primary" disabled={busy}
        onClick={() => void run(() => restock(machine.serial))}>
        Restock all slots
      </button>
    </aside>
  );
}
