/**
 * Parameters of an indicator instance against what its definition declares. A layout stores the
 * parameters an instance had; the file may have changed since (an input renamed, removed, retyped
 * or given a smaller range), and a stale one makes the server refuse every request ("has no
 * parameter ...", "must be at most ..."): the indicator shows "Could not compute" until it is
 * removed and added again.
 */

import type { IndicatorDef, IndicatorParam, IndicatorParams, ParamValue } from "../api";

/** Why `value` is not acceptable for `p`, or null. */
export function paramProblem(p: IndicatorParam, value: ParamValue): string | null {
  if (p.type === "int" || p.type === "float" || p.type === "width") {
    if (typeof value !== "number" || !Number.isFinite(value)) return "Enter a number";
    if (p.type !== "float" && !Number.isInteger(value)) return "Enter a whole number";
    if (p.min !== undefined && value < p.min) return `At least ${p.min}`;
    if (p.max !== undefined && value > p.max) return `At most ${p.max}`;
  }
  if (p.type === "string" && String(value).length > (p.max_length ?? 100)) return `At most ${p.max_length ?? 100} characters`;
  if ((p.type === "source" || p.type === "choice") && !(p.choices ?? []).includes(String(value))) return "Pick a value";
  if (p.type === "color" && !/^#[0-9a-f]{6}$/i.test(String(value))) return "Pick a colour";
  return null;
}

/** The value has the type the parameter declares (the dialog's checks assume it). */
function typeProblem(p: IndicatorParam, value: unknown): boolean {
  switch (p.type) {
    case "bool":
      return typeof value !== "boolean";
    case "int":
    case "float":
    case "width":
      return typeof value !== "number";
    default:
      return typeof value !== "string";
  }
}

/**
 * Every parameter the definition has, with the stored value if it is still acceptable and the
 * default if not (missing, of another type, out of range, not among the choices). Stored ones
 * the definition no longer has are dropped.
 */
export function reconcileParams(def: IndicatorDef, stored: IndicatorParams | undefined): IndicatorParams {
  const out: IndicatorParams = {};
  for (const p of def.params) {
    const value = stored?.[p.name];
    out[p.name] = value !== undefined && !typeProblem(p, value) && paramProblem(p, value) === null ? value : p.default;
  }
  return out;
}
