/** What the values route says about an answer besides the numbers (docs/api.md, "notice"). */

export interface WarmupCut {
  kind: "warmup_cut";
  /** Bars of earlier history the settings need to settle. */
  needed: number;
  /** Bars the server uses at most. */
  limit: number;
  message: string;
}

/** The small warning for a legend row, or null when the answer has no (usable) notice. */
export function warmupNotice(notice: unknown): { label: string; tip: string } | null {
  if (typeof notice !== "object" || notice === null) return null;
  const n = notice as Partial<WarmupCut>;
  if (n.kind !== "warmup_cut" || typeof n.needed !== "number" || typeof n.limit !== "number") return null;
  const tip =
    typeof n.message === "string" && n.message
      ? n.message
      : n.message === undefined
        ? `These settings need ${n.needed.toLocaleString("en-US")} bars of earlier history to settle, but only ${n.limit.toLocaleString("en-US")} are used.`
        : "";
  return tip ? { label: "Warmup cut", tip } : null;
}
