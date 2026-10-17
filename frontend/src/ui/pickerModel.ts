/**
 * What the Indicators list shows, as data (no DOM): the two sections, the search, the state
 * of each of your files and who may delete it. `indicatorDialogs.ts` only draws this.
 *
 * Built-in and your own definitions are listed apart (CI-3). A file of yours is in one of
 * four states the server decides (`CustomStatus`); the list says why it cannot be added
 * instead of leaving it out, so a file that does not appear is never a mystery.
 */

import type { CustomEntry, IndicatorCatalog, IndicatorDef, IndicatorKind } from "../api";

/** `shadowed`: a file of yours with a built-in's id. Rows carry the id alone until the chart keeps each instance's kind. */
export type RowState = "ok" | "error" | "quarantined" | "not_run" | "shadowed";

/** May this be done here? When not, `reason` is what the tooltip says. */
export interface Gate {
  allowed: boolean;
  reason: string | null;
}

export interface PickerRow {
  key: string;
  kind: IndicatorKind;
  id: string;
  title: string;
  name: string;
  where: "on the chart" | "own pane" | null;
  file: string | null;
  state: RowState;
  /** The definition to add; null for a row that cannot be added. */
  def: IndicatorDef | null;
  addable: boolean;
  /** Why it cannot be added (or what went wrong with the file). */
  why: string | null;
  line: number | null;
  /** Deleting a definition; null for a built-in one (it is not a file of yours). */
  canDelete: Gate | null;
}

export interface PickerSection {
  kind: IndicatorKind;
  heading: string;
  rows: PickerRow[];
  /** Shown instead of rows when there are none and nothing is searched for. */
  hint: string | null;
  /** Lines that apply to the whole section (weaker isolation, a built-in that did not load). */
  notices: string[];
}

export interface PickerModel {
  sections: PickerSection[];
  /** Shown when a search matches nothing at all. */
  empty: string | null;
}

const NO_EDITING = "This server does not say whether editing is allowed.";

function where(def: IndicatorDef | null): PickerRow["where"] {
  return def ? (def.overlay ? "on the chart" : "own pane") : null;
}

function stem(file: string): string {
  return file.replace(/\.py$/i, "");
}

function gate(catalog: IndicatorCatalog): Gate {
  const editing = catalog.editing;
  if (!editing) return { allowed: false, reason: NO_EDITING };
  return editing.allowed ? { allowed: true, reason: null } : { allowed: false, reason: editing.reason };
}

function builtinRow(def: IndicatorDef): PickerRow {
  return {
    key: `builtin:${def.id}`,
    kind: "builtin",
    id: def.id,
    title: def.title,
    name: def.name,
    where: where(def),
    file: null,
    state: "ok",
    def,
    addable: true,
    why: null,
    line: null,
    canDelete: null,
  };
}

function problem(entry: CustomEntry, catalog: IndicatorCatalog): { state: RowState; why: string } {
  switch (entry.status) {
    case "quarantined":
      return {
        state: "quarantined",
        why: `Switched off after repeated failures${entry.reason ? ` (${entry.reason})` : ""}. Edit the file to try again.`,
      };
    case "not_run": {
      const cause = catalog.worker?.error || entry.reason;
      return { state: "not_run", why: `The indicator worker is not available${cause ? `: ${cause}` : "."}` };
    }
    default:
      return { state: "error", why: entry.reason || "The file did not load." };
  }
}

function customRow(entry: CustomEntry, catalog: IndicatorCatalog, builtinIds: Set<string>): PickerRow {
  const base = {
    key: `custom:${entry.file}`,
    kind: "custom" as const,
    id: entry.id,
    file: entry.file,
    line: entry.line,
    canDelete: gate(catalog),
  };
  const def = entry.status === "ok" ? entry.definition : null;
  if (def && builtinIds.has(def.id)) {
    return {
      ...base,
      title: def.title,
      name: def.name,
      where: where(def),
      state: "shadowed",
      def: null,
      addable: false,
      why: "Same id as a built-in indicator, so it cannot be added yet.",
    };
  }
  if (def) {
    return { ...base, title: def.title, name: def.name, where: where(def), state: "ok", def, addable: true, why: null };
  }
  const { state, why } = problem(entry, catalog);
  return { ...base, title: stem(entry.file), name: entry.id, where: null, state, def: null, addable: false, why };
}

/** A server from before the sections: the flat list holds the built-in ones and those of yours that run, `errors` the rest. */
function legacyCustom(catalog: IndicatorCatalog): CustomEntry[] {
  const running: CustomEntry[] = catalog.indicators
    .filter((d) => !d.builtin)
    .map((d) => ({ id: d.id, kind: "custom", file: `${d.id}.py`, status: "ok", reason: null, line: null, definition: d }));
  const failed: CustomEntry[] = catalog.errors.map((e) => ({
    id: stem(e.file),
    kind: "custom",
    file: e.file,
    status: "error",
    reason: e.error,
    line: null,
    definition: null,
  }));
  return [...running, ...failed];
}

function matches(row: PickerRow, words: string[]): boolean {
  const text = `${row.title} ${row.name} ${row.id} ${row.file ?? ""}`.toLowerCase();
  return words.every((w) => text.includes(w));
}

/** Ones that can be added first, then by title; a file that cannot be added is still listed, after them. */
function order(a: PickerRow, b: PickerRow): number {
  return Number(!a.addable) - Number(!b.addable) || a.title.toLowerCase().localeCompare(b.title.toLowerCase());
}

export function pickerModel(catalog: IndicatorCatalog, query: string): PickerModel {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const builtinDefs = catalog.builtin ?? catalog.indicators.filter((d) => d.builtin);
  const customEntries = catalog.custom ?? legacyCustom(catalog);
  const builtinIds = new Set(builtinDefs.map((d) => d.id));

  const builtin = builtinDefs.map(builtinRow).filter((r) => matches(r, words)).sort(order);
  const custom = customEntries.map((e) => customRow(e, catalog, builtinIds)).filter((r) => matches(r, words)).sort(order);

  // Errors the server lists besides the files in `custom`: a built-in that did not load is
  // not one of yours; a file already listed above is not shown twice.
  const listed = new Set(customEntries.map((e) => e.file));
  const extra = catalog.custom ? catalog.errors.filter((e) => !listed.has(e.file)) : [];
  const builtinNotices = extra.filter((e) => e.kind !== "custom").map((e) => `${e.file}: ${e.error}`);
  const customNotices = [
    ...(catalog.isolation?.notice ? [catalog.isolation.notice] : []),
    // What the worker can still do here, for example its own folder (owner's decision B1).
    ...(catalog.isolation?.notes ?? []).filter((n) => n.startsWith("its own folder")),
    ...extra.filter((e) => e.kind === "custom").map((e) => `${e.file}: ${e.error}`),
  ];

  const searching = words.length > 0;
  const all: PickerSection[] = [
    { kind: "builtin", heading: "Built-in", rows: builtin, hint: null, notices: builtinNotices },
    {
      kind: "custom",
      heading: "Your files",
      rows: custom,
      hint: custom.length || searching ? null : "No files of yours yet.",
      notices: customNotices,
    },
  ];
  const sections = all.filter((s) => !searching || s.rows.length > 0);

  return { sections, empty: searching && sections.length === 0 ? "No indicator matches." : null };
}
