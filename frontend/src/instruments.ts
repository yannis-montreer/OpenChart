import type { Instrument } from "./api";

/** The instruments the chart offers: the selector, the watchlist, the live subscriptions.
 * A removed instrument is hidden (its data is kept) and stays in Settings, Removed. */
export const visibleInstruments = (all: Instrument[]): Instrument[] => all.filter((i) => i.enabled);

export const removedInstruments = (all: Instrument[]): Instrument[] => all.filter((i) => !i.enabled);

/** The instrument to open: the remembered one if it is still offered, else the first. */
export function chooseInstrument(all: Instrument[], remembered: string | undefined): Instrument | undefined {
  const offered = visibleInstruments(all);
  return offered.find((i) => i.instrument === remembered) ?? offered[0];
}

/** "about 12.4 MB": the stored size of a removed instrument. */
export function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

/** Under a removed instrument's name: what is kept, and what will not fire meanwhile. */
export function removedLine(i: Instrument): string {
  const s = i.stored;
  const parts = [i.display_name];
  if (s) {
    parts.push(`about ${sizeText(s.bytes)} stored`);
    if (s.active_alerts) parts.push(`${s.active_alerts} active alert${s.active_alerts === 1 ? "" : "s"} paused until it is added back`);
  }
  return parts.join(" · ");
}
