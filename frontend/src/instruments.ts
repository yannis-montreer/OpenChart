import type { DeletedInstrument, DeletePreview, Instrument } from "./api";

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

// Delete data… on a removed instrument (docs/decisions-instrument-delete.md)

const n = (value: number): string => value.toLocaleString("en-US");
const count = (value: number, one: string, many = `${one}s`): string => `${n(value)} ${value === 1 ? one : many}`;

/** "1 drawing and 2 alerts", leaving out a kind there is none of. */
export function drawingsAndAlerts(drawings: number, alerts: number): string {
  return [drawings ? count(drawings, "drawing") : "", alerts ? count(alerts, "alert") : ""].filter(Boolean).join(" and ");
}

/** What the delete removes, one line each; the drawings and alerts are kept unless `both`. */
export function deleteLines(p: DeletePreview, both: boolean): string[] {
  const s = p.stored;
  const lines = [`${n(s.m1 ?? 0)} minute and ${n(s.h1 ?? 0)} hourly candles, ${count(s.cached ?? 0, "cached bar")}`];
  if (s.closures) lines.push(count(s.closures, "market closure"));
  if (s.tick_files) lines.push(`${count(s.tick_files, "tick file")} (${sizeText(s.tick_bytes)})`);
  if (s.indicators) lines.push(`${count(s.indicators, "indicator")} on its chart`);
  const named = drawingsAndAlerts(s.drawings, s.alerts);
  if (named) lines.push(both ? `${named}${s.alerts ? ", and their alert log" : ""}` : `Kept unless the box below is ticked: ${named}`);
  return lines;
}

export const deleteSize = (p: DeletePreview): string => `about ${sizeText(p.stored.bytes)}`;

/** The Delete button is live only on the exact name, and with the box ticked when there are drawings or alerts. */
export function canDelete(p: DeletePreview, typed: string, both: boolean): boolean {
  return typed === p.instrument && (both || !p.needs_drawings_and_alerts);
}

export function deletedText(name: string, r: DeletedInstrument): string {
  const named = drawingsAndAlerts(r.drawings, r.alerts);
  const parts = [named ? `${name} deleted, with ${named}.` : `${name} deleted.`];
  if (r.tick_error) parts.push(`Some tick files could not be deleted (${r.tick_error}).`);
  parts.push(
    r.vacuum
      ? `The database went from ${sizeText(r.vacuum.before)} to ${sizeText(r.vacuum.after)}.`
      : `The database was not compacted (${r.vacuum_error ?? "skipped"}); SQLite reuses the space.`,
  );
  return parts.join(" ");
}
