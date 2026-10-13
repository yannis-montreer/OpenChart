import type { Bar } from "../api";

export interface MergeResult {
  bars: Bar[];
  /** True when bars were inserted or replaced before the last bar we had. */
  rewritesHistory: boolean;
  /** Bars at or after our last bar, in order: can be applied as live updates. */
  tail: Bar[];
}

/**
 * Merge freshly fetched bars into the bars on screen. Fresh bars win. Missing bars in
 * the middle (a bar that formed while we were disconnected) mean the history must be
 * redrawn; anything from our last bar onwards can be applied as plain updates.
 *
 * An incomplete fresh bar never replaces our last bar: the server builds that bar from
 * stored M1 only, so it lacks the forming minute that live updates already gave us.
 */
export function mergeBars(existing: Bar[], fresh: Bar[]): MergeResult {
  if (!existing.length) return { bars: [...fresh], rewritesHistory: fresh.length > 0, tail: [] };
  const lastT = existing[existing.length - 1].t;
  const byT = new Map(existing.map((b) => [b.t, b]));
  let rewritesHistory = false;
  const keep = (b: Bar) => !(b.t === lastT && b.complete === false);
  for (const bar of fresh) {
    if (!keep(bar)) continue;
    if (bar.t < lastT) {
      const old = byT.get(bar.t);
      if (!old || old.o !== bar.o || old.h !== bar.h || old.l !== bar.l || old.c !== bar.c || old.v !== bar.v) {
        rewritesHistory = true;
      }
    }
    byT.set(bar.t, bar);
  }
  const bars = [...byT.values()].sort((a, b) => a.t - b.t);
  const tail = fresh.filter((b) => b.t >= lastT && keep(b)).sort((a, b) => a.t - b.t);
  return { bars, rewritesHistory, tail };
}
