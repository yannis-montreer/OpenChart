// What is kept and for how long (storage steps 2, docs/decisions-retention.md): the labels
// and lines of Settings, Data (raw ticks) and Settings, Instruments (minute history).

export const TICK_RETENTION = ["7d", "30d", "90d", "1y", "forever", "off"] as const;
export type TickRetention = (typeof TICK_RETENTION)[number];

export const TICK_LABELS: Record<TickRetention, string> = {
  "7d": "7 days",
  "30d": "30 days",
  "90d": "90 days",
  "1y": "1 year",
  forever: "Forever",
  off: "Don't store ticks",
};

export const M1_DEPTHS = ["3m", "1y", "2y"] as const;
export type M1Depth = (typeof M1_DEPTHS)[number];

export const M1_LABELS: Record<M1Depth, string> = {
  "3m": "3 months",
  "1y": "1 year",
  "2y": "2 years",
};

export const DEFAULT_TICK_RETENTION: TickRetention = "30d";
export const DEFAULT_M1_DEPTH: M1Depth = "2y";

export const TICK_HELP =
  "Raw ticks are needed for second charts (1s to 30s) of past periods and later for replays. " +
  "About 17 to 21 MB per instrument a month. Older days are deleted just after midnight UTC.";

export const M1_HELP =
  "How far back charts from 1 minute to 30 minutes can be scrolled. Charts of 1 hour and longer " +
  "always go back as far as OANDA has history (for EUR/USD to 2002), whatever you choose here. " +
  "One year takes about 22 MB.";

export function tickLabel(choice: string): string {
  const label = TICK_LABELS[choice as TickRetention] ?? choice;
  return choice === DEFAULT_TICK_RETENTION ? `${label} (default)` : label;
}

export function depthLabel(choice: string): string {
  const label = M1_LABELS[choice as M1Depth] ?? choice;
  return choice === DEFAULT_M1_DEPTH ? `${label} (default)` : label;
}

/** "14 Oct 2025" for a UTC epoch second. */
export function utcDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** "Minute history: 1 year (back to 14 Oct 2025)". */
export function depthLine(depth: string, from: number): string {
  return `Minute history: ${M1_LABELS[depth as M1Depth] ?? depth} (back to ${utcDate(from)})`;
}

/** What saving a new depth does, for the result line. */
export function depthSaved(instrument: string, depth: string, downloading: boolean): string {
  const label = M1_LABELS[depth as M1Depth] ?? depth;
  return downloading
    ? `${instrument}: minute history ${label}. The missing minutes download in the background, newest first.`
    : `${instrument}: minute history ${label}. Older minutes are deleted just after midnight UTC.`;
}
