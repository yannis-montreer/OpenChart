/**
 * A chart's indicators as the server keeps them (CI-3), as data with no DOM: what an instance
 * becomes in the browser, what goes back, what a message from the server asks for, and the
 * words of the notes. `chartSync.ts` does the calls; `indicators.ts` draws.
 */

import type { ChartIndicator, ChartIndicatorIn, ChartIndicators, IndicatorEntry, IndicatorKind, SkippedEntry } from "../api";

/**
 * Why an instance is a placeholder in the legend instead of a drawn indicator. The server
 * keeps it in the list either way, with its values as they were.
 */
export type GhostState = "missing" | "error" | "quarantined" | "not_run" | "shadowed";

export interface Instance {
  entry: IndicatorEntry;
  state: GhostState | null;
  /** Inputs whose stored values no longer fitted the definition: they show their defaults. */
  reset: string[];
}

export function fromServer(c: ChartIndicator): Instance {
  const entry: IndicatorEntry = {
    uid: c.uid,
    kind: c.kind,
    id: c.id,
    params: structuredClone(c.params),
    visible: c.visible,
    price_labels: c.price_labels,
    legend_values: c.legend_values,
  };
  return { entry, state: c.status === "ok" ? null : c.status, reset: [...c.reset] };
}

/** The whole list, in legend order. `uid` and `kind` an entry does not have yet are left for the server to fill. */
export function toServer(entries: IndicatorEntry[]): ChartIndicatorIn[] {
  return entries.map((e) => ({
    ...(e.uid !== undefined ? { uid: e.uid } : {}),
    ...(e.kind !== undefined ? { kind: e.kind } : {}),
    id: e.id,
    params: structuredClone(e.params),
    visible: e.visible,
    price_labels: e.price_labels,
    legend_values: e.legend_values,
  }));
}

/** A uid the server accepts (letters, digits, `_` and `-`, at most 40). */
export function newUid(): string {
  const bytes = new Uint8Array(9);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(36).padStart(2, "0")).join("");
}

/** What the legend says in place of an indicator that cannot be drawn. */
export function legendState(state: GhostState): { label: string; tip: string } {
  switch (state) {
    case "missing":
      return { label: "Not found", tip: "There is no indicator with this id on the server (its file may have been moved or deleted). It stays in the list with its settings; remove it if you do not need it." };
    case "error":
      return { label: "File error", tip: "The file does not load. Fix it and the indicator comes back with its settings." };
    case "quarantined":
      return { label: "Switched off", tip: "Switched off after repeated failures. Edit the file to try again." };
    case "not_run":
      return { label: "Worker off", tip: "The indicator worker is not available, so your own files cannot run. The indicator stays in the list." };
    case "shadowed":
      return { label: "Built-in has this id", tip: "A file of yours has the same id as a built-in indicator, so this one cannot be drawn yet." };
  }
}

/** The state of an instance for the definition the catalog holds under its id (undefined: none). */
export function ghostOf(e: { kind?: IndicatorKind; id: string }, def: { builtin: boolean } | undefined): GhostState | null {
  if (!def) return "missing";
  if (e.kind !== undefined && (e.kind === "builtin") !== def.builtin) return "shadowed";
  return null;
}

/** Entries of the browser's old list that look like an instance (the server checks the rest). */
export function legacyEntries(raw: unknown): unknown[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((e) => typeof e === "object" && e !== null && typeof (e as { id?: unknown }).id === "string");
}

/** The old list goes to a chart whose list was never set, and only when it holds something. */
export function askToImport(legacy: unknown[], chart: ChartIndicators): boolean {
  return legacy.length > 0 && chart.revision === 0;
}

/** The old list can go once every enabled chart has a list of its own. */
export function legacyDone(revisions: number[]): boolean {
  return revisions.length > 0 && revisions.every((r) => r > 0);
}

/** What a `chart_indicators_changed` message asks for. A newer list wins over any edit not sent yet. */
export function wsAction(
  msg: ChartIndicators,
  current: { source: string; instrument: string } | null,
  known: number,
): "take" | "note" | "ignore" {
  if (!current || msg.source !== current.source || msg.instrument !== current.instrument) return "note";
  return msg.revision > known ? "take" : "ignore";
}

/** "EMA: length" per indicator that had inputs reset. */
export function resetItems(chart: ChartIndicators, titleOf: (id: string) => string): string[] {
  return chart.indicators.filter((i) => i.reset.length).map((i) => `${titleOf(i.id)}: ${i.reset.join(", ")}`);
}

export type SyncEvent =
  | { type: "saved" }
  | { type: "imported"; instrument: string; count: number; skipped: SkippedEntry[] }
  | { type: "conflict"; instrument: string }
  | { type: "remote"; instrument: string }
  | { type: "reset"; instrument: string; items: string[] }
  | { type: "applied"; instrument: string; layout: string; skipped: SkippedEntry[] }
  | { type: "dropped"; instrument: string; ids: string[] }
  | { type: "failed"; instrument: string; message: string }
  | { type: "unloaded"; instrument: string };

function left(skipped: SkippedEntry[]): string {
  return skipped.map((s) => (s.id ? `${s.id} (${s.reason})` : s.reason)).join(", ");
}

/** The toast for an event that needs one. */
export function syncToast(e: Exclude<SyncEvent, { type: "saved" }>): { title: string; body: string } {
  switch (e.type) {
    case "imported": {
      const n = e.count === 1 ? "The 1 indicator this browser had is" : `The ${e.count} indicators this browser had are`;
      const tail = e.skipped.length ? ` Left out: ${left(e.skipped)}.` : "";
      return {
        title: `Indicators copied to ${e.instrument}`,
        body: `${n} now ${e.instrument}'s own list. They used to be shared by every chart; each chart keeps its own list on the server now.${tail}`,
      };
    }
    case "conflict":
      return {
        title: "Indicators changed elsewhere",
        body: `${e.instrument}'s list was changed from another browser. The newer list is shown; your last change was not saved.`,
      };
    case "remote":
      return { title: "Indicators updated", body: `${e.instrument}'s list was changed from another browser.` };
    case "reset":
      return {
        title: "Settings reset",
        body: `The definition changed, so these went back to their defaults: ${e.items.join("; ")}. Your next change on this chart keeps them.`,
      };
    case "applied":
      return { title: "Layout loaded with changes", body: `Left out of "${e.layout}" on ${e.instrument}: ${left(e.skipped)}.` };
    case "dropped":
      return {
        title: "Removed from the list",
        body: `${e.ids.join(", ")} ${e.ids.length === 1 ? "no longer exists" : "no longer exist"} on the server, so ${e.ids.length === 1 ? "it was" : "they were"} taken off ${e.instrument}'s list.`,
      };
    case "failed":
      return { title: "Indicators not saved", body: `Could not save ${e.instrument}'s indicators: ${e.message}` };
    case "unloaded":
      return {
        title: "Indicators not saved",
        body: `${e.instrument}'s indicators could not be loaded, so changes are not kept. Reload the page once the server answers.`,
      };
  }
}
