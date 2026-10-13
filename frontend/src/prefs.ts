/**
 * Per-browser display preferences. Durable state (drawings, alerts, layouts) lives on
 * the server; these are only conveniences, so storage failures are ignored.
 */

import type { ChartType, DrawingStyle, IndicatorEntry, Price } from "./api";

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
  indicators: IndicatorEntry[]; // the chart's indicators when no layout is saved
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
  indicators: [],
  layoutId: null,
};

export function loadPrefs(): Prefs {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (raw) return { ...DEFAULT_PREFS, ...JSON.parse(raw) };
  } catch {
    /* storage unavailable */
  }
  return { ...DEFAULT_PREFS };
}

export function savePrefs(prefs: Prefs): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* storage unavailable */
  }
}

/** The IANA zone a timezone preference stands for. */
export function resolveTimezone(pref: string): string {
  return pref === "local" ? Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" : pref;
}
