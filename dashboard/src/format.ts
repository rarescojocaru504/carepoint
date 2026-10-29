export function money(cents: number, currency = "RON"): string {
  return new Intl.NumberFormat("en-GB", { style: "currency", currency }).format(cents / 100);
}

export function ago(iso: string | null, now = Date.now()): string {
  if (iso === null) return "never";
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function duration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

export function percent(share: number | null): string {
  return share === null ? "—" : `${Math.round(share * 1000) / 10}%`;
}

export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** "in 12m" / "in 40s" until a future instant. */
export function remaining(iso: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((Date.parse(iso) - now) / 1000));
  return seconds < 60 ? `in ${seconds}s` : `in ${Math.floor(seconds / 60)}m`;
}
