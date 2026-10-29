import { useCallback, useEffect, useState } from "react";

import { type AdminMachine, type Kpis, api, errorMessage } from "../api";
import { KpiTiles } from "../components/KpiTiles";
import { MachineMap } from "../components/MachineMap";
import { ago } from "../format";
import { useLiveRefresh } from "../live";

export function StatusBadge({ machine }: { machine: AdminMachine }) {
  if (machine.status !== "active") return <span className="badge warn">{machine.status}</span>;
  return machine.online ? (
    <span className="badge good">online</span>
  ) : (
    <span className="badge bad">offline</span>
  );
}

export function Overview({ open }: { open: (machineId: string) => void }) {
  const [machines, setMachines] = useState<AdminMachine[] | null>(null);
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [, tick] = useState(0);

  const load = useCallback(() => {
    Promise.all([api.machines(), api.kpis()])
      .then(([m, k]) => {
        setMachines(m);
        setKpis(k);
        setError(null);
      })
      .catch((e: unknown) => setError(errorMessage(e)));
  }, []);
  useEffect(load, [load]);
  useLiveRefresh(load, () => true);
  // Keep "last seen" ages honest between notices.
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="page">
      <h1>Overview</h1>
      {error && <p className="error">{error}</p>}
      <KpiTiles kpis={kpis} />
      <div className="overview-grid">
        <section className="card">
          <h2>Map</h2>
          <MachineMap machines={machines ?? []} onSelect={open} />
        </section>
        <section className="card">
          <h2>Machines</h2>
          <table className="table" data-testid="machines">
            <thead>
              <tr>
                <th>Machine</th>
                <th>Status</th>
                <th>Last seen</th>
                <th className="num">Units</th>
                <th>Warnings</th>
              </tr>
            </thead>
            <tbody>
              {(machines ?? []).map((machine) => (
                <tr key={machine.id} className="clickable" onClick={() => open(machine.id)}
                  data-testid={`machine-${machine.serial_number}`}>
                  <td>
                    <div className="strong">{machine.serial_number}</div>
                    <div className="muted small">{machine.name}</div>
                  </td>
                  <td data-testid="status">
                    <StatusBadge machine={machine} />
                  </td>
                  <td className="muted">{ago(machine.last_seen_at)}</td>
                  <td className="num">{machine.units}</td>
                  <td>
                    {machine.sold_out_slots > 0 && <span className="badge bad">{machine.sold_out_slots} sold out</span>}{" "}
                    {machine.low_stock_slots > 0 && <span className="badge warn">{machine.low_stock_slots} low</span>}{" "}
                    {machine.active_reservations > 0 && (
                      <span className="badge">{machine.active_reservations} reserved</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}
