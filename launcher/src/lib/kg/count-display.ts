// Count display helpers for the KG collections view and the /codegraph
// page (v0.2.105 lazy-load counts).
//
// A live object count runs only for the acting project's own + shared +
// granted collections (and the explicit "Refresh counts" action); every
// other collection carries a cached number, or none at all. These helpers
// render that third state ("—") and the age of a cached number, in ONE
// place so both views word it identically.

/** "—" when the collection was never counted (or its count was
 *  invalidated), the number otherwise. */
export function formatCount(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : String(n);
}

/** Human age of a cached count: "" when it has no timestamp. */
export function formatCountAge(
  countedAt: number | null | undefined,
  now: number = Date.now(),
): string {
  if (countedAt === null || countedAt === undefined) return '';
  const secs = Math.max(0, Math.floor((now - countedAt) / 1000));
  if (secs < 60) return 'counted just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `counted ${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `counted ${hours} h ago`;
  const days = Math.floor(hours / 24);
  return `counted ${days} d ago`;
}

/** Sum of per-class counts for one codegraph card; null when any class
 *  was never counted (a partial sum would read as a real total). */
export function sumCounts(
  parts: Array<number | null | undefined>,
): number | null {
  if (parts.some((p) => p === null || p === undefined)) return null;
  return parts.reduce((acc: number, p) => acc + (p as number), 0);
}
