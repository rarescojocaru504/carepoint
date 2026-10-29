// The server's clock, as seen from the phone (ADR-027).
//
// A hold ends at the server's `expires_at`. A phone whose clock is a minute
// off would otherwise show a countdown a minute wrong, so every API response's
// Date header updates an offset, and countdowns use server time. The header
// has one-second resolution, which is all a countdown needs.

let offsetMs = 0;

export function noteServerDate(header: string | null, receivedAt: number = Date.now()): void {
  if (header === null) return;
  const serverTime = Date.parse(header);
  if (!Number.isNaN(serverTime)) offsetMs = serverTime - receivedAt;
}

export function serverNow(): number {
  return Date.now() + offsetMs;
}

/** Whole seconds until `expiresAt`, never negative. */
export function secondsLeft(expiresAt: string, now: number = serverNow()): number {
  return Math.max(0, Math.floor((Date.parse(expiresAt) - now) / 1000));
}

/** 754 → "12:34". */
export function formatCountdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export function resetServerClock(): void {
  offsetMs = 0;
}
