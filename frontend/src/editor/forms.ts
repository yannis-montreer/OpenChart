/**
 * The checks behind the New, Rename, Duplicate and Import dialogs, as data: what is wrong
 * with the values typed so far, found before any request is made. The server checks again
 * and has the last word (a name taken in the meantime is still a 409).
 */

import type { TemplateInfo } from "../api";
import { fileProblem, idFromFilename, idProblem, isId, normalizeIdInput, titleProblem } from "./rules";

export type Problems<K extends string> = Partial<Record<K, string>>;

/** An id to start from for a title, as you type it ("SMA cross" gives "sma_cross"); empty when nothing usable is left. */
export function idFromTitle(title: string): string {
  const id = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40)
    .replace(/_+$/, "");
  return id === "" ? "" : /^[0-9]/.test(id) ? `x${id}`.slice(0, 40) : id;
}

/** The first free `<id>_2`, `<id>_3` ... the server suggests the same. */
export function freeId(id: string, taken: readonly string[]): string {
  const set = new Set(taken);
  for (let n = 2; ; n++) {
    const suffix = `_${n}`;
    const candidate = `${id.slice(0, 40 - suffix.length)}${suffix}`;
    if (!set.has(candidate)) return candidate;
  }
}

function idChecks(typed: string, taken: readonly string[]): string | undefined {
  const id = normalizeIdInput(typed);
  const bad = idProblem(id);
  if (bad) return bad;
  const has = new Set(taken.map((t) => t.toLowerCase()));
  if (has.has(id)) return `There is already a file named ${id}. Try ${freeId(id, taken)}.`;
  return undefined;
}

function put<K extends string>(out: Problems<K>, key: K, problem: string | undefined | null): void {
  if (problem) out[key] = problem;
}

export function checkNew(
  v: { id: string; template: string; title: string },
  templates: readonly TemplateInfo[],
  taken: readonly string[],
): Problems<"id" | "template" | "title"> {
  const out: Problems<"id" | "template" | "title"> = {};
  put(out, "id", idChecks(v.id, taken));
  if (!templates.some((t) => t.id === v.template)) out.template = "Pick a template.";
  if (v.title !== "") put(out, "title", titleProblem(v.title));
  return out;
}

export function checkRename(v: { title: string }, current: string): Problems<"title"> {
  const out: Problems<"title"> = {};
  put(out, "title", titleProblem(v.title));
  if (!out.title && v.title === current) out.title = "That is the title already.";
  return out;
}

export function checkDuplicate(v: { id: string; title: string }, taken: readonly string[]): Problems<"id" | "title"> {
  const out: Problems<"id" | "title"> = {};
  if (v.id.trim() !== "") put(out, "id", idChecks(v.id, taken)); // empty: the server picks the first free one
  if (v.title !== "") put(out, "title", titleProblem(v.title));
  return out;
}

export interface ImportCheck {
  /** The id typed, normalised; undefined when it is left to the server (made of the file's name). */
  id: string | undefined;
  /** The id the file will get. */
  effective: string;
  problems: Problems<"file" | "id">;
}

export function checkImport(v: { id: string }, file: { name: string; size: number } | null, taken: readonly string[]): ImportCheck {
  const typed = normalizeIdInput(v.id);
  const id = typed === "" ? undefined : typed;
  const effective = id ?? idFromFilename(file?.name ?? "");
  const problems: Problems<"file" | "id"> = {};
  put(problems, "file", file ? fileProblem(file) : "Pick a .py file.");
  put(problems, "id", idChecks(effective, taken));
  return { id, effective, problems };
}

/** The free id a `409` suggests, when it is one. */
export function suggestionText(detail: unknown): string | null {
  if (detail === null || typeof detail !== "object") return null;
  const s = (detail as { suggested?: unknown }).suggested;
  return typeof s === "string" && isId(s) ? s : null;
}
