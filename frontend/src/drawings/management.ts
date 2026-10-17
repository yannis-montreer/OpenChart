/** Locking, hiding and listing drawings, and the patch that takes a change back (pure). */

import type { Drawing, DrawingPatch, DrawingStyle } from "../api";
import type { DrawingType } from "./geometry";
import { fillOf } from "./shapes";

export const TYPE_NAMES: Record<DrawingType, string> = {
  hline: "horizontal line",
  trendline: "trendline",
  ray: "ray",
  extended: "extended line",
  vline: "vertical line",
  rect: "rectangle",
  channel: "parallel channel",
  fib: "Fibonacci retracement",
  text: "text",
  measure: "measure",
};

type Flagged = Pick<Drawing, "style">;

export const isLocked = (d: Flagged): boolean => d.style.locked === true;
export const isHiddenDrawing = (d: Flagged): boolean => d.style.hidden === true;

/** A locked drawing is not moved from the chart (its handles and body do nothing). */
export const canMove = (d: Flagged): boolean => !isLocked(d);
export const canDeleteFromChart = (d: Flagged): boolean => !isLocked(d);

/** What the chart paints and hits: nothing while all drawings are hidden, else the unhidden ones. */
export function visibleDrawings<T extends Flagged>(list: readonly T[], allHidden: boolean): T[] {
  return allHidden ? [] : list.filter((d) => !isHiddenDrawing(d));
}

/** The ids whose `field` is not yet `value` ("lock all", "hide all", and their opposites). */
export function bulkChange(list: readonly Drawing[], field: "locked" | "hidden", value: boolean): string[] {
  return list.filter((d) => (d.style[field] === true) !== value).map((d) => d.id);
}

/** The values a style key had before a patch, for the keys the patch names (absent keys as their default). */
function styleBefore(d: Drawing, patch: Partial<DrawingStyle>): Partial<DrawingStyle> {
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(patch) as (keyof DrawingStyle)[]) {
    const had = d.style[key];
    if (had !== undefined) out[key] = had;
    else if (key === "fill") out[key] = fillOf(d.type, undefined);
    else out[key] = false; // extend, locked, hidden
  }
  return out as Partial<DrawingStyle>;
}

/** The patch that puts `d` back as it is now, after `patch` is applied. */
export function patchBefore(d: Drawing, patch: DrawingPatch): DrawingPatch {
  const out: DrawingPatch = {};
  if (patch.points) out.points = d.points;
  if (patch.style) out.style = styleBefore(d, patch.style);
  if (patch.label !== undefined || patch.clear_label) {
    if (d.label) out.label = d.label;
    else out.clear_label = true;
  }
  return out;
}

/** A clone is a new drawing: neither locked nor hidden. */
export function cloneStyle(style: DrawingStyle): DrawingStyle {
  const { locked: _locked, hidden: _hidden, ...rest } = style;
  return rest;
}

/** The row of a drawing in the list. */
export function rowTitle(d: Drawing): string {
  const name = TYPE_NAMES[d.type];
  const title = name[0].toUpperCase() + name.slice(1);
  if (d.label) return `${title}: ${d.label}`;
  return d.type === "hline" ? `${title} ${d.points[0].p}` : title;
}
