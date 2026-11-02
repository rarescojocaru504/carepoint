import L from "leaflet";
import { useEffect, useRef } from "react";

import type { AdminMachine } from "../api";

const CLUJ_NAPOCA: L.LatLngTuple = [46.77, 23.59]; // the demo centre (ADR-033)

function colour(machine: AdminMachine): string {
  if (machine.status !== "active") return "#d97706";
  return machine.online ? "#059669" : "#dc2626";
}

/** OpenStreetMap with one marker per machine (coloured by state). Circle
 *  markers need no image assets, which bundlers tend to break. */
export function MachineMap({
  machines,
  onSelect,
}: {
  machines: AdminMachine[];
  onSelect: (id: string) => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const markers = useRef<L.LayerGroup | null>(null);
  const fitted = useRef(false);

  useEffect(() => {
    if (element.current === null) return undefined;
    const instance = L.map(element.current, { scrollWheelZoom: false }).setView(CLUJ_NAPOCA, 13);
    // The single host OpenStreetMap asks clients to use (no a/b/c subdomains).
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "© OpenStreetMap contributors",
    }).addTo(instance);
    markers.current = L.layerGroup().addTo(instance);
    map.current = instance;
    return () => {
      // Leaving mid-animation would make Leaflet touch removed panes.
      instance.stop();
      instance.remove();
      map.current = null;
    };
  }, []);

  useEffect(() => {
    const layer = markers.current;
    if (layer === null || map.current === null) return;
    layer.clearLayers();
    for (const machine of machines) {
      L.circleMarker([machine.location.lat, machine.location.lng], {
        radius: 10,
        color: "#ffffff",
        weight: 2,
        fillColor: colour(machine),
        fillOpacity: 0.95,
      })
        .bindTooltip(
          `${machine.serial_number} · ${machine.name}<br>${machine.online ? "online" : "offline"}, ${machine.units} units`,
        )
        .on("click", () => onSelect(machine.id))
        .addTo(layer);
    }
    if (!fitted.current && machines.length > 0) {
      map.current.fitBounds(
        L.latLngBounds(machines.map((m) => [m.location.lat, m.location.lng] as L.LatLngTuple)),
        { padding: [40, 40], animate: false },
      );
      fitted.current = true;
    }
  }, [machines, onSelect]);

  return <div ref={element} className="map" aria-label="Machine map" />;
}
