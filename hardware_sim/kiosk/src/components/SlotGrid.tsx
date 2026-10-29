import type { SlotView } from "../api";

interface SlotGridProps {
  slots: SlotView[];
  active: string | null;
}

/** The machine's glass front: every slot with what is physically inside. */
export function SlotGrid({ slots, active }: SlotGridProps) {
  return (
    <div className="slot-grid">
      {slots.map((slot) => {
        const empty = slot.product_sku === null;
        const soldOut = !empty && slot.quantity === 0;
        const classes = ["slot", empty && "unassigned", soldOut && "sold-out", active === slot.slot && "active"];
        return (
          <div key={slot.slot} className={classes.filter(Boolean).join(" ")} data-testid={`slot-${slot.slot}`}>
            <div className="slot-code">{slot.slot}</div>
            <div className="slot-name">{empty ? "—" : slot.product_name}</div>
            <div className="slot-stock" aria-label={`${slot.slot} stock`}>
              <span className="slot-quantity" data-testid={`qty-${slot.slot}`}>
                {slot.quantity}
              </span>
              <span className="slot-capacity">/{slot.capacity}</span>
            </div>
            <div className="slot-bar" aria-hidden="true">
              <div style={{ width: `${(slot.quantity / slot.capacity) * 100}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** The pickup tray: one parcel drops in per unit released. */
export function PickupTray({ units }: { units: number }) {
  return (
    <div className="tray" aria-label={`${units} item(s) in the tray`}>
      {Array.from({ length: units }, (_, i) => (
        <div key={i} className="parcel" style={{ left: `${18 + i * 18}%` }} />
      ))}
      <span className="tray-label">Pickup tray</span>
    </div>
  );
}
