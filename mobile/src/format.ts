/** 1250, "RON" → "12.50 RON". Prices are integer cents (ADR-006). */
export function formatPrice(cents: number, currency: string): string {
  return `${(cents / 100).toFixed(2)} ${currency}`;
}

/** 350 → "350 m", 1234 → "1.2 km". */
export function formatDistance(metres: number): string {
  if (metres < 1000) return `${Math.round(metres / 10) * 10} m`;
  return `${(metres / 1000).toFixed(1)} km`;
}
