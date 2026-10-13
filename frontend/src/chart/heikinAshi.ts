/**
 * Heikin Ashi bars, for display only (SPEC section 11). Drawings, alerts, the legend and
 * the price labels keep using the real bars.
 *
 *   close = (open + high + low + close) / 4 of the real bar
 *   open  = (open + close) / 2 of the previous Heikin Ashi bar; for the first bar loaded,
 *           (open + close) / 2 of the real bar
 *   high  = max(real high, HA open, HA close), low = min(real low, HA open, HA close)
 *
 * The bar still forming is recomputed from the real bar on every update: its open is
 * fixed (the previous bar is closed), its close, high and low move with the price, and it
 * becomes final when the real bar closes. Earlier bars never change.
 */

import type { Bar } from "../api";

export function haStep(prev: Bar | undefined, bar: Bar): Bar {
  const close = (bar.o + bar.h + bar.l + bar.c) / 4;
  const open = prev ? (prev.o + prev.c) / 2 : (bar.o + bar.c) / 2;
  return {
    t: bar.t,
    o: open,
    h: Math.max(bar.h, open, close),
    l: Math.min(bar.l, open, close),
    c: close,
    v: bar.v,
    complete: bar.complete,
  };
}

export function heikinAshi(bars: readonly Bar[]): Bar[] {
  const out: Bar[] = [];
  for (const bar of bars) out.push(haStep(out[out.length - 1], bar));
  return out;
}
