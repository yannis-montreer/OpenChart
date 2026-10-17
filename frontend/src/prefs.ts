/**
 * Per-browser display preferences. Durable state (drawings, alerts, layouts) lives on
 * the server; these are only conveniences, so storage failures are ignored.
 */

import type { ChartType, DrawingStyle, Price } from "./api";

export interface Prefs {
  instrument: string | null;
  timeframe: string;
  price: Price;
  theme: "dark" | "light";
  timezone: string; // IANA name, "local" or "UTC"
  favorites: string[];
  panelOpen: boolean;
  panelView: "watchlist" | "alerts";
  drawingsHidden: boolean;
  drawingStyle: DrawingStyle; // style of the next new drawing (the last one chosen)
  chartType: ChartType;
  layoutId: string | null; // the layout the chart was loaded from or saved as
}

const KEY = "openchart.prefs.v1";

export const DEFAULT_PREFS: Prefs = {
  instrument: null,
  timeframe: "15m",
  price: "mid",
  theme: "dark",
  timezone: "local",
  favorites: ["1m", "5m", "15m", "30m", "1h", "4h", "1D", "1W"],
  panelOpen: true,
  panelView: "watchlist",
  drawingsHidden: false,
  drawingStyle: { color: "#4d8dff", width: 2, dash: "solid" },
  chartType: "candles",
  layoutId: null,
};

type Store = Pick<Storage, "getItem" | "setItem">;

function browserStore(): Store | null {
  try {
    return window.localStorage;
  } catch {
    return null; // storage blocked
  }
}

function readRaw(store: Store | null): Record<string, unknown> | null {
  try {
    const raw = store?.getItem(KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

export function loadPrefs(store: Store | null = browserStore()): Prefs {
  const raw = readRaw(store);
  if (!raw) return { ...DEFAULT_PREFS };
  const { indicators: _legacy, ...rest } = raw; // the old list is read apart, by readLegacyIndicators
  return { ...DEFAULT_PREFS, ...(rest as Partial<Prefs>) };
}

/** Writes the preferences; the old indicator list stays in storage until `dropLegacyIndicators`. */
export function savePrefs(prefs: Prefs, store: Store | null = browserStore()): void {
  try {
    const legacy = readRaw(store)?.indicators;
    store?.setItem(KEY, JSON.stringify(Array.isArray(legacy) && legacy.length ? { ...prefs, indicators: legacy } : prefs));
  } catch {
    /* storage unavailable */
  }
}

/**
 * The list of indicators this browser kept before CI-3 (one list for every chart). It is
 * copied once to each chart that has none on the server, then dropped.
 */
export function readLegacyIndicators(store: Store | null = browserStore()): unknown[] {
  const list = readRaw(store)?.indicators;
  return Array.isArray(list) ? list : [];
}

export function dropLegacyIndicators(store: Store | null = browserStore()): void {
  const raw = readRaw(store);
  if (!raw || !("indicators" in raw)) return;
  try {
    const { indicators: _legacy, ...rest } = raw;
    store?.setItem(KEY, JSON.stringify(rest));
  } catch {
    /* storage unavailable */
  }
}

/** The IANA zone a timezone preference stands for. */
export function resolveTimezone(pref: string): string {
  return pref === "local" ? Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" : pref;
}
