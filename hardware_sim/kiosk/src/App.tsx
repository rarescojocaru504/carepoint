import { useEffect, useState } from "react";

import { type MachineSnapshot, listMachines } from "./api";
import { Kiosk } from "./Kiosk";

/** `/kiosk/<serial>` is one machine's screen; `/` lists the simulated machines. */
export function App() {
  const match = /^\/kiosk\/([A-Z0-9-]{4,32})\/?$/.exec(window.location.pathname);
  return match?.[1] ? <Kiosk serial={match[1]} /> : <MachinePicker />;
}

function MachinePicker() {
  const [machines, setMachines] = useState<MachineSnapshot[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    listMachines().then(setMachines, () => setError(true));
  }, []);

  return (
    <div className="picker">
      <div className="brand large">
        Care<span>Point</span>
      </div>
      <h1>Simulated machines</h1>
      {error && <p className="notice">The simulator API is not reachable.</p>}
      {machines === null && !error && <p className="muted">Loading…</p>}
      <ul>
        {machines?.map((machine) => (
          <li key={machine.serial}>
            <a href={`/kiosk/${machine.serial}`}>
              <span>{machine.serial}</span>
              <span className={`status ${machine.online ? "online" : "offline"}`}>
                {machine.online ? "Online" : "Offline"}
              </span>
              <span className="muted">
                {machine.slots.reduce((sum, slot) => sum + slot.quantity, 0)} items in{" "}
                {machine.slots.filter((slot) => slot.product_sku !== null).length} slots
              </span>
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
