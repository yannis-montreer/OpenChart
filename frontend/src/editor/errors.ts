/**
 * What the error panel shows, as data (no DOM): the error of a save or a run, its "line N"
 * pieces, and the save-time findings. Everything here is plain text for `textContent`: a
 * message, a traceback or a finding comes from the server (and from the user's own code),
 * so nothing in it is ever read as markup or as a link, except a line number that moves
 * the cursor.
 */

import { ApiError } from "../api";

export interface ErrorInfo {
  type: string;
  message: string;
  /** 1-based, null when the error names no line. */
  line: number | null;
  traceback: string | null;
}

function record(x: unknown): Record<string, unknown> | null {
  return x !== null && typeof x === "object" && !Array.isArray(x) ? (x as Record<string, unknown>) : null;
}

/** The error object of a definition (`error`), of a 422 (`detail`), or a plain text. Null for no error. */
export function errorOf(x: unknown): ErrorInfo | null {
  if (typeof x === "string") return { type: "Error", message: x, line: null, traceback: null };
  const r = record(x);
  if (!r) return null;
  const line = typeof r.line === "number" && Number.isInteger(r.line) && r.line >= 1 ? r.line : null;
  return {
    type: typeof r.type === "string" && r.type ? r.type : "Error",
    message: typeof r.message === "string" ? r.message : "",
    line,
    traceback: typeof r.traceback === "string" && r.traceback ? r.traceback : null,
  };
}

/** The error of a failed request: a 422's detail when it is one, else its text. */
export function errorFromApi(err: unknown): ErrorInfo {
  if (err instanceof ApiError) {
    const detail = err.detail;
    if (typeof detail === "string" && detail) return errorOf(detail)!;
    const r = record(detail);
    if (r && typeof r.message === "string") return errorOf(r)!;
    return { type: "Error", message: err.message, line: null, traceback: null };
  }
  return { type: "Error", message: err instanceof Error ? err.message : String(err), line: null, traceback: null };
}

export interface Piece {
  text: string;
  /** Set on a piece that names a line of the file (and only then): clicking it moves the cursor there. */
  line?: number;
}

/** A text split at its "line N" mentions; one that names a line outside the file is left as text. */
export function pieces(text: string, lineCount: number): Piece[] {
  const out: Piece[] = [];
  let at = 0;
  for (const m of text.matchAll(/\bline (\d+)\b/gi)) {
    const n = Number(m[1]);
    if (!(n >= 1 && n <= lineCount)) continue;
    const start = m.index ?? 0;
    if (start > at) out.push({ text: text.slice(at, start) });
    out.push({ text: m[0], line: n });
    at = start + m[0].length;
  }
  if (at < text.length) out.push({ text: text.slice(at) });
  return out;
}

export interface Finding {
  level: "warning" | "note";
  text: string;
}

/** The save-time findings (lookahead, warmup) of this version of the file, once each. */
export function findingsOf(checks: unknown, version: string): Finding[] {
  if (!Array.isArray(checks)) return [];
  const out: Finding[] = [];
  const seen = new Set<string>();
  for (const c of checks) {
    const entry = record(c);
    if (!entry || entry.version !== version || !Array.isArray(entry.findings)) continue;
    for (const f of entry.findings) {
      const finding = record(f);
      if (!finding || typeof finding.message !== "string") continue;
      const kind = typeof finding.check === "string" && finding.check ? finding.check[0].toUpperCase() + finding.check.slice(1) : "Check";
      const text = `${kind}: ${finding.message}`;
      if (seen.has(text)) continue;
      seen.add(text);
      out.push({ level: finding.level === "warning" ? "warning" : "note", text });
    }
  }
  return out;
}
