import type { Kpis } from "../api";
import { duration, percent } from "../format";

interface Tile {
  label: string;
  value: string;
  hint: string;
  tone?: "good" | "warn" | "bad" | undefined;
}

function tiles(k: Kpis): Tile[] {
  const allOnline = k.machines_online === k.machines_total;
  return [
    { label: "Reservations today", value: String(k.reservations_24h), hint: `${k.reservations_7d} in 7 days` },
    {
      label: "Pickup rate",
      value: percent(k.pickup_rate_7d),
      hint: "dispensed of finished, 7 days",
      tone: k.pickup_rate_7d === null ? undefined : k.pickup_rate_7d >= 0.8 ? "good" : "warn",
    },
    { label: "Expired holds", value: String(k.expired_7d), hint: "7 days", tone: k.expired_7d > 0 ? "warn" : undefined },
    { label: "Hold to pickup", value: duration(k.avg_hold_to_pickup_seconds_7d), hint: "average, 7 days" },
    {
      label: "Stock-outs",
      value: String(k.stock_outs),
      hint: `${k.low_stock_slots} slots running low`,
      tone: k.stock_outs > 0 ? "bad" : k.low_stock_slots > 0 ? "warn" : "good",
    },
    {
      label: "Machines online",
      value: `${k.machines_online}/${k.machines_total}`,
      hint: `uptime ${percent(k.uptime_24h)} (24 h)`,
      tone: allOnline ? "good" : "bad",
    },
  ];
}

export function KpiTiles({ kpis }: { kpis: Kpis | null }) {
  if (kpis === null) return <div className="tiles muted">Loading KPIs…</div>;
  return (
    <div className="tiles">
      {tiles(kpis).map((tile) => (
        <div key={tile.label} className={`tile${tile.tone ? ` ${tile.tone}` : ""}`} data-testid={`kpi-${tile.label}`}>
          <div className="tile-label">{tile.label}</div>
          <div className="tile-value">{tile.value}</div>
          <div className="tile-hint">{tile.hint}</div>
        </div>
      ))}
    </div>
  );
}
